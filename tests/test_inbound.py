from __future__ import annotations

import imaplib
from typing import Any

import pytest

import notify.providers.email_inbound as inbound
from notify.core import Client
from notify.models import Scope
from tests.conftest import PNG, wait_for
from tests.mailfactory import reply_raw, split_headers

AGENT = Client("agent", frozenset({Scope.send, Scope.read, Scope.manage}))


def send(srv) -> tuple[str, str]:
    with srv.client() as c:
        out = c.post("/v1/notifications", json={"targets": ["device:alex-email"], "message": "Pick", "kinds": ["email"],
                                                 "reply": True}).json()
        wait_for(lambda: c.get(f"/v1/notifications/{out['id']}").json()["deliveries"][0]["state"] == "dispatched")
    return out["id"], out["deliveries"][0]["id"]


def poll(srv) -> dict:
    p = srv.service.providers["mail"]
    return srv.call(lambda: inbound.poll_once(srv.service, p, srv.mailbox))


def test_poll_sorts_mail(email_live):
    nid, did = send(email_live)
    mb = email_live.mailbox
    good = mb.add(reply_raw(did, attachment=PNG))
    forged = mb.add(reply_raw(did, message_id="<f@x>", auth_results=["mx.example.org; dkim=none",
                                                                       "mx.example.org; dkim=pass header.d=example.org"]))
    auto = mb.add(reply_raw(did, message_id="<a@x>", extra=[("Auto-Submitted", "auto-replied")]))
    huge = mb.add(reply_raw(did, message_id="<h@x>"), size=inbound.MAX_MESSAGE_BYTES + 1)
    stray = mb.add(reply_raw("dlv_" + "f" * 22, message_id="<s@x>"))
    assert poll(email_live) == {"accepted": 1, "quarantined": 4, "duplicates": 0}
    assert mb.folders["Processed"] == [good]
    assert sorted(mb.folders["Quarantine"]) == sorted([forged, auto, huge, stray])
    assert mb.fetched == [good]  # bodies of rejected mail are never downloaded
    reasons = {r[0] for r in email_live.service.db.query("SELECT reason FROM reply_quarantine")}
    assert reasons == {"dkim_fail", "automatic", "too_large", "no_thread"}
    got = email_live.call(lambda: email_live.service.replies.claim(AGENT, nid, "w" * 12, 0))
    assert got["text"] == "Go with B." and got["attachments"][0]["size"] == len(PNG)
    assert mb.opened == mb.closed == 1


def test_crash_between_commit_and_move_is_not_processed_twice(email_live):
    nid, did = send(email_live)
    mb = email_live.mailbox
    uid = mb.add(reply_raw(did))
    mb.fail_move_once = True
    with pytest.raises(OSError):
        poll(email_live)
    assert mb.closed == 1
    assert poll(email_live) == {"accepted": 0, "quarantined": 0, "duplicates": 1}
    assert mb.folders["Processed"] == [uid]
    assert email_live.service.db.scalar("SELECT COUNT(*) FROM replies") == 1


def test_unreadable_message_is_quarantined(email_live, monkeypatch):
    nid, did = send(email_live)
    mb = email_live.mailbox
    uid = mb.add(reply_raw(did))

    def boom(raw: bytes) -> Any:
        raise ValueError("parser exploded")
    monkeypatch.setattr(inbound.replyparse, "parse_reply", boom)
    assert poll(email_live)["quarantined"] == 1
    assert mb.folders["Quarantine"] == [uid]
    assert email_live.service.db.scalar("SELECT reason FROM reply_quarantine") == "unreadable"


def test_worker_loop_polls_and_reports_status(email_live):
    nid, did = send(email_live)
    email_live.mailbox.add(reply_raw(did))
    email_live.app.state.workers.inbound_interval = 0.1
    wait_for(lambda: email_live.service.inbound_status.get("mail", {}).get("last_success"), timeout=10)
    assert email_live.service.inbound_status["mail"]["last_error"] is None
    assert email_live.service.db.scalar("SELECT COUNT(*) FROM replies") == 1


# --- ImapMailbox against a fake imaplib client --------------------------------

class FakeIMAP:
    instances: list["FakeIMAP"] = []
    move_ok = True
    login_ok = True
    create_adds = True

    def __init__(self, host: str, port: int, ssl_context: Any = None, timeout: float | None = None):
        self.calls: list[tuple] = []
        self.sock = type("S", (), {"shutdown": lambda s, h: None, "close": lambda s: None})()
        self.folders = [b'(\\HasNoChildren) "." INBOX', b'(\\HasNoChildren) "." "Processed"']
        type(self).instances.append(self)

    def login(self, u, p):
        if not self.login_ok:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Authentication failed.")
        return ("OK", [b"ok"])

    def select(self, name, readonly=False):
        self.calls.append(("select", name))
        return ("OK", [b"2"])

    def list(self):
        return ("OK", self.folders)

    def create(self, name):
        self.calls.append(("create", name))
        if self.create_adds:
            self.folders.append(b'(\\HasNoChildren) "." ' + name.encode())
        return ("OK", [b""])

    def uid(self, cmd, *args):
        self.calls.append(("uid", cmd, *args))
        if cmd == "FETCH" and args[1] == "(RFC822.SIZE)":
            return ("OK", [b"1 (UID 7 RFC822.SIZE 1234)", b"2 (UID 9 RFC822.SIZE 99)"])
        if cmd == "FETCH":
            return ("OK", [(b"1 (UID 7 BODY[HEADER]<0> {5}", b"X: y\r\n"), b")"])
        if cmd == "MOVE":
            if not self.move_ok:
                raise imaplib.IMAP4.error("MOVE not supported")
            return ("OK", [b""])
        return ("OK", [b""])

    def expunge(self):
        self.calls.append(("expunge",))
        return ("OK", [b""])

    def logout(self):
        return ("BYE", [b""])


@pytest.fixture
def fake_imap(monkeypatch):
    class F(FakeIMAP):
        instances: list = []
        move_ok = True
        login_ok = True
        create_adds = True
    monkeypatch.setattr(inbound.imaplib, "IMAP4_SSL", F)
    return F


def box() -> inbound.ImapMailbox:
    return inbound.ImapMailbox("imap.test", 993, "u", "p")


def test_imap_open_list_fetch_creates_missing_folder(fake_imap):
    mb = box()
    mb.open()
    try:
        assert mb.list() == [(b"7", 1234), (b"9", 99)]
        assert mb.fetch_headers(b"7") == b"X: y\r\n"
    finally:
        mb.close()
    calls = fake_imap.instances[0].calls
    assert ("create", '"Quarantine"') in calls and ("create", '"Processed"') not in calls
    assert ("select", '"INBOX"') in calls


def test_imap_move_falls_back_to_copy_delete(fake_imap):
    fake_imap.move_ok = False
    mb = box()
    mb.open()
    mb.move(b"7", "Processed")
    mb.close()
    calls = fake_imap.instances[0].calls
    assert ("uid", "COPY", b"7", '"Processed"') in calls
    assert ("uid", "STORE", b"7", "+FLAGS.SILENT", "(\\Deleted)") in calls and ("uid", "EXPUNGE", b"7") in calls


def test_imap_login_failure_raises(fake_imap):
    fake_imap.login_ok = False
    mb = box()
    with pytest.raises(imaplib.IMAP4.error):
        mb.open()
    assert mb._imap is None
    mb._timer.join(1)
    assert not mb._timer.is_alive()


def test_imap_test_reports_authserv_ids(fake_imap, monkeypatch):
    monkeypatch.setattr(FakeIMAP, "uid", lambda self, cmd, *a: (
        ("OK", [b"1 (UID 7 RFC822.SIZE 10)"]) if a[1] == "(RFC822.SIZE)" else
        ("OK", [(b"1 (UID 7 BODY[HEADER] {9}", b"Authentication-Results: mx.example.org; dkim=pass\r\n\r\n"), b")"])))
    out = box().test()
    assert out["ok"] and out["messages"] == 1 and out["authserv_ids"] == ["mx.example.org"]


def test_imap_move_falls_back_to_plain_expunge(fake_imap, monkeypatch):
    fake_imap.move_ok = False
    orig = fake_imap.uid

    def uid(self, cmd, *a):
        if cmd == "EXPUNGE":
            raise imaplib.IMAP4.error("no UIDPLUS")
        return orig(self, cmd, *a)
    monkeypatch.setattr(fake_imap, "uid", uid)
    mb = box()
    mb.open()
    mb.move(b"7", "Processed")
    mb.close()
    assert ("expunge",) in fake_imap.instances[0].calls


def test_imap_missing_folder_that_cannot_be_created_raises(fake_imap):
    fake_imap.create_adds = False
    mb = box()
    with pytest.raises(inbound.MailboxError, match="'Quarantine'"):
        mb.open()
    assert mb._imap is None


def test_imap_header_fetch_is_bounded(fake_imap):
    mb = box()
    mb.open()
    mb.fetch_headers(b"7")
    mb.close()
    assert ("uid", "FETCH", b"7", "(BODY.PEEK[HEADER]<0.65536>)") in fake_imap.instances[0].calls


def test_imap_hard_deadline_unblocks_stalled_call(fake_imap):
    import threading
    import time
    ev = threading.Event()

    def stall(self, cmd, *a):
        ev.wait(10)
        raise OSError("socket closed")
    fake_imap.uid = stall
    shut: list[int] = []

    def open_with_sock():
        mb = inbound.ImapMailbox("imap.test", 993, "u", "p", timeout=0.2)
        mb.open()
        mb._imap.sock = type("S", (), {"shutdown": lambda s, h: (shut.append(h), ev.set()), "close": lambda s: None})()
        return mb
    mb = open_with_sock()
    t0 = time.monotonic()
    with pytest.raises(OSError):
        mb.list()
    assert time.monotonic() - t0 < 2 and shut
    mb.close()


def test_zero_or_missing_size_is_quarantined_unfetched(email_live):
    nid, did = send(email_live)
    mb = email_live.mailbox
    uid = mb.add(reply_raw(did), size=0)
    assert poll(email_live) == {"accepted": 0, "quarantined": 1, "duplicates": 0}
    assert mb.folders["Quarantine"] == [uid] and mb.fetched == []
    assert email_live.service.db.scalar("SELECT reason FROM reply_quarantine") == "too_large"


def test_header_differential_is_quarantined(email_live):
    nid, did = send(email_live)
    mb = email_live.mailbox
    forged = reply_raw(did, auth_results=["mx.example.org; dkim=none", "mx.example.org; dkim=pass header.d=example.org"])
    uid = mb.add(forged)
    benign = split_headers(reply_raw(did))
    mb.fetch_headers = lambda u: benign  # what a truncated fetch would have shown
    assert poll(email_live) == {"accepted": 0, "quarantined": 1, "duplicates": 0}
    assert mb.folders["Quarantine"] == [uid]
    assert email_live.service.db.scalar("SELECT reason FROM reply_quarantine") == "header_mismatch"
    assert email_live.service.db.scalar("SELECT COUNT(*) FROM replies") == 0


def test_truncated_header_block_is_too_large(email_live):
    nid, did = send(email_live)
    mb = email_live.mailbox
    uid = mb.add(reply_raw(did, extra=[("X-Pad", "a" * 70000)]))
    mb.fetch_headers = lambda u: mb.messages[u][:inbound.HEADER_FETCH_BYTES]
    assert poll(email_live)["quarantined"] == 1
    assert mb.folders["Quarantine"] == [uid] and mb.fetched == []
    assert email_live.service.db.scalar("SELECT reason FROM reply_quarantine") == "too_large"
