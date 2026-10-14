"""Reply mailbox of the email provider: IMAP over implicit TLS (imaplib, run in worker threads).

`ImapMailbox` is the only code that speaks IMAP. `poll_once` makes one pass: list, judge each message on
its headers, fetch and parse accepted mail off the event loop, store, then move it to Processed or
Quarantine. The database commit always happens before the move, so a crash in between ends as a
duplicate on the next pass, never as a second reply.
"""

from __future__ import annotations

import asyncio
import imaplib
import logging
import re
import sqlite3
import ssl
import threading
from typing import TYPE_CHECKING, Any

from .. import replyparse
from ..replies import Verdict

if TYPE_CHECKING:
    from ..core import NotifyService
    from .email import EmailProvider

log = logging.getLogger("notify.inbound")

MAX_MESSAGE_BYTES = 15728640
SESSION_SECONDS = 120
HEADER_FETCH_BYTES = 65536


class MailboxError(Exception):
    pass


def _q(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ok(resp: tuple[str, Any], what: str) -> Any:
    typ, data = resp
    if typ != "OK":
        raise MailboxError(f"{what} failed: {data!r}"[:300])
    return data


class ImapMailbox:
    def __init__(self, host: str, port: int, user: str, password: str, *, verify_tls: bool = True,
                 inbox: str = "INBOX", processed: str = "Processed", quarantine: str = "Quarantine",
                 timeout: float = SESSION_SECONDS):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.verify_tls = verify_tls
        self.inbox, self.processed, self.quarantine = inbox, processed, quarantine
        self.timeout = timeout
        self._imap: Any = None
        self._timer: threading.Timer | None = None

    @classmethod
    def for_provider(cls, p: EmailProvider) -> ImapMailbox:
        return cls(p.imap_host, p.imap_port, p.imap_user, p.imap_password, verify_tls=p.verify_tls,
                   inbox=p.reply_inbox, processed=p.reply_processed, quarantine=p.reply_quarantine)

    def _kill(self) -> None:
        """Hard session deadline: close the socket so a stalled server cannot hold the poller."""
        sock = getattr(self._imap, "sock", None)
        for op in (lambda: sock.shutdown(2), lambda: sock.close()):
            try:
                op()
            except Exception:
                pass

    def open(self) -> None:
        try:
            self._open()
        except BaseException:
            self.close()
            raise

    def _open(self) -> None:
        ctx = ssl.create_default_context()
        if not self.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._timer = threading.Timer(self.timeout, self._kill)
        self._timer.daemon = True
        self._timer.start()
        self._imap = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ctx, timeout=30)
        self._imap.login(self.user, self.password)
        have = self.folders()
        for f in (self.processed, self.quarantine):
            if f not in have:
                self._imap.create(_q(f))  # result ignored: it may exist with other flags or case
        have = self.folders()
        for f in (self.processed, self.quarantine):
            if f not in have:
                raise MailboxError(f"reply folder {f!r} does not exist and could not be created")
        _ok(self._imap.select(_q(self.inbox)), f"select {self.inbox}")

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None

    def folders(self) -> set[str]:
        out = set()
        for line in _ok(self._imap.list(), "list") or []:
            if isinstance(line, bytes):
                m = re.match(rb'\([^)]*\) (?:"[^"]*"|NIL) (.+)$', line)
                if m:
                    out.add(m.group(1).decode(errors="replace").strip('"'))
        return out

    def list(self) -> list[tuple[bytes, int]]:
        typ, data = self._imap.uid("FETCH", "1:*", "(RFC822.SIZE)")
        if typ != "OK":
            return []  # some servers answer NO on an empty mailbox
        out = []
        for item in data or []:
            line = item[0] if isinstance(item, tuple) else item
            if isinstance(line, bytes):
                u, s = re.search(rb"UID (\d+)", line), re.search(rb"RFC822\.SIZE (\d+)", line)
                if u:
                    out.append((u.group(1), int(s.group(1)) if s else 0))
        return out

    def _fetch(self, uid: bytes, what: str) -> bytes:
        data = _ok(self._imap.uid("FETCH", uid, what), f"fetch {uid!r}")
        part = next((x for x in data or [] if isinstance(x, tuple)), None)
        if part is None:
            raise MailboxError(f"message {uid!r} vanished")
        return part[1]

    def fetch_headers(self, uid: bytes) -> bytes:
        return self._fetch(uid, f"(BODY.PEEK[HEADER]<0.{HEADER_FETCH_BYTES}>)")

    def fetch(self, uid: bytes) -> bytes:
        return self._fetch(uid, "(BODY.PEEK[])")

    def move(self, uid: bytes, folder: str) -> None:
        try:
            if self._imap.uid("MOVE", uid, _q(folder))[0] == "OK":
                return
        except imaplib.IMAP4.error:
            pass  # no MOVE extension
        _ok(self._imap.uid("COPY", uid, _q(folder)), f"copy to {folder}")
        _ok(self._imap.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)"), "flag deleted")
        try:
            _ok(self._imap.uid("EXPUNGE", uid), "expunge")  # UIDPLUS: only this message
        except imaplib.IMAP4.error:
            self._imap.expunge()

    def test(self) -> dict[str, Any]:
        """Admin connection test: log in, check folders, show which authserv-ids the newest message carries."""
        try:
            self.open()
            items = self.list()
            ids: list[str] = []
            if items:
                head = replyparse.parse_headers(self.fetch_headers(items[-1][0]))
                for k, v in head.raw_items():
                    if k.lower() == "authentication-results":
                        ident = str(v).split(";")[0].strip().split(" ")[0]
                        if ident and ident not in ids:
                            ids.append(ident)
            return {"ok": True, "messages": len(items), "folders": sorted(self.folders()), "authserv_ids": ids}
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if len(self.password) >= 4:
                err = err.replace(self.password, "***")
            return {"ok": False, "error": err[:300]}
        finally:
            self.close()


async def poll_once(svc: NotifyService, provider: EmailProvider, mailbox: Any) -> dict[str, int]:
    replies = svc.replies
    stats = {"accepted": 0, "quarantined": 0, "duplicates": 0}
    await asyncio.to_thread(mailbox.open)
    try:
        for uid, size in await asyncio.to_thread(mailbox.list):
            raw_headers = await asyncio.to_thread(mailbox.fetch_headers, uid)
            head = replyparse.parse_headers(raw_headers)
            try:
                verdict = (Verdict("quarantine", "too_large")
                           if size > MAX_MESSAGE_BYTES or size <= 0 or len(raw_headers) >= HEADER_FETCH_BYTES
                           else replies.classify(head, raw_headers, provider.authserv_id))
                if verdict.action == "accept":
                    raw = await asyncio.to_thread(mailbox.fetch, uid)
                    parsed = await asyncio.to_thread(replyparse.parse_reply, raw)
                    # The verdict came from a truncated header fetch: judge the stored message's own headers again.
                    full_headers = raw[:raw.find(b"\r\n\r\n") + 4] if b"\r\n\r\n" in raw else raw
                    del raw
                    full = replyparse.parse_headers(full_headers)
                    again = replies.classify(full, full_headers, provider.authserv_id)
                    same = (again.action == "accept" and again.delivery is not None and verdict.delivery is not None
                            and again.delivery["id"] == verdict.delivery["id"]
                            and replyparse.from_address(full) == replyparse.from_address(head))
                    if not same:
                        head = full
                        verdict = Verdict("quarantine", "header_mismatch", verdict.delivery, verdict.notification)
                    else:
                        try:
                            replies.store(again, replyparse.message_key(full, full_headers), parsed)
                        except sqlite3.IntegrityError:
                            verdict = Verdict("duplicate")
            except (MailboxError, OSError, imaplib.IMAP4.error):
                raise  # connection trouble: stop this pass, retry later
            except sqlite3.Error:
                raise  # transient database trouble (locked, busy): retry the pass; duplicates were handled above
            except Exception:
                log.exception("reply could not be read", extra={"uid": uid.decode(errors="replace")})
                verdict = Verdict("quarantine", "unreadable")
            if verdict.action == "quarantine":
                replies.quarantine(head, verdict)
                stats["quarantined"] += 1
            else:
                stats["accepted" if verdict.action == "accept" else "duplicates"] += 1
            folder = mailbox.quarantine if verdict.action == "quarantine" else mailbox.processed
            await asyncio.to_thread(mailbox.move, uid, folder)
    finally:
        await asyncio.to_thread(mailbox.close)
    return stats
