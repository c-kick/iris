"""Self-registration: an app asks for access, the admin approves it.

The app gets its token immediately, but the token is unknown to the
authenticator (only its hash sits in `registrations`) until the request is
approved; approval copies that hash into the API client configuration, so the
app never has to be reconfigured. Admin scope can never be self-requested.
"""

from __future__ import annotations

import hmac
import html
import ipaddress
import logging
import os
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import hash_token, new_token
from .config import _NAME_RE, ConfigError
from .configops import commit_config
from .core import SYSTEM_CLIENT, Client, Invalid
from .db import dumps, iso, loads, new_id, now_iso, parse_iso, utcnow
from .models import NotificationIn, Scope

log = logging.getLogger("notify.registrations")

router = APIRouter(tags=["registration"])

PENDING_DAYS = 7
PER_ADDRESS_PER_HOUR = 5
MAX_PENDING = 20
SELF_SCOPES = {Scope.send, Scope.read, Scope.manage}
SCOPE_WORDS = {"send": "send notifications", "read": "read its own history",
               "manage": "retry or cancel its own notifications", "admin": "administer everything"}


def scope_phrase(scopes: list[str]) -> str:
    """['send', 'read'] -> 'send notifications and read its own history'."""
    words = [SCOPE_WORDS.get(s, s) for s in scopes]
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


class RegistrationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64, description="lowercase name for this app, e.g. node-red")
    description: str | None = Field(default=None, max_length=200, description="what it will send, for the approver")
    scopes: list[Scope] = Field(default_factory=lambda: [Scope.send, Scope.read])

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip()
        if not _NAME_RE.match(v):
            raise ValueError("use lowercase letters, digits, '.', '_' or '-' (max 64), starting with a letter or digit")
        return v

    @field_validator("scopes")
    @classmethod
    def _self_scopes(cls, v: list[Scope]) -> list[Scope]:
        if not v:
            raise ValueError("request at least one scope")
        bad = [s.value for s in v if s not in SELF_SCOPES]
        if bad:
            raise ValueError(f"{', '.join(bad)} cannot be requested; ask the administrator")
        order = [Scope.send, Scope.read, Scope.manage]
        return sorted(set(v), key=order.index)


# ------------------------------------------------------------------ data access

def expire_stale(db: Any) -> None:
    cutoff = iso(utcnow() - timedelta(days=PENDING_DAYS))
    with db.tx() as c:
        c.execute("UPDATE registrations SET status='expired', decided_at=?, decided_by='expiry'"
                  " WHERE status='pending' AND created_at < ?", (now_iso(), cutoff))


def row_out(r: Any, config: Any = None) -> dict[str, Any]:
    status = r["status"]
    if status == "approved" and config is not None:
        client = config.clients.get(r["name"])
        if client is None or client.token_sha256 != r["token_sha256"]:
            status = "revoked"
    return {"id": r["id"], "name": r["name"], "description": r["description"], "status": status,
            "requested_scopes": loads(r["requested_scopes_json"], []),
            "granted_scopes": loads(r["granted_scopes_json"]), "source_ip": r["source_ip"],
            "user_agent": r["user_agent"], "created_at": r["created_at"], "decided_at": r["decided_at"],
            "decided_by": r["decided_by"]}


def list_registrations(app: Any, recent: int = 20) -> dict[str, Any]:
    db = app.state.service.db
    expire_stale(db)
    pending = db.query("SELECT * FROM registrations WHERE status='pending' ORDER BY created_at")
    decided = db.query("SELECT * FROM registrations WHERE status!='pending' ORDER BY decided_at DESC LIMIT ?",
                       (recent,))
    cfg = app.state.config
    return {"pending": [row_out(r, cfg) for r in pending], "recent": [row_out(r, cfg) for r in decided]}


def approve(app: Any, rid: str, scopes: list[Scope] | None, description: str | None, by: str) -> dict[str, Any]:
    db = app.state.service.db
    expire_stale(db)
    r = db.one("SELECT * FROM registrations WHERE id=?", (rid,))
    if r is None:
        raise KeyError(rid)
    if r["status"] != "pending":
        raise ConfigError(f"this request is already {r['status']}")
    granted = scopes if scopes is not None else [Scope(s) for s in loads(r["requested_scopes_json"], [])]
    if not granted:
        raise ConfigError("grant at least one permission, or deny the request")
    desc = description if description is not None else r["description"]

    def mutate(data: dict[str, Any]) -> None:
        if r["name"] in data["clients"]:
            raise ConfigError(f"an API client named {r['name']!r} already exists; deny this request or revoke that client first")
        data["clients"][r["name"]] = {"token_sha256": r["token_sha256"], "scopes": [s.value for s in granted],
                                      "description": desc or None}
    commit_config(app, mutate)
    with db.tx() as c:
        c.execute("UPDATE registrations SET status='approved', granted_scopes_json=?, decided_at=?, decided_by=?,"
                  " decision_sha256=NULL WHERE id=?", (dumps([s.value for s in granted]), now_iso(), by, rid))
    log.info("registration approved", extra={"registration_id": rid, "client": r["name"], "by": by,
                                             "scopes": [s.value for s in granted]})
    return row_out(db.one("SELECT * FROM registrations WHERE id=?", (rid,)), app.state.config)


def deny(app: Any, rid: str, by: str) -> dict[str, Any]:
    db = app.state.service.db
    r = db.one("SELECT * FROM registrations WHERE id=?", (rid,))
    if r is None:
        raise KeyError(rid)
    if r["status"] != "pending":
        raise ConfigError(f"this request is already {r['status']}")
    with db.tx() as c:
        c.execute("UPDATE registrations SET status='denied', decided_at=?, decided_by=?, decision_sha256=NULL"
                  " WHERE id=?", (now_iso(), by, rid))
    log.info("registration denied", extra={"registration_id": rid, "client": r["name"], "by": by})
    return row_out(db.one("SELECT * FROM registrations WHERE id=?", (rid,)))


# ------------------------------------------------------------------ admin alert

def _external_url(app: Any, request: Request) -> str:
    cfg = app.state.config
    base = cfg.server.external_url or os.environ.get("NOTIFY_EXTERNAL_URL") or str(request.base_url)
    return base.rstrip("/")


def notify_admin(app: Any, request: Request, reg: dict[str, Any], auto: bool,
                 decision_code: str | None = None) -> None:
    cfg = app.state.config
    targets = cfg.registration.notify_targets or [f"user:{u}" for u in cfg.users]
    if not targets:
        log.warning("access request not announced: no people configured", extra={"client": reg["name"]})
        return
    base = _external_url(app, request)
    scopes = reg["granted_scopes"] if auto else reg["requested_scopes"]
    wants = scope_phrase(scopes)
    about = f" “{reg['description']}”" if reg["description"] else ""
    where = f" from {reg['source_ip']}" if reg["source_ip"] else ""
    actions: list[dict[str, str]] = []
    if auto or not decision_code:
        title = f"Access granted: {reg['name']}" if auto else f"Access request: {reg['name']}"
        message = (f"{reg['name']}{where} was approved automatically to {wants}.{about}" if auto else
                   f"{reg['name']}{where} wants to {wants}.{about} Open the admin page to approve or deny.")
        url, url_title = f"{base}/admin#requests", "Review access requests"
    else:
        title = f"Access request: {reg['name']}"
        message = f"{reg['name']}{where} wants to {wants}.{about} Tap to approve or deny."
        decide = f"{base}/admin/decide/{reg['id']}?k={decision_code}"
        url, url_title = decide, "Approve or deny"
        actions = [{"id": "approve", "label": "Approve", "url": f"{decide}&action=approve"},
                   {"id": "deny", "label": "Deny", "url": f"{decide}&action=deny"}]
    req = NotificationIn(targets=targets, title=title, message=message, severity="normal",
                         url=url, url_title=url_title, actions=actions,
                         source="hnl-notify/access-requests", tags=["access-request"],
                         data={"registration_id": reg["id"], "client": reg["name"]})
    try:
        app.state.service.submit(SYSTEM_CLIENT, req)
    except Invalid as e:
        log.warning("access request not announced", extra={"client": reg["name"], "error": str(e)})


# ------------------------------------------------------------------ public endpoints

def _in_subnets(ip: str | None, subnets: list[str]) -> bool:
    if not ip or not subnets:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in ipaddress.ip_network(n) for n in subnets)


@router.post("/v1/registrations", status_code=202)
async def request_access(body: RegistrationIn, request: Request) -> dict[str, Any]:
    """Ask for an API token. No authentication: the token only works once an admin approves it."""
    app = request.app
    cfg = app.state.config
    if not cfg.registration.enabled:
        raise HTTPException(403, "self-registration is turned off; ask the administrator for a token")
    db = app.state.service.db
    expire_stale(db)
    ip = request.client.host if request.client else None
    hour_ago = iso(utcnow() - timedelta(hours=1))
    recent = db.scalar("SELECT COUNT(*) FROM registrations WHERE source_ip IS ? AND created_at >= ?", (ip, hour_ago))
    if recent >= PER_ADDRESS_PER_HOUR:
        raise HTTPException(429, "too many access requests from this address; try again in an hour")
    if body.name in cfg.clients:
        raise HTTPException(409, f"{body.name!r} is already registered; choose another name or ask the administrator")
    if db.one("SELECT 1 FROM registrations WHERE name=? AND status='pending'", (body.name,)):
        raise HTTPException(409, f"a request for {body.name!r} is already waiting for approval")
    if db.scalar("SELECT COUNT(*) FROM registrations WHERE status='pending'") >= MAX_PENDING:
        raise HTTPException(429, "too many access requests are waiting for approval")

    token = new_token()
    decision_code = secrets.token_urlsafe(24)  # single-use capability for the phone decision link
    rid = new_id("reg")
    with db.tx() as c:
        c.execute("INSERT INTO registrations(id, name, description, requested_scopes_json, token_sha256, status,"
                  " source_ip, user_agent, created_at, decision_sha256) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (rid, body.name, body.description, dumps([s.value for s in body.scopes]), hash_token(token),
                   "pending", ip, (request.headers.get("user-agent") or "")[:200] or None, now_iso(),
                   hash_token(decision_code)))
    log.info("registration requested", extra={"registration_id": rid, "client": body.name, "source_ip": ip})

    auto = (_in_subnets(ip, cfg.registration.auto_approve_subnets)
            and set(body.scopes) <= set(cfg.registration.auto_approve_scopes))
    if auto:
        reg = approve(app, rid, None, None, by="auto-approve")
    else:
        reg = row_out(db.one("SELECT * FROM registrations WHERE id=?", (rid,)))
    notify_admin(app, request, reg, auto=auto, decision_code=None if auto else decision_code)
    return {**reg, "token": token, "status_url": "/v1/registrations/me",
            "message": ("Approved automatically; the token works now." if auto else
                        "Waiting for approval. Keep this token: it starts working once the administrator "
                        "approves, without any change on your side. Check GET /v1/registrations/me.")}


@router.get("/v1/registrations/me")
async def my_registration(request: Request) -> dict[str, Any]:
    """Status of the access request that issued this bearer token."""
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(401, "send the token you received as: Authorization: Bearer <token>",
                            headers={"WWW-Authenticate": "Bearer"})
    db = request.app.state.service.db
    expire_stale(db)
    r = db.one("SELECT * FROM registrations WHERE token_sha256=? ORDER BY created_at DESC LIMIT 1",
               (hash_token(token.strip()),))
    if r is None:
        raise HTTPException(404, "no access request was made with this token")
    out = row_out(r, request.app.state.config)
    out.pop("source_ip", None)
    out.pop("user_agent", None)
    return out


# ------------------------------------------------------------------ phone decision page
#
# The alert's link carries a single-use code for one request (only its hash is
# stored). Opening the page never decides anything: a button press (POST) does.
# The code is cleared on any decision, from here or from the admin UI.

LOGO = ('<svg width="26" height="26" viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="12" '
        'fill="var(--action)"/><circle cx="16" cy="16" r="5" fill="var(--surface)"/></svg>')


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark"><meta name="robots" content="noindex">
<title>{html.escape(title)} · hnl-notify</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&display=swap">
<link rel="stylesheet" href="/admin/static/app.css"></head>
<body><main class="gate"><div class="panel decide">
<div class="brand">{LOGO}<h1>hnl-notify</h1></div>
{body}
</div></main></body></html>"""
    return HTMLResponse(doc, status_code=status,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def _gone() -> HTMLResponse:
    return _page("Link no longer valid", """
<h2>This link no longer works</h2>
<p>The request was already decided, has expired, or the link is incomplete.</p>
<p><a class="btn" href="/admin#requests">Open access requests</a></p>""", status=410)


def _check_code(request: Request, rid: str, code: str | None) -> Any:
    """The pending registration row if `code` is its current decision code, else None (throttled)."""
    from .admin import Throttle  # admin imports this module; import lazily to avoid a cycle

    app = request.app
    if not hasattr(app.state, "decide_throttle"):
        app.state.decide_throttle = Throttle()
    throttle = app.state.decide_throttle
    key = f"decide:{request.client.host if request.client else '?'}"
    throttle.check(key)
    db = app.state.service.db
    expire_stale(db)
    r = db.one("SELECT * FROM registrations WHERE id=?", (rid,))
    ok = (r is not None and code and r["status"] == "pending" and r["decision_sha256"]
          and hmac.compare_digest(hash_token(code), r["decision_sha256"]))
    if not ok:
        throttle.fail(key)
        return None
    throttle.ok(key)
    return r


def _when(iso_ts: str | None) -> str:
    dt = parse_iso(iso_ts)
    return dt.astimezone().strftime("%d %b %H:%M") if dt else ""


@router.get("/admin/decide/{rid}", include_in_schema=False)
async def decision_page(rid: str, request: Request, k: str | None = None, action: str | None = None) -> HTMLResponse:
    r = _check_code(request, rid, k)
    if r is None:
        return _gone()
    e = html.escape
    scopes = loads(r["requested_scopes_json"], [])
    items = "".join(f"<li>{e(SCOPE_WORDS.get(s, s))}</li>" for s in scopes)
    origin = " ".join(x for x in [f"from {r['source_ip']}" if r["source_ip"] else "",
                                  f"({r['user_agent']})" if r["user_agent"] else ""] if x)
    approve = '<button class="btn primary" name="decision" value="approve"{af}>Approve</button>'
    deny = '<button class="btn danger" name="decision" value="deny"{af}>Deny</button>'
    first, second = (deny, approve) if action == "deny" else (approve, deny)
    buttons = first.format(af=" autofocus") + second.format(af="")
    return _page(f"{r['name']} wants access", f"""
<h2>{e(r['name'])} wants access</h2>
{f'<p>{e(r["description"])}</p>' if r["description"] else ''}
<p class="hint">Asked {e(_when(r['created_at']))} {e(origin)}</p>
<p>If you approve, it may:</p>
<ul>{items}</ul>
<form method="post" action="/admin/decide/{e(rid)}">
  <input type="hidden" name="k" value="{e(k or '')}">
  <div class="decide-buttons">{buttons}</div>
</form>
<p class="hint">To grant fewer permissions, use the <a href="/admin#requests">admin page</a> instead.</p>""")


@router.post("/admin/decide/{rid}", include_in_schema=False)
async def decide_from_phone(rid: str, request: Request) -> HTMLResponse:
    form = dict(parse_qsl((await request.body()).decode(errors="replace")))
    decision = form.get("decision")
    if decision not in ("approve", "deny"):
        return _page("Choose Approve or Deny", "<h2>Choose Approve or Deny</h2>", status=400)
    r = _check_code(request, rid, form.get("k"))
    if r is None:
        return _gone()
    e = html.escape
    try:
        if decision == "approve":
            reg = approve(request.app, rid, None, None, by="phone link")
            words = scope_phrase(reg["granted_scopes"] or [])
            return _page(f"{r['name']} approved", f"""
<h2>{e(r['name'])} approved</h2>
<p>It can now {e(words)}. It keeps the token it already has, so nothing else needs to change.</p>
<p class="hint">Revoke it any time under API access in the <a href="/admin#access">admin page</a>.</p>""")
        deny(request.app, rid, by="phone link")
        return _page(f"{r['name']} denied", f"""
<h2>{e(r['name'])} denied</h2>
<p>Its token will never work. It can ask again later.</p>""")
    except ConfigError as ex:
        return _page("Could not decide", f"<h2>Could not decide</h2><p>{e(str(ex))}</p>"
                                         '<p><a class="btn" href="/admin#requests">Open access requests</a></p>',
                     status=409)
