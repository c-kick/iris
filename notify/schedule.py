"""Scheduled notifications: time rules (pure functions, no I/O)."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from .core import NotifyService

log = logging.getLogger("notify.schedule")

IMMEDIATE_AHEAD = 5          # a send time this close (or already slightly past) means "now"
PAST_GRACE = 60              # further in the past than this is rejected
HORIZON_DAYS = 366
MAX_LATE_DEFAULT = 3600
MAX_SCHEDULED = 100          # per client
MAX_SCHEDULED_BYTES = 50 * 1024 * 1024
OVERDUE_SECONDS = 60
UI_CLIENT_ID = "admin-ui"    # the admin page's client (admin.UI_CLIENT; a test keeps them equal)
# the moment a notification actually went out, wherever a list or a window needs it
EFFECTIVE_SQL = "COALESCE(released_at, send_at, created_at)"

_DURATION = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", re.IGNORECASE)


def local_zone() -> ZoneInfo:
    """The household time zone: $TZ when set and valid, else UTC."""
    name = os.environ.get("TZ")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return ZoneInfo("UTC")


def parse_duration(v: int | str) -> int:
    """Seconds from a bare positive integer or a d/h/m/s string such as '2h30m'."""
    if isinstance(v, bool):
        raise ValueError("duration must be a positive number of seconds or like 2h30m")
    if isinstance(v, int):
        seconds = v
    else:
        s = str(v).strip() if isinstance(v, str) else ""
        if s.isascii() and s.isdigit():
            seconds = int(s)
        else:
            m = _DURATION.match(s)
            if not s or not m or not any(m.groups()):
                raise ValueError(f"invalid duration {v!r}: use seconds or a form like 90s, 45m, 2h30m, 1d")
            d, h, mi, sec = (int(g or 0) for g in m.groups())
            seconds = ((d * 24 + h) * 60 + mi) * 60 + sec
    if seconds <= 0:
        raise ValueError(f"duration must be greater than zero, got {v!r}")
    return seconds


def to_utc(dt: datetime, zone: ZoneInfo) -> datetime:
    """Aware datetimes are converted; naive ones are read as wall-clock time in `zone`."""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC)
    local = dt.replace(tzinfo=zone, fold=0)
    utc = local.astimezone(UTC)
    if utc.astimezone(zone).replace(tzinfo=None) != dt:  # round trip moved it: it fell in a DST gap
        raise ValueError(f"{dt.isoformat()} does not exist in {zone.key} (DST change)")
    return utc


def resolve_send_at(send_at: datetime | None, delay: int | str | None, now: datetime,
                    zone: ZoneInfo) -> datetime | None:
    """The UTC send time, or None when the notification should go out immediately."""
    if send_at is None and delay is None:
        return None
    if delay is not None:
        seconds = parse_duration(delay)
        if seconds > HORIZON_DAYS * 86400:
            raise ValueError(f"the send time may be at most {HORIZON_DAYS} days ahead")
        when = now + timedelta(seconds=seconds)
    else:
        when = to_utc(send_at, zone)
    ahead = (when - now).total_seconds()
    if ahead < -PAST_GRACE:
        raise ValueError("the send time is in the past")
    if ahead > HORIZON_DAYS * 86400:
        raise ValueError(f"the send time may be at most {HORIZON_DAYS} days ahead")
    if ahead <= IMMEDIATE_AHEAD:
        return None
    return when


class Scheduler:
    """Release of scheduled notifications: one door, one transaction, one row at a time."""

    def __init__(self, svc: NotifyService):
        self.svc = svc

    def release(self, nid: str, *, force: bool = False) -> str:
        """Turn a scheduled notification into deliveries (or a recorded problem) and return its status.
        Used by the scheduled release, send-now and the retry of a missed/failed release."""
        status, committed = self._release(nid, force)
        if committed:  # every outcome changed the row: wake SSE streams, and the dispatcher for new deliveries
            self.svc._after_commit(dispatch=True)
        return status

    def _release(self, nid: str, force: bool) -> tuple[str, bool]:
        from .core import NotFound, SYSTEM_CLIENT
        from .db import dumps, loads, now_iso, parse_iso, utcnow
        from .models import QUEUED, Severity, derive_notification_status
        from .routing import RoutingError

        svc = self.svc
        with svc.db.tx() as c:
            n = c.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
            if n is None:
                raise NotFound(f"notification {nid} not found")
            eligible = n["status"] == "scheduled" or (
                force and n["release_problem"] is not None
                and not c.execute("SELECT 1 FROM deliveries WHERE notification_id=?", (nid,)).fetchone())
            if not eligible:
                return n["status"], False
            now = utcnow()
            stamp = now_iso()
            if force:
                c.execute("UPDATE notifications SET deliver_by=NULL WHERE id=?", (nid,))

            def finish(status: str, problem: str | None, routing: dict[str, Any] | None = None) -> str:
                c.execute("UPDATE notifications SET status=?, released_at=?, release_problem=?, dismissed_at=NULL,"
                          " routing_json=COALESCE(?, routing_json), updated_at=? WHERE id=?",
                          (status, stamp, problem, dumps(routing) if routing is not None else None, stamp, nid))
                return status

            def status_changed(to: str) -> str:
                svc._emit(c, "notification.status_changed", n, None, {"from": n["status"], "to": to})
                return to

            if not force and n["deliver_by"] and now > parse_iso(n["deliver_by"]):
                finish("expired", f"missed: due at {n['send_at']}, could not be sent until {stamp}")
                svc._emit(c, "notification.missed", n, None,
                          {"send_at": n["send_at"], "deliver_by": n["deliver_by"], "missed_at": stamp})
                return "expired", True
            if n["client_id"] not in svc.config.clients and n["client_id"] not in (SYSTEM_CLIENT.id, UI_CLIENT_ID):
                finish("failed", "sender no longer exists")
                return status_changed("failed"), True

            old = loads(n["routing_json"], {})
            kinds = old.get("kinds")
            try:
                route = svc.router.resolve(loads(n["targets_json"], []), Severity(n["severity"]), kinds)
            except RoutingError as e:
                finish("failed", f"routing failed: {e}")
                return status_changed("failed"), True
            meta = [{"filename": r["filename"], "content_type": r["content_type"], "size": r["size"]}
                    for r in c.execute("SELECT filename, content_type, size FROM attachments WHERE notification_id=?"
                                       " ORDER BY idx", (nid,))]
            nrow = dict(n)
            deliveries = svc._plan_deliveries(nrow, route, meta)
            routing: dict[str, Any] = {"suppressed": route.suppressed, "via": {d["device"]: d["via"] for d in deliveries}}
            if kinds is not None:
                routing["kinds"] = kinds
            problem = None
            if not deliveries:
                reasons = "; ".join(f"{s['target']}: {s['reason']}" for s in route.suppressed) or "no targets"
                problem = f"routed to no device at send time: {reasons}"
            elif n["reply_wanted"] and not any(
                    (svc.providers[d["provider"]].capabilities().get("replies") or {}).get("supported")
                    for d in deliveries):
                problem = "reply requested but no email device received it"
            status = finish(derive_notification_status([QUEUED] * len(deliveries)), problem, routing)
            n = c.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
            svc._emit(c, "notification.created", n, None,
                      {"severity": n["severity"], "targets": loads(n["targets_json"], []),
                       "deliveries": len(deliveries), "suppressed": route.suppressed, "scheduled": True})
            for d in deliveries:
                c.execute(
                    "INSERT INTO deliveries(id, notification_id, device, provider, state, max_attempts,"
                    " next_attempt_at, tracking, observable_states_json, capability_gaps_json, created_at,"
                    " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (d["id"], nid, d["device"], d["provider"], QUEUED, svc.config.server.max_attempts, stamp,
                     d["tracking"], dumps(d["observable_states"]), dumps(d["capability_gaps"]), stamp, stamp))
                svc._emit(c, "delivery.queued", n, d["id"],
                          {"device": d["device"], "provider": d["provider"], "to": QUEUED,
                           "capability_gaps": d["capability_gaps"]})
        return status, True

    def cancel(self, client: Any, nid: str) -> dict[str, Any] | None:
        """Cancel a still-scheduled notification of the caller's (any, for an admin) in one conditional
        UPDATE; None when no row changed, so the caller falls back to the delivery cancel."""
        from .db import now_iso
        svc = self.svc
        stamp = now_iso()
        with svc.db.tx() as c:
            cur = c.execute("UPDATE notifications SET status='cancelled', updated_at=? WHERE id=?"
                            " AND status='scheduled' AND (?=1 OR client_id=?)",
                            (stamp, nid, int(client.admin), client.id))
            if not cur.rowcount:
                return None
            n = c.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
            svc._emit(c, "notification.status_changed", n, None,
                      {"from": "scheduled", "to": "cancelled", "reason": f"cancelled by {client.id}"})
        svc._after_commit()
        return {"notification": svc.get(client, nid, detail=False), "results": [{"result": "cancelled"}]}

    def update(self, client: Any, nid: str, patch: Any) -> dict[str, Any]:
        """Edit a scheduled notification (or send it now) in one transaction; any problem leaves the row alone."""
        from pydantic import ValidationError
        from .core import Conflict, Invalid, NotFound
        from .db import dumps, iso, loads, now_iso, parse_iso, utcnow
        from .models import NotificationIn

        svc = self.svc
        given = patch.model_fields_set
        with svc.db.tx() as c:
            n = c.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
            if n is None or not svc._visible(client, n["client_id"]):
                raise NotFound(f"notification {nid} not found")
            if n["status"] != "scheduled":
                raise Conflict(f"notification is {n['status']}", {"status": n["status"]})
            now = utcnow()
            zone = local_zone()

            for name in ("message", "severity", "targets", "require_ack", "max_late_seconds"):
                if name in given and getattr(patch, name) is None:
                    raise Invalid(f"{name} cannot be cleared")

            send_at = parse_iso(n["send_at"])
            if patch.send_now:
                send_at = now
            elif patch.send_at is not None or patch.delay is not None:
                try:
                    when = resolve_send_at(patch.send_at, patch.delay, now, zone)
                except ValueError as e:
                    raise Invalid(str(e)) from e
                send_at = now if when is None else when  # "now-ish" means now
            max_late = patch.max_late_seconds if "max_late_seconds" in given else n["max_late_seconds"]
            ttl = patch.ttl_seconds if "ttl_seconds" in given else n["ttl_seconds"]
            if "expires_at" in given:
                try:
                    explicit = None if patch.expires_at is None else to_utc(patch.expires_at, zone)
                except ValueError as e:
                    raise Invalid(str(e)) from e
            else:
                explicit = parse_iso(n["explicit_expires_at"]) if n["explicit_expires_at"] else None

            # the merged content must pass the same field rules as a submit
            old_routing = loads(n["routing_json"], {})
            merged: dict[str, Any] = {
                "targets": loads(n["targets_json"], []), "message": n["message"], "title": n["title"],
                "severity": n["severity"], "url": n["url"], "url_title": n["url_title"],
                "actions": loads(n["actions_json"], []), "tags": loads(n["tags_json"], []),
                "kinds": old_routing.get("kinds"), "require_ack": bool(n["require_ack"]), "ttl_seconds": ttl}
            for name in ("targets", "message", "title", "severity", "url", "url_title", "actions", "tags",
                         "kinds", "require_ack"):
                if name in given:
                    v = getattr(patch, name)
                    merged[name] = [a.model_dump() for a in v] if name == "actions" and v is not None else v
            if merged["actions"] is None:
                merged["actions"] = []
            if merged["tags"] is None:
                merged["tags"] = []
            try:
                checked = NotificationIn.model_validate(merged)
            except ValidationError as e:
                raise Invalid("; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())) from e

            expires = explicit
            if ttl is not None:
                ttl_exp = send_at + timedelta(seconds=ttl)
                expires = ttl_exp if expires is None else min(expires, ttl_exp)
            if explicit is not None and explicit <= send_at:
                raise Invalid("expires_at must be after the send time")
            if expires is not None and expires <= now:
                raise Invalid("notification is already expired (expires_at/ttl_seconds in the past)")

            stamp = now_iso()
            new = dict(n)
            new.update({
                "title": checked.title, "message": checked.message, "severity": checked.severity.value,
                "url": checked.url, "url_title": checked.url_title,
                "actions_json": dumps([a.model_dump() for a in checked.actions]), "tags_json": dumps(checked.tags),
                "targets_json": dumps(checked.targets), "require_ack": int(checked.require_ack),
                "send_at": iso(send_at), "max_late_seconds": max_late,
                "deliver_by": iso(send_at + timedelta(seconds=max_late)), "ttl_seconds": ttl,
                "explicit_expires_at": iso(explicit), "expires_at": iso(expires), "updated_at": stamp})
            meta = [dict(r) for r in c.execute("SELECT filename, content_type, size FROM attachments"
                                                " WHERE notification_id=? ORDER BY idx", (nid,))]
            preview = svc.scheduled_preview(new, checked.targets, checked.severity, checked.kinds, meta)
            routing = {k: v for k, v in old_routing.items() if k != "kinds"}
            routing["preview"] = preview
            if checked.kinds is not None:
                routing["kinds"] = checked.kinds
            c.execute(
                "UPDATE notifications SET title=?, message=?, severity=?, url=?, url_title=?, actions_json=?,"
                " tags_json=?, targets_json=?, require_ack=?, send_at=?, max_late_seconds=?, deliver_by=?,"
                " ttl_seconds=?, explicit_expires_at=?, expires_at=?, routing_json=?, updated_at=? WHERE id=?",
                (new["title"], new["message"], new["severity"], new["url"], new["url_title"], new["actions_json"],
                 new["tags_json"], new["targets_json"], new["require_ack"], new["send_at"], max_late,
                 new["deliver_by"], ttl, new["explicit_expires_at"], new["expires_at"], dumps(routing), stamp, nid))
            data: dict[str, Any] = {"fields": sorted(given), "by": client.id}
            if new["send_at"] != n["send_at"]:
                data["send_at"] = {"from": n["send_at"], "to": new["send_at"]}
            svc._emit(c, "notification.updated", n, None, data)
        svc._after_commit(dispatch=True)  # send-now or an earlier time: the dispatcher re-checks its next wake-up
        return svc.get(client, nid, detail=False)

    def problems(self, client: Any) -> list[dict[str, Any]]:
        """Notifications that could not go out (undismissed release problem) or are overdue, newest first."""
        from .db import iso, parse_iso, utcnow
        svc = self.svc
        own = "" if client.admin else " AND client_id=?"
        p: list[Any] = [] if client.admin else [client.id]
        now = utcnow()
        cut = iso(now - timedelta(seconds=OVERDUE_SECONDS))
        rows = svc.db.query(
            "SELECT * FROM notifications WHERE ((release_problem IS NOT NULL AND dismissed_at IS NULL)"
            " OR (status='scheduled' AND send_at <= ?))" + own
            + " ORDER BY COALESCE(released_at, send_at) DESC LIMIT 50", [cut, *p])
        out = []
        for r in rows:
            reason = r["release_problem"]
            if reason is None:
                late = int((now - parse_iso(r["send_at"])).total_seconds())  # type: ignore[operator]
                reason = f"overdue: still waiting {late} s after its send time"
            out.append({"notification_id": r["id"], "title": r["title"], "severity": r["severity"],
                        "source": r["source"], "client": r["client_id"], "status": r["status"],
                        "send_at": r["send_at"], "reason": reason, "at": r["released_at"] or r["send_at"]})
        return out

    def overdue_count(self) -> int:
        from .db import iso, utcnow
        return int(self.svc.db.scalar(
            "SELECT COUNT(*) FROM notifications WHERE status='scheduled' AND send_at <= ?",
            (iso(utcnow() - timedelta(seconds=OVERDUE_SECONDS)),)) or 0)

    def next_in(self) -> float | None:
        """Seconds until the earliest scheduled send time (negative when already due), None when nothing waits."""
        from .db import parse_iso, utcnow
        first = self.svc.db.scalar("SELECT MIN(send_at) FROM notifications WHERE status='scheduled'")
        return (parse_iso(first) - utcnow()).total_seconds() if first else None  # type: ignore[operator]

    def release_due(self) -> int:
        """Release every scheduled notification whose time has come; a bad row never stops the others."""
        from .db import now_iso
        svc = self.svc
        ids = [r["id"] for r in svc.db.query(
            "SELECT id FROM notifications WHERE status='scheduled' AND send_at <= ? ORDER BY send_at", (now_iso(),))]
        for nid in ids:
            try:
                self.release(nid)
            except Exception as e:
                log.exception("scheduled release failed", extra={"notification_id": nid})
                try:
                    with svc.db.tx() as c:
                        n = c.execute("SELECT * FROM notifications WHERE id=? AND status='scheduled'", (nid,)).fetchone()
                        if n is not None:
                            stamp = now_iso()
                            c.execute("UPDATE notifications SET status='failed', released_at=?, release_problem=?,"
                                      " updated_at=? WHERE id=?",
                                      (stamp, f"release error: {type(e).__name__}: {e}", stamp, nid))
                            svc._emit(c, "notification.status_changed", n, None, {"from": "scheduled", "to": "failed"})
                except Exception:
                    log.exception("could not record a failed release", extra={"notification_id": nid})
        if ids:
            svc._after_commit(dispatch=True)
        return len(ids)
