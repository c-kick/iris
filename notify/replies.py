"""Replies to notifications: match inbound mail to a delivery, store it, hand it to a waiting agent,
and answer replies nobody collects. Provider-agnostic: the email inbound poller feeds it.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import timedelta
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any, Literal

from . import replyparse
from .handles import handle_matches
from .db import dumps, iso, loads, new_id, parse_iso, utcnow
from .models import DELIVERED, DISPATCHED, INTERACTED, NotificationIn

if TYPE_CHECKING:
    from .core import Client, NotifyService

log = logging.getLogger("notify.replies")

LISTEN_SECONDS = 120
GRACE_SECONDS = 600
AUTOREPLY_EVERY_SECONDS = 3600


def conversation_gate(c: sqlite3.Connection, conv: str, handle: str | None, *,
                      internal: bool = False) -> sqlite3.Row | None:
    """The conversation row, or None for a conversation that never had replies. Raises NotFound (fail closed) for a
    reply conversation without a row, and for a handle that is missing or wrong once the conversation has one.
    Legacy rows pass; callers apply the old token rule to them."""
    from .core import NotFound
    row = c.execute("SELECT * FROM conversations WHERE id=?", (conv,)).fetchone()
    if row is None:
        if c.execute("SELECT 1 FROM notifications WHERE conversation_id=? AND reply_wanted=1 LIMIT 1",
                     (conv,)).fetchone():
            raise NotFound("notification not found")
        return None
    if row["handle_sha256"] and not internal and not handle_matches(handle, conv, row["handle_sha256"]):
        raise NotFound("notification not found")
    return row


@dataclass
class Verdict:
    action: Literal["accept", "quarantine", "duplicate"]
    reason: str | None = None
    delivery: sqlite3.Row | None = None
    notification: sqlite3.Row | None = None


class Replies:
    def __init__(self, service: NotifyService):
        self.svc = service
        self.now = utcnow  # seam: tests move the clock

    # ------------------------------------------------------------ inbound
    def classify(self, msg: EmailMessage, raw_headers: bytes, authserv_id: str,
                 provider: str | None = None) -> Verdict:
        """Spec §3 steps 2-6 on the header block only; the body is fetched only for an accept. `provider` is the
        email provider whose mailbox is polled."""
        if replyparse.header_block_problem(raw_headers):
            return Verdict("quarantine", "malformed_headers")
        if replyparse.is_automatic(msg):
            return Verdict("quarantine", "automatic")
        db = self.svc.db
        sender = replyparse.from_address(msg)
        d, n = self._thread_match(replyparse.thread_delivery_ids(msg), sender, provider)
        if d is None:
            return Verdict("quarantine", "no_thread")
        if n is None or not n["reply_wanted"]:
            return Verdict("quarantine", "not_reply_enabled", d, n)
        if not sender or sender != (d["recipient_address"] or "").strip().lower():
            return Verdict("quarantine", "sender_mismatch", d, n)
        if not replyparse.dkim_passes(msg, authserv_id, sender.rpartition("@")[2]):
            return Verdict("quarantine", "dkim_fail", d, n)
        if db.scalar("SELECT 1 FROM replies WHERE message_id=?", (replyparse.message_key(msg, raw_headers),)):
            return Verdict("duplicate", None, d, n)
        return Verdict("accept", None, d, n)

    def _thread_match(self, ids: list[str], sender: str,
                      provider: str | None) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
        """The delivery this mail answers. `ids` come from one References chain, newest first: take the newest
        delivery that accepts this reply (reply-enabled, sent to this sender, through the polled provider), so an
        answer to a non-reply mail in a reply thread (an autoreply, a reply:false follow-up) still finds the
        conversation. When none qualifies, the newest known delivery decides the quarantine reason."""
        db = self.svc.db
        first: tuple[sqlite3.Row | None, sqlite3.Row | None] = (None, None)
        for did in ids:
            d = db.one("SELECT * FROM deliveries WHERE id=?", (did,))
            if d is None:
                continue
            n = db.one("SELECT * FROM notifications WHERE id=?", (d["notification_id"],))
            if first[0] is None:
                first = (d, n)
            if (n is not None and n["reply_wanted"] and sender
                    and sender == (d["recipient_address"] or "").strip().lower()
                    and (provider is None or d["provider"] == provider)):
                return d, n
        return first

    def quarantine(self, msg: EmailMessage, verdict: Verdict, key: str | None = None) -> bool:
        """Record a rejected message once: a message already recorded under the same key (its Message-ID, else a
        hash of its headers) is not recorded or announced again. Returns whether a row was added."""
        n, d = verdict.notification, verdict.delivery
        key = (key if key is not None else replyparse.header(msg, "Message-ID").strip())[:300]
        with self.svc.db.tx() as c:
            if key and c.execute("SELECT 1 FROM reply_quarantine WHERE message_id=? LIMIT 1", (key,)).fetchone():
                return False
            c.execute("INSERT INTO reply_quarantine(received_at, from_address, subject, message_id, reason,"
                      " notification_id) VALUES (?,?,?,?,?,?)",
                      (iso(self.now()), replyparse.from_address(msg)[:320], replyparse.header(msg, "Subject")[:300],
                       key, verdict.reason, n["id"] if n is not None else None))
            if n is not None:
                self.svc._emit(c, "reply.quarantined", n, d["id"] if d is not None else None, {"reason": verdict.reason})
        self.svc._after_commit()
        log.warning("reply quarantined", extra={"reason": verdict.reason,
                                                "notification_id": n["id"] if n is not None else None})
        return True

    def store(self, verdict: Verdict, key: str, p: replyparse.ParsedReply) -> str:
        d, n = verdict.delivery, verdict.notification
        assert d is not None and n is not None
        rid = new_id("rpl")
        with self.svc.db.tx() as c:
            c.execute("INSERT INTO replies(id, conversation_id, notification_id, delivery_id, message_id, from_address,"
                      " subject, received_at, text, stripped, truncated, dropped_attachments_json, state)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, n["conversation_id"] or n["id"], n["id"], d["id"], key, p.from_address, p.subject,
                       iso(self.now()), p.text, int(p.stripped), int(p.truncated), dumps(p.dropped), "waiting"))
            c.executemany("INSERT INTO reply_attachments(reply_id, idx, filename, content_type, size, sha256, data)"
                          " VALUES (?,?,?,?,?,?,?)",
                          [(rid, i, a.filename, a.content_type, len(a.data), hashlib.sha256(a.data).hexdigest(), a.data)
                           for i, a in enumerate(p.attachments)])
            self.svc._emit(c, "reply.received", n, d["id"], {"reply_id": rid, "attachments": len(p.attachments)})
        self.svc._after_commit()
        return rid

    # ------------------------------------------------------------ agents
    def _access(self, client: Client, nid: str, handle: str | None) -> sqlite3.Row:
        """The one gate for claim, list and attachment download. Runs before anything else, so a wrong handle
        can't refresh `listener_seen_at`. Returns the notification row; every refusal is the same NotFound."""
        from .core import NotFound
        svc = self.svc
        if ":" in nid:  # a handle pasted where an id goes; never echoed
            raise NotFound("notification not found")
        try:
            n = svc._notification_row(client, nid)
        except NotFound:
            # the root may be gone (retention) while the conversation lives on: fall back to the newest notification
            # of it this client may see (never the service's own autoreplies)
            n = svc.db.one("SELECT * FROM notifications WHERE conversation_id=? AND auto_submitted=0"
                           " AND (?=1 OR client_id=?) ORDER BY created_at DESC, id DESC LIMIT 1",
                           (nid, int(client.admin), client.id))
            if n is None:
                raise NotFound("notification not found") from None
        with svc.db._lock:
            row = conversation_gate(svc.db.conn, n["conversation_id"] or n["id"], handle)
        if row is None or (row["legacy"] and client.id != n["client_id"]):  # legacy: the sending token only
            raise NotFound("notification not found")
        return n

    async def claim(self, client: Client, nid: str, waiter: str, wait: float,
                    handle: str | None = None) -> dict[str, Any] | None:
        """Long-poll: the reply this waiter already holds, else the oldest waiting reply of the conversation,
        else the oldest late-collectable unclaimed one."""
        n = self._access(client, nid, handle)
        conv = n["conversation_id"] or n["id"]
        self._gone_if_dead(conv)
        deadline = time.monotonic() + max(0.0, min(wait, 55.0))
        while True:
            got = self._claim_once(client, conv, waiter)
            if got is not None:
                return self._out(got)
            left = deadline - time.monotonic()
            if left <= 0:
                self._gone_if_dead(conv)
                return None
            await self.svc.bus.wait(min(left, 5.0))  # every commit (including a stored reply) wakes it

    def _gone_if_dead(self, conv: str) -> None:
        from .core import Gone
        reason = self.dead_reason(conv)
        if reason:
            raise Gone(reason)

    def dead_reason(self, conv: str) -> str | None:
        """Why no reply can ever come to this conversation, or None while one still can: nothing of it is
        scheduled, being sent or dispatched, and no reply waits to be collected."""
        db = self.svc.db
        if not db.scalar("SELECT 1 FROM notifications WHERE conversation_id=? OR id=? LIMIT 1", (conv, conv)):
            return None
        if db.scalar("SELECT 1 FROM replies WHERE conversation_id=? AND state IN ('waiting','unclaimed') LIMIT 1",
                     (conv,)):
            return None
        if db.scalar("SELECT 1 FROM notifications n WHERE (n.conversation_id=? OR n.id=?) AND (n.status='scheduled'"
                     " OR EXISTS (SELECT 1 FROM deliveries d WHERE d.notification_id=n.id AND"
                     " (d.dispatched_at IS NOT NULL OR d.state IN ('queued','sending')))) LIMIT 1", (conv, conv)):
            return None
        root = db.one("SELECT status, release_problem FROM notifications WHERE id=?", (conv,))
        if root is None:
            root = db.one("SELECT status, release_problem FROM notifications WHERE conversation_id=?"
                          " ORDER BY created_at, id LIMIT 1", (conv,))
        if root["status"] == "cancelled":
            return "notification was cancelled"
        if root["status"] == "expired" and (root["release_problem"] or "").startswith("missed"):
            return "notification was missed"
        return "notification was not sent"

    def _claim_once(self, client: Client, conv: str, waiter: str) -> sqlite3.Row | None:
        now = iso(self.now())
        with self.svc.db.tx() as c:
            c.execute("UPDATE notifications SET listener_seen_at=? WHERE id=? OR conversation_id=?", (now, conv, conv))
            r = c.execute("SELECT * FROM replies WHERE conversation_id=? AND state='claimed' AND waiter_id=?"
                          " ORDER BY received_at, id LIMIT 1", (conv, waiter)).fetchone()
            if r is not None:
                return r
            r = c.execute("SELECT * FROM replies WHERE conversation_id=? AND state='waiting'"
                          " ORDER BY received_at, id LIMIT 1", (conv,)).fetchone()
            if r is None:  # a late reply (swept while nobody listened) of a non-legacy conversation
                r = c.execute("SELECT * FROM replies WHERE conversation_id=? AND state='unclaimed' AND late_ok=1"
                              " ORDER BY received_at, id LIMIT 1", (conv,)).fetchone()
            if r is None:
                return None
            c.execute("UPDATE replies SET state='claimed', claimed_at=?, claimed_by=?, waiter_id=? WHERE id=?",
                      (now, client.id, waiter, r["id"]))
            n = c.execute("SELECT * FROM notifications WHERE id=?", (r["notification_id"],)).fetchone()
            self.svc._emit(c, "reply.claimed", n, r["delivery_id"], {"reply_id": r["id"], "client": client.id})
            d = c.execute("SELECT * FROM deliveries WHERE id=?", (r["delivery_id"],)).fetchone()
            if d is not None and d["state"] in (DISPATCHED, DELIVERED):
                self.svc._transition(c, d, INTERACTED, {"interacted_at": now,
                                                        "interaction_json": dumps({"reply_id": r["id"]})},
                                     {"reply_id": r["id"]})
            r = c.execute("SELECT * FROM replies WHERE id=?", (r["id"],)).fetchone()
        self.svc._after_commit()
        return r

    def list(self, client: Client, nid: str, handle: str | None = None) -> list[dict[str, Any]]:
        n = self._access(client, nid, handle)
        rows = self.svc.db.query("SELECT * FROM replies WHERE conversation_id=? ORDER BY received_at, id",
                                 (n["conversation_id"] or n["id"],))
        out = [self._out(r) for r in rows]
        for o in out:  # content of a reply nobody claimed is never handed out, handle or not
            if o["state"] != "claimed":
                o.update(subject=None, text=None, attachments=[], dropped_attachments=[])
        return out

    def attachment(self, client: Client, rid: str, idx: int, handle: str | None = None) -> tuple[str, str, bytes]:
        from .core import NotFound
        r = self.svc.db.one("SELECT conversation_id, state FROM replies WHERE id=?", (rid,))
        if r is None or r["state"] != "claimed":
            raise NotFound(f"reply {rid} not found")
        self._access(client, r["conversation_id"], handle)
        a = self.svc.db.one("SELECT filename, content_type, data FROM reply_attachments WHERE reply_id=? AND idx=?",
                            (rid, idx))
        if a is None or a["data"] is None:
            raise NotFound(f"attachment {idx} of reply {rid} not found (or purged)")
        return a["filename"], a["content_type"], a["data"]

    def _out(self, r: sqlite3.Row) -> dict[str, Any]:
        n = self.svc.db.one("SELECT title FROM notifications WHERE id=?", (r["notification_id"],))
        atts = self.svc.db.query("SELECT idx, filename, content_type, size, purged_at FROM reply_attachments"
                                 " WHERE reply_id=? ORDER BY idx", (r["id"],))
        return {
            "id": r["id"], "conversation_id": r["conversation_id"], "notification_id": r["notification_id"],
            "notification_title": n["title"] if n is not None else None, "from": r["from_address"],
            "subject": r["subject"], "received_at": r["received_at"], "state": r["state"], "text": r["text"],
            "stripped": bool(r["stripped"]), "truncated": bool(r["truncated"]),
            "dropped_attachments": loads(r["dropped_attachments_json"], []), "claimed_at": r["claimed_at"],
            "claimed_by": r["claimed_by"], "autoreply_notification_id": r["autoreply_notification_id"],
            "late": bool(r["late_ok"]),
            "attachments": [{"idx": a["idx"], "filename": a["filename"], "content_type": a["content_type"],
                             "size": a["size"], "purged": a["purged_at"] is not None,
                             "url": f"/v1/replies/{r['id']}/attachments/{a['idx']}"} for a in atts],
        }

    # ------------------------------------------------------------ unclaimed
    def sweep_unclaimed(self) -> int:
        now = self.now()
        rows = self.svc.db.query(
            "SELECT r.* FROM replies r WHERE r.state='waiting' AND r.received_at <= ?"
            " AND COALESCE((SELECT MAX(listener_seen_at) FROM notifications"
            " WHERE id=r.conversation_id OR conversation_id=r.conversation_id), '') < ? ORDER BY r.received_at",
            (iso(now - timedelta(seconds=GRACE_SECONDS)), iso(now - timedelta(seconds=LISTEN_SECONDS))))
        swept = 0
        for r in rows:
            with self.svc.db.tx() as c:
                cur = c.execute("SELECT state FROM replies WHERE id=?", (r["id"],)).fetchone()
                if cur is None or cur["state"] != "waiting":
                    continue
                seen = c.execute("SELECT MAX(listener_seen_at) FROM notifications WHERE id=:c OR conversation_id=:c",
                                 {"c": r["conversation_id"]}).fetchone()
                if seen is not None and seen[0] and seen[0] >= iso(now - timedelta(seconds=LISTEN_SECONDS)):
                    continue  # someone started listening since the query
                conv = c.execute("SELECT legacy FROM conversations WHERE id=?", (r["conversation_id"],)).fetchone()
                late_ok = int(conv is not None and not conv["legacy"])  # legacy replies are never handed out late
                c.execute("UPDATE replies SET state='unclaimed', late_ok=? WHERE id=?", (late_ok, r["id"]))
                n = c.execute("SELECT * FROM notifications WHERE id=?", (r["notification_id"],)).fetchone()
                self.svc._emit(c, "reply.unclaimed", n, r["delivery_id"], {"reply_id": r["id"]})
            swept += 1
            try:
                self._autoreply(r)
            except Exception:
                log.exception("autoreply failed", extra={"reply_id": r["id"]})
        if swept:
            self.svc._after_commit()
        return swept

    def _autoreply(self, r: sqlite3.Row) -> None:
        from .core import SYSTEM_CLIENT, Invalid
        db = self.svc.db
        last = db.scalar("SELECT MAX(n.created_at) FROM replies x JOIN notifications n"
                         " ON n.id=x.autoreply_notification_id WHERE x.conversation_id=?", (r["conversation_id"],))
        if last and parse_iso(last) > self.now() - timedelta(seconds=AUTOREPLY_EVERY_SECONDS):  # type: ignore[operator]
            return
        d = db.one("SELECT device FROM deliveries WHERE id=?", (r["delivery_id"],))
        n = db.one("SELECT title FROM notifications WHERE id=?", (r["notification_id"],))
        if d is None or n is None:
            return
        received = parse_iso(r["received_at"]).strftime("%Y-%m-%d %H:%M UTC")  # type: ignore[union-attr]
        legacy = db.scalar("SELECT legacy FROM conversations WHERE id=?", (r["conversation_id"],))
        text = (f"This agent is no longer listening, so your reply was not delivered. Received {received}."
                if legacy != 0 else
                f"The agent isn't listening right now. Your reply has been kept and the agent can still pick it up"
                f" later. Received {received}.")
        req = NotificationIn(
            targets=[f"device:{d['device']}"], kinds=["email"], in_reply_to=r["notification_id"],
            title=f"Re: {n['title'][:990]}" if n["title"] else None, source="hnl-notify/replies",
            message=text,
            tags=["reply-autoreply"], data={"reply_id": r["id"]})
        req._auto_submitted = True
        req._internal = True
        try:
            out, _ = self.svc.submit(SYSTEM_CLIENT, req)
        except Invalid as e:
            log.warning("autoreply not sent", extra={"reply_id": r["id"], "error": str(e)})
            return
        with db.tx() as c:
            c.execute("UPDATE replies SET autoreply_notification_id=? WHERE id=?", (out["id"], r["id"]))

    # ------------------------------------------------------------ admin
    def recent(self, limit: int = 25) -> list[dict[str, Any]]:
        return [self._out(r) for r in self.svc.db.query("SELECT * FROM replies ORDER BY received_at DESC LIMIT ?",
                                                         (max(1, min(limit, 100)),))]

    def quarantined(self, limit: int = 25) -> list[dict[str, Any]]:
        return [dict(r) for r in self.svc.db.query("SELECT * FROM reply_quarantine ORDER BY id DESC LIMIT ?",
                                                    (max(1, min(limit, 100)),))]
