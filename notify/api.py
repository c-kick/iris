"""REST API (/v1), health and metrics."""

from __future__ import annotations

import functools
import hmac
import json
import logging
import re
import time
import weakref
from typing import Any, AsyncIterator
from urllib.parse import parse_qsl, quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from . import __version__, arr
from .core import Client, Conflict, Forbidden, Gone, Invalid, NotFound, NotifyService
from .models import NotificationIn, ScheduleUpdate, Scope, SubscriptionIn, event_matches

log = logging.getLogger("notify.api")

router = APIRouter()


def service(request: Request) -> NotifyService:
    return request.app.state.service


def require(scope: Scope):
    def dep(request: Request) -> Client:
        client = request.app.state.auth.from_header(request.headers.get("authorization"))
        if client is None:
            raise HTTPException(401, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"})
        if not client.can(scope):
            raise HTTPException(403, f"client {client.id!r} lacks scope {scope.value!r}")
        return client
    return dep


def require_any(*scopes: Scope):
    """Like require(), but any one of the scopes will do (admin always passes)."""
    def dep(request: Request) -> Client:
        client = request.app.state.auth.from_header(request.headers.get("authorization"))
        if client is None:
            raise HTTPException(401, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"})
        if not any(client.can(s) for s in scopes):
            raise HTTPException(403, f"client {client.id!r} lacks scope {' or '.join(repr(s.value) for s in scopes)}")
        return client
    return dep


def _errors(fn):
    """Map core exceptions to HTTP errors."""
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except NotFound as e:
            raise HTTPException(404, str(e)) from e
        except Forbidden as e:
            raise HTTPException(403, str(e)) from e
        except Gone as e:
            raise HTTPException(410, str(e)) from e
        except Invalid as e:
            raise HTTPException(422, str(e)) from e
        except Conflict as e:
            raise HTTPException(409, {"error": str(e), "results": e.detail}) from e
    return wrapper


# ------------------------------------------------------------------ notifications

@router.post("/v1/notifications", status_code=202, tags=["notifications"])
@_errors
async def submit(body: NotificationIn, response: Response, request: Request,
                 idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                 reply_handle: str | None = Header(default=None, alias="X-Reply-Handle", max_length=200),
                 client: Client = Depends(require(Scope.send))) -> dict[str, Any]:
    if idempotency_key and not body.idempotency_key:
        body.idempotency_key = idempotency_key
    if reply_handle and not body.reply_handle:  # the body field wins over the header
        body.reply_handle = reply_handle
    out, created = await service(request).submit_async(client, body)
    if body.dry_run:
        response.status_code = 200
    elif not created:
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
    else:
        response.headers["Location"] = f"/v1/notifications/{out['id']}"
    return out


@router.get("/v1/notifications", tags=["notifications"])
@_errors
async def list_notifications(request: Request, status: str | None = None, source: str | None = None,
                             tag: str | None = None, since: str | None = None,
                             before: str | None = Query(default=None, description="id cursor: only sent before this one"),
                             limit: int = Query(default=50, ge=1, le=200),
                             client: Client = Depends(require(Scope.read))) -> dict[str, Any]:
    items = service(request).list(client, status=status, source=source, tag=tag, since=since, before=before, limit=limit)
    queue = status is not None and status.strip() == "scheduled"
    return {"items": items, "next_before": None if queue or len(items) != limit else items[-1]["id"]}


@router.get("/v1/notifications/{nid}", tags=["notifications"])
@_errors
async def get_notification(nid: str, request: Request, client: Client = Depends(require(Scope.read))) -> dict[str, Any]:
    return service(request).get(client, nid)


@router.get("/v1/notifications/{nid}/events", tags=["notifications"])
@_errors
async def notification_events(nid: str, request: Request, client: Client = Depends(require(Scope.read))) -> dict[str, Any]:
    return {"items": service(request).get(client, nid)["events"]}


@router.patch("/v1/notifications/{nid}", tags=["notifications"])
@_errors
async def update_scheduled(nid: str, body: ScheduleUpdate, request: Request,
                           client: Client = Depends(require(Scope.send))) -> dict[str, Any]:
    return service(request).scheduler.update(client, nid, body)


@router.post("/v1/notifications/{nid}/cancel", tags=["notifications"])
@_errors
async def cancel(nid: str, request: Request, client: Client = Depends(require_any(Scope.send, Scope.manage))) -> dict[str, Any]:
    return await service(request).cancel(client, nid)


@router.post("/v1/notifications/{nid}/retry", tags=["notifications"])
@_errors
async def retry(nid: str, request: Request, client: Client = Depends(require(Scope.manage))) -> dict[str, Any]:
    return service(request).retry(client, nid)


# ------------------------------------------------------------------ replies

@router.post("/v1/notifications/{nid}/replies/claim", tags=["replies"], responses={204: {"description": "no reply yet"},
                                                                                            410: {"description": "no reply can ever come (cancelled, missed or never sent)"}})
@_errors
async def claim_reply(nid: str, request: Request,
                      waiter: str = Query(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$",
                                          description="Opaque id of this wait; a retry with the same id gets the same reply"),
                      wait: float = Query(default=50, ge=0, le=55, description="Seconds to wait for a reply"),
                      reply_handle: str | None = Header(default=None, alias="X-Reply-Handle", max_length=200),
                      client: Client = Depends(require(Scope.read))) -> Response:
    out = await service(request).replies.claim(client, nid, waiter, wait, reply_handle)
    return Response(status_code=204) if out is None else JSONResponse(out)


@router.post("/v1/notifications/{nid}/replies/{rid}/collected", status_code=204, tags=["replies"])
@_errors
async def reply_collected(nid: str, rid: str, request: Request,
                          reply_handle: str | None = Header(default=None, alias="X-Reply-Handle", max_length=200),
                          client: Client = Depends(require(Scope.read))) -> Response:
    """The waiter printed this reply (notify-wait calls it): the claim never hands it out again."""
    service(request).replies.mark_collected(client, nid, rid, reply_handle)
    return Response(status_code=204)


@router.get("/v1/notifications/{nid}/replies", tags=["replies"])
@_errors
async def list_replies(nid: str, request: Request,
                       reply_handle: str | None = Header(default=None, alias="X-Reply-Handle", max_length=200),
                       client: Client = Depends(require(Scope.read))) -> dict[str, Any]:
    return {"items": service(request).replies.list(client, nid, reply_handle)}


@router.get("/v1/replies/{rid}/attachments/{idx}", tags=["replies"])
@_errors
async def reply_attachment(rid: str, idx: int, request: Request,
                           reply_handle: str | None = Header(default=None, alias="X-Reply-Handle", max_length=200),
                           client: Client = Depends(require(Scope.read))) -> Response:
    filename, ctype, data = service(request).replies.attachment(client, rid, idx, reply_handle)
    if (len(ctype) > 100 or not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", ctype)
            or ctype.lower() in ("text/html", "image/svg+xml")):
        ctype = "application/octet-stream"
    return Response(content=data, media_type=ctype,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
                             "X-Content-Type-Options": "nosniff"})


@router.get("/v1/attention", tags=["notifications"])
@_errors
async def attention(request: Request, client: Client = Depends(require(Scope.read))) -> dict[str, Any]:
    return service(request).attention(client)


# ------------------------------------------------------------------ targets/providers

@router.get("/v1/targets", tags=["routing"])
async def targets(request: Request, client: Client = Depends(require(Scope.read))) -> dict[str, Any]:
    return service(request).targets()


@router.get("/v1/providers", tags=["routing"])
async def providers(request: Request, client: Client = Depends(require(Scope.read))) -> dict[str, Any]:
    return {"items": service(request).providers_info()}


@router.get("/v1/whoami", tags=["ops"])
async def whoami(client: Client = Depends(require(Scope.send))) -> dict[str, Any]:
    return {"client": client.id, "scopes": sorted(s.value for s in client.scopes)}


# ------------------------------------------------------------------ events

@router.get("/v1/events", tags=["events"])
async def events(request: Request, after: int = 0, notification_id: str | None = None, types: str | None = None,
                 source: str | None = None, limit: int = Query(default=100, ge=1, le=500),
                 client: Client = Depends(require(Scope.read))) -> dict[str, Any]:
    items, cursor = service(request).events_page(client, after, notification_id=notification_id,
                                                 types=types.split(",") if types else None, source=source, limit=limit)
    return {"items": items, "cursor": cursor}


MAX_STREAMS_PER_CLIENT = 5
_streams: dict[str, int] = {}  # open SSE streams per client (one process, so in memory is exact)


@router.get("/v1/events/stream", tags=["events"])
async def event_stream(request: Request, after: int | None = None, notification_id: str | None = None,
                       types: str | None = None, source: str | None = None,
                       last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
                       client: Client = Depends(require(Scope.read))) -> StreamingResponse:
    svc = service(request)
    if last_event_id and last_event_id.isdigit():
        cursor = int(last_event_id)
    elif after is not None:
        cursor = after
    else:
        cursor = svc.last_event_id()
    type_list = types.split(",") if types else None
    if _streams.get(client.id, 0) >= MAX_STREAMS_PER_CLIENT:
        raise HTTPException(429, f"at most {MAX_STREAMS_PER_CLIENT} event streams per client; close one first")
    _streams[client.id] = _streams.get(client.id, 0) + 1
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            _streams[client.id] = max(0, _streams.get(client.id, 1) - 1)

    async def gen() -> AsyncIterator[bytes]:
        try:
            async for chunk in _stream():
                yield chunk
        finally:
            release()

    async def _stream() -> AsyncIterator[bytes]:
        nonlocal cursor
        yield b"retry: 3000\n\n"
        idle = 0.0
        while not await request.is_disconnected():
            items = svc.events_after(client, cursor, notification_id=notification_id, types=None,
                                     source=source, limit=200)
            for e in items:
                cursor = e["id"]
                if type_list and not any(event_matches(t, e["type"]) for t in type_list):
                    continue
                yield f"id: {e['id']}\nevent: {e['type']}\ndata: {json.dumps(e, default=str)}\n\n".encode()
            if items:
                idle = 0.0
                continue
            await svc.bus.wait(5.0)
            idle += 5.0
            if idle >= 15.0:
                idle = 0.0
                yield b": keepalive\n\n"

    body = gen()
    weakref.finalize(body, release)  # a stream the server never started iterating is released too
    return StreamingResponse(body, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ subscriptions

@router.post("/v1/subscriptions", status_code=201, tags=["events"])
@_errors
async def create_subscription(body: SubscriptionIn, request: Request,
                              client: Client = Depends(require(Scope.read))) -> dict[str, Any]:
    return service(request).create_subscription(client, body)


@router.get("/v1/subscriptions", tags=["events"])
async def list_subscriptions(request: Request, client: Client = Depends(require(Scope.read))) -> dict[str, Any]:
    return {"items": service(request).list_subscriptions(client)}


@router.post("/v1/subscriptions/{sid}/enable", tags=["events"])
@_errors
async def enable_subscription(sid: str, request: Request, client: Client = Depends(require(Scope.read))) -> dict[str, Any]:
    """Turn a subscription that was disabled after failures back on (owner or admin). Events from while it was off
    are not replayed: catch up with GET /v1/events."""
    return service(request).enable_subscription(client, sid)


@router.delete("/v1/subscriptions/{sid}", status_code=204, tags=["events"])
@_errors
async def delete_subscription(sid: str, request: Request, client: Client = Depends(require(Scope.read))) -> Response:
    service(request).delete_subscription(client, sid)
    return Response(status_code=204)


# ------------------------------------------------------------------ ingest adapters

@router.api_route("/v1/ingest/arr", methods=["POST", "PUT"], status_code=202, tags=["ingest"])
@_errors
async def ingest_arr(request: Request, response: Response,
                     targets: list[str] = Query(description="Targets, repeated or comma-separated"),
                     kinds: str | None = Query(default=None, description="Comma-separated delivery kinds: push, email"),
                     client: Client = Depends(require(Scope.send))) -> dict[str, Any]:
    """Radarr / Sonarr / Lidarr / Prowlarr "Webhook" connection. The token may be sent as the Basic-auth password."""
    try:
        payload = json.loads(await request.body())
    except ValueError as e:
        raise HTTPException(422, "body is not JSON") from e
    if not isinstance(payload, dict):
        raise HTTPException(422, "body must be a JSON object")
    wanted = [t.strip() for raw in targets for t in raw.split(",") if t.strip()]
    try:
        body = arr.translate(payload, wanted)
        if kinds is not None:
            body = NotificationIn.model_validate(
                {**body.model_dump(), "kinds": [k.strip() for k in kinds.split(",") if k.strip()]})
    except ValueError as e:  # includes pydantic ValidationError (e.g. no targets)
        raise HTTPException(422, str(e)) from e
    except (TypeError, AttributeError, KeyError) as e:  # a field of an unexpected type in the app's payload
        raise HTTPException(422, f"unexpected webhook payload: {type(e).__name__}") from e
    out, created = await service(request).submit_async(client, body)
    if not created:
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
    else:
        response.headers["Location"] = f"/v1/notifications/{out['id']}"
    return out


# ------------------------------------------------------------------ provider callbacks

@router.post("/v1/callbacks/{provider}/{secret}", include_in_schema=False)
async def provider_callback(provider: str, secret: str, request: Request) -> Response:
    svc = service(request)
    expected = svc.config.server.callback_secret
    if (not expected or not hmac.compare_digest(secret.encode(), expected.encode())
            or provider not in svc.providers):
        return Response(status_code=404)
    raw = (await request.body()).decode(errors="replace")
    ctype = request.headers.get("content-type", "")
    try:
        form = json.loads(raw) if "json" in ctype else dict(parse_qsl(raw))
    except ValueError:
        return Response(status_code=400)
    if not isinstance(form, dict):
        return Response(status_code=400)
    parsed = svc.providers[provider].parse_callback({k: str(v) for k, v in form.items()})
    if parsed is None:
        return Response(status_code=204)
    receipt, st = parsed
    d = svc.delivery_by_receipt(provider, receipt)
    if d is not None:
        svc.apply_receipt(d["id"], st, via="callback")
    return Response(status_code=204)


# ------------------------------------------------------------------ ops

@router.get("/health/live", tags=["ops"])
async def live() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


_last_write_probe = float("-inf")


@router.get("/health/ready", tags=["ops"])
async def ready(request: Request) -> JSONResponse:
    svc = service(request)
    workers = request.app.state.workers
    checks: dict[str, Any] = {}
    try:
        svc.db.scalar("SELECT 1")
        global _last_write_probe
        if time.monotonic() - _last_write_probe >= 5.0:  # unauthenticated: at most one probe write per 5 s
            with svc.db.tx() as c:
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('ready_probe', datetime('now'))")
            _last_write_probe = time.monotonic()
        checks["database"] = "ok"
    except Exception:
        log.exception("readiness: database check failed")
        checks["database"] = "error (see the service log)"
    age = workers.heartbeat_age("dispatcher") if workers else None
    checks["dispatcher"] = "ok" if age is not None and age < 30 else f"stale ({age})"
    problems = {n: p.config_problems() for n, p in svc.providers.items() if p.config_problems()}
    checks["providers"] = "ok" if not problems else problems
    ready_devices = [d["target"] for d in svc.targets()["devices"] if d["ready"] and d["enabled"]]
    checks["devices"] = f"{len(ready_devices)} ready" if ready_devices else "none ready; configure one in /admin"
    overdue = svc.scheduler.overdue_count()
    checks["scheduler"] = "ok" if not overdue else f"{overdue} overdue"
    if not request.app.state.store.admin_configured():
        checks["admin"] = "setup required: open /admin with the setup code from the container log"
    ok = checks["database"] == "ok" and checks["dispatcher"] == "ok" and checks["scheduler"] == "ok" and not problems \
        and bool(ready_devices)
    return JSONResponse({"status": "ready" if ok else "not_ready", "version": __version__, "checks": checks},
                        status_code=200 if ok else 503)


@router.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
async def metrics(request: Request) -> PlainTextResponse:
    svc = service(request)
    st = svc.stats()
    lines = ["# TYPE notify_deliveries gauge"]
    for state, count in sorted(st["deliveries_by_state"].items()):
        lines.append(f'notify_deliveries{{state="{state}"}} {count}')
    lines.append("# TYPE notify_notifications gauge")
    for status, count in sorted(st["notifications_by_status"].items()):
        lines.append(f'notify_notifications{{status="{status}"}} {count}')
    lines += ["# TYPE notify_oldest_pending_age_seconds gauge",
              f"notify_oldest_pending_age_seconds {st['oldest_pending_age_seconds']}",
              "# TYPE notify_send_attempts_total counter", f"notify_send_attempts_total {st['attempts_total']}",
              "# TYPE notify_webhook_deliveries gauge"]
    for state, count in sorted(st["webhooks_by_state"].items()):
        lines.append(f'notify_webhook_deliveries{{state="{state}"}} {count}')
    nxt = svc.scheduler.next_in()
    if nxt is not None:
        lines += ["# TYPE notify_scheduled_next_seconds gauge", f"notify_scheduled_next_seconds {nxt:.1f}"]
    lines += ["# TYPE notify_scheduled_overdue gauge", f"notify_scheduled_overdue {svc.scheduler.overdue_count()}"]
    lines.append("# TYPE notify_provider_ready gauge")
    for name, p in svc.providers.items():
        lines.append(f'notify_provider_ready{{provider="{name}"}} {int(p.configured)}')
        quota = p.health().get("quota")
        if quota:
            lines.append(f'notify_provider_quota_remaining{{provider="{name}"}} {quota["remaining"]}')
    workers = request.app.state.workers
    for name in ("dispatcher", "receipts", "webhooks", "inbound"):
        age = workers.heartbeat_age(name) if workers else None
        if age is not None:
            lines.append(f'notify_worker_heartbeat_age_seconds{{worker="{name}"}} {age:.1f}')
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
