"""Bug hunt (mail / providers slice) after the 0.13.1 hardening. Each test asserts the correct behaviour and fails on
bb0294f; see the BH-1NN report for the analysis."""

from __future__ import annotations

import asyncio
import threading
import time
from email import policy

import pytest

import notify.providers.email_inbound as inbound
from notify import attachments
from notify import replyparse as rp
from notify.config import ServerConfig
from tests.conftest import wait_for
from tests.mailfactory import reply_raw, split_headers
from tests.test_inbound import FakeIMAP, poll
from tests.test_replies_core import send


# --- BH-101: an IP:port allow entry is accepted but can never match -----------------------------------------------

def test_bh101_ip_port_allow_entry_is_honoured_or_refused():
    """README: 0.13.1 limits a bare IP/CIDR to ports 80/443. The obvious way to allow Home Assistant on its own port,
    `192.168.1.10:8123`, passes settings validation, so it must also allow the URL (or be refused when saved)."""
    try:
        hosts = ServerConfig(attachment_url_hosts=["192.168.1.10:8123"]).attachment_url_hosts
    except ValueError:
        return  # refused at save time: the admin learns to write 192.168.1.10/32:8123
    attachments.check_url_allowed("http://192.168.1.10:8123/api/camera_proxy/camera.door", hosts)


# --- BH-102: a poll pass is no longer bounded by the session limit, nor by a stop ------------------------------------

class SlowIMAP(FakeIMAP):
    """Answers every header fetch after `delay` seconds; a killed socket makes the next command fail, like a real one."""
    instances: list = []
    delay = 0.3
    count = 10

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.killed = threading.Event()
        outer = self

        class Sock:
            def shutdown(self, how):
                outer.killed.set()

            def close(self):
                outer.killed.set()
        self.sock = Sock()

    def uid(self, cmd, *args):
        if self.killed.is_set():
            raise OSError("socket closed by the session deadline")
        if cmd == "FETCH" and args[0] == "1:*":
            return ("OK", [f"{i} (UID {i} RFC822.SIZE 900 FLAGS ())".encode() for i in range(1, self.count + 1)])
        if cmd == "FETCH":
            time.sleep(self.delay)
            if self.killed.is_set():
                raise OSError("socket closed by the session deadline")
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            head = split_headers(reply_raw("dlv_" + "f" * 22, message_id=f"<slow{uid}@example.org>"))
            return ("OK", [(f"{uid} (UID {uid} BODY[HEADER]<0> {{{len(head)}}}".encode(), head), b")"])
        return ("OK", [b""])


def test_bh102_stop_during_a_poll_is_bounded_by_the_session_limit(email_live, monkeypatch):
    """README (Operations): a stop during a mailbox poll waits for that step, 'bounded by the 120 s IMAP session limit',
    hence stop_grace_period 130 s. Here the limit is 0.6 s; ten slow messages must not hold a cancelled poll for 3 s."""
    monkeypatch.setattr(inbound.imaplib, "IMAP4_SSL", SlowIMAP)
    svc = email_live.service
    p = svc.providers["mail"]
    mb = inbound.ImapMailbox("imap.test", 993, "u", "p", timeout=0.6)

    async def stop_mid_pass() -> float:
        task = asyncio.ensure_future(inbound.poll_once(svc, p, mb))
        await asyncio.sleep(0.2)
        t0 = time.monotonic()
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        return time.monotonic() - t0
    took = email_live.call(stop_mid_pass, timeout=30)
    assert took < 0.6 + 0.5, f"cancelled poll held the mailbox for {took:.1f} s past a 0.6 s session limit"


# --- BH-103: a DKIM-verified reply that is too complex loses its conversation -----------------------------------------

def _many_parts(did: str, n: int) -> bytes:
    m = rp.parse_message(reply_raw(did))
    for i in range(n):
        m.add_attachment(b"x", maintype="application", subtype="octet-stream", filename=f"f{i}.bin")
    return m.as_bytes(policy=policy.SMTP)


def test_bh103_too_complex_reply_keeps_its_notification(email_live):
    """The verdict before the full parse already proved sender, thread and DKIM (like header_mismatch, which keeps
    them). Dropping them means no reply.quarantined event for the conversation and no notification in the record:
    the waiting agent never learns the person answered."""
    nid, did = send(email_live)
    email_live.mailbox.add(_many_parts(did, 300))
    assert poll(email_live)["quarantined"] == 1
    db = email_live.service.db
    assert db.scalar("SELECT reason FROM reply_quarantine") == "too_complex"
    assert db.scalar("SELECT notification_id FROM reply_quarantine") == nid
    assert db.scalar("SELECT COUNT(*) FROM events WHERE type='reply.quarantined' AND notification_id=?", (nid,)) == 1


# --- BH-104: an emergency alert whose every attempt had an unknown outcome cannot be recalled -------------------------

def test_bh104_failed_ambiguous_emergency_is_still_recalled_by_tag(server_factory, fake):
    """Every attempt got a 5xx ('outcome unknown; may be duplicated'): Pushover may be ringing the phone. The fix's
    cancel(tag=...) exists for exactly 'an attempt whose answer was lost and whose receipt we never saw', but cancel
    refuses a failed delivery before asking Pushover."""
    live = server_factory(max_attempts=2)
    fake.responses = [(502, {"status": 0}), (502, {"status": 0})]
    with live.client() as c:
        n = c.post("/v1/notifications", json={"targets": ["device:alex-phone"], "message": "smoke",
                                              "severity": "critical"}).json()
        wait_for(lambda: len(fake.messages) >= 1)
        live.fast_forward()
        wait_for(lambda: c.get(f"/v1/notifications/{n['id']}").json()["deliveries"][0]["state"] == "failed",
                 timeout=20)
        got = c.get(f"/v1/notifications/{n['id']}").json()
        assert "outcome unknown" in (got["deliveries"][0]["attempt_log"][-1]["error"] or "")
        c.post(f"/v1/notifications/{n['id']}/cancel")
    assert f"tag:{n['id']}" in fake.cancelled
