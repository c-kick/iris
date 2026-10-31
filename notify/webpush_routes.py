"""Browser push surface: enrollment page and API, service worker, report endpoint.

No login. Every route answers only on the service's own external address (scheme, host and port), so a
browser can never be enrolled against, or report to, some other origin.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from .admin import _throttle
from .auth import hash_token
from .config import ConfigError
from .configops import commit_config
from .core import external_base
from .db import now_iso, parse_iso, utcnow
from .providers.webpush import WebPushProvider, push_endpoint_ok
from .webpush_crypto import unb64u

log = logging.getLogger("notify.webpush")

UI = Path(__file__).parent / "push_ui"
router = APIRouter(prefix="/push", include_in_schema=False)
_enroll_lock = threading.Lock()
_REPORT_PER_MINUTE = 120
_reports: dict[str, list[float]] = {}
_reports_lock = threading.Lock()
CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        p = urlsplit(url)
        scheme = p.scheme.lower()
        return scheme, (p.hostname or "").lower(), p.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None


def host_guard(request: Request) -> None:
    """404 unless the request arrived on server.external_url (X-Forwarded-Proto and Host honoured)."""
    base = external_base(request.app.state.config)
    scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    host = request.headers.get("host") or ""
    if not base or _origin(base) is None or _origin(f"{scheme}://{host}") != _origin(base):
        raise HTTPException(404)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _label(user_agent: str) -> str:
    ua = user_agent or ""
    browser = next((name for token, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                             ("Chrome/", "Chrome"), ("Safari/", "Safari")) if token in ua), None)
    system = next((name for token, name in (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iOS"),
                                            ("Mac OS X", "macOS"), ("Linux", "Linux")) if token in ua), None)
    return f"{browser} on {system}" if browser and system else "Browser"


def _static(name: str, media: str) -> Response:
    return Response((UI / name).read_bytes(), media_type=media, headers={
        "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


def _enrollment(db: Any, code: str) -> Any:
    row = db.one("SELECT * FROM enrollments WHERE code_sha256=?", (hash_token(code),)) if code else None
    if row is None or row["used_at"] or parse_iso(row["expires_at"]) <= utcnow():  # type: ignore[operator]
        return None
    return row


@router.get("/enroll")
def enroll_page(request: Request, c: str = "") -> Response:
    host_guard(request)
    throttle = _throttle(request)
    key = f"enroll:{_ip(request)}"
    throttle.check(key)
    row = _enrollment(request.app.state.service.db, c)
    if row is None:
        throttle.fail(key)
        return HTMLResponse("<!doctype html><title>Link expired</title><p>This link was already used or has "
                            "expired. Ask for a new one.</p>", status_code=410, headers={"Content-Security-Policy": CSP})
    provider = request.app.state.config.providers.get(row["provider"])
    public = (provider.model_dump().get("vapid_public_key") if provider else "") or ""
    html = (UI / "enroll.html").read_text().replace("{{KEY}}", public)
    return HTMLResponse(html, headers={"Content-Security-Policy": CSP, "Cache-Control": "no-store",
                                       "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})


@router.get("/enroll.js")
def enroll_js(request: Request) -> Response:
    host_guard(request)
    return _static("enroll.js", "application/javascript")


@router.get("/sw.js")
def service_worker(request: Request) -> Response:
    host_guard(request)
    resp = _static("sw.js", "application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/push/"
    return resp


def _bad(detail: str) -> HTTPException:
    return HTTPException(400, detail)


def _device_name(cfg: Any, owner: str) -> str:
    name, n = f"{owner}-browser", 1
    while name in cfg.devices:
        n += 1
        name = f"{owner}-browser-{n}"
    return name


@router.post("/enroll", status_code=201)
def enroll(body: dict[str, Any], request: Request) -> dict[str, str]:
    host_guard(request)
    app = request.app
    db = app.state.service.db
    throttle = _throttle(request)
    key = f"enroll:{_ip(request)}"
    throttle.check(key)
    sub = body.get("subscription") if isinstance(body.get("subscription"), dict) else {}
    keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
    endpoint, p256dh, auth = (str(sub.get("endpoint") or ""), str(keys.get("p256dh") or ""),
                              str(keys.get("auth") or ""))
    if not push_endpoint_ok(endpoint):
        raise _bad("this browser's push service is not supported")
    try:
        if len(unb64u(p256dh)) != 65 or len(unb64u(auth)) < 16:
            raise ValueError
    except ValueError:
        raise _bad("the browser sent an invalid subscription") from None
    user_agent = str(body.get("user_agent") or "")[:300]
    with _enroll_lock:  # check, write the device and burn the code as one step
        row = _enrollment(db, str(body.get("code") or ""))
        if row is None:
            throttle.fail(key)
            raise HTTPException(410, "this link was already used or has expired")
        out: dict[str, str] = {}

        def mutate(data: dict[str, Any]) -> None:
            owner = row["owner"]
            if owner not in data["users"] or row["provider"] not in data["providers"]:
                raise ConfigError("the person or provider for this link no longer exists")
            name = _device_name(app.state.config, owner)
            data["devices"][name] = {"provider": row["provider"], "owner": owner, "description": _label(user_agent),
                                     "address": {"endpoint": endpoint, "p256dh": p256dh, "auth": auth,
                                                 "user_agent": user_agent}}
            data["users"][owner]["devices"].append(name)
            out["device"] = name
        try:
            commit_config(app, mutate)
        except ConfigError as e:
            raise HTTPException(409, str(e)) from e
        with db.tx() as c:
            c.execute("UPDATE enrollments SET used_at=?, device=? WHERE id=?", (now_iso(), out["device"], row["id"]))
    throttle.ok(key)
    log.info("browser enrolled", extra={"device": out["device"], "provider": row["provider"]})
    return out


def _rate_ok(ip: str) -> bool:
    now = time.monotonic()
    with _reports_lock:
        hits = [t for t in _reports.get(ip, []) if now - t < 60]
        if len(hits) >= _REPORT_PER_MINUTE:
            _reports[ip] = hits
            return False
        hits.append(now)
        _reports[ip] = hits
        return True


@router.post("/report", status_code=204)
async def report(request: Request) -> Response:
    """The service worker says a push was shown or clicked. Always 204: nothing is revealed to a forger."""
    host_guard(request)
    if not _rate_ok(_ip(request)):
        return Response(status_code=204)
    try:
        body = await request.json()
        did, token, kind = str(body["d"]), str(body["k"]), body["e"]
    except (ValueError, KeyError, TypeError):
        return Response(status_code=204)
    if kind in ("shown", "clicked"):
        svc = request.app.state.service
        row = svc.db.one("SELECT provider, device FROM deliveries WHERE id=?", (did,))
        provider = svc.providers.get(row["provider"]) if row else None
        if isinstance(provider, WebPushProvider) and provider.report_ok(did, token):
            svc.apply_report(did, kind, row["device"])
    return Response(status_code=204)
