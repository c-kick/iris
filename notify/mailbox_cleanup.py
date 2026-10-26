"""Reply mailbox cleanup: delete old mail from the Processed and Quarantine folders.

`CleanupMailbox` is its own IMAP entry point next to `ImapMailbox` (which creates folders and selects INBOX
read-write): it never creates a folder, never selects the poller's folder, and only issues STORE/EXPUNGE in the
one folder it selected read-write itself. `run_pass` drives it; it holds the provider's lock per step (open, scan,
one batch) so a poll never waits longer than one batch. Sizes and dates only: no message body is fetched.
"""

from __future__ import annotations

import asyncio
import imaplib
import re
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from .db import iso
from .providers.email_inbound import SESSION_SECONDS, MailboxError, _ok, _q, mask_secret

if TYPE_CHECKING:
    from .config import MailboxCleanupConfig
    from .providers.email import EmailProvider

BATCH = 200
MIB = 1048576
SPECIAL_USE = frozenset({"\\sent", "\\trash", "\\junk", "\\all", "\\archive", "\\drafts", "\\flagged"})
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DATE_RE = re.compile(r"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d\d):(\d\d):(\d\d) ([+-])(\d\d)(\d\d)")


@dataclass(frozen=True)
class Msg:
    uid: bytes
    size: int
    arrived: datetime


def parse_internaldate(value: str) -> datetime:
    """'14-Oct-2026 09:13:12 +0200' -> aware UTC datetime. Month names are mapped here, so no locale is involved."""
    m = _DATE_RE.fullmatch(value)
    if not m or m.group(2).lower() not in _MONTHS:
        raise ValueError(f"unreadable INTERNALDATE {value!r}")
    d, mon, y, hh, mm, ss, sign, oh, om = m.groups()
    off = timezone(timedelta(hours=int(oh), minutes=int(om)) * (-1 if sign == "-" else 1))
    return datetime(int(y), _MONTHS[mon.lower()], int(d), int(hh), int(mm), int(ss), tzinfo=off).astimezone(timezone.utc)


def select(msgs: list[Msg], now: datetime, days: int, cap_bytes: int) -> tuple[list[bytes], list[bytes]]:
    """(by_age, by_cap) UIDs. Age first; then, over the rest, the oldest go until the total is at or under the cap.
    0 days / 0 bytes = that rule off. Oldest means INTERNALDATE, then UID."""
    ordered = sorted(msgs, key=lambda m: (m.arrived, int(m.uid)))
    aged = [m for m in ordered if days and m.arrived < now - timedelta(days=days)]
    gone = {m.uid for m in aged}
    rest = [m for m in ordered if m.uid not in gone]
    by_cap: list[bytes] = []
    if cap_bytes:
        total = sum(m.size for m in rest)
        for m in rest:
            if total <= cap_bytes:
                break
            by_cap.append(m.uid)
            total -= m.size
    return [m.uid for m in aged], by_cap


class CleanupMailbox:
    def __init__(self, host: str, port: int, user: str, password: str, *, verify_tls: bool = True,
                 timeout: float = SESSION_SECONDS, imap_factory: Any = imaplib.IMAP4_SSL, inbox: str = "INBOX"):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.inbox = inbox  # the poller's folder: never deleted from
        self.verify_tls, self.timeout, self._factory = verify_tls, timeout, imap_factory
        self._imap: Any = None
        self._timer: threading.Timer | None = None
        self._selected: str | None = None  # set by an OK read-write SELECT; cleared by EXAMINE or any failure
        self.caps: set[str] = set()
        self.folders: dict[str, set[str]] = {}
        self.uidvalidity: bytes | None = None

    @classmethod
    def for_provider(cls, p: EmailProvider, **kw: Any) -> CleanupMailbox:
        return cls(p.imap_host, p.imap_port, p.imap_user, p.imap_password, verify_tls=p.verify_tls,
                   inbox=p.reply_inbox, **kw)

    def _arm(self) -> None:
        """(Re)start the hard session deadline: closing the socket stops a stalled server from holding a thread."""
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.timeout, self._kill)
        self._timer.daemon = True
        self._timer.start()

    def _kill(self) -> None:
        sock = getattr(self._imap, "sock", None)
        for op in (lambda: sock.shutdown(2), lambda: sock.close()):
            try:
                op()
            except Exception:
                pass

    def open(self) -> None:
        try:
            self._open()
        except BaseException as e:
            self.close()
            if isinstance(e, Exception):
                raise MailboxError(mask_secret(f"{type(e).__name__}: {e}", self.password)[:300]) from None
            raise

    def _open(self) -> None:
        ctx = ssl.create_default_context()
        if not self.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._arm()
        self._imap = self._factory(self.host, self.port, ssl_context=ctx, timeout=30)
        self._imap.login(self.user, self.password)
        # CAPABILITY after login: imaplib's `.capabilities` is filled before login and can lack UIDPLUS
        data = _ok(self._imap.capability(), "capability")
        self.caps = set(data[0].decode(errors="replace").upper().split()) if data and data[0] else set()
        self.folders = self._list()

    def _list(self) -> dict[str, set[str]]:
        if "SPECIAL-USE" in self.caps:
            _ok(self._imap.xatom("LIST", '""', '"*"', "RETURN", "(SPECIAL-USE)"), "list")
            lines = self._imap.response("LIST")[1]  # untagged LIST lines gathered by xatom
        else:
            lines = _ok(self._imap.list(), "list")
        out: dict[str, set[str]] = {}
        for item in lines or []:
            raw = item[0] + b" " + item[1] if isinstance(item, tuple) else item
            m = re.match(rb'\(([^)]*)\) (?:"[^"]*"|NIL) (.+)$', raw) if isinstance(raw, bytes) else None
            if m:
                name = m.group(2).decode(errors="replace").strip().strip('"')
                out[name] = {a.lower() for a in m.group(1).decode(errors="replace").split()}
        return out

    def _attrs(self, name: str) -> set[str] | None:
        for k, v in self.folders.items():
            if k.lower() == name.lower():
                return v
        return None

    def refusal(self, inbox: str, processed: str, quarantine: str) -> str | None:
        names = [inbox.lower(), processed.lower(), quarantine.lower()]
        if names[1] == names[0] or names[2] == names[0]:
            return "a cleanup folder is the folder the poller reads"
        if names[1] == names[2]:
            return "the processed and quarantine folders are the same folder"
        for f in (processed, quarantine):
            attrs = self._attrs(f)
            if attrs is None:
                return f"folder {f!r} does not exist on the server"
            if attrs & SPECIAL_USE:
                return f"folder {f!r} is marked {', '.join(sorted(attrs & SPECIAL_USE))} by the server"
        return None

    def scan(self, folder: str, *, readonly: bool) -> list[Msg]:
        """UID, size and arrival date of every message. EXAMINE when readonly; else SELECT, kept selected."""
        self._selected = None
        _ok(self._imap.select(_q(folder), readonly), f"select {folder}")
        if not readonly:
            self._selected = folder
        resp = self._imap.response("UIDVALIDITY")
        self.uidvalidity = resp[1][0] if resp and resp[1] else None
        typ, data = self._imap.uid("FETCH", "1:*", "(RFC822.SIZE INTERNALDATE)")
        if typ != "OK":
            return []  # some servers answer NO on an empty folder
        out = []
        for item in data or []:
            line = item[0] if isinstance(item, tuple) else item
            if isinstance(line, bytes):
                u, s = re.search(rb"UID (\d+)", line), re.search(rb"RFC822\.SIZE (\d+)", line)
                d = re.search(rb'INTERNALDATE "([^"]+)"', line)
                if u and d:
                    out.append(Msg(u.group(1), int(s.group(1)) if s else 0, parse_internaldate(d.group(1).decode())))
        return out

    def reselect(self, folder: str, uidvalidity: bytes | None) -> bool:
        """A new session for the same folder; False when the server renumbered it meanwhile."""
        self.close()
        self.open()
        self.scan(folder, readonly=False)
        return uidvalidity is not None and self.uidvalidity == uidvalidity  # unknown numbering: fail closed

    def delete_batch(self, folder: str, uids: list[bytes]) -> int:
        if self._selected != folder or folder.lower() in ("inbox", self.inbox.lower()):
            raise MailboxError("refusing to delete: the target folder is not the selected read-write folder")
        self._arm()
        try:
            uidset = b",".join(uids).decode()
            _ok(self._imap.uid("STORE", uidset, "+FLAGS.SILENT", "(\\Deleted)"), "flag deleted")
            if "UIDPLUS" in self.caps:
                _ok(self._imap.uid("EXPUNGE", uidset), "expunge")
            else:
                _ok(self._imap.expunge(), "expunge")
        except BaseException:
            self._selected = None
            raise
        return len(uids)

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None
        self._selected = None


async def run_pass(open_mailbox: Callable[[], CleanupMailbox], lock: asyncio.Lock, provider: EmailProvider,
                   cfg: MailboxCleanupConfig, *, now: datetime, dry_run: bool,
                   deadline: float | None = None) -> dict[str, Any]:
    """One pass over Processed and Quarantine. The lock is held per step, never across a pass."""
    async def locked(fn: Callable[..., Any], *args: Any) -> Any:
        async with lock:
            t = asyncio.ensure_future(asyncio.to_thread(fn, *args))
            try:
                return await asyncio.shield(t)
            except asyncio.CancelledError:
                await asyncio.wait({t})  # the lock goes only after the thread is done with the session
                raise

    result: dict[str, Any] = {"refused": None, "folders": {}}
    mb = open_mailbox()
    mb.inbox = provider.reply_inbox
    try:
        await locked(mb.open)
        result["refused"] = mb.refusal(provider.reply_inbox, provider.reply_processed, provider.reply_quarantine)
        if result["refused"]:
            return result
        for folder, days, mib in ((provider.reply_processed, cfg.processed_days, cfg.processed_max_mib),
                                  (provider.reply_quarantine, cfg.quarantine_days, cfg.quarantine_max_mib)):
            st: dict[str, Any] = {"messages": 0, "bytes": 0, "oldest": None, "removed_age": 0, "removed_cap": 0,
                                  "removed_bytes": 0, "after_messages": 0, "after_bytes": 0, "cap_hit": False,
                                  "unfinished": False, "error": None}
            result["folders"][folder] = st
            if not dry_run and result["folders"].keys() - {folder} and deadline is not None \
                    and time.monotonic() >= deadline:
                st["unfinished"] = True  # out of time before this folder
                continue
            try:
                await _clean_folder(locked, mb, folder, days, mib * MIB, st, now, dry_run, deadline)
            except Exception as e:  # one folder failing keeps what the other already did
                st["error"] = mask_secret(f"{type(e).__name__}: {e}", mb.password)[:300]
                st["unfinished"] = True
            st["after_messages"] = st["messages"] - st["removed_age"] - st["removed_cap"]
            st["after_bytes"] = st["bytes"] - st["removed_bytes"]
        return result
    finally:
        await asyncio.to_thread(mb.close)


async def _clean_folder(locked: Callable[..., Any], mb: CleanupMailbox, folder: str, days: int, cap_bytes: int,
                        st: dict[str, Any], now: datetime, dry_run: bool, deadline: float | None) -> None:
    msgs = await locked(lambda: mb.scan(folder, readonly=dry_run))
    uidv = mb.uidvalidity
    by_age, by_cap = select(msgs, now, days, cap_bytes)
    sizes = {m.uid: m.size for m in msgs}
    st.update(messages=len(msgs), bytes=sum(sizes.values()), cap_hit=bool(by_cap),
              oldest=iso(min((m.arrived for m in msgs), default=None)))
    todo = by_age + by_cap
    if dry_run:
        st.update(removed_age=len(by_age), removed_cap=len(by_cap), removed_bytes=sum(sizes[u] for u in todo))
        return
    aged = set(by_age)
    for i in range(0, len(todo), BATCH):
        if i and deadline is not None and time.monotonic() >= deadline:
            st["unfinished"] = True
            return
        batch = todo[i:i + BATCH]

        def step() -> bool:
            for attempt in (0, 1):
                try:
                    if mb._selected != folder and not mb.reselect(folder, uidv):
                        return False
                    mb.delete_batch(folder, batch)
                    return True
                except (OSError, imaplib.IMAP4.error, MailboxError):
                    if attempt:
                        raise
                    mb._selected = None  # the session may have dropped (kill timer): reopen once, then retry
            return False
        if not await locked(step):
            st["unfinished"] = True  # UIDVALIDITY changed: the scan no longer describes the folder
            return
        n_age = sum(1 for u in batch if u in aged)
        st["removed_age"] += n_age
        st["removed_cap"] += len(batch) - n_age
        st["removed_bytes"] += sum(sizes[u] for u in batch)
