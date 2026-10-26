"""Mailbox cleanup: selection rules and the read-only-by-default IMAP entry point, against an imaplib-level fake."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from notify.config import MailboxCleanupConfig
from notify.mailbox_cleanup import (BATCH, MIB, CleanupMailbox, Msg, parse_internaldate, run_pass, select)
from notify.providers.email_inbound import MailboxError

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def msg(uid: int, days_old: float, size: int = 10) -> Msg:
    return Msg(str(uid).encode(), size, NOW - timedelta(days=days_old))


def u(*ids: int) -> list[bytes]:
    return [str(i).encode() for i in ids]


@pytest.mark.parametrize("msgs,days,cap,age,capped", [
    ([msg(1, 40), msg(2, 5)], 30, 0, [1], []),                                  # age only
    ([msg(1, 3, 60), msg(2, 2, 60), msg(3, 1, 60)], 0, 100, [], [1, 2]),       # cap only: oldest first until <= cap
    ([msg(1, 3, 60), msg(2, 2, 60)], 0, 60, [], [1]),
    ([msg(5, 2, 50), msg(4, 2, 50), msg(6, 2, 50)], 0, 100, [], [4]),          # equal dates: lower UID first
    ([msg(1, 99, 10), msg(2, 1, 10)], 0, 0, [], []),                           # both rules off
    ([msg(1, 3, 50), msg(2, 2, 50)], 0, 100, [], []),                           # exactly at the cap
    ([msg(1, 5, 500), msg(2, 2, 10), msg(3, 1, 10)], 0, 100, [], [1]),          # oldest bigger than the cap goes alone
    ([msg(1, 40, 500), msg(2, 2, 60), msg(3, 1, 30)], 30, 100, [1], []),       # age-removed bytes don't count to the cap
])
def test_select(msgs, days, cap, age, capped):
    assert select(msgs, NOW, days, cap) == (u(*age), u(*capped))


@pytest.mark.parametrize("value,expected", [
    ("14-Oct-2026 09:13:12 +0200", datetime(2026, 10, 14, 7, 13, 12, tzinfo=timezone.utc)),
    ("14-Oct-2026 09:13:12 -0700", datetime(2026, 10, 14, 16, 13, 12, tzinfo=timezone.utc)),
    (" 4-Oct-2026 09:13:12 +0200", datetime(2026, 10, 4, 7, 13, 12, tzinfo=timezone.utc)),
    ("31-Dec-2026 23:30:00 -0100", datetime(2027, 1, 1, 0, 30, tzinfo=timezone.utc)),
])
def test_parse_internaldate(value, expected):
    got = parse_internaldate(value)
    assert got == expected and got.utcoffset() == timedelta(0)


# ----------------------------------------------------------------------------------------------- fake server

def date_for(dt: datetime) -> str:
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"{dt.day:2d}-{months[dt.month - 1]}-{dt.year} {dt:%H:%M:%S} +0000"


class FakeServer:
    """Shared state behind FakeIMAP sessions: folders with messages, capabilities, and a log of mutations."""

    def __init__(self, uidplus=True, special_use=True):
        self.uidplus, self.special_use = uidplus, special_use
        self.attrs = {"INBOX": [], "Processed": [], "Quarantine": [], "Sent Items": ["\\Sent"], "Old Mail": []}
        self.mail: dict[str, list[dict]] = {k: [] for k in self.attrs}
        self.uidvalidity = 7
        self.log: list[tuple] = []  # (selected folder, readonly, command)
        self.sessions: list[FakeIMAP] = []
        self.login_error = None
        self.fetch_no = False
        self.drop_after_expunges = None  # after this many expunges the current connection dies
        self.expunges = 0
        self.uidvalidity_after_drop = None

    def add(self, folder, uid, days_old, size=10):
        self.mail[folder].append({"uid": uid, "size": size, "date": NOW - timedelta(days=days_old), "deleted": False})

    def factory(self, host, port, ssl_context=None, timeout=None):
        s = FakeIMAP(self)
        self.sessions.append(s)
        return s


class FakeIMAP:
    def __init__(self, server):
        self.srv = server
        self.sock = SimpleNamespace(shutdown=lambda h: None, close=lambda: None)
        self.capabilities = ("IMAP4REV1",)  # what imaplib knows before login: no UIDPLUS
        self.selected = None
        self.readonly = False
        self.untagged = {}
        self.dead = False

    def _alive(self):
        if self.dead:
            raise OSError("connection reset by peer")

    def _expunged(self):
        self.srv.expunges += 1
        if self.srv.expunges == self.srv.drop_after_expunges:
            self.dead = True
            if self.srv.uidvalidity_after_drop:
                self.srv.uidvalidity = self.srv.uidvalidity_after_drop

    def login(self, user, password):
        if self.srv.login_error:
            import imaplib
            raise imaplib.IMAP4.error(self.srv.login_error.replace("PW", password))
        return ("OK", [b"ok"])

    def capability(self):
        caps = ["IMAP4REV1"] + (["UIDPLUS"] if self.srv.uidplus else []) + (["SPECIAL-USE"] if self.srv.special_use else [])
        return ("OK", [" ".join(caps).encode()])

    def _lines(self, with_attrs):
        out = []
        for name, attrs in self.srv.attrs.items():
            a = " ".join(["\\HasNoChildren"] + (attrs if with_attrs else []))
            q = f'"{name}"' if " " in name else name
            out.append(f'({a}) "." {q}'.encode())
        return out

    def list(self):
        return ("OK", self._lines(False))

    def xatom(self, name, *args):
        self.srv.log.append((self.selected, self.readonly, "LIST " + " ".join(args)))
        self.untagged["LIST"] = self._lines(True)
        return ("OK", [b"done"])

    def response(self, code):
        if code == "UIDVALIDITY":
            return (code, [str(self.srv.uidvalidity).encode()])
        return (code, self.untagged.pop(code, [None]))

    def select(self, mailbox="INBOX", readonly=False):
        self._alive()
        name = mailbox.strip('"')
        assert name in self.srv.mail, f"select of unknown folder {name}"
        self.selected, self.readonly = name, readonly
        self.srv.log.append((name, readonly, "EXAMINE" if readonly else "SELECT"))
        return ("OK", [b"1"])

    def uid(self, cmd, *args):
        self._alive()
        folder = self.selected
        if cmd == "FETCH":
            if self.srv.fetch_no:
                return ("NO", [b"empty"])
            return ("OK", [(f"{i} (UID {m['uid']} RFC822.SIZE {m['size']} "
                            f'INTERNALDATE "{date_for(m["date"])}")').encode()
                           for i, m in enumerate(self.srv.mail[folder], 1)])
        self.srv.log.append((folder, self.readonly, f"UID {cmd} {args[0]}"))
        assert folder != "INBOX" and not self.readonly, f"{cmd} outside a read-write target: {folder}"
        ids = {int(x) for x in args[0].split(",")}
        if cmd == "STORE":
            for m in self.srv.mail[folder]:
                if m["uid"] in ids:
                    m["deleted"] = True
        elif cmd == "EXPUNGE":
            assert self.srv.uidplus
            self.srv.mail[folder] = [m for m in self.srv.mail[folder] if not (m["deleted"] and m["uid"] in ids)]
            self._expunged()
        return ("OK", [b""])

    def expunge(self):
        self.srv.log.append((self.selected, self.readonly, "EXPUNGE"))
        assert self.selected != "INBOX" and not self.readonly, f"EXPUNGE outside a read-write target: {self.selected}"
        self.srv.mail[self.selected] = [m for m in self.srv.mail[self.selected] if not m["deleted"]]
        self._expunged()
        return ("OK", [b""])

    def create(self, name):
        raise AssertionError("cleanup never creates a folder")

    def logout(self):
        return ("BYE", [b""])


def mailbox(srv, password="s3cret-pw") -> CleanupMailbox:
    return CleanupMailbox("imap.test", 993, "u", password, imap_factory=srv.factory)


PROV = SimpleNamespace(reply_inbox="INBOX", reply_processed="Processed", reply_quarantine="Quarantine")
CFG = MailboxCleanupConfig()


# ----------------------------------------------------------------------------------------------- refusal

@pytest.mark.parametrize("inbox,processed,quarantine", [
    ("INBOX", "INBOX", "Quarantine"), ("inbox", "Processed", "INBOX"), ("INBOX", "Processed", "processed"),
    ("INBOX", "Sent Items", "Quarantine"), ("INBOX", "Processed", "sent items"),
])
def test_refusal(inbox, processed, quarantine):
    srv = FakeServer()
    mb = mailbox(srv)
    mb.open()
    assert mb.refusal(inbox, processed, quarantine)
    mb.close()


def test_no_refusal_for_own_folders_and_unknown_folder_refused():
    srv = FakeServer()
    mb = mailbox(srv)
    mb.open()
    assert mb.refusal("INBOX", "Processed", "Quarantine") is None
    assert mb.refusal("INBOX", "Processed", "Nope")
    mb.close()


def test_special_use_list_only_when_advertised():
    srv = FakeServer(special_use=False)
    mb = mailbox(srv)
    mb.open()
    assert not any("SPECIAL-USE" in c for _, _, c in srv.log)
    mb.close()
    srv2 = FakeServer()
    mb = mailbox(srv2)
    mb.open()
    assert any("RETURN (SPECIAL-USE)" in c for _, _, c in srv2.log)
    mb.close()


def test_folder_with_space_sent_quoted():
    srv = FakeServer()
    seen = []
    real = FakeIMAP.select
    FakeIMAP.select = lambda self, mailbox="INBOX", readonly=False: (seen.append(mailbox), real(self, mailbox, readonly))[1]
    try:
        mb = mailbox(srv)
        mb.open()
        mb.scan("Old Mail", readonly=True)
        mb.close()
    finally:
        FakeIMAP.select = real
    assert seen == ['"Old Mail"']


def test_uidplus_from_post_login_capability():
    srv = FakeServer(uidplus=True)
    srv.add("Processed", 1, 40)
    mb = mailbox(srv)
    mb.open()
    assert "UIDPLUS" not in srv.sessions[0].capabilities and "UIDPLUS" in mb.caps
    mb.scan("Processed", readonly=False)
    mb.delete_batch("Processed", u(1))
    mb.close()
    assert ("Processed", False, "UID EXPUNGE 1") in srv.log and not any(c == "EXPUNGE" for _, _, c in srv.log)


def test_without_uidplus_plain_expunge_in_target_only():
    srv = FakeServer(uidplus=False)
    srv.add("Processed", 1, 40)
    srv.add("Processed", 2, 1)
    mb = mailbox(srv)
    mb.open()
    mb.scan("Processed", readonly=False)
    mb.delete_batch("Processed", u(1))
    mb.close()
    assert ("Processed", False, "EXPUNGE") in srv.log
    assert [m["uid"] for m in srv.mail["Processed"]] == [2]


def test_delete_batch_needs_prior_readwrite_select():
    srv = FakeServer()
    srv.add("Processed", 1, 40)
    mb = mailbox(srv)
    mb.open()
    with pytest.raises(MailboxError):
        mb.delete_batch("Processed", u(1))
    mb.scan("Processed", readonly=True)  # EXAMINE clears the selection
    with pytest.raises(MailboxError):
        mb.delete_batch("Processed", u(1))
    mb.scan("Quarantine", readonly=False)
    with pytest.raises(MailboxError):
        mb.delete_batch("Processed", u(1))
    mb.close()
    assert not any(c.startswith(("UID STORE", "UID EXPUNGE", "EXPUNGE")) for _, _, c in srv.log)


def test_no_on_fetch_is_empty():
    srv = FakeServer()
    srv.fetch_no = True
    mb = mailbox(srv)
    mb.open()
    assert mb.scan("Processed", readonly=True) == []
    mb.close()


def test_login_error_masks_password():
    srv = FakeServer()
    srv.login_error = "login failed for PW"
    mb = mailbox(srv, password="hunter2-secret")
    with pytest.raises(MailboxError) as e:
        mb.open()
    assert "***" in str(e.value) and "hunter2-secret" not in str(e.value)


# ----------------------------------------------------------------------------------------------- run_pass

def run(srv, cfg=CFG, **kw):
    kw.setdefault("dry_run", False)
    return asyncio.run(run_pass(lambda: mailbox(srv), asyncio.Lock(), PROV, cfg, now=NOW, **kw))


def test_dry_run_examines_only_and_matches_select():
    srv = FakeServer()
    for i in range(1, 4):
        srv.add("Processed", i, 40, 100)
    srv.add("Processed", 4, 1, 100)
    srv.add("Quarantine", 9, 100, 50)
    out = run(srv, dry_run=True)
    p = out["folders"]["Processed"]
    assert (p["messages"], p["bytes"], p["removed_age"], p["removed_bytes"]) == (4, 400, 3, 300)
    assert (p["after_messages"], p["after_bytes"], p["unfinished"]) == (1, 100, False)
    assert out["folders"]["Quarantine"]["removed_age"] == 1
    assert len(srv.mail["Processed"]) == 4
    assert all(readonly for sel, readonly, c in srv.log if c in ("EXAMINE", "SELECT"))
    assert not any(c.startswith(("UID STORE", "UID EXPUNGE", "EXPUNGE")) for _, _, c in srv.log)


def test_real_run_deletes_in_target_only_and_never_touches_inbox():
    srv = FakeServer()
    srv.add("INBOX", 1, 400)
    srv.add("Processed", 1, 40)
    srv.add("Processed", 2, 1)
    srv.add("Quarantine", 1, 100)
    out = run(srv)
    assert [m["uid"] for m in srv.mail["Processed"]] == [2] and srv.mail["Quarantine"] == []
    assert len(srv.mail["INBOX"]) == 1
    assert out["folders"]["Processed"]["removed_age"] == 1
    assert not any(sel == "INBOX" for sel, _, c in srv.log if c != "LIST \"\" \"*\"")


def test_cap_removal_reported():
    srv = FakeServer()
    for i in range(1, 4):
        srv.add("Processed", i, i, MIB)
    out = run(srv, MailboxCleanupConfig(processed_days=0, processed_max_mib=1))
    p = out["folders"]["Processed"]
    assert p["cap_hit"] and p["removed_cap"] == 2 and p["removed_bytes"] == 2 * MIB and p["after_messages"] == 1


def test_refused_pass_does_nothing():
    srv = FakeServer()
    srv.add("Processed", 1, 400)
    prov = SimpleNamespace(reply_inbox="INBOX", reply_processed="Sent Items", reply_quarantine="Quarantine")
    out = asyncio.run(run_pass(lambda: mailbox(srv), asyncio.Lock(), prov, CFG, now=NOW, dry_run=False))
    assert out["refused"] and out["folders"] == {}
    assert not any(c in ("SELECT", "EXAMINE") for _, _, c in srv.log)


def test_500_messages_three_batches_timer_rearmed(monkeypatch):
    srv = FakeServer()
    for i in range(1, 501):
        srv.add("Processed", i, 40)
    arms = []
    real = CleanupMailbox._arm
    monkeypatch.setattr(CleanupMailbox, "_arm", lambda self: (arms.append(1), real(self))[1])
    out = run(srv)
    assert out["folders"]["Processed"]["removed_age"] == 500 and srv.mail["Processed"] == []
    stores = [c for _, _, c in srv.log if c.startswith("UID STORE")]
    assert len(stores) == 3 and BATCH == 200
    assert len(arms) == 1 + 3  # at open, then once per batch


def test_deadline_stops_between_batches():
    srv = FakeServer()
    for i in range(1, 501):
        srv.add("Processed", i, 40)
    out = run(srv, deadline=time.monotonic() - 1)
    p = out["folders"]["Processed"]
    assert p["unfinished"] is True and p["removed_age"] == 200 and len(srv.mail["Processed"]) == 300


def test_dropped_session_reconnects_and_continues():
    srv = FakeServer()
    for i in range(1, 401):
        srv.add("Processed", i, 40)
    srv.drop_after_expunges = 1  # the connection dies between batch 1 and 2
    out = run(srv)
    p = out["folders"]["Processed"]
    assert p["removed_age"] == 400 and p["error"] is None and not p["unfinished"] and srv.mail["Processed"] == []
    assert len(srv.sessions) == 2


def test_uidvalidity_change_after_reconnect_aborts_folder():
    srv = FakeServer()
    for i in range(1, 401):
        srv.add("Processed", i, 40)
    srv.add("Quarantine", 1, 200)
    srv.drop_after_expunges = 1
    srv.uidvalidity_after_drop = 8
    out = run(srv)
    p = out["folders"]["Processed"]
    assert p["unfinished"] is True and p["removed_age"] == 200 and len(srv.mail["Processed"]) == 200


def test_unknown_uidvalidity_fails_closed_on_reselect():
    srv = FakeServer()
    mb = mailbox(srv)
    mb.open()
    assert mb.reselect("Processed", None) is False
    mb.close()


def test_folder_error_is_recorded_and_other_folder_still_runs():
    srv = FakeServer()
    srv.add("Processed", 1, 40)
    srv.add("Quarantine", 1, 200)
    real = FakeIMAP.uid

    def boom(self, cmd, *args):
        if cmd == "FETCH" and self.selected == "Processed":
            raise OSError("boom s3cret-pw")
        return real(self, cmd, *args)
    FakeIMAP.uid = boom
    try:
        out = run(srv)
    finally:
        FakeIMAP.uid = real
    p, q = out["folders"]["Processed"], out["folders"]["Quarantine"]
    assert "boom" in p["error"] and "s3cret-pw" not in p["error"] and p["unfinished"]
    assert q["error"] is None and q["removed_age"] == 1


def test_deadline_checked_before_second_folder():
    srv = FakeServer()
    srv.add("Processed", 1, 40)
    srv.add("Quarantine", 1, 200)
    out = run(srv, deadline=time.monotonic() - 1)
    assert out["folders"]["Processed"]["removed_age"] == 1
    q = out["folders"]["Quarantine"]
    assert q["unfinished"] is True and q["removed_age"] == 0 and len(srv.mail["Quarantine"]) == 1


def test_delete_batch_refuses_configured_inbox_name():
    srv = FakeServer()
    srv.attrs["Mail"] = []
    srv.mail["Mail"] = []
    srv.add("Mail", 1, 40)
    mb = CleanupMailbox("imap.test", 993, "u", "pw", imap_factory=srv.factory, inbox="mail")
    mb.open()
    mb.scan("Mail", readonly=False)
    with pytest.raises(MailboxError):
        mb.delete_batch("Mail", u(1))
    mb.close()


def test_poll_gets_lock_between_batches():
    srv = FakeServer()
    for i in range(1, 601):
        srv.add("Processed", i, 40)
    order = []

    async def go():
        lock = asyncio.Lock()
        mb = mailbox(srv)
        orig = mb.delete_batch
        batches = 0

        def counting(folder, uids):
            nonlocal batches
            batches += 1
            order.append(f"batch{batches}")
            time.sleep(0.05)
            return orig(folder, uids)
        mb.delete_batch = counting

        async def poller():
            await asyncio.sleep(0.02)   # arrives while batch 1 runs
            async with lock:
                order.append("poll")
        await asyncio.gather(run_pass(lambda: mb, lock, PROV, CFG, now=NOW, dry_run=False), poller())
    asyncio.run(go())
    assert order.index("poll") < order.index("batch2")
