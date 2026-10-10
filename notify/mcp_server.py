"""MCP interface (streamable HTTP at /mcp). Same core, auth and scopes as REST."""

from __future__ import annotations

import json
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .attachments import MCP_INLINE_MAX
from .core import Client, Conflict, Invalid, NotFound, NotifyService
from .models import NotificationIn, Scope

SCOPE_KEY = "notify.client"

INSTRUCTIONS = """\
hnl-notify is the household notification service. Send notifications to logical
targets (user:NAME, device:NAME, group:NAME, channel:NAME) — call list_targets
first to see what exists and what each device can observably report.

Delivery kinds: devices are either push (phone/desktop) or email. Email devices
are opt-in: a notification reaches them only when send_notification lists
kinds=["email"] (email only) or kinds=["push", "email"] (both); without kinds
it goes to push devices only. list_targets shows each device's kind.

attachments: send files with a notification, preferably by url (only from hosts
an admin allowed; hnl-notify fetches them once) or inline as base64 (at most
256 KiB over MCP). Up to 10 files, 10 MiB in total. Email carries every file;
push (Pushover) shows the first image (image/*, at most 5 MiB) as a picture in
the notification. Anything a device cannot carry is listed in capability_gaps.

Delivery states: queued → sending → dispatched (provider accepted) → delivered →
acknowledged; or failed / expired / cancelled. Only states listed in a delivery's
observable_states can ever occur for it: e.g. a Pushover message below severity
critical ends at 'dispatched' because Pushover never reports device delivery for
it. Do not describe 'dispatched' as 'delivered'. capability_gaps lists requested
features a device could not honour. Use severity critical only for real
emergencies: on a phone it repeats loudly until acknowledged."""


def _client(ctx: Context) -> Client:
    req = ctx.request_context.request
    client = req.scope.get(SCOPE_KEY) if req is not None else None
    if client is None:
        raise ToolError("not authenticated")
    return client


def _need(client: Client, scope: Scope) -> None:
    if not client.can(scope):
        raise ToolError(f"client {client.id!r} lacks scope {scope.value!r}")


def _compact(n: dict[str, Any]) -> dict[str, Any]:
    """Agent-friendly notification summary."""
    return {
        "id": n["id"], "status": n["status"], "severity": n["severity"], "title": n["title"],
        "message": n["message"], "targets": n["targets"], "source": n["source"], "tags": n["tags"],
        "created_at": n["created_at"], "expires_at": n["expires_at"], "suppressed": n.get("suppressed", []),
        "deliveries": [{
            "id": d["id"], "device": d["device"], "state": d["state"], "final": d["final"],
            "observable_states": d["observable_states"], "capability_gaps": d["capability_gaps"],
            "attempts": d["attempts"], "last_error": d["last_error"], "acknowledged_by": d["acknowledged_by"],
            "timestamps": {k: v for k, v in d["timestamps"].items() if v},
            **({"attempt_log": d["attempt_log"]} if "attempt_log" in d else {}),
        } for d in n["deliveries"]],
        **({"attachments": n["attachments"]} if "attachments" in n else {}),
    }


def build_mcp(get_service: Any) -> FastMCP:
    mcp = FastMCP("hnl-notify", instructions=INSTRUCTIONS, stateless_http=True, json_response=True,
                  streamable_http_path="/mcp", host="0.0.0.0")

    def svc() -> NotifyService:
        return get_service()

    async def call(fn: Any, *a: Any, **kw: Any) -> Any:
        try:
            res = fn(*a, **kw)
            if hasattr(res, "__await__"):
                res = await res
            return res
        except NotFound as e:
            raise ToolError(str(e)) from e
        except Invalid as e:
            raise ToolError(f"invalid request: {e}") from e
        except Conflict as e:
            raise ToolError(f"{e}: {json.dumps(e.detail, default=str)}") from e

    @mcp.tool()
    async def send_notification(
        ctx: Context,
        targets: list[str],
        message: str,
        title: str | None = None,
        severity: Literal["min", "low", "normal", "high", "critical"] = "normal",
        url: str | None = None,
        url_title: str | None = None,
        tags: list[str] | None = None,
        ttl_seconds: int | None = None,
        require_ack: bool = False,
        data: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        source: str | None = None,
        kinds: list[Literal["push", "email"]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Send a notification to logical targets (e.g. ["user:alex"]).

        severity: min (silent) | low (quiet) | normal | high (bypasses quiet hours) |
        critical (emergency: repeats until acknowledged — real emergencies only).
        kinds: which delivery kinds to use; omitted = the default devices (push). email devices only
        receive when 'email' is listed.
        attachments: up to 10 files, 10 MiB in total: {"url": ..., "filename"?: ...} (preferred; only from
        hosts an admin allowed) or {"filename": ..., "content_type"?: ..., "data_base64": ...} (inline,
        at most 256 KiB per notification over MCP).
        ttl_seconds: drop/expire if not dispatched in time. Returns the notification with
        per-device deliveries, observable_states and capability_gaps."""
        client = _client(ctx)
        _need(client, Scope.send)
        req = NotificationIn(targets=targets, message=message, title=title, severity=severity, url=url,
                             url_title=url_title, tags=tags or [], ttl_seconds=ttl_seconds, require_ack=require_ack,
                             data=data, idempotency_key=idempotency_key, source=source or f"mcp:{client.id}", kinds=kinds,
                             attachments=attachments or [])
        out, created = await call(svc().submit_async, client, req, inline_limit=MCP_INLINE_MAX)
        res = _compact(out)
        res["created"] = created
        return res

    @mcp.tool()
    async def preview_routing(
        ctx: Context,
        targets: list[str],
        severity: Literal["min", "low", "normal", "high", "critical"] = "normal",
        require_ack: bool = False,
        ttl_seconds: int | None = None,
        kinds: list[Literal["push", "email"]] | None = None,
    ) -> dict[str, Any]:
        """Dry run: which devices a notification would reach, what each can observably report,
        and which requested features would be unsupported. Sends nothing.

        kinds: which delivery kinds to use; omitted = the default devices (push). email devices only
        receive when 'email' is listed."""
        client = _client(ctx)
        _need(client, Scope.send)
        req = NotificationIn(targets=targets, message="(preview)", severity=severity, require_ack=require_ack,
                             ttl_seconds=ttl_seconds, dry_run=True, kinds=kinds)
        out, _ = await call(svc().submit, client, req)
        return out

    @mcp.tool()
    async def get_notification(ctx: Context, notification_id: str, include_attempts: bool = False) -> dict[str, Any]:
        """Current status of one notification, its deliveries and (optionally) every provider attempt
        with the raw provider response."""
        client = _client(ctx)
        _need(client, Scope.read)
        n = await call(svc().get, client, notification_id, detail=True)
        out = _compact(n)
        if not include_attempts:
            for d in out["deliveries"]:
                d.pop("attempt_log", None)
        out["events"] = [{"type": e["type"], "at": e["created_at"], "data": e["data"]} for e in n["events"]]
        return out

    @mcp.tool()
    async def list_notifications(ctx: Context, status: str | None = None, source: str | None = None,
                                 tag: str | None = None, since: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Notification history, newest first. status may be a comma list of: pending, dispatched,
        delivered, acknowledged, partial, failed, expired, cancelled, suppressed. since is ISO-8601 UTC."""
        client = _client(ctx)
        _need(client, Scope.read)
        items = await call(svc().list, client, status=status, source=source, tag=tag, since=since, limit=limit)
        return [_compact(n) for n in items]

    @mcp.tool()
    async def list_attention(ctx: Context) -> dict[str, Any]:
        """Deliveries needing attention: failed or expired (last 7 days), retrying, stuck in the queue,
        critical alerts still awaiting acknowledgement, and providers that are not ready."""
        client = _client(ctx)
        _need(client, Scope.read)
        return await call(svc().attention, client)

    @mcp.tool()
    async def list_targets(ctx: Context) -> dict[str, Any]:
        """Users, devices, groups and channels you can address, with each device's capabilities
        (delivery receipts, acknowledgement, actions, links, cancel, TTL, attachment limits) and
        readiness. Each device has a kind (push or email) and opt_in: opt-in devices (email by default)
        only receive when send_notification asks for their kind via kinds."""
        client = _client(ctx)
        _need(client, Scope.read)
        return svc().targets()

    @mcp.tool()
    async def retry_notification(ctx: Context, notification_id: str) -> dict[str, Any]:
        """Re-queue failed deliveries of a notification (or retry waiting ones immediately)."""
        client = _client(ctx)
        _need(client, Scope.manage)
        out = await call(svc().retry, client, notification_id)
        return {"results": out["results"], "notification": _compact(out["notification"])}

    @mcp.tool()
    async def cancel_notification(ctx: Context, notification_id: str) -> dict[str, Any]:
        """Cancel deliveries not yet dispatched, and stop repeating critical alerts where the provider
        allows it. Messages already shown on a device cannot be recalled."""
        client = _client(ctx)
        _need(client, Scope.manage)
        out = await call(svc().cancel, client, notification_id)
        return {"results": out["results"], "notification": _compact(out["notification"])}

    @mcp.resource("notify://targets", mime_type="application/json")
    def targets_resource(ctx: Context) -> str:
        """Addressable targets and device capabilities."""
        _need(_client(ctx), Scope.read)
        return json.dumps(svc().targets(), default=str)

    @mcp.resource("notify://notifications/{notification_id}", mime_type="application/json")
    def notification_resource(notification_id: str, ctx: Context) -> str:
        """Full status of a notification, including provider attempts and events."""
        client = _client(ctx)
        _need(client, Scope.read)
        try:
            return json.dumps(svc().get(client, notification_id), default=str)
        except NotFound as e:
            raise ToolError(str(e)) from e

    return mcp
