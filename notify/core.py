"""Notification core: intake, state machine, events, queries.

Every state change goes through `_transition`, which enforces the delivery
state machine, appends an event, fans the event out to matching webhook
subscriptions (same transaction) and re-derives the notification status.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

from . import attachments, schedule
from .attachments import PreparedAttachment
from .config import NotifyConfig
from .handles import new_handle
from .db import Database, dumps, in_seconds, iso, loads, new_id, now_iso, parse_iso, utcnow
from .models import (ACKNOWLEDGED, ACTIVE_STATES, CANCELLED, DELIVERED, DISPATCHED, EXPIRED,
                     FAILED, INTERACTED, QUEUED, SENDING, TRANSITIONS, NotificationIn, Scope,
                     Severity, SubscriptionIn, derive_notification_status, event_matches)
from .providers.base import AttachmentData, OutboundMessage, Provider, ReceiptStatus, SendResult
from .routing import RoutePlan, Router, RoutingError

log = logging.getLogger("notify.core")

FINAL_STATES = {FAILED, EXPIRED, CANCELLED, ACKNOWLEDGED, INTERACTED}


class NotFound(Exception):
    pass


class Conflict(Exception):
    def __init__(self, message: str, detail: Any = None):
        super().__init__(message)
        self.detail = detail


class Invalid(Exception):
    pass


class Forbidden(Exception):
    pass


class Gone(Exception):
    """The thing can never happen any more (a reply to a conversation that will never be sent)."""


def external_base(cfg: Any) -> str | None:
    """This service's own base URL (server.external_url, else NOTIFY_EXTERNAL_URL), no trailing slash."""
    return (cfg.server.external_url or os.environ.get("NOTIFY_EXTERNAL_URL") or "").rstrip("/") or None


@dataclass(frozen=True)
class Client:
    id: str
    scopes: frozenset[Scope]
    via_session: bool = False  # the signed-in admin page, not a bearer token

    @property
    def admin(self) -> bool:
        return Scope.admin in self.scopes

    def can(self, scope: Scope) -> bool:
        return self.admin or scope in self.scopes


SYSTEM_CLIENT = Client("hnl-notify", frozenset({Scope.admin}))  # the service's own notifications


class EventBus:
    """Wakes SSE streams when events are committed."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def notify(self) -> None:
        self._event.set()
        self._event = asyncio.Event()

    async def wait(self, timeout: float) -> None:
        ev = self._event
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except TimeoutError:
            pass


@dataclass
class SubmitPlan:
    """A routed, planned notification that has not been written yet."""
    now: datetime
    expires_at: datetime | None
    route: RoutePlan
    nrow: dict[str, Any]
    deliveries: list[dict[str, Any]]
    attachment_meta: list[dict[str, Any]] = field(default_factory=list)
    send_at: datetime | None = None  # set = scheduled: `deliveries` is then only the routing preview


_DECISION_CODE = re.compile(r"([?&]k=)[^&#]+")


def _mask_decision(url: str | None) -> str | None:
    """Single-use decision links (access and chat-history requests) never leave in a listing: the code is the
    whole authorisation of the login-free decision page."""
    if url and "/admin/decide" in url:
        return _DECISION_CODE.sub(r"\1hidden", url)
    return url


def _mask_tree(v: Any) -> Any:
    """Decision codes also sit in the request summaries of the attempt log (the push's url and actions)."""
    if isinstance(v, str):
        return _DECISION_CODE.sub(r"\1hidden", v) if "/admin/decide" in v else v
    if isinstance(v, list):
        return [_mask_tree(x) for x in v]
    if isinstance(v, dict):
        return {k: _mask_tree(x) for k, x in v.items()}
    return v


MAX_SUBSCRIPTIONS_PER_CLIENT = 20
MAX_SUBSCRIPTIONS_PER_NOTIFICATION = 5
WEBHOOK_DISABLE_AFTER_SECONDS = 86400  # failures alone never disable a subscription before a day without success


def backoff_seconds(attempt: int, base: float, cap: float) -> float:
    delay = min(cap, base * (3 ** max(0, attempt - 1)))
    return max(5.0, delay * random.uniform(0.8, 1.2))


class NotifyService:
    def __init__(self, config: NotifyConfig, db: Database, providers: dict[str, Provider]):
        self.config = config
        self.db = db
        self.providers = providers
        self.router = Router(config)
        self.bus = EventBus()
        self.wake = asyncio.Event()
        self.http: Any = None  # shared httpx client, set by the app
        # attachment fetching builds its own locked-down client; these are seams for the app and tests
        self.fetch_transport: Any = None
        self.resolve = attachments.default_resolve
        self._intake: asyncio.Semaphore | None = None  # created on first use, in the serving loop
        from .replies import Replies  # replies imports core for types; import here to avoid a cycle
        self.replies = Replies(self)
        from .chat import Chat  # chat imports core for types, like replies
        self.chat = Chat(self)
        self.scheduler = schedule.Scheduler(self)
        self.inbound_status: dict[str, dict[str, Any]] = {}  # per provider: last_success, last_error, last_stats
        self.open_mailbox: Any = None  # seam: tests return a fake mailbox; None = the provider's own
        self.open_cleanup_mailbox: Any = None  # same seam for mailbox cleanup (a CleanupMailbox)
        self.cleanup_status: dict[str, dict[str, Any]] = {}  # per provider: last_run/success/error/result/by
        self.cleanup_busy_wait = 30.0  # a manual cleanup waits this long for the mailbox lock
        self.mailbox_cache: dict[str, tuple[float, dict[str, Any]]] = {}  # admin panel preview, per provider
        self._mailbox_locks: dict[str, asyncio.Lock] = {}
        self.loop: asyncio.AbstractEventLoop | None = None  # the serving loop, set by the app

    def mailbox_lock(self, name: str) -> asyncio.Lock:
        """One lock per reply mailbox: the poll, a cleanup step and the connection test never overlap."""
        return self._mailbox_locks.setdefault(name, asyncio.Lock())

    async def run_mailbox_cleanup(self, name: str, *, dry_run: bool, by: str,
                                  deadline_seconds: float | None = None) -> dict[str, Any]:
        from .cleanup_service import run_mailbox_cleanup
        return await run_mailbox_cleanup(self, name, dry_run=dry_run, by=by, deadline_seconds=deadline_seconds)

    def reconfigure(self, config: NotifyConfig, providers: dict[str, Provider]) -> None:
        """Swap in a new configuration (admin UI edits). Queued deliveries keep their device names."""
        self.config = config
        self.router = Router(config)
        self.providers = providers
        self._set_wake()

    def _set_wake(self) -> None:
        """asyncio.Event is not thread-safe: from a worker thread (browser enrollment), hand it to the loop."""
        loop = self.loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            loop.call_soon_threadsafe(self.wake.set)
        else:
            self.wake.set()

    # ------------------------------------------------------------------ util
    def _after_commit(self, dispatch: bool = False) -> None:
        self.bus.notify()
        if dispatch:
            self.wake.set()

    def _visible(self, client: Client, client_id: str) -> bool:
        return client.admin or client.id == client_id

    def _notification_row(self, client: Client, nid: str) -> sqlite3.Row:
        row = self.db.one("SELECT * FROM notifications WHERE id=?", (nid,))
        if row is None or not self._visible(client, row["client_id"]):
            raise NotFound(f"notification {nid} not found")
        return row

    def callback_url(self, provider: str) -> str | None:
        s = self.config.server
        if s.public_base_url and s.callback_secret:
            return f"{s.public_base_url.rstrip('/')}/v1/callbacks/{provider}/{s.callback_secret}"
        return None

    def message_for(self, n: sqlite3.Row | dict, delivery_id: str, provider: str, *, with_data: bool = False,
                    atts: list[AttachmentData] | None = None,
                    device: str | None = None) -> OutboundMessage:
        """`atts` overrides the stored attachments (planning before the notification is written);
        with_data loads the bytes and leaves out purged attachments."""
        if atts is None:
            rows = [r for r in self.db.query(
                "SELECT idx, filename, content_type, size, purged_at FROM attachments WHERE notification_id=?"
                " ORDER BY idx", (n["id"],)) if not (with_data and r["purged_at"])]
            atts = [AttachmentData(r["filename"], r["content_type"], r["size"]) for r in rows]
            if with_data:
                prov = self.providers.get(provider)
                if prov is not None and prov.attachment_data == "none":
                    wanted = []  # the adapter carries no attachments: never hold the bytes for it
                elif prov is not None and prov.attachment_data == "first_image":
                    pick = prov.first_image(atts)
                    wanted = [] if pick is None else [pick]
                else:
                    wanted = range(len(atts))
                for i in wanted:  # one BLOB in memory at a time per load
                    atts[i].data = self.db.scalar("SELECT data FROM attachments WHERE notification_id=? AND idx=?",
                                                  (n["id"], rows[i]["idx"]))
        return OutboundMessage(
            notification_id=n["id"], delivery_id=delivery_id, title=n["title"], message=n["message"],
            severity=Severity(n["severity"]), url=n["url"], url_title=n["url_title"],
            actions=loads(n["actions_json"], []), tags=loads(n["tags_json"], []),
            require_ack=bool(n["require_ack"]),
            created_at=parse_iso(self._went_out(n)),  # type: ignore[arg-type]
            expires_at=parse_iso(n["expires_at"]), callback_url=self.callback_url(provider),
            attachments=atts, reply=bool(n["reply_wanted"]), auto_submitted=bool(n["auto_submitted"]),
            thread_delivery_ids=self._thread_delivery_ids(n["in_reply_to"], device) if device else [],
            external_url=external_base(self.config),
        )

    @staticmethod
    def _shown_message(n: sqlite3.Row, viewer: Client | None) -> str:
        """A chat history request names the agent's reason, which only the signed-in admin page shows."""
        if (n["client_id"] == "hnl-notify" and (n["idempotency_key"] or "").startswith("chat-history:")
                and not (viewer is not None and viewer.via_session)):
            return "Group chat history request (details in the signed-in admin page)"
        return n["message"]

    @staticmethod
    def _went_out(n: sqlite3.Row | dict) -> str:
        """The effective time as provider timestamps want it: release, else planned send, else submit."""
        def get(k: str) -> Any:
            try:
                return n[k]
            except (KeyError, IndexError):
                return None
        return get("released_at") or get("send_at") or n["created_at"]

    def _thread_delivery_ids(self, ref: str | None, device: str) -> list[str]:
        """Deliveries to the same device along the in_reply_to chain, oldest first (for email threading)."""
        ids: list[str] = []
        seen: set[str] = set()
        while ref and ref not in seen and len(seen) < 20:
            seen.add(ref)
            prev = self.db.one("SELECT in_reply_to FROM notifications WHERE id=?", (ref,))
            if prev is None:
                break
            d = self.db.one("SELECT id FROM deliveries WHERE notification_id=? AND device=? ORDER BY created_at, id"
                            " LIMIT 1", (ref, device))
            if d is not None:
                ids.append(d["id"])
            ref = prev["in_reply_to"]
        return ids[::-1]

    # ---------------------------------------------------------------- events
    def _emit(self, c: sqlite3.Connection, etype: str, n: sqlite3.Row | dict, delivery_id: str | None,
              data: dict[str, Any]) -> int:
        now = now_iso()
        cur = c.execute(
            "INSERT INTO events(type, notification_id, delivery_id, client_id, source, data_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (etype, n["id"], delivery_id, n["client_id"], n["source"], dumps(data), now))
        event_id = int(cur.lastrowid)  # type: ignore[arg-type]
        tags = loads(n["tags_json"], [])
        admins = [name for name, cl in self.config.clients.items() if Scope.admin in cl.scopes]
        owners = [n["client_id"], *admins]
        for sub in c.execute(  # narrowed in SQL: this runs for every event, inside the write transaction
                "SELECT * FROM subscriptions WHERE active=1 AND (notification_id IS NULL OR notification_id=?)"
                f" AND client_id IN ({','.join('?' * len(owners))})", (n["id"], *owners)).fetchall():
            owner = self.config.clients.get(sub["client_id"])
            owner_admin = owner is not None and Scope.admin in owner.scopes
            if not owner_admin and sub["client_id"] != n["client_id"]:
                continue
            if sub["notification_id"] and sub["notification_id"] != n["id"]:
                continue
            if sub["source"] and sub["source"] != n["source"]:
                continue
            if sub["tag"] and sub["tag"] not in tags:
                continue
            if not any(event_matches(p, etype) for p in loads(sub["events_json"], ["*"])):
                continue
            c.execute("INSERT OR IGNORE INTO webhook_deliveries(subscription_id, event_id, next_attempt_at, created_at)"
                      " VALUES (?,?,?,?)", (sub["id"], event_id, now, now))
        log.info("event", extra={"event_type": etype, "notification_id": n["id"], "delivery_id": delivery_id,
                                 "event_id": event_id, **{f"e_{k}": v for k, v in data.items() if isinstance(v, (str, int, float, bool)) }})
        return event_id

    def _refresh_status(self, c: sqlite3.Connection, nid: str) -> str:
        n = c.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
        if n["status"] == "scheduled":  # no deliveries yet; the release sets the status itself
            return "scheduled"
        states = [r[0] for r in c.execute("SELECT state FROM deliveries WHERE notification_id=?", (nid,))]
        new = derive_notification_status(states)
        if new != n["status"]:
            c.execute("UPDATE notifications SET status=?, updated_at=? WHERE id=?", (new, now_iso(), nid))
            self._emit(c, "notification.status_changed", n, None, {"from": n["status"], "to": new})
        return new

    def _transition(self, c: sqlite3.Connection, d: sqlite3.Row, new_state: str,
                    fields: dict[str, Any] | None = None, data: dict[str, Any] | None = None,
                    event_type: str | None = None) -> None:
        old = d["state"]
        if new_state != old and new_state not in TRANSITIONS[old]:
            raise Conflict(f"delivery {d['id']}: cannot go from {old} to {new_state}")
        now = now_iso()
        fields = dict(fields or {})
        fields["state"] = new_state
        fields["updated_at"] = now
        tracking = fields.get("tracking", d["tracking"])
        if new_state in FINAL_STATES or (new_state == DISPATCHED and tracking == "none"):
            fields.setdefault("finalized_at", now)
            fields.setdefault("next_poll_at", None)
        if new_state != SENDING:
            fields.setdefault("lease_until", None)
        sets = ", ".join(f"{k}=?" for k in fields)
        # guarded on the state the caller read: a row that moved on meanwhile is never overwritten
        if c.execute(f"UPDATE deliveries SET {sets} WHERE id=? AND state=?",
                     (*fields.values(), d["id"], old)).rowcount == 0:
            raise Conflict(f"delivery {d['id']}: state changed meanwhile (expected {old})")
        n = c.execute("SELECT * FROM notifications WHERE id=?", (d["notification_id"],)).fetchone()
        payload = {"device": d["device"], "provider": d["provider"], "from": old, "to": new_state, **(data or {})}
        if event_type or new_state != SENDING:
            self._emit(c, event_type or f"delivery.{new_state}", n, d["id"], payload)
        self._refresh_status(c, d["notification_id"])

    # ---------------------------------------------------------------- intake
    def _replay(self, client: Client, req: NotificationIn) -> tuple[dict[str, Any], bool] | None:
        if req.idempotency_key and not req.dry_run:
            row = self.db.one("SELECT id, send_at, created_at FROM notifications WHERE client_id=? AND idempotency_key=?",
                              (client.id, req.idempotency_key))
            if row:
                # the request's send time as it resolved when the row was made (a delay moves with the clock)
                same = self._send_time(req, parse_iso(row["created_at"])) == parse_iso(row["send_at"])
                if not same:
                    raise Conflict("idempotency key already used for a different send time; use PATCH")
                out = self.get(client, row["id"], detail=False)
                if self.db.one("SELECT 1 FROM conversations WHERE handle_notification_id=?", (row["id"],)):
                    out.update(reply_handle=None, reply_handle_issued=True)  # shown once; a replay cannot repeat it
                return out, False
        return None

    @staticmethod
    def _send_time(req: NotificationIn, now: datetime) -> datetime | None:
        """The UTC send time (None = immediate); a bad one is the sender's mistake (422)."""
        try:
            return schedule.resolve_send_at(req.send_at, req.delay, now, schedule.local_zone())
        except ValueError as e:
            raise Invalid(str(e)) from e

    @staticmethod
    def _preview(route: RoutePlan, deliveries: list[dict[str, Any]]) -> dict[str, Any]:
        return {"devices": [{"device": d["device"], "provider": d["provider"], "via": d["via"],
                             "capability_gaps": d["capability_gaps"]} for d in deliveries],
                "suppressed": route.suppressed}

    def scheduled_preview(self, nrow: dict[str, Any], targets: list[str], severity: Severity,
                          kinds: list[str] | None, attachment_meta: list[dict[str, Any]] | None = None
                          ) -> dict[str, Any]:
        """How a scheduled notification would route right now: devices and suppressed, no delivery ids."""
        try:
            route = self.router.resolve(targets, severity, kinds)
        except RoutingError as e:
            raise Invalid(str(e)) from e
        return self._preview(route, self._plan_deliveries(nrow, route, attachment_meta or []))

    def _check_caps(self, client: Client, req: NotificationIn, meta: list[dict[str, Any]]) -> None:
        """Per-client limits on scheduled notifications (admin and internal submissions are exempt)."""
        if client.admin or req._internal:
            return
        row = self.db.one(
            "SELECT COUNT(*) AS n, COALESCE(SUM((SELECT SUM(size) FROM attachments a WHERE a.notification_id=n.id)),0)"
            " AS bytes FROM notifications n WHERE n.client_id=? AND n.status='scheduled'", (client.id,))
        if row["n"] + 1 > schedule.MAX_SCHEDULED:
            raise Invalid(f"at most {schedule.MAX_SCHEDULED} scheduled notifications per client")
        if row["bytes"] + sum(m["size"] or 0 for m in meta) > schedule.MAX_SCHEDULED_BYTES:
            raise Invalid(f"scheduled attachments are limited to {schedule.MAX_SCHEDULED_BYTES // (1024 * 1024)} MiB"
                          " per client")

    def _plan_deliveries(self, nrow: dict[str, Any], route: RoutePlan,
                         attachment_meta: list[dict[str, Any]]) -> list[dict[str, Any]]:
        atts = [AttachmentData(m["filename"] or f"attachment-{i + 1}", m["content_type"] or "", m["size"])
                for i, m in enumerate(attachment_meta)]
        deliveries = []
        for device_name, via in route.devices.items():
            dev = self.config.devices[device_name]
            provider = self.providers[dev.provider]
            did = new_id("dlv")
            plan = provider.plan(self.message_for(nrow, did, dev.provider, atts=atts), dev.address)
            gaps = list(plan.capability_gaps)
            if nrow.get("reply_wanted"):
                replies = provider.capabilities().get("replies") or {}
                if not replies.get("supported"):
                    gaps.append(f"replies: {replies.get('note') or 'not supported on this path'}")
            problems = provider.config_problems() + provider.device_problems(dev.address)
            if problems:
                gaps.append("provider not ready (will retry until fixed): " + "; ".join(problems))
            rejected = self.rejected_problems(device_name, provider, dev.address)
            if rejected:
                gaps.append("device rejected at the last check (fails unless fixed): " + "; ".join(rejected))
            deliveries.append({"id": did, "device": device_name, "provider": dev.provider, "via": via,
                               "tracking": plan.tracking, "observable_states": plan.observable_states,
                               "capability_gaps": gaps})
        return deliveries

    def plan_submit(self, client: Client, req: NotificationIn,
                    attachment_meta: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], bool] | SubmitPlan:
        """Everything before the transaction: idempotent replay, expiry, routing, per-device plans.
        Returns the final result for a replay or dry run, else a plan for `commit`."""
        replay = self._replay(client, req)
        if replay:
            return replay

        now = utcnow()
        send_at = self._send_time(req, now)
        explicit: datetime | None = None
        if req.expires_at is not None:
            if send_at is not None:  # one zone rule per request: offset-less means local time once scheduled
                try:
                    explicit = schedule.to_utc(req.expires_at, schedule.local_zone())
                except ValueError as e:
                    raise Invalid(str(e)) from e
            else:
                explicit = req.expires_at if req.expires_at.tzinfo else req.expires_at.replace(tzinfo=now.tzinfo)
        expires_at = explicit
        if req.ttl_seconds is not None:
            ttl_exp = (send_at or now) + timedelta(seconds=req.ttl_seconds)  # counts from the send time
            expires_at = ttl_exp if expires_at is None else min(expires_at, ttl_exp)
        if send_at is not None and explicit is not None and explicit <= send_at:
            raise Invalid("expires_at must be after the send time")
        if expires_at is not None and expires_at <= now:
            raise Invalid("notification is already expired (expires_at/ttl_seconds in the past)")
        try:
            route = self.router.resolve(req.targets, req.severity, req.kinds)
        except RoutingError as e:
            raise Invalid(str(e)) from e
        conversation_id = None
        if req.in_reply_to:
            if ":" in req.in_reply_to:  # a handle in the wrong field; never echoed
                raise Invalid("in_reply_to takes a notification id; pass the handle as reply_handle")
            prev = self.db.one("SELECT id, client_id, conversation_id, status FROM notifications WHERE id=?",
                               (req.in_reply_to,))
            if prev is None or not self._visible(client, prev["client_id"]):
                raise Invalid(f"in_reply_to: notification {req.in_reply_to} not found")
            if prev["status"] == "scheduled":
                raise Invalid("cannot follow up a notification that has not been sent yet")
            conversation_id = prev["conversation_id"] or prev["id"]
            try:
                with self.db._lock:
                    self._gate(self.db.conn, conversation_id, req)
            except NotFound as e:
                raise Invalid(f"in_reply_to: notification {req.in_reply_to} not found") from e

        nrow = {
            "id": new_id("ntf"), "client_id": client.id, "source": req.source or client.id, "title": req.title,
            "message": req.message, "severity": req.severity.value, "url": req.url, "url_title": req.url_title,
            "actions_json": dumps([a.model_dump() for a in req.actions]),
            "data_json": dumps(req.data) if req.data is not None else None,
            "tags_json": dumps(req.tags), "targets_json": dumps(req.targets),
            "require_ack": int(req.require_ack), "expires_at": iso(expires_at),
            "created_at": iso(now), "updated_at": iso(now), "idempotency_key": req.idempotency_key,
        }
        nrow.update({"reply_wanted": int(req.reply), "in_reply_to": req.in_reply_to,
                     "conversation_id": conversation_id or nrow["id"], "auto_submitted": int(req._auto_submitted)})
        if send_at is not None:
            nrow.update({
                "send_at": iso(send_at), "max_late_seconds": req.max_late_seconds or schedule.MAX_LATE_DEFAULT,
                "ttl_seconds": req.ttl_seconds, "explicit_expires_at": iso(explicit)})
            nrow["deliver_by"] = iso(send_at + timedelta(seconds=nrow["max_late_seconds"]))
        meta = attachment_meta or []
        if send_at is not None:
            self._check_caps(client, req, meta)
        deliveries = self._plan_deliveries(nrow, route, meta)
        if req.dry_run:
            out = {"dry_run": True, "severity": req.severity.value, "expires_at": iso(expires_at),
                   "routing": route.as_dict(),
                   "deliveries": [{k: v for k, v in d.items() if k != "id"} for d in deliveries]}
            if send_at is not None:
                out["send_at"] = iso(send_at)
            return out, True
        return SubmitPlan(now=now, expires_at=expires_at, route=route, nrow=nrow, deliveries=deliveries,
                          attachment_meta=meta, send_at=send_at)

    def commit(self, client: Client, req: NotificationIn, plan: SubmitPlan,
               prepared: list[PreparedAttachment | None]) -> tuple[dict[str, Any], bool]:
        """The transactional half of a submit. `prepared` holds one entry per request attachment
        (None = not fetched); bytes are stored when the plan has deliveries or is scheduled."""
        nrow, route = plan.nrow, plan.route
        scheduled = plan.send_at is not None
        deliveries = [] if scheduled else plan.deliveries  # a scheduled plan only carries the preview
        nid, source = nrow["id"], nrow["source"]
        status = "scheduled" if scheduled else derive_notification_status([QUEUED] * len(deliveries))
        routing = {"suppressed": route.suppressed, "via": {d["device"]: d["via"] for d in deliveries}}
        if scheduled:
            routing["preview"] = self._preview(route, plan.deliveries)
        if req.kinds is not None:
            routing["kinds"] = req.kinds
        rows = []
        for i, (it, m) in enumerate(zip(req.attachments, plan.attachment_meta)):
            p = prepared[i] if i < len(prepared) else None
            if p is not None:
                rows.append((nid, i, p.filename, p.content_type, p.size, p.sha256, p.source, p.source_host,
                             p.data if (plan.deliveries or scheduled) else None))
            else:  # URL attachment that was never fetched (nothing to send, not scheduled)
                rows.append((nid, i, m["filename"] or f"attachment-{i + 1}", m["content_type"] or
                             "application/octet-stream", m["size"] or 0, "", m["source"],
                             urlsplit(it.url or "").hostname, None))
        webhook_secret = None
        reply_handle = None
        try:
            with self.db.tx() as c:
                if req.in_reply_to:  # again under the write lock: a handle may have appeared since plan_submit
                    try:
                        self._gate(c, nrow["conversation_id"], req)
                    except NotFound as e:
                        raise Invalid(f"in_reply_to: notification {req.in_reply_to} not found") from e
                c.execute(
                    "INSERT INTO notifications(id, client_id, idempotency_key, source, title, message, severity, url,"
                    " url_title, actions_json, data_json, tags_json, targets_json, require_ack, expires_at, status,"
                    " routing_json, created_at, updated_at, reply_wanted, in_reply_to, conversation_id, auto_submitted,"
                    " send_at, max_late_seconds, deliver_by, ttl_seconds, explicit_expires_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (nid, client.id, req.idempotency_key, source, req.title, req.message, req.severity.value,
                     req.url, req.url_title, nrow["actions_json"], nrow["data_json"], nrow["tags_json"],
                     nrow["targets_json"], nrow["require_ack"], nrow["expires_at"], status, dumps(routing),
                     nrow["created_at"], nrow["updated_at"], nrow["reply_wanted"], nrow["in_reply_to"],
                     nrow["conversation_id"], nrow["auto_submitted"], nrow.get("send_at"),
                     nrow.get("max_late_seconds"), nrow.get("deliver_by"), nrow.get("ttl_seconds"),
                     nrow.get("explicit_expires_at")))
                if req.reply or req.in_reply_to:
                    conv = nrow["conversation_id"]
                    c.execute("INSERT OR IGNORE INTO conversations(id, legacy, created_at) VALUES (?,0,?)",
                              (conv, nrow["created_at"]))
                    if req.reply:
                        handle, digest = new_handle(conv)
                        if c.execute("UPDATE conversations SET handle_sha256=?, handle_notification_id=?"
                                     " WHERE id=? AND handle_sha256 IS NULL AND legacy=0",
                                     (digest, nid, conv)).rowcount:
                            reply_handle = handle
                c.executemany(
                    "INSERT INTO attachments(notification_id, idx, filename, content_type, size, sha256, source,"
                    " source_host, data) VALUES (?,?,?,?,?,?,?,?,?)", rows)
                if req.webhook is not None:
                    webhook_secret = req.webhook.secret or secrets.token_urlsafe(32)
                    c.execute(
                        "INSERT INTO subscriptions(id, client_id, url, secret, events_json, notification_id,"
                        " description, created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (new_id("sub"), client.id, req.webhook.url, webhook_secret, dumps(req.webhook.events), nid,
                         "per-notification webhook", nrow["created_at"]))
                n = c.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
                self._emit(c, "notification.scheduled" if scheduled else "notification.created", n, None,
                           {"severity": req.severity.value, "targets": req.targets, "deliveries": len(deliveries),
                            "suppressed": route.suppressed, **({"send_at": nrow["send_at"]} if scheduled else {})})
                for d in deliveries:
                    c.execute(
                        "INSERT INTO deliveries(id, notification_id, device, provider, state, max_attempts,"
                        " next_attempt_at, tracking, observable_states_json, capability_gaps_json, created_at,"
                        " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (d["id"], nid, d["device"], d["provider"], QUEUED, self.config.server.max_attempts,
                         nrow["created_at"], d["tracking"], dumps(d["observable_states"]),
                         dumps(d["capability_gaps"]), nrow["created_at"], nrow["created_at"]))
                    self._emit(c, "delivery.queued", n, d["id"],
                               {"device": d["device"], "provider": d["provider"], "to": QUEUED,
                                "capability_gaps": d["capability_gaps"]})
        except sqlite3.IntegrityError:
            # concurrent submit with the same idempotency key won the race
            replay = self._replay(client, req)
            if replay:
                return replay
            raise
        self._after_commit(dispatch=True)
        out = self.get(client, nid, detail=False)
        if webhook_secret:
            out["webhook_secret"] = webhook_secret
        if reply_handle:
            out["reply_handle"] = reply_handle
        return out, True

    def submit(self, client: Client, req: NotificationIn) -> tuple[dict[str, Any], bool]:
        """Synchronous submit for internal callers; URL attachments need `submit_async`."""
        replay = self._replay(client, req)
        if replay:
            return replay
        if any(a.url is not None for a in req.attachments):
            raise Invalid("URL attachments need the asynchronous submit path")
        try:
            decoded = attachments.decode_inline(req.attachments)
        except attachments.AttachmentError as e:
            raise Invalid(str(e)) from e
        res = self.plan_submit(client, req, attachments.metadata(req.attachments))
        return res if isinstance(res, tuple) else self.commit(client, req, res, decoded)

    async def submit_async(self, client: Client, req: NotificationIn, *,
                           inline_limit: int | None = None) -> tuple[dict[str, Any], bool]:
        """The intake path for REST, MCP and ingest adapters: fetches URL attachments only when
        the notification will actually be delivered somewhere."""
        replay = self._replay(client, req)  # answered before any attachment check, so a replay never fails on them
        if replay:
            return replay
        try:
            meta = attachments.metadata(req.attachments)
            if inline_limit and sum(m["size"] for m in meta if m["source"] == "inline") > inline_limit:
                raise Invalid(f"inline attachments over MCP are limited to {inline_limit // 1024} KiB;"
                              " pass a url instead")
            for i, it in enumerate(req.attachments):
                if it.url is not None:
                    attachments.check_url_allowed(it.url, self.config.server.attachment_url_hosts, i)
            prepared: list[PreparedAttachment | None] = []
            if req.dry_run:
                attachments.decode_inline(req.attachments)
            res = self.plan_submit(client, req, meta)
            if isinstance(res, tuple):
                return res
            if req.attachments and (res.deliveries or res.send_at):
                if self._intake is None:
                    self._intake = asyncio.Semaphore(1)
                async with self._intake:
                    fetched = await attachments.prepare(
                        req.attachments, self.config.server.attachment_url_hosts, self.fetch_transport,
                        self.resolve)
                prepared = list(fetched)
                if res.expires_at is not None and res.expires_at <= utcnow():
                    raise Invalid("notification expired while attachments were fetched")
                res.attachment_meta = [{"filename": p.filename, "content_type": p.content_type, "size": p.size,
                                        "source": p.source} for p in fetched]
                try:
                    res.deliveries = self._plan_deliveries(res.nrow, res.route, res.attachment_meta)
                    if res.send_at:  # URL sizes were unknown before the fetch
                        self._check_caps(client, req, res.attachment_meta)
                except KeyError as e:  # a device or provider was removed by a reconfigure meanwhile
                    raise Invalid(f"the target configuration changed while attachments were fetched ({e});"
                                  " submit again") from None
            elif req.attachments:
                prepared = attachments.decode_inline(req.attachments)
        except attachments.AttachmentError as e:
            raise Invalid(str(e)) from e
        for it in req.attachments:
            it.data_base64 = None  # decoded (or fetched) already; don't hold ~1.4x the bytes through the commit
        return self.commit(client, req, res, prepared)

    # ------------------------------------------------------------ dispatcher
    def recover_leases(self, all_sending: bool = False) -> int:
        now = now_iso()
        sql = "SELECT * FROM deliveries WHERE state=?" + ("" if all_sending else " AND lease_until < ?")
        rows = self.db.query(sql, (SENDING,) if all_sending else (SENDING, now))
        for d in rows:
            try:
                with self.db.tx() as c:
                    if d["cancel_requested"]:  # cancelled while in flight: never queue it again
                        self._transition(c, d, CANCELLED, {}, {"reason": "cancelled while a send attempt was"
                                                                         " in progress (send interrupted)"})
                        continue
                    if d["attempts"] >= d["max_attempts"]:  # a crash loop must not resend without limit
                        self._transition(c, d, FAILED, {"last_error": "send interrupted on every attempt"},
                                         {"reason": f"send interrupted {d['attempts']} times (lease expired or restart);"
                                                    " not retried again", "attempts": d["attempts"]})
                        continue
                    self._transition(c, d, QUEUED, {"next_attempt_at": now},
                                     {"reason": "send interrupted (lease expired or restart); may duplicate if the provider had accepted"},
                                     event_type="delivery.queued")
            except Conflict:
                continue  # the send finished meanwhile
        if rows:
            log.warning("recovered interrupted deliveries", extra={"count": len(rows)})
            self._after_commit(dispatch=True)
        return len(rows)

    def expire_due(self) -> int:
        now = now_iso()
        rows = self.db.query(
            "SELECT d.*, n.expires_at AS n_expires_at FROM deliveries d JOIN notifications n ON n.id=d.notification_id"
            " WHERE d.state=? AND ((n.expires_at IS NOT NULL AND n.expires_at <= ?)"
            " OR (n.deliver_by IS NOT NULL AND n.deliver_by <= ?))", (QUEUED, now, now))
        for d in rows:
            ttl = d["n_expires_at"] is not None and d["n_expires_at"] <= now
            reason = "ttl elapsed before dispatch" if ttl else "lateness limit passed before dispatch"
            try:
                with self.db.tx() as c:
                    self._transition(c, d, EXPIRED, {"last_error": d["last_error"]},
                                     {"reason": reason, "last_error": d["last_error"]})
                    if not ttl:
                        c.execute("UPDATE notifications SET release_problem=? WHERE id=?",
                                  ("not sent within its lateness limit", d["notification_id"]))
            except Conflict:
                continue  # claimed meanwhile
        if rows:
            self._after_commit()
        return len(rows)

    def claim_due(self, provider: str, limit: int) -> list[sqlite3.Row]:
        if limit <= 0:
            return []
        now = now_iso()
        claimed = []
        p = self.providers.get(provider)
        lease = self.lease_seconds(provider)
        cancelled = False
        with self.db.tx() as c:
            rows = c.execute(
                "SELECT * FROM deliveries WHERE state=? AND provider=? AND next_attempt_at <= ?"
                " ORDER BY next_attempt_at LIMIT ?", (QUEUED, provider, now, limit)).fetchall()
            for d in rows:
                if d["cancel_requested"]:  # cancelled while an earlier attempt was in flight: never send again
                    self._transition(c, d, CANCELLED, {}, {"reason": "cancelled while a send attempt was in progress"})
                    cancelled = True
                    continue
                dev = self.config.devices.get(d["device"])
                recipient = p.recipient(dev.address) if p is not None and dev is not None else None
                c.execute("UPDATE deliveries SET state=?, attempts=attempts+1, lease_no=lease_no+1, lease_until=?,"
                          " recipient_address=?, updated_at=? WHERE id=?", (SENDING, in_seconds(lease), recipient, now, d["id"]))
                claimed.append(c.execute("SELECT * FROM deliveries WHERE id=?", (d["id"],)).fetchone())
        if cancelled:
            self._after_commit()
        return claimed

    def fail_orphaned(self) -> int:
        """Queued deliveries of a provider that was removed are never claimed: fail them, visibly."""
        names = list(self.providers)
        rows = self.db.query("SELECT * FROM deliveries WHERE state=? AND provider NOT IN (%s)"
                             % ",".join("?" * len(names)), (QUEUED, *names)) if names else \
            self.db.query("SELECT * FROM deliveries WHERE state=?", (QUEUED,))
        n = 0
        for d in rows:
            try:
                with self.db.tx() as c:
                    self._transition(c, d, FAILED, {"last_error": f"provider {d['provider']!r} is no longer configured"},
                                     {"reason": f"provider {d['provider']!r} is no longer configured"})
                n += 1
            except Conflict:
                continue
        if n:
            self._after_commit()
        return n

    def abandon_receipt(self, d: sqlite3.Row, reason: str) -> None:
        """Stop tracking a receipt for good: the delivery ends as expired instead of waiting forever."""
        try:
            with self.db.tx() as c:
                cur = c.execute("SELECT * FROM deliveries WHERE id=?", (d["id"],)).fetchone()
                if cur is None or cur["state"] not in (DISPATCHED, DELIVERED):
                    return
                self._transition(c, cur, EXPIRED, {"last_error": f"receipt tracking abandoned: {reason}"},
                                 {"reason": f"receipt tracking abandoned: {reason}",
                                  "delivered_at": cur["delivered_at"]})
        except Conflict:
            return
        self._after_commit()

    def lease_seconds(self, provider: str) -> float:
        """How long a claim holds a delivery; the worker bounds each send to well inside it."""
        lease: float = self.config.server.lease_seconds
        p = self.providers.get(provider)
        if p and p.send_deadline_seconds:
            lease = max(lease, p.send_deadline_seconds + 30)
        return lease

    def send_timeout(self, provider: str) -> float:
        return max(5.0, self.lease_seconds(provider) - 10)

    def next_due_in(self) -> float | None:
        nxt = self.db.scalar("SELECT MIN(next_attempt_at) FROM deliveries WHERE state=?", (QUEUED,))
        if not nxt:
            return None
        return max(0.0, (parse_iso(nxt) - utcnow()).total_seconds())  # type: ignore[operator]

    def complete_send(self, d: sqlite3.Row, result: SendResult, started_at: str) -> None:
        try:
            self._complete_send(d, result, started_at)
        finally:
            self._after_commit()

    def _complete_send(self, d: sqlite3.Row, result: SendResult, started_at: str) -> None:
        s = self.config.server
        now = utcnow()
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO attempts(delivery_id, attempt_no, kind, started_at, finished_at, outcome, http_status,"
                " request_json, response_body, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (d["id"], d["attempts"], "send", started_at, iso(now), result.outcome, result.http_status,
                 dumps(result.request_summary), result.response_body, result.error))
            cur = c.execute("SELECT * FROM deliveries WHERE id=?", (d["id"],)).fetchone()
            if cur["state"] != SENDING or cur["lease_no"] != d["lease_no"]:
                return  # lease was recovered meanwhile (and maybe re-claimed, even after a retry); the newer run owns it
            n = c.execute("SELECT * FROM notifications WHERE id=?", (d["notification_id"],)).fetchone()
            if result.outcome == "success":
                fields: dict[str, Any] = {"dispatched_at": iso(now), "provider_message_id": result.provider_message_id,
                                          "receipt": result.receipt, "last_error": None}
                if cur["tracking"] == "receipt" and result.receipt:
                    provider = self.providers.get(cur["provider"])
                    fields["next_poll_at"] = in_seconds(provider.receipt_poll_seconds if provider else 30)
                elif cur["tracking"] == "receipt":
                    fields["tracking"] = "none"  # provider gave no receipt after all
                self._transition(c, cur, DISPATCHED, fields,
                                 {"provider_message_id": result.provider_message_id, "attempt": cur["attempts"]})
                if cur["tracking"] == "report" and (cur["delivered_at"] or cur["acknowledged_at"]):
                    self._apply_early_report(c, cur["id"])
                return
            expires = parse_iso(n["expires_at"])
            deliver_by = parse_iso(n["deliver_by"])
            if result.outcome == "retryable" and cur["cancel_requested"]:
                self._transition(c, cur, CANCELLED, {"last_error": result.error},
                                 {"reason": "cancelled while a send attempt was in progress; not retried",
                                  "error": result.error})
                return
            if result.outcome == "retryable" and cur["attempts"] < cur["max_attempts"]:
                delay = backoff_seconds(cur["attempts"], s.retry_base_seconds, s.retry_max_seconds)
                if result.retry_after:
                    delay = max(delay, result.retry_after)
                next_at = now + timedelta(seconds=delay)
                if expires is not None and next_at >= expires:
                    self._transition(c, cur, EXPIRED, {"last_error": result.error},
                                     {"reason": "ttl would elapse before the next retry", "error": result.error})
                    return
                if deliver_by is not None and next_at >= deliver_by:
                    self._transition(c, cur, EXPIRED, {"last_error": result.error},
                                     {"reason": "lateness limit would pass before the next retry",
                                      "error": result.error})
                    c.execute("UPDATE notifications SET release_problem=? WHERE id=?",
                              ("not sent within its lateness limit", n["id"]))
                    return
                self._transition(c, cur, QUEUED, {"next_attempt_at": iso(next_at), "last_error": result.error},
                                 {"attempt": cur["attempts"], "error": result.error, "http_status": result.http_status,
                                  "next_attempt_at": iso(next_at)}, event_type="delivery.attempt_failed")
                return
            reason = "permanent provider error" if result.outcome == "permanent" else "retries exhausted"
            self._transition(c, cur, FAILED, {"last_error": result.error},
                             {"reason": reason, "error": result.error, "http_status": result.http_status,
                              "attempts": cur["attempts"]})

    def cancel_requested_after_dispatch(self, delivery_id: str) -> bool:
        return bool(self.db.scalar("SELECT cancel_requested FROM deliveries WHERE id=?", (delivery_id,)))

    # -------------------------------------------------------------- receipts
    def due_polls(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM deliveries WHERE tracking='receipt' AND receipt IS NOT NULL AND next_poll_at IS NOT NULL"
            " AND next_poll_at <= ? AND state IN (?,?) ORDER BY next_poll_at LIMIT ?",
            (now_iso(), DISPATCHED, DELIVERED, limit))

    def apply_receipt(self, delivery_id: str, st: ReceiptStatus, via: str) -> None:
        with self.db.tx() as c:
            d = c.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            if d is None:
                return
            c.execute("UPDATE deliveries SET poll_errors=0 WHERE id=?", (delivery_id,))
            provider = self.providers.get(d["provider"])
            poll_every = provider.receipt_poll_seconds if provider else 30
            evidence = {"via": via}
            if st.delivered_at and d["state"] == DISPATCHED:
                self._transition(c, d, DELIVERED, {"delivered_at": iso(st.delivered_at)},
                                 {**evidence, "delivered_at": iso(st.delivered_at)})
                d = c.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            elif st.delivered_at and not d["delivered_at"]:
                c.execute("UPDATE deliveries SET delivered_at=? WHERE id=?", (iso(st.delivered_at), delivery_id))
            if st.acknowledged_at and d["state"] in (DISPATCHED, DELIVERED):
                self._transition(c, d, ACKNOWLEDGED,
                                 {"acknowledged_at": iso(st.acknowledged_at), "acknowledged_by": st.acknowledged_by},
                                 {**evidence, "acknowledged_at": iso(st.acknowledged_at),
                                  "acknowledged_by": st.acknowledged_by})
            elif st.interaction and d["state"] in (DISPATCHED, DELIVERED, ACKNOWLEDGED):
                self._transition(c, d, INTERACTED, {"interacted_at": now_iso(), "interaction_json": dumps(st.interaction)},
                                 {**evidence, "interaction": st.interaction})
            elif st.expired and d["state"] in (DISPATCHED, DELIVERED):
                self._transition(c, d, EXPIRED, {"last_error": "expired without acknowledgement"},
                                 {**evidence, "reason": "provider alert expired without acknowledgement",
                                  "delivered_at": d["delivered_at"]})
            elif d["state"] in (DISPATCHED, DELIVERED) and d["tracking"] == "receipt":
                c.execute("UPDATE deliveries SET next_poll_at=? WHERE id=?", (in_seconds(poll_every), delivery_id))
        self._after_commit()

    def _apply_early_report(self, c: sqlite3.Connection, delivery_id: str) -> None:
        """A browser reported before the dispatch committed: replay what it stored on the row."""
        d = c.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        evidence = {"via": "report"}
        if d["delivered_at"]:
            self._transition(c, d, DELIVERED, {}, {**evidence, "delivered_at": d["delivered_at"]})
            d = c.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        if d["acknowledged_at"]:
            self._transition(c, d, ACKNOWLEDGED, {},
                             {**evidence, "acknowledged_at": d["acknowledged_at"],
                              "acknowledged_by": d["acknowledged_by"]})

    def apply_report(self, delivery_id: str, kind: Literal["shown", "clicked"], device: str) -> None:
        """A browser reported that it showed (or the user clicked) a `report`-tracked delivery."""
        now = utcnow()
        st = (ReceiptStatus(delivered_at=now) if kind == "shown"
              else ReceiptStatus(delivered_at=now, acknowledged_at=now, acknowledged_by=device))
        with self.db.tx() as c:  # read and write together: the dispatch commit cannot land in between
            d = c.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            if d is None or d["tracking"] != "report" or d["finalized_at"]:
                return  # by design: a click after the horizon finalized an ordinary delivery is ignored
            if d["state"] == SENDING:  # the dispatch commit replays this (see _apply_early_report)
                c.execute("UPDATE deliveries SET delivered_at=COALESCE(delivered_at, ?),"
                          " acknowledged_at=COALESCE(acknowledged_at, ?),"
                          " acknowledged_by=COALESCE(acknowledged_by, ?) WHERE id=?",
                          (iso(now), iso(st.acknowledged_at) if st.acknowledged_at else None,
                           st.acknowledged_by, delivery_id))
                return
            state = d["state"]
        if state in (DISPATCHED, DELIVERED):
            self.apply_receipt(delivery_id, st, via="report")

    def expire_reports(self) -> int:
        """Close `report` deliveries whose lifetime has passed without the report that would finish them."""
        now = utcnow()
        rows = self.db.query(
            "SELECT d.*, n.expires_at AS n_expires_at, n.severity AS n_severity, n.require_ack AS n_require_ack"
            " FROM deliveries d JOIN notifications n ON n.id=d.notification_id"
            " WHERE d.tracking='report' AND d.state IN (?,?) AND d.finalized_at IS NULL"
            " AND d.dispatched_at IS NOT NULL", (DISPATCHED, DELIVERED))
        closed = 0
        for d in rows:
            provider = self.providers.get(d["provider"])
            ttl = getattr(provider, "default_ttl_seconds", 86400)
            horizon = parse_iso(d["n_expires_at"]) or parse_iso(d["dispatched_at"]) + timedelta(seconds=ttl)
            if horizon > now:
                continue
            needs_ack = d["n_severity"] in ("high", "critical") or bool(d["n_require_ack"])
            with self.db.tx() as c:
                cur = c.execute("SELECT * FROM deliveries WHERE id=?", (d["id"],)).fetchone()
                if cur["state"] not in (DISPATCHED, DELIVERED):
                    continue  # a report landed meanwhile
                if cur["state"] == DISPATCHED:
                    reason = "not shown within its lifetime"
                elif needs_ack:
                    reason = "not acknowledged within its lifetime"
                else:
                    c.execute("UPDATE deliveries SET finalized_at=?, updated_at=? WHERE id=?",
                              (iso(now), iso(now), d["id"]))
                    self._refresh_status(c, d["notification_id"])
                    closed += 1
                    continue
                self._transition(c, cur, EXPIRED, {"last_error": reason},
                                 {"reason": reason, "delivered_at": cur["delivered_at"]})
                closed += 1
        if closed:
            self._after_commit()
        return closed

    def poll_failed(self, d: sqlite3.Row, error: str, http_status: int | None, body: str | None) -> None:
        errors = d["poll_errors"] + 1
        n = self.db.one("SELECT expires_at, created_at FROM notifications WHERE id=?", (d["notification_id"],))
        horizon = parse_iso(d["dispatched_at"]) or utcnow()
        horizon += timedelta(seconds=10800 + 3600)  # provider max expire window + margin
        give_up = utcnow() > horizon or (http_status is not None and 400 <= http_status < 500 and errors >= 5)
        with self.db.tx() as c:
            c.execute("INSERT INTO attempts(delivery_id, attempt_no, kind, started_at, finished_at, outcome,"
                      " http_status, response_body, error) VALUES (?,?,?,?,?,?,?,?,?)",
                      (d["id"], errors, "receipt_poll", now_iso(), now_iso(), "retryable", http_status, body, error))
            c.execute("UPDATE deliveries SET poll_errors=?, last_error=?, next_poll_at=? WHERE id=?",
                      (errors, f"receipt poll: {error}",
                       None if give_up else in_seconds(min(900, 30 * errors)), d["id"]))
        if give_up:
            log.warning("receipt tracking abandoned", extra={"delivery_id": d["id"], "error": error,
                                                             "expires_at": n["expires_at"] if n else None})
            self.abandon_receipt(d, f"receipt poll: {error}")

    def delivery_by_receipt(self, provider: str, receipt: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM deliveries WHERE provider=? AND receipt=?", (provider, receipt))

    # ---------------------------------------------------------------- manage
    async def cancel(self, client: Client, nid: str) -> dict[str, Any]:
        done = self.scheduler.cancel(client, nid)  # a scheduled one: `send` or `manage` suffices for one's own
        if done is not None:
            return done
        if not client.can(Scope.manage):
            raise Forbidden("cancelling a sent notification needs the manage scope")
        n = self._notification_row(client, nid)
        results = []
        for did in [r["id"] for r in self.db.query("SELECT id FROM deliveries WHERE notification_id=? ORDER BY id",
                                                   (nid,))]:
            with self.db.tx() as c:  # read and act on the current state, never on a row read before an await
                d = c.execute("SELECT * FROM deliveries WHERE id=?", (did,)).fetchone()
                if d["state"] == QUEUED:
                    self._transition(c, d, CANCELLED, {}, {"reason": f"cancelled by {client.id} before dispatch"})
                    results.append({"delivery": did, "result": "cancelled"})
                    continue
                if d["state"] == SENDING:
                    c.execute("UPDATE deliveries SET cancel_requested=1 WHERE id=?", (did,))
                    results.append({"delivery": did, "result": "cancel_requested",
                                    "note": "send in progress; cancelled after dispatch if the provider supports it,"
                                            " and never retried"})
                    continue
            if d["state"] in (DISPATCHED, DELIVERED):
                results.append(await self._recall(d, n, f"provider alert cancelled by {client.id}"))
            elif d["state"] in (FAILED, EXPIRED) and self._maybe_sent(d):
                results.append(await self._recall_unseen(d, n))
            else:
                results.append({"delivery": did, "result": "not_cancellable", "reason": f"already {d['state']}"})
        self._after_commit()
        if not any(r["result"] in ("cancelled", "cancel_requested", "recalled") for r in results):
            raise Conflict("nothing to cancel", results)
        return {"notification": self.get(client, nid, detail=False), "results": results}

    async def _recall(self, d: sqlite3.Row, n: sqlite3.Row, reason: str) -> dict[str, Any]:
        """Ask the provider to stop a dispatched message (e.g. a repeating emergency alert)."""
        provider = self.providers.get(d["provider"])
        sev = Severity(n["severity"])
        if not (provider and d["receipt"] and provider.can_cancel_after_dispatch(sev)):
            return {"delivery": d["id"], "result": "not_cancellable",
                    "reason": f"{d['provider']} cannot recall a message already dispatched at severity {sev.value}"}
        res = await provider.cancel(d["receipt"], tag=n["id"])  # by tag: also stops a duplicate of the alert
        with self.db.tx() as c:
            c.execute("INSERT INTO attempts(delivery_id, attempt_no, kind, started_at, finished_at, outcome,"
                      " http_status, response_body, error) VALUES (?,?,?,?,?,?,?,?,?)",
                      (d["id"], d["attempts"], "cancel", now_iso(), now_iso(), res.outcome,
                       res.http_status, res.response_body, res.error))
            if res.outcome == "success":
                cur = c.execute("SELECT * FROM deliveries WHERE id=?", (d["id"],)).fetchone()
                if cur["state"] in (DISPATCHED, DELIVERED):
                    self._transition(c, cur, CANCELLED, {}, {"reason": reason})
        return {"delivery": d["id"], "result": "cancelled" if res.outcome == "success" else "error",
                "error": res.error}

    def _maybe_sent(self, d: sqlite3.Row) -> bool:
        """A failed delivery may still have reached the phone: an attempt whose answer was lost."""
        return bool(self.db.scalar("SELECT 1 FROM attempts WHERE delivery_id=? AND kind='send'"
                                   " AND error LIKE '%outcome unknown%' LIMIT 1", (d["id"],)))

    async def _recall_unseen(self, d: sqlite3.Row, n: sqlite3.Row) -> dict[str, Any]:
        """Stop an emergency alert we never got a receipt for (by its tag, the notification id). The delivery
        keeps its state: whether it rang is unknown."""
        provider = self.providers.get(d["provider"])
        if not (provider and provider.can_cancel_after_dispatch(Severity(n["severity"]))):
            return {"delivery": d["id"], "result": "not_cancellable", "reason": f"already {d['state']}"}
        res = await provider.cancel("", tag=n["id"])
        with self.db.tx() as c:
            c.execute("INSERT INTO attempts(delivery_id, attempt_no, kind, started_at, finished_at, outcome,"
                      " http_status, response_body, error) VALUES (?,?,?,?,?,?,?,?,?)",
                      (d["id"], d["attempts"], "cancel", now_iso(), now_iso(), res.outcome,
                       res.http_status, res.response_body, res.error))
        return {"delivery": d["id"], "result": "recalled" if res.outcome == "success" else "error",
                "note": "an attempt's outcome was unknown: any alert it raised was cancelled", "error": res.error}

    async def cancel_after_dispatch(self, delivery_id: str) -> None:
        """The worker's follow-up when a cancel arrived while the send was in flight (no scope check: the
        person who asked was already authorised)."""
        d = self.db.one("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
        if d is None or d["state"] not in (DISPATCHED, DELIVERED):
            return
        n = self.db.one("SELECT * FROM notifications WHERE id=?", (d["notification_id"],))
        try:
            res = await self._recall(d, n, "cancel requested while the send was in progress")
        finally:
            self._after_commit()
        if res["result"] != "cancelled":
            log.warning("cancel after dispatch did not succeed", extra={"delivery_id": delivery_id, **res})

    def retry(self, client: Client, nid: str) -> dict[str, Any]:
        n = self._notification_row(client, nid)
        expires = parse_iso(n["expires_at"])
        if expires is not None and expires <= utcnow():
            raise Conflict("notification has expired; submit a new one instead")
        if n["release_problem"] is not None and not self.db.scalar(
                "SELECT 1 FROM deliveries WHERE notification_id=?", (nid,)):
            self.scheduler.release(nid, force=True)  # a release that never produced deliveries: try it again now
            return {"notification": self.get(client, nid, detail=False), "results": [{"result": "released"}]}
        results = []
        now = now_iso()
        purged = self.db.scalar("SELECT COUNT(*) FROM attachments WHERE notification_id=? AND purged_at IS NOT NULL",
                                (nid,))
        gap = (f"attachments purged after {self.config.server.attachment_retention_days} days: {purged} not sent"
               if purged else None)
        for d in self.db.query("SELECT * FROM deliveries WHERE notification_id=?", (nid,)):
            if d["state"] == FAILED:
                with self.db.tx() as c:  # a person chose to send it now: the unattended-lateness limit no longer applies
                    c.execute("UPDATE notifications SET deliver_by=NULL WHERE id=?", (nid,))
                fields: dict[str, Any] = {"attempts": 0, "next_attempt_at": now, "finalized_at": None,
                                          "last_error": d["last_error"], "dismissed_at": None, "cancel_requested": 0}
                gaps = loads(d["capability_gaps_json"], [])
                if gap and gap not in gaps:
                    fields["capability_gaps_json"] = dumps([*gaps, gap])
                try:
                    with self.db.tx() as c:
                        self._transition(c, d, QUEUED, fields,
                                         {"reason": f"manual retry by {client.id}", "previous_error": d["last_error"]})
                except Conflict:
                    results.append({"delivery": d["id"], "result": "not_retryable", "reason": "state changed meanwhile"})
                    continue
                results.append({"delivery": d["id"], "result": "requeued"})
            elif d["state"] == QUEUED:
                with self.db.tx() as c:
                    c.execute("UPDATE deliveries SET next_attempt_at=? WHERE id=?", (now, d["id"]))
                results.append({"delivery": d["id"], "result": "retry_now"})
            else:
                results.append({"delivery": d["id"], "result": "not_retryable", "reason": f"state is {d['state']}"})
        self._after_commit(dispatch=True)
        if not any(r["result"] in ("requeued", "retry_now") for r in results):
            raise Conflict("nothing to retry (only failed or waiting deliveries can be retried)", results)
        return {"notification": self.get(client, nid, detail=False), "results": results}

    def dismiss(self, client: Client, nid: str) -> dict[str, Any]:
        """Take a notification's failed/expired deliveries off the attention list; history is unchanged."""
        self._notification_row(client, nid)
        with self.db.tx() as c:
            n = c.execute("UPDATE deliveries SET dismissed_at=? WHERE notification_id=? AND state IN (?,?)"
                          " AND dismissed_at IS NULL", (now_iso(), nid, FAILED, EXPIRED)).rowcount
            n += c.execute("UPDATE notifications SET dismissed_at=? WHERE id=? AND release_problem IS NOT NULL"
                           " AND dismissed_at IS NULL", (now_iso(), nid)).rowcount
        if not n:
            raise Conflict("nothing to dismiss (no failed or expired deliveries left on the attention list)")
        return {"dismissed": n}

    # --------------------------------------------------------------- queries
    def _delivery_out(self, d: sqlite3.Row, attempts: list[sqlite3.Row] | None = None) -> dict[str, Any]:
        observable = loads(d["observable_states_json"], [])
        out: dict[str, Any] = {
            "id": d["id"], "device": d["device"], "provider": d["provider"], "state": d["state"],
            "final": d["finalized_at"] is not None,
            "tracking": d["tracking"], "observable_states": observable,
            "capability_gaps": loads(d["capability_gaps_json"], []),
            "attempts": d["attempts"], "max_attempts": d["max_attempts"],
            "next_attempt_at": d["next_attempt_at"] if d["state"] == QUEUED else None,
            "last_error": d["last_error"],
            "provider_ref": {"message_id": d["provider_message_id"], "receipt": d["receipt"]},
            "timestamps": {"created": d["created_at"], "dispatched": d["dispatched_at"],
                           "delivered": d["delivered_at"], "acknowledged": d["acknowledged_at"],
                           "interacted": d["interacted_at"], "finalized": d["finalized_at"]},
            "acknowledged_by": d["acknowledged_by"],
            "interaction": loads(d["interaction_json"]),
        }
        if attempts is not None:
            out["attempt_log"] = [{
                "attempt": a["attempt_no"], "kind": a["kind"], "started_at": a["started_at"],
                "finished_at": a["finished_at"], "outcome": a["outcome"], "http_status": a["http_status"],
                "request": _mask_tree(loads(a["request_json"])), "response": a["response_body"], "error": a["error"],
            } for a in attempts]
        return out

    def _notification_out(self, n: sqlite3.Row, deliveries: list[dict[str, Any]],
                          viewer: Client | None = None) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for d in deliveries:
            counts[d["state"]] = counts.get(d["state"], 0) + 1
        routing = loads(n["routing_json"], {})
        return {
            "id": n["id"], "status": n["status"], "severity": n["severity"], "title": n["title"],
            "message": self._shown_message(n, viewer), "targets": loads(n["targets_json"], []),
            "source": n["source"],
            "client": n["client_id"], "tags": loads(n["tags_json"], []), "data": loads(n["data_json"]),
            "url": _mask_decision(n["url"]), "url_title": n["url_title"],
            "actions": [{**a, **({"url": _mask_decision(a["url"])} if a.get("url") else {})}
                        for a in loads(n["actions_json"], [])],
            "require_ack": bool(n["require_ack"]),
            "idempotency_key": None if self._has_handle(n) else n["idempotency_key"],
            "created_at": n["created_at"], "updated_at": n["updated_at"], "expires_at": n["expires_at"],
            "sent_at": None if n["status"] == "scheduled" else self._went_out(n),
            "suppressed": routing.get("suppressed", []), "delivery_counts": counts, "deliveries": deliveries,
            **self._attachments_out(n["id"]),
            **({"send_at": n["send_at"], "max_late_seconds": n["max_late_seconds"], "deliver_by": n["deliver_by"],
                "released_at": n["released_at"], "ttl_seconds": n["ttl_seconds"],
                **({"kinds": routing.get("kinds"), "routing_preview": routing.get("preview")} if n["status"] == "scheduled" else {})}
               if n["send_at"] else {}),
            **({"reply": bool(n["reply_wanted"]), "in_reply_to": n["in_reply_to"],
                "conversation_id": n["conversation_id"] or n["id"]}
               if n["reply_wanted"] or n["in_reply_to"] else {}),
        }

    @staticmethod
    def _gate(c: sqlite3.Connection, conv: str, req: NotificationIn) -> None:
        from .replies import conversation_gate  # replies imports core; import here to avoid a cycle
        conversation_gate(c, conv, req.reply_handle, internal=req._internal)

    def _has_handle(self, n: sqlite3.Row) -> bool:
        """Idempotency keys stay out of read outputs in a conversation with a handle (defence in depth)."""
        if not (n["reply_wanted"] or n["in_reply_to"]):
            return False
        return self.db.one("SELECT 1 FROM conversations WHERE id=? AND handle_sha256 IS NOT NULL",
                           (n["conversation_id"] or n["id"],)) is not None

    def _attachments_out(self, nid: str) -> dict[str, Any]:
        rows = self.db.query("SELECT filename, content_type, size, sha256, source, purged_at FROM attachments"
                             " WHERE notification_id=? ORDER BY idx", (nid,))
        if not rows:
            return {}  # notifications without attachments keep the 0.4.0 shape
        return {"attachments": [{"filename": r["filename"], "content_type": r["content_type"], "size": r["size"],
                                 "sha256": r["sha256"], "source": r["source"], "purged": r["purged_at"] is not None}
                                for r in rows]}

    def get(self, client: Client, nid: str, detail: bool = True) -> dict[str, Any]:
        n = self._notification_row(client, nid)
        ds = self.db.query("SELECT * FROM deliveries WHERE notification_id=? ORDER BY created_at, id", (nid,))
        deliveries = []
        for d in ds:
            att = self.db.query("SELECT * FROM attempts WHERE delivery_id=? ORDER BY id", (d["id"],)) if detail else None
            deliveries.append(self._delivery_out(d, att))
        out = self._notification_out(n, deliveries, client)
        if detail:
            out["events"] = [self._event_out(e) for e in
                             self.db.query("SELECT * FROM events WHERE notification_id=? ORDER BY id", (nid,))]
        return out

    def list(self, client: Client, *, status: str | None = None, source: str | None = None, tag: str | None = None,
             since: str | None = None, before: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM notifications WHERE 1=1"
        params: list[Any] = []
        if not client.admin:
            sql += " AND client_id=?"
            params.append(client.id)
        if status:
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params += statuses
        else:
            sql += f" AND {schedule.IN_HISTORY_SQL}"
        if source:
            sql += " AND source=?"
            params.append(source)
        if tag:
            sql += " AND EXISTS (SELECT 1 FROM json_each(tags_json) WHERE value=?)"
            params.append(tag)
        if since:
            try:
                ts = parse_iso(since)
            except ValueError:
                raise Invalid("since must be an ISO 8601 time") from None
            if ts is not None and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            sql += f" AND {schedule.EFFECTIVE_SQL} >= ?"
            params.append(iso(ts))  # stored times are UTC 'Z' strings: compare like with like
        # the scheduled listing is the whole queue by send time, with no cursor and no page cap
        queue = status is not None and status.strip() == "scheduled"
        if before and not queue:
            ref = self.db.one("SELECT client_id FROM notifications WHERE id=?", (before,))
            if ref is None or not self._visible(client, ref["client_id"]):
                raise Invalid(f"before: notification {before} not found (purged?); start again without it")
            sql += (f" AND ({schedule.EFFECTIVE_SQL}, id) <"
                    f" (SELECT {schedule.EFFECTIVE_SQL}, id FROM notifications WHERE id = ?)")
            params.append(before)
        sql += " ORDER BY send_at, id LIMIT ?" if queue else f" ORDER BY {schedule.EFFECTIVE_SQL} DESC, id DESC LIMIT ?"
        params.append(1000 if queue else max(1, min(limit, 200)))
        out = []
        for n in self.db.query(sql, params):
            ds = [self._delivery_out(d) for d in
                  self.db.query("SELECT * FROM deliveries WHERE notification_id=? ORDER BY created_at, id", (n["id"],))]
            out.append(self._notification_out(n, ds, client))
        return out

    def attention(self, client: Client, stuck_after_seconds: int = 120) -> dict[str, Any]:
        """Things a human or agent should look at: failures, stuck queue, unacknowledged alerts."""
        own = "" if client.admin else " AND n.client_id=?"
        p: list[Any] = [] if client.admin else [client.id]
        since = iso(utcnow() - timedelta(days=7))
        stuck_cut = iso(utcnow() - timedelta(seconds=stuck_after_seconds))

        def rows(where: str, params: list[Any]) -> list[dict[str, Any]]:
            res = self.db.query(
                "SELECT d.*, n.title, n.severity, n.source, n.created_at AS n_created FROM deliveries d"
                " JOIN notifications n ON n.id=d.notification_id WHERE " + where + own
                + " ORDER BY d.updated_at DESC LIMIT 50", params + p)
            return [{"notification_id": r["notification_id"], "delivery_id": r["id"], "title": r["title"],
                     "severity": r["severity"], "source": r["source"], "device": r["device"], "state": r["state"],
                     "attempts": r["attempts"], "last_error": r["last_error"], "created_at": r["n_created"],
                     "at": r["updated_at"],
                     "next_attempt_at": r["next_attempt_at"] if r["state"] == QUEUED else None} for r in res]

        return {
            "failed": rows("d.state=? AND d.updated_at >= ? AND d.dismissed_at IS NULL", [FAILED, since]),
            "expired": rows("d.state=? AND d.updated_at >= ? AND d.dismissed_at IS NULL", [EXPIRED, since]),
            "retrying": rows("d.state=? AND d.attempts > 0", [QUEUED]),
            "stuck": rows("d.state IN (?,?) AND d.created_at <= ?", [QUEUED, SENDING, stuck_cut]),
            "awaiting_acknowledgement": rows("(d.tracking='receipt' OR (d.tracking='report' AND (n.severity IN ('high','critical')"
                                         " OR n.require_ack=1))) AND d.state IN (?,?)", [DISPATCHED, DELIVERED]),
            "release_problems": self.scheduler.problems(client),
            "providers": {name: p.health() for name, p in self.providers.items() if not p.configured},
        }

    def rejected_problems(self, device: str, provider: Provider, address: dict[str, Any]) -> list[str]:
        """What the provider said was wrong with the address: its own memory, else the persisted flag
        (device_check.record_check), which survives provider rebuilds and restarts."""
        live = provider.address_problems(address)
        if live:
            return live
        row = self.db.one("SELECT value FROM meta WHERE key=?", (f"device_invalid:{device}",))
        if row is None:
            return []
        text = row["value"] or ""
        if not text or text[:2] == "20" and text[4:5] == "-":  # empty, or a 0.9.x flag holding a timestamp
            text = "rejected by the provider at the last send or check; a successful send clears this"
        return [text]

    def targets(self) -> dict[str, Any]:
        cfg = self.config

        def device_out(name: str) -> dict[str, Any]:
            d = cfg.devices[name]
            p = self.providers[d.provider]
            caps = {**p.capabilities(), **d.capabilities}
            problems = p.config_problems() + p.device_problems(d.address) + self.rejected_problems(name, p, d.address)
            return {"target": f"device:{name}", "description": d.description, "owner": d.owner,
                    "provider": d.provider, "kind": cfg.device_kind(name), "opt_in": cfg.device_opt_in(name),
                    "direct_only": d.direct_only,
                    "enabled": d.enabled, "min_severity": d.min_severity.value,
                    "ready": not problems, "problems": problems, "capabilities": caps}

        return {
            "users": [{"target": f"user:{n}", "display_name": u.display_name, "devices": cfg.devices_of_user(n)}
                      for n, u in cfg.users.items()],
            "devices": [device_out(n) for n in cfg.devices],
            "groups": [{"target": f"group:{n}", "description": g.description, "members": g.members}
                       for n, g in cfg.groups.items()],
            "channels": [{"target": f"channel:{n}", "description": ch.description, "targets": ch.targets,
                          "min_severity": ch.min_severity.value} for n, ch in cfg.channels.items()],
            "severities": [s.value for s in Severity],
        }

    def providers_info(self) -> list[dict[str, Any]]:
        return [{"name": name, "type": p.type, "kind": p.kind, **p.health(), "capabilities": p.capabilities()}
                for name, p in self.providers.items()]

    # ---------------------------------------------------------------- events
    @staticmethod
    def _event_out(e: sqlite3.Row) -> dict[str, Any]:
        return {"id": e["id"], "type": e["type"], "notification_id": e["notification_id"],
                "delivery_id": e["delivery_id"], "source": e["source"], "created_at": e["created_at"],
                "data": loads(e["data_json"], {})}

    def events_after(self, client: Client, after: int, *, notification_id: str | None = None,
                     types: list[str] | None = None, source: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.events_page(client, after, notification_id=notification_id, types=types, source=source,
                                limit=limit)[0]

    def events_page(self, client: Client, after: int, *, notification_id: str | None = None,
                    types: list[str] | None = None, source: str | None = None,
                    limit: int = 100) -> tuple[list[dict[str, Any]], int]:
        """Events after `after`, and the cursor to continue from: the last event *scanned*, so a type
        filter that matches nothing in this window still moves the poller forward."""
        sql = "SELECT * FROM events WHERE id > ?"
        params: list[Any] = [after]
        if not client.admin:
            sql += " AND client_id=?"
            params.append(client.id)
        if notification_id:
            sql += " AND notification_id=?"
            params.append(notification_id)
        if source:
            sql += " AND source=?"
            params.append(source)
        sql += " ORDER BY id LIMIT ?"
        params.append(max(1, min(limit, 500)))
        rows = self.db.query(sql, params)
        cursor = rows[-1]["id"] if rows else after
        out = [self._event_out(e) for e in rows]
        if types:
            out = [e for e in out if any(event_matches(t, e["type"]) for t in types)]
        return out, cursor

    def last_event_id(self) -> int:
        return int(self.db.scalar("SELECT COALESCE(MAX(id), 0) FROM events") or 0)

    # --------------------------------------------------------- subscriptions
    def _sub_out(self, s: sqlite3.Row) -> dict[str, Any]:
        pending = self.db.scalar("SELECT COUNT(*) FROM webhook_deliveries WHERE subscription_id=? AND state='pending'",
                                 (s["id"],))
        return {"id": s["id"], "client": s["client_id"], "url": s["url"], "events": loads(s["events_json"], []),
                "notification_id": s["notification_id"], "source": s["source"], "tag": s["tag"],
                "description": s["description"], "active": bool(s["active"]),
                "consecutive_failures": s["consecutive_failures"], "last_success_at": s["last_success_at"],
                "last_failure_at": s["last_failure_at"], "last_error": s["last_error"], "pending": pending,
                "created_at": s["created_at"]}

    def create_subscription(self, client: Client, req: SubscriptionIn) -> dict[str, Any]:
        if req.notification_id:
            self._notification_row(client, req.notification_id)
        if not client.admin:
            if req.notification_id:  # per notification: its submit webhook plus a few more
                used = self.db.scalar("SELECT COUNT(*) FROM subscriptions WHERE client_id=? AND notification_id=?",
                                      (client.id, req.notification_id)) or 0
                cap = MAX_SUBSCRIPTIONS_PER_NOTIFICATION
            else:
                used = self.db.scalar("SELECT COUNT(*) FROM subscriptions WHERE client_id=? AND notification_id IS NULL",
                                      (client.id,)) or 0
                cap = MAX_SUBSCRIPTIONS_PER_CLIENT
            if used >= cap:
                raise Invalid(f"at most {cap} subscriptions {'per notification' if req.notification_id else 'per client'};"
                              " delete one first")
        sid = new_id("sub")
        secret = req.secret or secrets.token_urlsafe(32)
        with self.db.tx() as c:
            c.execute("INSERT INTO subscriptions(id, client_id, url, secret, events_json, notification_id, source, tag,"
                      " description, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (sid, client.id, req.url, secret, dumps(req.events), req.notification_id, req.source, req.tag,
                       req.description, now_iso()))
        out = self._sub_out(self.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,)))  # type: ignore[arg-type]
        out["secret"] = secret
        return out

    def list_subscriptions(self, client: Client) -> list[dict[str, Any]]:
        sql = "SELECT * FROM subscriptions" + ("" if client.admin else " WHERE client_id=?") + " ORDER BY created_at"
        return [self._sub_out(s) for s in self.db.query(sql, () if client.admin else (client.id,))]

    def delete_subscription(self, client: Client, sid: str) -> None:
        s = self.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))
        if s is None or not self._visible(client, s["client_id"]):
            raise NotFound(f"subscription {sid} not found")
        with self.db.tx() as c:
            c.execute("DELETE FROM subscriptions WHERE id=?", (sid,))

    def claim_webhooks(self, limit: int = 10) -> list[dict[str, Any]]:
        now = now_iso()
        rows = self.db.query(
            "SELECT w.id AS wid, w.attempts, s.id AS sid, s.url, s.secret, e.* FROM webhook_deliveries w"
            " JOIN subscriptions s ON s.id=w.subscription_id JOIN events e ON e.id=w.event_id"
            " WHERE w.state='pending' AND w.next_attempt_at <= ? AND s.active=1 ORDER BY w.id LIMIT ?", (now, limit))
        out = []
        for r in rows:
            n = self.db.one("SELECT * FROM notifications WHERE id=?", (r["notification_id"],))
            ev = self._event_out(r)
            if n is not None:
                ev["notification"] = {"id": n["id"], "status": n["status"], "severity": n["severity"],
                                      "title": n["title"], "source": n["source"], "tags": loads(n["tags_json"], []),
                                      "data": loads(n["data_json"])}
            out.append({"wid": r["wid"], "attempts": r["attempts"], "sid": r["sid"], "url": r["url"],
                        "secret": r["secret"], "event": ev})
        return out

    def webhook_result(self, item: dict[str, Any], ok: bool, status: int | None, error: str | None) -> None:
        s = self.config.server
        now = utcnow()
        attempts = item["attempts"] + 1
        with self.db.tx() as c:
            if ok:
                c.execute("UPDATE webhook_deliveries SET state='delivered', attempts=?, last_status=?, delivered_at=?,"
                          " last_error=NULL WHERE id=?", (attempts, status, iso(now), item["wid"]))
                c.execute("UPDATE subscriptions SET consecutive_failures=0, failing_since=NULL, last_success_at=? WHERE id=?",
                          (iso(now), item["sid"]))
                return
            final = attempts >= s.webhook_max_attempts
            nxt = now + timedelta(seconds=min(600, 5 * 3 ** (attempts - 1)))
            c.execute("UPDATE webhook_deliveries SET state=?, attempts=?, last_status=?, last_error=?, next_attempt_at=?"
                      " WHERE id=?", ("failed" if final else "pending", attempts, status, error, iso(nxt), item["wid"]))
            c.execute("UPDATE subscriptions SET failing_since=COALESCE(failing_since, ?) WHERE id=?", (iso(now), item["sid"]))
            c.execute("UPDATE subscriptions SET consecutive_failures=consecutive_failures+1, last_failure_at=?,"
                      " last_error=? WHERE id=?", (iso(now), error, item["sid"]))
            sub = c.execute("SELECT consecutive_failures, failing_since FROM subscriptions WHERE id=?",
                            (item["sid"],)).fetchone()
            # a short outage with a backlog fails many deliveries at once: disable only when the failures have also
            # lasted a day (measured from the first failure of this streak, not the last success: a quiet
            # subscription may not have succeeded for days)
            since = parse_iso(sub["failing_since"]) if sub else None
            long_down = since is None or now - since >= timedelta(seconds=WEBHOOK_DISABLE_AFTER_SECONDS)
            if sub and sub[0] >= s.webhook_disable_after_failures and long_down:
                c.execute("UPDATE subscriptions SET active=0, last_error=? WHERE id=?",
                          (f"disabled after {sub[0]} consecutive failures: {error}", item["sid"]))
                log.warning("webhook subscription disabled", extra={"subscription_id": item["sid"]})

    # ------------------------------------------------------------ operations
    def retention_sweep(self) -> int:
        cutoff = iso(utcnow() - timedelta(days=self.config.server.retention_days))
        with self.db.tx() as c:
            ids = [r[0] for r in c.execute(
                f"SELECT id FROM notifications WHERE status != 'scheduled' AND {schedule.EFFECTIVE_SQL} < ?"
                " AND NOT EXISTS (SELECT 1 FROM deliveries d"
                " WHERE d.notification_id=notifications.id AND d.finalized_at IS NULL)", (cutoff,))]
            for nid in ids:
                c.execute("DELETE FROM notifications WHERE id=?", (nid,))
            c.execute("DELETE FROM conversations WHERE NOT EXISTS"
                      " (SELECT 1 FROM notifications n WHERE n.conversation_id=conversations.id)")
            c.execute("DELETE FROM subscriptions WHERE notification_id IS NOT NULL AND NOT EXISTS"
                      " (SELECT 1 FROM notifications n WHERE n.id=subscriptions.notification_id)")
            att_cutoff = iso(utcnow() - timedelta(days=self.config.server.attachment_retention_days))
            c.execute("UPDATE attachments SET data=NULL, purged_at=? WHERE data IS NOT NULL AND notification_id IN"
                      f" (SELECT id FROM notifications WHERE status != 'scheduled' AND {schedule.EFFECTIVE_SQL} < ?)"
                      " AND NOT EXISTS (SELECT 1 FROM deliveries d"
                      " WHERE d.notification_id=attachments.notification_id AND d.state IN (?,?))",
                      (now_iso(), att_cutoff, QUEUED, SENDING))
            c.execute("UPDATE reply_attachments SET data=NULL, purged_at=? WHERE data IS NOT NULL AND reply_id IN"
                      " (SELECT id FROM replies WHERE received_at < ?)", (now_iso(), att_cutoff))
            c.execute("DELETE FROM reply_quarantine WHERE received_at < ?", (cutoff,))
            c.execute("DELETE FROM events WHERE notification_id IS NULL AND type LIKE 'mailbox.%' AND created_at < ?",
                      (cutoff,))
            old = "(SELECT id FROM chat_groups WHERE state='closed' AND closed_at < ?)"
            c.execute(f"DELETE FROM chat_wakes WHERE message_id IN (SELECT id FROM chat_messages WHERE group_id IN {old})"
                      f" OR member_id IN (SELECT id FROM chat_members WHERE group_id IN {old})", (cutoff, cutoff))
            # history requests point at the group and its members (foreign keys are on): they go first
            c.execute(f"DELETE FROM chat_history_requests WHERE group_id IN {old}"
                      f" OR member_id IN (SELECT id FROM chat_members WHERE group_id IN {old})", (cutoff, cutoff))
            c.execute(f"DELETE FROM chat_messages WHERE group_id IN {old}", (cutoff,))
            c.execute(f"DELETE FROM chat_members WHERE group_id IN {old}", (cutoff,))
            c.execute("DELETE FROM chat_groups WHERE state='closed' AND closed_at < ?", (cutoff,))
            c.execute("DELETE FROM events WHERE type LIKE 'group.%' AND created_at < ?", (cutoff,))
        return len(ids)

    def stats(self) -> dict[str, Any]:
        by_state = {r[0]: r[1] for r in self.db.query("SELECT state, COUNT(*) FROM deliveries GROUP BY state")}
        by_status = {r[0]: r[1] for r in self.db.query("SELECT status, COUNT(*) FROM notifications GROUP BY status")}
        oldest = self.db.scalar("SELECT MIN(created_at) FROM deliveries WHERE state IN (?,?)", tuple(ACTIVE_STATES))
        oldest_age = (utcnow() - parse_iso(oldest)).total_seconds() if oldest else 0.0  # type: ignore[operator]
        wh = {r[0]: r[1] for r in self.db.query("SELECT state, COUNT(*) FROM webhook_deliveries GROUP BY state")}
        return {"deliveries_by_state": by_state, "notifications_by_status": by_status,
                "oldest_pending_age_seconds": round(oldest_age, 1), "webhooks_by_state": wh,
                "attempts_total": self.db.scalar("SELECT COUNT(*) FROM attempts WHERE kind='send'") or 0}
