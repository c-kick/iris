"""Admin API behind the management UI (/admin).

Authentication: a browser session (password set during first-run setup with a
one-time code from the container log), or a bearer token with the `admin`
scope. Browser mutations must carry `X-Notify-Admin: 1` (cannot be sent
cross-site without a CORS preflight, which is never granted) and a same-origin
Origin header when present.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from urllib.parse import urlsplit
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import __version__, timeline
from .auth import hash_token, new_token
from .config import ConfigError, MailboxCleanupConfig
from .db import iso, new_id, now_iso, utcnow
from .webpush_crypto import generate_vapid_keys
from .core import Client, Conflict, Invalid, NotFound, NotifyService, external_base
from .models import NotificationIn, ScheduleUpdate, Scope, Severity
from .providers import REGISTRY
from .clientip import client_ip
from .configops import commit_config
from .cleanup_service import record_event
from .device_check import clear_flag
from .providers.email_inbound import run_locked
from .schedule import local_zone
from .chat import _clean_desc, model_label, reply_excerpt, status_view, text_or_files, who
from .registrations import announce_targets, approve, deny, list_registrations
from .store import ConfigStore

router = APIRouter(prefix="/admin", include_in_schema=False)

COOKIE = "notify_admin"
UI_CLIENT = Client("admin-ui", frozenset({Scope.admin}), via_session=True)
MIN_PASSWORD = 10
ENROLLMENT_HOURS = 24


class Throttle:
    """Login/setup attempt limiter: per source address, exponential lockout."""

    def __init__(self) -> None:
        self.failures: dict[str, tuple[int, float]] = {}

    def check(self, key: str) -> None:
        count, until = self.failures.get(key, (0, 0.0))
        if until > time.monotonic():
            raise HTTPException(429, f"too many attempts; try again in {int(until - time.monotonic()) + 1} s")

    def fail(self, key: str) -> None:
        count, _ = self.failures.get(key, (0, 0.0))
        count += 1
        lock = 0.0 if count < 5 else min(900.0, 30.0 * 2 ** (count - 5))
        self.failures[key] = (count, time.monotonic() + lock)

    def ok(self, key: str) -> None:
        self.failures.pop(key, None)


def _throttle(request: Request) -> Throttle:
    if not hasattr(request.app.state, "throttle"):
        request.app.state.throttle = Throttle()
    return request.app.state.throttle


def _store(request: Request) -> ConfigStore:
    return request.app.state.store


def _svc(request: Request) -> NotifyService:
    return request.app.state.service


def _ip(request: Request) -> str:
    return client_ip(request)


def admin(request: Request) -> Client:
    header = request.headers.get("authorization")
    if header:
        client = request.app.state.auth.from_header(header)
        if client is None:
            raise HTTPException(401, "invalid bearer token")
        if not client.admin:
            raise HTTPException(403, "admin scope required")
        return client
    if not _store(request).session_valid(request.cookies.get(COOKIE)):
        raise HTTPException(401, "sign in required")
    if request.method not in ("GET", "HEAD"):
        _check_browser_mutation(request)
    return UI_CLIENT


def require_session(client: Client) -> None:
    """Reply content is for the signed-in admin page only; an admin bearer token never sees it."""
    if not client.via_session:
        raise HTTPException(403, "this needs the signed-in admin page")


def _check_browser_mutation(request: Request) -> None:
    if request.headers.get("x-notify-admin") != "1":
        raise HTTPException(403, "missing X-Notify-Admin header")
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        raise HTTPException(403, "cross-origin request refused")


def _set_cookie(response: Response, sid: str, request: Request | None = None) -> None:
    https = request is not None and (request.url.scheme == "https"
                                     or request.headers.get("x-forwarded-proto", "").lower() == "https")
    response.set_cookie(COOKIE, sid, max_age=14 * 86400, httponly=True, samesite="strict", path="/admin",
                        secure=https)  # a session made over https is never sent over plain http


# ------------------------------------------------------------------ page & session

@router.get("")
@router.get("/")
async def page() -> FileResponse:
    from .app import ADMIN_UI
    return FileResponse(ADMIN_UI / "index.html", headers={"Cache-Control": "no-cache"})


@router.get("/api/session")
async def session(request: Request) -> dict[str, Any]:
    store = _store(request)
    return {"setup_required": not store.admin_configured(),
            "signed_in": store.session_valid(request.cookies.get(COOKIE)), "version": __version__}


class SetupIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=MIN_PASSWORD, max_length=256)


@router.post("/api/setup")
async def setup(body: SetupIn, request: Request, response: Response) -> dict[str, Any]:
    _check_browser_mutation(request)
    store = _store(request)
    if store.admin_configured():
        raise HTTPException(409, "already set up; sign in instead")
    key = f"setup:{_ip(request)}"
    throttle = _throttle(request)
    throttle.check(key)
    if not await asyncio.to_thread(store.check_setup_code, body.code):
        throttle.fail(key)
        raise HTTPException(403, "setup code is not valid; copy it from the container log")
    throttle.ok(key)
    await asyncio.to_thread(store.set_password, body.password)  # scrypt: off the event loop
    _set_cookie(response, store.new_session(), request)
    return {"ok": True}


class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


@router.post("/api/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    _check_browser_mutation(request)
    store = _store(request)
    key = f"login:{_ip(request)}"
    throttle = _throttle(request)
    throttle.check(key)
    if not await asyncio.to_thread(store.check_password, body.password):
        throttle.fail(key)
        raise HTTPException(403, "wrong password")
    throttle.ok(key)
    _set_cookie(response, store.new_session(), request)
    return {"ok": True}


@router.post("/api/logout")
async def logout(request: Request, response: Response) -> dict[str, Any]:
    _check_browser_mutation(request)  # no forced sign-out from another page on the same site
    _store(request).end_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/admin")
    return {"ok": True}


class PasswordIn(BaseModel):
    current: str = Field(min_length=1, max_length=256)
    new: str = Field(min_length=MIN_PASSWORD, max_length=256)


@router.post("/api/password")
async def change_password(body: PasswordIn, request: Request, response: Response,
                          client: Client = Depends(admin)) -> dict[str, Any]:
    require_session(client)  # a bearer token must not guess the password, nor turn itself into a session
    store = _store(request)
    key = f"password:{_ip(request)}"
    throttle = _throttle(request)
    throttle.check(key)
    if not await asyncio.to_thread(store.check_password, body.current):
        throttle.fail(key)
        raise HTTPException(403, "current password is wrong")
    throttle.ok(key)
    await asyncio.to_thread(store.set_password, body.new)  # signs out every session
    _set_cookie(response, store.new_session(), request)
    return {"ok": True}


# ------------------------------------------------------------------ state

def _state(request: Request) -> dict[str, Any]:
    svc = _svc(request)
    cfg = request.app.state.config
    masked = ConfigStore.masked(cfg)
    for c in masked["clients"].values():
        c.pop("token_sha256", None)
    targets = svc.targets()
    pending = svc.db.scalar("SELECT COUNT(*) FROM registrations WHERE status='pending'") or 0
    per_client = {r[0]: r[1] for r in svc.db.query(
        "SELECT client_id, COUNT(*) FROM notifications WHERE status='scheduled' GROUP BY client_id")}
    for name, c in masked["clients"].items():
        c["scheduled"] = per_client.get(name, 0)
    # webhooks turned off after failures, per client (host only: a webhook URL can carry a secret in its path)
    for r in svc.db.query("SELECT id, client_id, url, last_error FROM subscriptions WHERE active=0 ORDER BY created_at"):
        if r["client_id"] in masked["clients"]:
            masked["clients"][r["client_id"]].setdefault("webhooks_off", []).append(
                {"id": r["id"], "host": urlsplit(r["url"]).netloc[-200:], "last_error": r["last_error"]})
    return {
        "version": __version__,
        "appearance": _store(request).appearance(),
        "pending_registrations": pending,
        "scheduled_count": sum(per_client.values()),
        "config": masked,
        "devices": {d["target"].split(":", 1)[1]: {"ready": d["ready"], "problems": d["problems"],
                                                    "capabilities": d["capabilities"],
                                                    "kind": cfg.device_kind(d["target"].split(":", 1)[1]),
                                                    "opt_in": cfg.device_opt_in(d["target"].split(":", 1)[1]),
                                                    "direct_only": d["direct_only"]}
                    for d in targets["devices"]},
        "providers": {name: {"type": p.type, "kind": p.kind, **p.health()} for name, p in svc.providers.items()},
        "provider_types": {t: {"label": cls.label or t, "kind": cls.kind, "settings": cls.settings_schema,
                               "address": cls.address_schema} for t, cls in REGISTRY.items()},
        "severities": [s.value for s in Severity],
        "scopes": [s.value for s in Scope],
        "stats": svc.stats(),
    }


class AppearanceIn(BaseModel):
    """The admin account's appearance: dark from sunset to sunrise at this place, when sun_dark is on. There is no
    default place: both coordinates are null until the owner enters them, and turning it on needs them."""
    model_config = ConfigDict(extra="forbid", strict=True)
    sun_dark: bool
    latitude: Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)] | None
    longitude: Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)] | None

    @model_validator(mode="after")
    def _place(self) -> "AppearanceIn":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("give both latitude and longitude, or neither")
        if self.sun_dark and self.latitude is None:
            raise ValueError("dark mode from sunset to sunrise needs a place: latitude and longitude")
        return self


@router.get("/api/appearance")
async def get_appearance(request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    return _store(request).appearance()


@router.put("/api/appearance")
async def put_appearance(body: AppearanceIn, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    store = _store(request)
    store.set_appearance(body.model_dump())
    return store.appearance()


@router.get("/api/state")
async def state(request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    return _state(request)


def _commit(request: Request, mutate: Any) -> dict[str, Any]:
    try:
        commit_config(request.app, mutate)
    except ConfigError as e:
        raise HTTPException(422, str(e)) from e
    return _state(request)


def _drop_refs(data: dict[str, Any], ref: str) -> None:
    for g in data["groups"].values():
        g["members"] = [m for m in g["members"] if m != ref]
    for ch in data["channels"].values():
        ch["targets"] = [t for t in ch["targets"] if t != ref]
    for mc in data["mailbox_cleanup"].values():
        mc["alert_targets"] = [t for t in mc["alert_targets"] if t != ref]


# ------------------------------------------------------------------ providers

class ProviderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    settings: dict[str, Any] = Field(default_factory=dict)


_IDENTITY_HINTS = ("host", "port", "user", "security", "verify_tls", "api_base", "url")


def _identity_fields(secret_key: str, schema: dict[str, Any]) -> list[str]:
    """The settings that say *where* a secret is sent: change one and the stored secret is not carried over."""
    imap = secret_key.startswith("imap_")
    return [k for k, spec in schema.items() if spec.get("kind") != "secret"
            and any(h in k for h in _IDENTITY_HINTS) and (k == "verify_tls" or k.startswith("imap_") == imap)]


def _norm(v: Any, spec: dict[str, Any]) -> str:
    """Compare as stored: an empty field means the setting's default."""
    if v in (None, ""):
        v = spec.get("default")
    return "" if v is None else str(v).strip().lower()


@router.put("/api/providers/{name}")
async def put_provider(name: str, body: ProviderIn, request: Request, client: Client = Depends(admin)) -> dict[str, Any]:
    require_session(client)  # provider settings hold the secrets that unlock the mailbox: signed-in page only
    cls = REGISTRY.get(body.type)
    if cls is None:
        raise HTTPException(422, f"unknown provider type {body.type!r}")

    def mutate(data: dict[str, Any]) -> None:
        old = data["providers"].get(name) or {}
        used = [d for d, dev in data["devices"].items() if dev["provider"] == name]
        if used and old.get("type") not in (None, body.type):
            raise ConfigError(f"remove the devices using it before changing its type: {', '.join(used)}")
        new: dict[str, Any] = {"type": body.type}
        for key, spec in cls.settings_schema.items():
            v = body.settings.get(key)
            kind = spec.get("kind")
            if kind == "secret" and not v and old.get("type") == body.type and old.get(key):
                moved = [k for k in _identity_fields(key, cls.settings_schema)
                         if _norm(body.settings.get(k), cls.settings_schema[k]) != _norm(old.get(k), cls.settings_schema[k])]
                if moved:  # never send a stored password to a server it was not entered for
                    raise ConfigError(f"{spec['label']}: enter it again when changing "
                                      f"{', '.join(cls.settings_schema[k]['label'] for k in moved)}")
                v = old.get(key)  # blank secret field = keep the stored value
            if kind == "int" and v not in (None, ""):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise ConfigError(f"{spec['label']}: enter a whole number") from None
            elif kind == "check" and v is not None and not isinstance(v, bool):
                raise ConfigError(f"{spec['label']}: must be true or false")
            elif kind == "select" and v not in (None, ""):
                choices = [o[0] for o in spec["options"]]
                if v not in choices:
                    raise ConfigError(f"{spec['label']}: choose one of {', '.join(choices)}")
            elif kind == "text" and v is not None:
                v = str(v).strip()
            if v not in (None, ""):
                new[key] = v
        if body.type == "webpush":  # the key pair is server-managed: regenerating it would orphan every browser
            same = old.get("type") == body.type and old.get("vapid_private_key") and old.get("vapid_public_key")
            new["vapid_private_key"], new["vapid_public_key"] = (
                (old["vapid_private_key"], old["vapid_public_key"]) if same else generate_vapid_keys())
        data["providers"][name] = new
    return _commit(request, mutate)


class EnrollmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(min_length=1, max_length=100)


@router.post("/api/providers/{name}/enrollments")
async def create_enrollment(name: str, body: EnrollmentIn, request: Request,
                            client: Client = Depends(admin)) -> dict[str, Any]:
    """A single-use link that enrolls one browser as a device of `owner`."""
    require_session(client)  # a new device receives decision links: signed-in page only
    cfg = request.app.state.config
    if cfg.providers.get(name) is None or cfg.providers[name].type != "webpush":
        raise HTTPException(404, f"{name} is not a browser push provider")
    if body.owner not in cfg.users:
        raise HTTPException(422, f"unknown person {body.owner!r}")
    base = external_base(cfg)
    if not base or not base.startswith("https://"):
        raise HTTPException(409, "browser push needs an https external URL: set 'Address of this service' first")
    code = secrets.token_urlsafe(24)
    expires = iso(utcnow() + timedelta(hours=ENROLLMENT_HOURS))
    with _svc(request).db.tx() as c:
        c.execute("INSERT INTO enrollments(id, provider, owner, code_sha256, created_at, expires_at)"
                  " VALUES (?,?,?,?,?,?)", (new_id("enr"), name, body.owner, hash_token(code), now_iso(), expires))
    return {"url": f"{base}/push/enroll?c={code}", "expires_at": expires}


@router.post("/api/providers/{name}/test")
async def test_provider(name: str, request: Request, client: Client = Depends(admin)) -> dict[str, Any]:
    require_session(client)
    svc = _svc(request)
    provider = svc.providers.get(name)
    if provider is None:
        raise HTTPException(404, f"provider {name} is not configured")
    # the IMAP part must not overlap a poll or a cleanup step
    return await run_locked(svc.mailbox_lock(name), provider.test_connection)


@router.delete("/api/providers/{name}")
async def delete_provider(name: str, request: Request, client: Client = Depends(admin)) -> dict[str, Any]:
    require_session(client)

    def mutate(data: dict[str, Any]) -> None:
        used = [d for d, dev in data["devices"].items() if dev["provider"] == name]
        if used:
            raise ConfigError(f"remove the devices using it first: {', '.join(used)}")
        data["providers"].pop(name, None)
        data["mailbox_cleanup"].pop(name, None)
    return _commit(request, mutate)


class AddressCheckIn(BaseModel):
    address: dict[str, Any] = Field(default_factory=dict)
    device: str | None = Field(default=None, description="existing device whose stored secrets fill blank fields")


@router.post("/api/providers/{name}/check")
async def check_address(name: str, body: AddressCheckIn, request: Request,
                        _: Client = Depends(admin)) -> dict[str, Any]:
    svc = _svc(request)
    provider = svc.providers.get(name)
    if provider is None:
        raise HTTPException(404, f"provider {name} is not configured")
    address = {k: v for k, v in body.address.items() if v not in (None, "")}
    existing = request.app.state.config.devices.get(body.device or "")
    if existing is not None and existing.provider == name:
        for k in provider.secret_address_fields:
            address.setdefault(k, existing.address.get(k))
    return await provider.validate_address(address)


# ------------------------------------------------------------------ devices / users / groups / channels

class DeviceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    owner: str | None = None
    description: str | None = None
    enabled: bool = True
    min_severity: Severity = Severity.min
    opt_in: bool | None = None  # full replace: omitted = kind default
    direct_only: bool = False
    address: dict[str, Any] = Field(default_factory=dict)


@router.put("/api/devices/{name}")
async def put_device(name: str, body: DeviceIn, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    def mutate(data: dict[str, Any]) -> None:
        pcfg = data["providers"].get(body.provider)
        if pcfg is None:
            raise ConfigError(f"set up provider {body.provider!r} first")
        cls = REGISTRY[pcfg["type"]]
        old = data["devices"].get(name) or {}
        address = {k: v for k, v in body.address.items() if k in cls.address_schema and v not in (None, "")}
        if old.get("provider") == body.provider:
            for k in cls.secret_address_fields:
                if not address.get(k) and old.get("address", {}).get(k):
                    address[k] = old["address"][k]
        owner = body.owner or None
        data["devices"][name] = {"provider": body.provider, "owner": owner, "description": body.description or None,
                                 "enabled": body.enabled, "min_severity": body.min_severity.value,
                                 "address": address, "capabilities": old.get("capabilities", {})}
        if body.opt_in is not None:
            data["devices"][name]["opt_in"] = body.opt_in
        if body.direct_only:
            data["devices"][name]["direct_only"] = True
        for uname, u in data["users"].items():
            if uname != owner and name in u["devices"] and old.get("owner") == uname:
                u["devices"].remove(name)  # moved to another owner
        if owner and owner in data["users"] and name not in data["users"][owner]["devices"]:
            data["users"][owner]["devices"].append(name)
    return _commit(request, mutate)


@router.delete("/api/devices/{name}")
async def delete_device(name: str, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    def mutate(data: dict[str, Any]) -> None:
        data["devices"].pop(name, None)
        for u in data["users"].values():
            u["devices"] = [d for d in u["devices"] if d != name]
        _drop_refs(data, f"device:{name}")
    out = _commit(request, mutate)
    clear_flag(_svc(request), name)
    return out


class UserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = None
    devices: list[str] = Field(default_factory=list)


@router.put("/api/users/{name}")
async def put_user(name: str, body: UserIn, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    def mutate(data: dict[str, Any]) -> None:
        data["users"][name] = {"display_name": body.display_name or None, "devices": body.devices}
        for dname, d in data["devices"].items():
            if d.get("owner") == name and dname not in body.devices:
                d["owner"] = None
            if dname in body.devices:
                d["owner"] = name
    return _commit(request, mutate)


@router.delete("/api/users/{name}")
async def delete_user(name: str, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    def mutate(data: dict[str, Any]) -> None:
        data["users"].pop(name, None)
        for d in data["devices"].values():
            if d.get("owner") == name:
                d["owner"] = None
        _drop_refs(data, f"user:{name}")
    return _commit(request, mutate)


class GroupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = None
    members: list[str] = Field(default_factory=list)


@router.put("/api/groups/{name}")
async def put_group(name: str, body: GroupIn, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    def mutate(data: dict[str, Any]) -> None:
        data["groups"][name] = {"description": body.description or None, "members": body.members}
    return _commit(request, mutate)


@router.delete("/api/groups/{name}")
async def delete_group(name: str, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    def mutate(data: dict[str, Any]) -> None:
        data["groups"].pop(name, None)
        _drop_refs(data, f"group:{name}")
    return _commit(request, mutate)


class ChannelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = None
    targets: list[str] = Field(default_factory=list)
    min_severity: Severity = Severity.min


@router.put("/api/channels/{name}")
async def put_channel(name: str, body: ChannelIn, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    def mutate(data: dict[str, Any]) -> None:
        data["channels"][name] = {"description": body.description or None, "targets": body.targets,
                                  "min_severity": body.min_severity.value}
    return _commit(request, mutate)


@router.delete("/api/channels/{name}")
async def delete_channel(name: str, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    def mutate(data: dict[str, Any]) -> None:
        data["channels"].pop(name, None)
    return _commit(request, mutate)


# ------------------------------------------------------------------ API clients (tokens)

class ClientIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = None
    scopes: list[Scope] = Field(min_length=1)
    chat_host: str | None = None


@router.post("/api/clients/{name}")
async def create_client(name: str, body: ClientIn, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    if name in request.app.state.config.clients:
        raise HTTPException(409, f"{name} already exists; use Replace token to issue a new one")
    token = new_token()

    def mutate(data: dict[str, Any]) -> None:
        data["clients"][name] = {"token_sha256": hash_token(token), "scopes": [s.value for s in body.scopes],
                                 "description": body.description or None}
        if body.chat_host:
            data["clients"][name]["chat_host"] = body.chat_host
    out = _commit(request, mutate)
    out["token"] = token
    return out


@router.put("/api/clients/{name}")
async def update_client(name: str, body: ClientIn, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    def mutate(data: dict[str, Any]) -> None:
        if name not in data["clients"]:
            raise ConfigError(f"unknown API client {name!r}")
        data["clients"][name].update(scopes=[s.value for s in body.scopes], description=body.description or None)
        if "chat_host" in body.model_fields_set:  # omitted = keep, empty = clear
            if body.chat_host:
                data["clients"][name]["chat_host"] = body.chat_host
            else:
                data["clients"][name].pop("chat_host", None)
    return _commit(request, mutate)


@router.post("/api/clients/{name}/rotate")
async def rotate_client(name: str, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    token = new_token()

    def mutate(data: dict[str, Any]) -> None:
        if name not in data["clients"]:
            raise ConfigError(f"unknown API client {name!r}")
        data["clients"][name]["token_sha256"] = hash_token(token)
    out = _commit(request, mutate)
    out["token"] = token
    return out


@router.post("/api/subscriptions/{sid}/enable")
async def enable_subscription(sid: str, request: Request, client: Client = Depends(admin)) -> dict[str, Any]:
    """Turn a webhook that was disabled after failures back on (not one of a deleted client)."""
    try:
        _svc(request).enable_subscription(client, sid)
    except NotFound as e:
        raise HTTPException(404, str(e)) from None
    return _state(request)


@router.delete("/api/clients/{name}")
async def delete_client(name: str, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    def mutate(data: dict[str, Any]) -> None:
        data["clients"].pop(name, None)
    _commit(request, mutate)
    # a later client with the same name must not inherit what this one scheduled
    svc = _svc(request)
    cancelled = 0
    with svc.db.tx() as c:
        stamp = now_iso()
        # its standing webhooks stop too: they would otherwise fire for a later client of the same name
        # and they are re-owned to a tombstone no client name can match ('#' is not allowed in names), so a later client
        # of the same name can neither see nor re-enable them
        c.execute("UPDATE subscriptions SET active=0, last_error=?, client_id=client_id || '#deleted' WHERE client_id=?",
                  ("the API client was deleted", name))
        for n in c.execute("SELECT * FROM notifications WHERE client_id=? AND status='scheduled'", (name,)).fetchall():
            c.execute("UPDATE notifications SET status='cancelled', released_at=?, updated_at=? WHERE id=?",
                      (stamp, stamp, n["id"]))
            svc.scheduler.drop_attachments(c, n["id"], stamp)
            svc._emit(c, "notification.status_changed", n, None,
                      {"from": "scheduled", "to": "cancelled", "reason": "client deleted"})
            cancelled += 1
    if cancelled:
        svc._after_commit()
    out = _state(request)  # after the cancel, so the counts match
    out["cancelled_scheduled"] = cancelled
    return out


# ------------------------------------------------------------------ server settings

class ServerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_base_url: str | None = None
    external_url: str | None = None
    max_attempts: int | None = None
    retention_days: int | None = None
    attachment_retention_days: int | None = None
    attachment_url_hosts: list[str] | None = None
    chat_cap: int | None = None
    chat_alert_targets: list[str] | None = None
    chat_paused_posts: int | None = None
    chat_resume_wakes: int | None = None
    trusted_proxies: list[str] | None = None


@router.put("/api/server")
async def put_server(body: ServerIn, request: Request, client: Client = Depends(admin)) -> dict[str, Any]:
    if body.trusted_proxies is not None:
        require_session(client)  # decides whose forwarded address the throttles believe: signed-in page only
    if body.chat_paused_posts is not None or body.chat_resume_wakes is not None:
        require_session(client)  # 0.14.0 owner chat settings: signed-in page only (the spec's ruling)

    def mutate(data: dict[str, Any]) -> None:
        s = data["server"]
        s["public_base_url"] = (body.public_base_url or "").rstrip("/") or None
        if body.external_url is not None:
            s["external_url"] = body.external_url.rstrip("/") or None
        if s["public_base_url"] and not s.get("callback_secret"):
            s["callback_secret"] = secrets.token_urlsafe(32)
        if body.max_attempts is not None:
            s["max_attempts"] = body.max_attempts
        if body.retention_days is not None:
            s["retention_days"] = body.retention_days
        if body.attachment_retention_days is not None:
            s["attachment_retention_days"] = body.attachment_retention_days
        if body.attachment_url_hosts is not None:
            s["attachment_url_hosts"] = [h.strip() for h in body.attachment_url_hosts if h.strip()]
        if body.chat_cap is not None:
            s["chat_cap"] = body.chat_cap
        if body.chat_alert_targets is not None:
            s["chat_alert_targets"] = [t.strip() for t in body.chat_alert_targets if t.strip()]
        if body.chat_paused_posts is not None:
            s["chat_paused_posts"] = body.chat_paused_posts
        if body.chat_resume_wakes is not None:
            s["chat_resume_wakes"] = body.chat_resume_wakes
        if body.trusted_proxies is not None:
            s["trusted_proxies"] = [t.strip() for t in body.trusted_proxies if t.strip()]
    return _commit(request, mutate)


# ------------------------------------------------------------------ self-registration

class RegistrationSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    notify_targets: list[str] = Field(default_factory=list)
    auto_approve_subnets: list[str] = Field(default_factory=list)
    auto_approve_scopes: list[Scope] = Field(default_factory=lambda: [Scope.send])


@router.put("/api/registration")
async def put_registration_settings(body: RegistrationSettingsIn, request: Request,
                                    _: Client = Depends(admin)) -> dict[str, Any]:
    def mutate(data: dict[str, Any]) -> None:
        data["registration"] = {"enabled": body.enabled, "notify_targets": body.notify_targets,
                                "auto_approve_subnets": [x.strip() for x in body.auto_approve_subnets if x.strip()],
                                "auto_approve_scopes": [s.value for s in body.auto_approve_scopes]}
    return _commit(request, mutate)


@router.get("/api/registrations")
async def registrations(request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    return list_registrations(request.app)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scopes: list[Scope] | None = None
    description: str | None = Field(default=None, max_length=200)


@router.post("/api/registrations/{rid}/{decision}")
async def decide(rid: str, decision: str, request: Request, body: DecisionIn | None = None,
                 client: Client = Depends(admin)) -> dict[str, Any]:
    body = body or DecisionIn()
    if body.scopes and Scope.admin in body.scopes:
        raise HTTPException(422, "grant admin by editing the client under API access, not through a request")
    try:
        if decision == "approve":
            reg = approve(request.app, rid, body.scopes, body.description, by=client.id)
        elif decision == "deny":
            reg = deny(request.app, rid, by=client.id)
        else:
            raise HTTPException(404, "unknown decision")
    except KeyError:
        raise HTTPException(404, "access request not found") from None
    except ConfigError as e:
        raise HTTPException(409, str(e)) from e
    return {"registration": reg, "state": _state(request)}


# ------------------------------------------------------------------ test send & activity

class TestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targets: list[str] = Field(min_length=1)
    severity: Severity = Severity.normal
    title: str | None = "Test from Iris"
    message: str = Field(default="If you can read this, delivery works.", min_length=1)
    kinds: list[Literal["push", "email"]] | None = None


@router.post("/api/test")
async def send_test(body: TestIn, request: Request, client: Client = Depends(admin)) -> dict[str, Any]:
    req = NotificationIn(targets=body.targets, severity=body.severity, title=body.title, message=body.message,
                         kinds=body.kinds, source="admin-ui", tags=["test"])
    try:
        out, _ = await _svc(request).submit_async(client, req)
    except Invalid as e:
        raise HTTPException(422, str(e)) from e
    return out


@router.get("/api/activity")
async def activity(request: Request, limit: int = 25, client: Client = Depends(admin)) -> dict[str, Any]:
    svc = _svc(request)
    admin_view = Client(client.id, frozenset({Scope.admin}), via_session=client.via_session)
    return {"notifications": svc.list(admin_view, limit=max(1, min(limit, 100))),
            "attention": svc.attention(admin_view), "timezone": local_zone().key}


@router.get("/api/scheduled")
async def scheduled(request: Request, client: Client = Depends(admin)) -> dict[str, Any]:
    admin_view = Client(client.id, frozenset({Scope.admin}), via_session=client.via_session)
    return {"items": _svc(request).list(admin_view, status="scheduled"), "timezone": local_zone().key}


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targets: list[str] = Field(min_length=1, max_length=50)
    severity: Severity = Severity.normal
    kinds: list[Literal["push", "email"]] | None = None
    title: str | None = Field(default=None, max_length=1000)
    message: str = Field(default="preview", min_length=1, max_length=10000)


@router.post("/api/preview")
async def preview(body: PreviewIn, request: Request, client: Client = Depends(admin)) -> dict[str, Any]:
    """Routing of an edited version as it would route now: the ordinary dry-run submit, nothing is stored."""
    req = NotificationIn(targets=body.targets, severity=body.severity, kinds=body.kinds, title=body.title,
                         message=body.message, source="admin-ui", dry_run=True)
    try:
        out, _ = await _svc(request).submit_async(client, req)
    except Invalid as e:
        raise HTTPException(422, str(e)) from e
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    return out


@router.patch("/api/notifications/{nid}")
async def edit_scheduled(nid: str, body: ScheduleUpdate, request: Request,
                         client: Client = Depends(admin)) -> dict[str, Any]:
    try:
        return _svc(request).scheduler.update(client, nid, body)
    except Invalid as e:
        raise HTTPException(422, str(e)) from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    except NotFound as e:
        raise HTTPException(404, str(e)) from e


def _hide_reply_content(reply: dict[str, Any]) -> None:
    """A bearer admin sees states and counts, never what people wrote."""
    reply.update({"from": None, "subject": None, "text": None, "attachments": [], "dropped_attachments": []})


def _hide_rejected_content(rejected: dict[str, Any]) -> None:
    rejected.update(from_address=None, subject=None, message_id=None)


@router.get("/api/replies")
async def replies_view(request: Request, limit: int = 25, client: Client = Depends(admin)) -> dict[str, Any]:
    svc = _svc(request)
    replies, quarantine = svc.replies.recent(limit), svc.replies.quarantined(limit)
    if not client.via_session:
        for r in replies:
            _hide_reply_content(r)
        for q in quarantine:
            _hide_rejected_content(q)
    return {"inbound": svc.inbound_status, "replies": replies, "quarantine": quarantine}


@router.get("/api/timeline")
async def timeline_view(request: Request, limit: int = 25, before: str | None = None, after: str | None = None,
                        direction: Literal["all", "sent", "received"] = "all", source: str | None = None,
                        find: str | None = None, client: Client = Depends(admin)) -> dict[str, Any]:
    """Everything sent and received, newest first. `find` is one entry by exact id and ignores the rest."""
    svc = _svc(request)
    admin_view = Client(client.id, frozenset({Scope.admin}), via_session=client.via_session)
    try:
        out = (timeline.find(svc, admin_view, find) if find is not None else
               timeline.page(svc, admin_view, limit=limit, before=before, after=after, direction=direction,
                             source=source or None))
    except Invalid as e:
        raise HTTPException(422, str(e)) from e
    if not client.via_session:
        for item in out["items"]:
            if item["kind"] == "reply":
                _hide_reply_content(item)
            elif item["kind"] == "rejected":
                _hide_rejected_content(item)
    return {**out, "sources": timeline.sources(svc), "inbound": svc.inbound_status, "timezone": local_zone().key}


# ------------------------------------------------------------------ agent group chats

def _preview(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= 120 else flat[:119] + "…"


def _group_out(svc: NotifyService, g: Any, show_text: bool) -> dict[str, Any]:
    gid = g["id"]
    now = svc.chat.now()
    members = []
    claims: dict[str, list[str]] = {}  # member id -> the messages it holds, oldest first
    for r in svc.db.query("SELECT id, claimed_by_member_id FROM chat_messages WHERE group_id=?"
                          " AND claimed_by_member_id IS NOT NULL ORDER BY rowid", (gid,)):
        claims.setdefault(r["claimed_by_member_id"], []).append(r["id"])
    for m in svc.db.query("SELECT * FROM chat_members WHERE group_id=? ORDER BY joined_at, rowid", (gid,)):
        v = status_view(m["kind"], m["status"], m["status_at"], m["active_at"], now) if m["left_at"] is None else {}
        members.append({"id": m["id"], "name": m["name"], "kind": m["kind"], "joined_at": m["joined_at"],
                        "last_seen_at": m["last_seen_at"], "left_at": m["left_at"], "status": v.get("status"),
                        "status_since": v.get("since"), "quiet_minutes": v.get("quiet_minutes"),
                        "claims": len(claims.get(m["id"], [])), "oldest_claim": (claims.get(m["id"]) or [None])[0],
                        "intent": m["intent"] if show_text else None,
                        "model": model_label(m["model"]), "model_id": m["model"]})
    presences = ([{"kind": p["kind"], "since": p["started_at"]} for p in svc.db.query(
        "SELECT kind, started_at FROM chat_presence WHERE host=? AND folder=? ORDER BY rowid", (g["host"], g["folder"]))]
        if g["state"] == "open" else [])
    last = svc.db.one("SELECT id, author, text, created_at FROM chat_messages WHERE group_id=? ORDER BY rowid DESC LIMIT 1",
                      (gid,))
    if last is not None and show_text and not last["text"].strip():  # only files: "2 files: a.png, b.log"
        last = {**dict(last), "text": text_or_files(last["text"], svc.chat.message_files([last["id"]]).get(last["id"]))}
    unread = svc.db.scalar("SELECT COUNT(*) FROM chat_messages WHERE group_id=? AND author IN ('claude', 'codex')"
                           " AND (? IS NULL OR created_at > ?)", (gid, g["owner_seen_at"], g["owner_seen_at"])) or 0
    return {"id": gid, "host": g["host"], "folder": g["folder"], "state": g["state"], "opened_at": g["opened_at"],
            "closed_at": g["closed_at"], "paused": bool(g["paused"]), "members": members, "presences": presences,
            "adhoc": bool(g["adhoc"]), "enabled": svc.chat.folder_enabled(g["host"], g["folder"]),
            "paused_posts": g["paused_posts"], "paused_posts_effective": svc.chat.paused_limit(g),
            "last_message": ({"author": last["author"], "text_preview": _preview(last["text"]) if show_text else None,
                              "created_at": last["created_at"]} if last else None),
            "unread": unread, "alert": bool(g["alert_at"]), "alert_at": g["alert_at"],
            "alert_kind": g["alert_kind"] if g["alert_at"] else None}


def _folder_out(svc: NotifyService, groups: list[Any], show_text: bool) -> dict[str, Any]:
    """One thread per (host, folder): the newest group's status plus how many earlier ones there are."""
    newest = groups[-1]
    out = _group_out(svc, newest, show_text)
    lm = out["last_message"]
    out["activity_at"] = (lm["created_at"] if lm else None) or newest["closed_at"] or newest["opened_at"] or ""
    out["alert_at"] = max((g["alert_at"] for g in groups if g["alert_at"]), default=None)
    out["alert"] = out["alert_at"] is not None  # the bell is the folder's: an earlier session's counts too
    kinds = {g["alert_kind"] or "push" for g in groups if g["alert_at"]}
    out["alert_kind"] = None if not kinds else "email" if kinds == {"email"} else "push"  # any push: the bell
    out["group_count"] = len(groups)
    out["earlier_count"] = len(groups) - 1
    out["pending_history_requests"] = svc.db.scalar(
        "SELECT COUNT(*) FROM chat_history_requests r JOIN chat_groups g ON g.id=r.group_id"
        " WHERE g.host=? AND g.folder=? AND r.state='pending'", (newest["host"], newest["folder"])) or 0
    return out


@router.get("/api/chats")
async def chats_view(request: Request, host: str | None = None, folder: str | None = None,
                     client: Client = Depends(admin)) -> dict[str, Any]:
    """Every group, open ones first, then closed ones by closing time (`groups`), and the same grouped into one
    entry per host and folder, newest activity first (`folders`). Message text only for the signed-in page."""
    svc = _svc(request)
    sql, params = "SELECT * FROM chat_groups WHERE 1=1", []
    if host:
        sql, params = sql + " AND host=?", params + [host]
    if folder:
        sql, params = sql + " AND folder=?", params + [folder]
    rows = svc.db.query(sql + " ORDER BY (state='open') DESC, CASE WHEN state='open' THEN opened_at END DESC,"
                        " closed_at DESC, rowid DESC", params)
    by_folder: dict[tuple[str, str], list[Any]] = {}
    for g in svc.db.query(sql + " ORDER BY rowid", params):
        by_folder.setdefault((g["host"], g["folder"]), []).append(g)
    folders = sorted((_folder_out(svc, gs, client.via_session) for gs in by_folder.values()),
                     key=lambda f: f["activity_at"], reverse=True)
    return {"groups": [_group_out(svc, g, client.via_session) for g in rows], "folders": folders}


@router.get("/api/chats/folder")
async def chat_folder(request: Request, host: str, folder: str, client: Client = Depends(admin)) -> dict[str, Any]:
    """The groups of one folder, oldest first, with message counts (never text). The last one is the current thread."""
    svc = _svc(request)
    groups = svc.db.query("SELECT * FROM chat_groups WHERE host=? AND folder=? ORDER BY rowid", (host, folder))
    if not groups:
        raise HTTPException(404, "folder not found")
    out = []
    for g in groups:
        total = svc.db.scalar("SELECT COUNT(*) FROM chat_messages WHERE group_id=?", (g["id"],)) or 0
        said = svc.db.scalar("SELECT COUNT(*) FROM chat_messages WHERE group_id=? AND author<>'system'",
                             (g["id"],)) or 0
        out.append({"id": g["id"], "state": g["state"], "opened_at": g["opened_at"], "closed_at": g["closed_at"],
                    "message_count": said, "total_count": total})
    return {"host": host, "folder": folder, "groups": out}


def _chat_group(svc: NotifyService, group_id: str) -> Any:
    g = svc.db.one("SELECT * FROM chat_groups WHERE id=?", (group_id,))
    if g is None:
        raise HTTPException(404, "group not found")
    return g


@router.get("/api/chats/{group_id}/messages")
async def chat_messages(group_id: str, request: Request, after: str | None = None,
                        client: Client = Depends(admin)) -> dict[str, Any]:
    svc = _svc(request)
    g = _chat_group(svc, group_id)
    sql = "SELECT * FROM chat_messages WHERE group_id=?"
    params: list[Any] = [group_id]
    if after:
        sql += " AND rowid > COALESCE((SELECT rowid FROM chat_messages WHERE id=? AND group_id=?), 0)"
        params += [after, group_id]
    out = []
    names = {r["id"]: r["name"] for r in svc.db.query("SELECT id, name FROM chat_members WHERE group_id=?", (group_id,))}
    for r in svc.db.query(sql + " ORDER BY rowid", params):
        wakes = {"delivered": 0, "skipped": 0, "pending": 0}
        for w in svc.db.query("SELECT state, COUNT(*) AS n FROM chat_wakes WHERE message_id=? GROUP BY state",
                              (r["id"],)):
            wakes["pending" if w["state"] in ("pending", "claimed") else w["state"]] += w["n"]
        m = {"id": r["id"], "author": r["author"], "created_at": r["created_at"], "wakes": wakes, "kind": r["kind"]}
        if r["author"] != "system" and r["kind"] == "request":
            named = [x["name"] or x["id"] for x in svc.chat.mentioned(r["id"])]
            if named:
                m["mentions"] = named
        hr = svc.db.one("SELECT id, state, reason FROM chat_history_requests WHERE message_id=?", (r["id"],))
        if hr is not None:
            m["history_request"] = {"id": hr["id"], "state": hr["state"]}
            if client.via_session:  # the reason is for the owner only, neutralised like in the push
                m["history_request"]["reason"] = _clean_desc(" ".join(hr["reason"].split()))
        if r["in_reply_to"]:
            m["in_reply_to"] = r["in_reply_to"]
        if r["claimed_by_member_id"]:  # a member id and kind only, no text: fine for a bearer admin too
            cm = svc.db.one("SELECT id, kind, name FROM chat_members WHERE id=?", (r["claimed_by_member_id"],))
            if cm is not None:
                m["claimed_by"] = who(cm["kind"], cm["name"], cm["id"])
                m["claimed_at"] = r["claimed_at"]
        if client.via_session:  # like replies: a bearer admin sees states and times, never what people wrote
            m["text"] = r["text"]
            m["member_id"] = r["author_member_id"]
            m["member_name"] = names.get(r["author_member_id"])
            if r["model"]:  # the model its author ran when it wrote this
                m["member_model"] = model_label(r["model"])
            if r["in_reply_to"]:
                q = svc.db.one("SELECT * FROM chat_messages WHERE id=? AND group_id=?", (r["in_reply_to"], group_id))
                if q is not None:
                    m["reply_author"] = "owner" if q["author"] == "owner" else q["author"]
                    m["reply_member_id"] = q["author_member_id"]
                    m["reply_member_name"] = names.get(q["author_member_id"])
                    m["reply_excerpt"] = reply_excerpt(
                        q["text"], None if q["text"].strip() else svc.chat.message_files([q["id"]]).get(q["id"]))
        out.append(m)
    if client.via_session and out:  # file names are what people wrote: like text, for the signed-in page only
        files = svc.chat.files.for_messages([m["id"] for m in out])
        for m in out:
            if m["id"] in files:
                m["files"] = files[m["id"]]
    pending = [h["id"] for h in svc.db.query(
        "SELECT id FROM chat_history_requests WHERE group_id=? AND state='pending'", (group_id,))]
    # ticks for every message of the group, so ones already on screen update too (counts only, no text)
    receipts = svc.chat.receipts(group_id)
    for m in out:
        if m["id"] in receipts:
            m["receipt"] = receipts[m["id"]]
    return {"messages": out, "group": _group_out(svc, g, client.via_session), "pending_history_requests": pending,
            "receipts": receipts}


class ChatPostIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = ""
    in_reply_to: str | None = None
    fyi: bool = False
    files: list[Annotated[str, Field(max_length=64)]] = Field(default_factory=list, max_length=10)  # pending upload ids


@router.post("/api/chats/{group_id}/messages", status_code=201)
async def chat_post(group_id: str, body: ChatPostIn, request: Request,
                    client: Client = Depends(admin)) -> dict[str, Any]:
    require_session(client)
    _check_browser_mutation(request)
    svc = _svc(request)
    if _chat_group(svc, group_id)["state"] != "open":
        raise HTTPException(409, "group is closed")
    try:
        return svc.chat.post_owner(group_id, body.text, body.in_reply_to, body.fyi, files=body.files or None)
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    except Invalid as e:
        raise HTTPException(409 if "closed" in str(e) else 422, str(e)) from e


# ------------------------------------------------------------------ chat files (owner)

FILE_MAX = 10 << 20  # one upload; BodyLimit lets a signed-in page send this much to the upload route only
FILE_CSP = "default-src 'none'; sandbox"


@router.put("/api/chats/{group_id}/files", status_code=201)
async def chat_file_upload(group_id: str, request: Request, name: str = Query("", max_length=255),
                           client: Client = Depends(admin)) -> dict[str, Any]:
    """One file per request, the raw bytes as the body (no multipart): it waits as a pending file until a post
    names it. The X-Notify-Admin header (checked in `admin`) makes this a preflighted request, like the JSON routes."""
    require_session(client)
    _check_browser_mutation(request)
    svc = _svc(request)
    if _chat_group(svc, group_id)["state"] != "open":
        raise HTTPException(409, "group is closed")
    data = await request.body()
    if len(data) > FILE_MAX:
        raise HTTPException(413, f"a file is at most {FILE_MAX >> 20} MiB")
    from .chat_files import NewFile
    try:
        return await asyncio.to_thread(svc.chat.files.add_pending, group_id,
                                       NewFile(filename=name or None,
                                               content_type=request.headers.get("content-type"), data=data))
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    except Invalid as e:
        raise HTTPException(422, str(e)) from e


@router.delete("/api/chats/{group_id}/files/{file_id}", status_code=204)
async def chat_file_discard(group_id: str, file_id: str, request: Request,
                            client: Client = Depends(admin)) -> Response:
    require_session(client)
    _check_browser_mutation(request)
    svc = _svc(request)
    _chat_group(svc, group_id)
    try:
        svc.chat.files.discard_pending(group_id, file_id)
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    return Response(status_code=204)


@router.get("/api/chats/files/{file_id}")
async def chat_file_get(file_id: str, request: Request, client: Client = Depends(admin)) -> Response:
    """The bytes, for the signed-in page only. Only a sniffed raster image is shown inline (with its sniffed type);
    everything else downloads. nosniff and a sandboxing CSP, so a file can never run as a page of this origin."""
    require_session(client)
    svc = _svc(request)
    # an id never changes its bytes: the id is the ETag, and the browser asks again each time (no-cache), so a
    # signed-out page shows nothing from its cache while an unchanged file costs only a 304, without loading it
    etag = f'"{file_id}"'
    if request.headers.get("if-none-match") == etag:
        row = svc.db.one("SELECT purged_at FROM chat_files WHERE id=? AND data IS NOT NULL", (file_id,))
        if row is not None and row["purged_at"] is None:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, no-cache"})
    from starlette.background import BackgroundTask
    from .fetchgate import BUSY_HEADERS, FILE_FETCHES, Busy
    try:
        release = await FILE_FETCHES.acquire()  # held until the body is sent: that is when the bytes leave memory
    except Busy as e:
        raise HTTPException(503, str(e), headers=BUSY_HEADERS) from e
    try:
        f = svc.chat.files.owner_get(file_id)
        if f.expired or f.data is None:
            raise HTTPException(410, "file expired")
    except NotFound as e:
        await release()
        raise HTTPException(404, "file not found") from e
    except BaseException:
        await release()
        raise
    inline = f.kind == "image"
    from urllib.parse import quote
    safe = re.sub(r'[\x00-\x1f\x7f"\\\\;]', "_", f.name or "file")  # one header line, one quoted name
    ascii_name = safe.encode("ascii", "replace").decode().replace("?", "_")
    disp = f"{'inline' if inline else 'attachment'}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe)}"
    return Response(f.data, media_type=f.type if inline else "application/octet-stream", headers={
        "Content-Disposition": disp, "X-Content-Type-Options": "nosniff", "Content-Security-Policy": FILE_CSP,
        "Cache-Control": "private, no-cache", "ETag": etag, "Cross-Origin-Resource-Policy": "same-origin"},
        background=BackgroundTask(release))


@router.post("/api/chats/{group_id}/seen")
async def chat_seen(group_id: str, request: Request, alert_upto: str | None = Query(None, max_length=40),
                    client: Client = Depends(admin)) -> dict[str, Any]:
    """The owner read the chat. alert_upto (the folder's alert_at as the page showed it): they also looked at it
    after a notification, so the bell goes, unless a newer notification came in since."""
    require_session(client)
    _check_browser_mutation(request)
    svc = _svc(request)
    _chat_group(svc, group_id)
    with svc.db.tx() as c:
        c.execute("UPDATE chat_groups SET owner_seen_at=? WHERE id=?", (now_iso(), group_id))
    if alert_upto:
        svc.chat.alert_seen(group_id, alert_upto)
    return {"ok": True}


def _chat_owner_action(request: Request, group_id: str, client: Client, action: str) -> Response:
    require_session(client)
    _check_browser_mutation(request)
    svc = _svc(request)
    _chat_group(svc, group_id)
    try:
        getattr(svc.chat, action)(group_id)
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    return Response(status_code=204)


def _history_decision(request: Request, rid: str, client: Client, approve: bool) -> Response:
    require_session(client)
    _check_browser_mutation(request)
    try:
        _svc(request).chat.decide_history(rid, approve, by="admin-ui")
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    return Response(status_code=204)


@router.post("/api/chats/history-requests/{rid}/approve", status_code=204)
async def chat_history_approve(rid: str, request: Request, client: Client = Depends(admin)) -> Response:
    return _history_decision(request, rid, client, True)


@router.post("/api/chats/history-requests/{rid}/deny", status_code=204)
async def chat_history_deny(rid: str, request: Request, client: Client = Depends(admin)) -> Response:
    return _history_decision(request, rid, client, False)


@router.post("/api/chats/messages/{message_id}/release", status_code=204)
async def chat_release_claim(message_id: str, request: Request, client: Client = Depends(admin)) -> Response:
    require_session(client)
    _check_browser_mutation(request)
    try:
        _svc(request).chat.release_claim_owner(message_id)
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    except Invalid as e:
        raise HTTPException(409, str(e)) from e
    return Response(status_code=204)


class ChatFolderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = Field(min_length=1, max_length=100)
    folder: str = Field(min_length=1, max_length=1024)
    enabled: bool = True


@router.get("/api/chats/folders")
async def chat_folders(request: Request, client: Client = Depends(admin)) -> dict[str, Any]:
    """Folders the owner can switch chats on for: enabled ones, ones that had a group, ones with sessions now."""
    svc = _svc(request)
    return {"folders": svc.chat.folders(), "hosts": sorted(svc.chat.chat_hosts())}


@router.put("/api/chats/folders")
async def chat_folder_set(body: ChatFolderIn, request: Request, client: Client = Depends(admin)) -> dict[str, Any]:
    require_session(client)
    _check_browser_mutation(request)
    try:
        return _svc(request).chat.set_folder(body.host, body.folder, body.enabled)
    except Invalid as e:
        raise HTTPException(422, str(e)) from e


class ChatFolderMoveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = Field(min_length=1, max_length=100)
    folder: str = Field(min_length=1, max_length=1024)
    new_host: str = Field(min_length=1, max_length=100)
    new_folder: str = Field(min_length=1, max_length=1024)


def _folder_errors(fn: Any) -> Any:
    try:
        return fn()
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    except Invalid as e:
        raise HTTPException(422, str(e)) from e


@router.post("/api/chats/folders/move")
async def chat_folder_move(body: ChatFolderMoveIn, request: Request, client: Client = Depends(admin)) -> dict[str, Any]:
    """Correct a folder entry with no history (another path or host)."""
    require_session(client)
    _check_browser_mutation(request)
    svc = _svc(request)
    return _folder_errors(lambda: svc.chat.move_folder(body.host, body.folder, body.new_host, body.new_folder))


class ChatFolderDeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = Field(min_length=1, max_length=100)
    folder: str = Field(min_length=1, max_length=1024)
    with_history: bool = False  # also delete the folder's closed chats now, instead of after retention_days


@router.post("/api/chats/folders/delete")
async def chat_folder_delete(body: ChatFolderDeleteIn, request: Request,
                             client: Client = Depends(admin)) -> dict[str, Any]:
    """Remove a folder entry; with with_history its closed chats go too. Refused while a chat is open or a session
    is present there."""
    require_session(client)
    _check_browser_mutation(request)
    svc = _svc(request)
    return _folder_errors(lambda: svc.chat.delete_folder(body.host, body.folder, body.with_history))


@router.post("/api/chats/folders/open", status_code=201)
async def chat_folder_open(body: ChatFolderIn, request: Request, client: Client = Depends(admin)) -> dict[str, Any]:
    """Open a folder's group now (an ad-hoc group) and invite the sessions there; the folder stays as it is."""
    require_session(client)
    _check_browser_mutation(request)
    svc = _svc(request)
    if body.host not in svc.chat.chat_hosts():
        raise HTTPException(422, "host must be the chat host of an API client")
    try:
        return svc.chat.open_owner(body.host, body.folder)
    except Invalid as e:
        raise HTTPException(422, str(e)) from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e


class ChatRenameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=40)


@router.put("/api/chats/members/{member_id}")
async def chat_rename_member(member_id: str, body: ChatRenameIn, request: Request,
                             client: Client = Depends(admin)) -> dict[str, Any]:
    require_session(client)
    _check_browser_mutation(request)
    try:
        return _svc(request).chat.rename_owner(member_id, body.name)
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    except Invalid as e:
        raise HTTPException(422, str(e)) from e


class ChatGroupSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused_posts: int | None = None  # None: the server setting chat_paused_posts


@router.put("/api/chats/{group_id}/settings")
async def chat_group_settings(group_id: str, body: ChatGroupSettingsIn, request: Request,
                              client: Client = Depends(admin)) -> dict[str, Any]:
    require_session(client)
    _check_browser_mutation(request)
    svc = _svc(request)
    _chat_group(svc, group_id)
    try:
        return svc.chat.set_paused_posts(group_id, body.paused_posts)
    except Invalid as e:
        raise HTTPException(422, str(e)) from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e


@router.post("/api/chats/{group_id}/watching", status_code=204)
async def chat_watching(group_id: str, request: Request, client: Client = Depends(admin)) -> Response:
    return _chat_owner_action(request, group_id, client, "owner_watching")


@router.post("/api/chats/{group_id}/pause", status_code=204)
async def chat_pause(group_id: str, request: Request, client: Client = Depends(admin)) -> Response:
    return _chat_owner_action(request, group_id, client, "pause_owner")


@router.post("/api/chats/{group_id}/resume", status_code=204)
async def chat_resume(group_id: str, request: Request, client: Client = Depends(admin)) -> Response:
    return _chat_owner_action(request, group_id, client, "resume_owner")


# ------------------------------------------------------------------ reply mailbox cleanup

MAILBOX_CACHE_SECONDS = 30.0


def _reply_provider(request: Request, name: str) -> Any:
    p = _svc(request).providers.get(name)
    if p is None or not getattr(p, "replies_ready", False):
        raise HTTPException(404, f"provider {name} has no reply mailbox")
    return p


@router.get("/api/mailbox")
async def mailbox_view(request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    svc = _svc(request)
    out = []
    for name, p in svc.providers.items():
        if not getattr(p, "replies_ready", False):
            continue
        cached = svc.mailbox_cache.get(name)
        if cached is None or time.monotonic() - cached[0] > MAILBOX_CACHE_SECONDS:
            cached = (time.monotonic(), await svc.run_mailbox_cleanup(name, dry_run=True, by="admin-ui"))
            svc.mailbox_cache[name] = cached
        cfg = svc.config.cleanup_for(name)
        out.append({"name": name, "folders": [p.reply_processed, p.reply_quarantine], "preview": cached[1],
                    "settings": cfg.model_dump(), "status": svc.cleanup_status.get(name) or {},
                    "alert_warning": None if cfg.alert_targets or announce_targets(svc.config) else
                    "nobody is set to hear about a folder over its size cap: add a person or set alert targets"})
    return {"providers": out}


@router.put("/api/mailbox/{name}")
async def mailbox_settings(name: str, body: MailboxCleanupConfig, request: Request,
                           client: Client = Depends(admin)) -> dict[str, Any]:
    require_session(client)
    _reply_provider(request, name)
    _svc(request).mailbox_cache.pop(name, None)
    state = _commit(request, lambda data: data["mailbox_cleanup"].__setitem__(name, body.model_dump()))
    record_event(_svc(request), "mailbox.settings", client.id, {"provider": name, "settings": body.model_dump()})
    return state


@router.post("/api/mailbox/{name}/cleanup")
async def mailbox_cleanup(name: str, request: Request, client: Client = Depends(admin)) -> dict[str, Any]:
    require_session(client)
    _reply_provider(request, name)
    svc = _svc(request)
    lock = svc.mailbox_lock(name)
    try:
        await asyncio.wait_for(lock.acquire(), svc.cleanup_busy_wait)
    except TimeoutError:
        raise HTTPException(409, "mailbox busy, try again") from None
    lock.release()  # run_pass takes the lock per step
    try:
        return await svc.run_mailbox_cleanup(name, dry_run=False, by=client.id, deadline_seconds=60)
    finally:
        svc.mailbox_cache.pop(name, None)


@router.post("/api/notifications/{nid}/{action}")
async def manage(nid: str, action: str, request: Request, client: Client = Depends(admin)) -> dict[str, Any]:
    svc = _svc(request)
    try:
        if action == "retry":
            return svc.retry(client, nid)
        if action == "cancel":
            return await svc.cancel(client, nid)
        if action == "dismiss":
            return svc.dismiss(client, nid)
        if action == "send-now":
            return svc.scheduler.update(client, nid, ScheduleUpdate(send_now=True))
    except Invalid as e:
        raise HTTPException(422, str(e)) from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    raise HTTPException(404, "unknown action")
