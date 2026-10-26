"""Admin API behind the management UI (/admin).

Authentication: a browser session (password set during first-run setup with a
one-time code from the container log), or a bearer token with the `admin`
scope. Browser mutations must carry `X-Notify-Admin: 1` (cannot be sent
cross-site without a CORS preflight, which is never granted) and a same-origin
Origin header when present.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .auth import hash_token, new_token
from .config import ConfigError, MailboxCleanupConfig
from .core import Client, Conflict, Invalid, NotFound, NotifyService
from .models import NotificationIn, Scope, Severity
from .providers import REGISTRY
from .configops import commit_config
from .cleanup_service import record_event
from .providers.email_inbound import run_locked
from .registrations import announce_targets, approve, deny, list_registrations
from .store import ConfigStore

router = APIRouter(prefix="/admin", include_in_schema=False)

COOKIE = "notify_admin"
UI_CLIENT = Client("admin-ui", frozenset({Scope.admin}), via_session=True)
MIN_PASSWORD = 10


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
    return request.client.host if request.client else "?"


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


def _set_cookie(response: Response, sid: str) -> None:
    response.set_cookie(COOKIE, sid, max_age=14 * 86400, httponly=True, samesite="strict", path="/admin")


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
    if not store.check_setup_code(body.code):
        throttle.fail(key)
        raise HTTPException(403, "setup code is not valid; copy it from the container log")
    throttle.ok(key)
    store.set_password(body.password)
    _set_cookie(response, store.new_session())
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
    if not store.check_password(body.password):
        throttle.fail(key)
        raise HTTPException(403, "wrong password")
    throttle.ok(key)
    _set_cookie(response, store.new_session())
    return {"ok": True}


@router.post("/api/logout")
async def logout(request: Request, response: Response) -> dict[str, Any]:
    _store(request).end_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/admin")
    return {"ok": True}


class PasswordIn(BaseModel):
    current: str = Field(min_length=1, max_length=256)
    new: str = Field(min_length=MIN_PASSWORD, max_length=256)


@router.post("/api/password")
async def change_password(body: PasswordIn, request: Request, response: Response,
                          _: Client = Depends(admin)) -> dict[str, Any]:
    store = _store(request)
    if not store.check_password(body.current):
        raise HTTPException(403, "current password is wrong")
    store.set_password(body.new)  # signs out every session
    _set_cookie(response, store.new_session())
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
    return {
        "version": __version__,
        "pending_registrations": pending,
        "config": masked,
        "devices": {d["target"].split(":", 1)[1]: {"ready": d["ready"], "problems": d["problems"],
                                                    "capabilities": d["capabilities"],
                                                    "kind": cfg.device_kind(d["target"].split(":", 1)[1]),
                                                    "opt_in": cfg.device_opt_in(d["target"].split(":", 1)[1])}
                    for d in targets["devices"]},
        "providers": {name: {"type": p.type, "kind": p.kind, **p.health()} for name, p in svc.providers.items()},
        "provider_types": {t: {"label": cls.label or t, "kind": cls.kind, "settings": cls.settings_schema,
                               "address": cls.address_schema} for t, cls in REGISTRY.items()},
        "severities": [s.value for s in Severity],
        "scopes": [s.value for s in Scope],
        "stats": svc.stats(),
    }


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


@router.put("/api/providers/{name}")
async def put_provider(name: str, body: ProviderIn, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
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
            if kind == "secret" and not v and old.get("type") == body.type:
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
        data["providers"][name] = new
    return _commit(request, mutate)


@router.post("/api/providers/{name}/test")
async def test_provider(name: str, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    svc = _svc(request)
    provider = svc.providers.get(name)
    if provider is None:
        raise HTTPException(404, f"provider {name} is not configured")
    # the IMAP part must not overlap a poll or a cleanup step
    return await run_locked(svc.mailbox_lock(name), provider.test_connection)


@router.delete("/api/providers/{name}")
async def delete_provider(name: str, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
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
    return _commit(request, mutate)


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


@router.post("/api/clients/{name}")
async def create_client(name: str, body: ClientIn, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    if name in request.app.state.config.clients:
        raise HTTPException(409, f"{name} already exists; use Replace token to issue a new one")
    token = new_token()

    def mutate(data: dict[str, Any]) -> None:
        data["clients"][name] = {"token_sha256": hash_token(token), "scopes": [s.value for s in body.scopes],
                                 "description": body.description or None}
    out = _commit(request, mutate)
    out["token"] = token
    return out


@router.put("/api/clients/{name}")
async def update_client(name: str, body: ClientIn, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    def mutate(data: dict[str, Any]) -> None:
        if name not in data["clients"]:
            raise ConfigError(f"unknown API client {name!r}")
        data["clients"][name].update(scopes=[s.value for s in body.scopes], description=body.description or None)
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


@router.delete("/api/clients/{name}")
async def delete_client(name: str, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
    def mutate(data: dict[str, Any]) -> None:
        data["clients"].pop(name, None)
    return _commit(request, mutate)


# ------------------------------------------------------------------ server settings

class ServerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_base_url: str | None = None
    external_url: str | None = None
    max_attempts: int | None = None
    retention_days: int | None = None
    attachment_retention_days: int | None = None
    attachment_url_hosts: list[str] | None = None


@router.put("/api/server")
async def put_server(body: ServerIn, request: Request, _: Client = Depends(admin)) -> dict[str, Any]:
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
    title: str | None = "Test from hnl-notify"
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
    admin_view = Client(client.id, frozenset({Scope.admin}))
    return {"notifications": svc.list(admin_view, limit=max(1, min(limit, 100))),
            "attention": svc.attention(admin_view)}


@router.get("/api/replies")
async def replies_view(request: Request, limit: int = 25, client: Client = Depends(admin)) -> dict[str, Any]:
    svc = _svc(request)
    replies, quarantine = svc.replies.recent(limit), svc.replies.quarantined(limit)
    if not client.via_session:  # a bearer admin sees states and counts, never what people wrote
        for r in replies:
            r.update({"from": None, "subject": None, "text": None, "attachments": [], "dropped_attachments": []})
        for q in quarantine:
            q.update(from_address=None, subject=None, message_id=None)
    return {"inbound": svc.inbound_status, "replies": replies, "quarantine": quarantine}


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
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    raise HTTPException(404, "unknown action")
