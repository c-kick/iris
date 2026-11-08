"""Hardening of providers and reply intake (review C4-C13): each test fails on the code before the fix."""

from __future__ import annotations

import asyncio
import imaplib
import json
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage

import httpx
import pytest

import notify.providers.email_inbound as inbound
from notify import replyparse as rp
from notify.models import Severity
from notify.providers.base import OutboundMessage
from notify.providers.pushover import PushoverProvider, redact_callback_secrets
from notify.providers.webpush import MAX_PLAINTEXT, WebPushProvider
from notify.webpush_crypto import encrypt, generate_vapid_keys
from tests.conftest import APP_TOKEN, USER_KEY, FakePushover, wait_for
from tests.mailfactory import FakeMailbox, reply_raw, split_headers
from tests.test_inbound import FakeIMAP, poll
from tests.test_replies_core import send

UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"  # ggignore: RFC 8291 appendix A test vector, not a secret
PUSH_ADDRESS = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc123", "p256dh": UA_PUBLIC, "auth": AUTH}
DLV = "dlv_0192837465abcdef012345"


def _msg(**kw) -> OutboundMessage:
    base = dict(notification_id="ntf_1", delivery_id="dlv_1", title="T", message="hi", severity=Severity.normal,
                url=None, url_title=None, actions=[], tags=[], require_ack=False, created_at=datetime.now(UTC),
                expires_at=None, external_url="https://notify.example.org")
    base.update(kw)
    return OutboundMessage(**base)


# --- C4 (C.8): web push payload stays within one record --------------------------------------------------------

def _webpush(fake: FakePushover) -> WebPushProvider:
    priv, pub = generate_vapid_keys()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return WebPushProvider("browser", {"vapid_private_key": priv, "vapid_public_key": pub,
                                       "contact": "mailto:alex@example.org"}, http)


def test_webpush_long_title_and_url_fit():
    fake = FakePushover()
    p = _webpush(fake)
    msg = _msg(title="\U0001F600" * 1000, url="https://example.org/" + "a" * 2028, message="m" * 4000)
    raw = p.build_payload(msg)
    data = json.loads(raw)
    assert len(raw) <= MAX_PLAINTEXT
    assert data["b"] == "…" and data["t"].endswith("…") and len(data["t"]) > 100  # body first, then the title
    assert data["u"] == "https://example.org/" + "a" * 2028  # the link survives when trimming the title suffices
    assert len(encrypt(raw, UA_PUBLIC, AUTH)) <= 4096
    r = asyncio.run(p.send(msg, PUSH_ADDRESS))
    assert r.outcome == "success" and len(fake.push_requests[0].content) <= 4096


def test_webpush_link_that_cannot_fit_is_dropped_and_text_kept():
    p = _webpush(FakePushover())
    url = "https://example.org/" + "\u00e9" * 2000  # a link is never cut (it would lead elsewhere), only dropped
    raw = p.build_payload(_msg(title="", message="", url=url, actions=[]))
    assert len(raw) <= MAX_PLAINTEXT and json.loads(raw)["u"] is None
    data = json.loads(p.build_payload(_msg(title="Title", message="hello", url=url)))
    assert data["u"] is None and data["t"] == "Title" and data["b"] == "hello"


def test_webpush_413_says_too_large():
    fake = FakePushover(push_responses=[(413, {})])
    r = asyncio.run(_webpush(fake).send(_msg(), PUSH_ADDRESS))
    assert r.outcome == "permanent" and "too large" in (r.error or "")


# --- C7 (E.3): the callback secret never reaches the attempt log -------------------------------------------------

def test_redact_drops_callback_url():
    out = PushoverProvider._redact({"token": APP_TOKEN, "user": USER_KEY, "message": "m",
                                    "callback": "https://notify.example.org/v1/callbacks/pushover/SECRET"})
    assert out["callback"] is True and "SECRET" not in json.dumps(out)
    assert PushoverProvider._redact({"message": "m"})["callback"] is False


def test_attempt_log_has_no_callback_secret_and_old_rows_are_migrated(server_factory, fake):
    secret = "s" * 32
    live = server_factory(public_base_url="https://notify.example.org", callback_secret=secret)
    with live.client() as c:
        n = c.post("/v1/notifications", json={"targets": ["device:alex-phone"], "message": "m",
                                              "severity": "critical"}).json()
        got = wait_for(lambda: (x := c.get(f"/v1/notifications/{n['id']}").json())["status"] == "dispatched" and x)
    assert fake.messages[0]["callback"].endswith(secret)  # still sent to Pushover
    assert secret not in json.dumps(got) and got["deliveries"][0]["attempt_log"][0]["request"]["callback"] is True
    db = live.service.db
    # a row logged before the fix
    with db.tx() as c:
        c.execute("UPDATE attempts SET request_json=json_set(request_json, '$.callback', ?)",
                  (f"https://notify.example.org/v1/callbacks/pushover/{secret}",))
    assert secret in db.scalar("SELECT request_json FROM attempts")
    assert redact_callback_secrets(db) == 1
    row = json.loads(db.scalar("SELECT request_json FROM attempts"))
    assert row["callback"] is True and secret not in json.dumps(row) and row["priority"] == "2"
    assert redact_callback_secrets(db) == 0  # idempotent


# --- C8 (E.4): cancel by tag; ambiguous failures say so -----------------------------------------------------------

def _pushover(handler) -> PushoverProvider:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PushoverProvider("pushover", {"app_token": APP_TOKEN, "api_base": "https://pushover.test/1"}, http)


def test_cancel_with_tag_uses_cancel_by_tag():
    fake = FakePushover()
    p = _pushover(fake.handler)
    assert asyncio.run(p.cancel("r" * 30, tag="ntf_abc")).outcome == "success"
    assert fake.cancelled == ["tag:ntf_abc"]
    assert asyncio.run(p.cancel("r" * 30)).outcome == "success"  # receipt-only callers keep working
    assert fake.cancelled[-1] == "r" * 30


@pytest.mark.parametrize("exc,ambiguous", [
    (httpx.ReadTimeout, True), (httpx.WriteError, True), (httpx.RemoteProtocolError, True),
    (httpx.ConnectError, False), (httpx.ConnectTimeout, False), (httpx.PoolTimeout, False)])
def test_ambiguous_send_errors_are_flagged(exc, ambiguous):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc("boom", request=request)
    r = asyncio.run(_pushover(handler).send(_msg(), {"user_key": USER_KEY}))
    assert r.outcome == "retryable"
    assert ("outcome unknown; may be duplicated" in (r.error or "")) is ambiguous


def test_5xx_is_flagged_ambiguous():
    r = asyncio.run(_pushover(lambda req: httpx.Response(502, json={"status": 0})).send(_msg(), {"user_key": USER_KEY}))
    assert r.outcome == "retryable" and r.error.startswith("outcome unknown; may be duplicated")


# --- C9 (E.5): the leading run of trusted Authentication-Results headers ----------------------------------------

def _dk(*headers: str) -> bool:
    return rp.dkim_passes(rp.parse_headers(split_headers(reply_raw(DLV, auth_results=list(headers)))),
                          "mx.example.org", "example.org")


def test_split_headers_are_never_combined():
    # E.5 ruled out after review: the receiving server may not strip forged headers, so a lower header with the
    # trusted id must never supply a method the server's own (first) header did not report
    assert not _dk("mx.example.org; dkim=pass header.d=example.org",
                   "mx.example.org; dmarc=pass header.from=example.org")
    assert _dk("mx.example.org; dkim=pass header.d=example.org; dmarc=pass header.from=example.org")


def test_trusted_fail_is_never_overridden_by_a_lower_pass():
    assert not _dk("mx.example.org; dkim=fail header.d=example.org",
                   "mx.example.org; dkim=pass header.d=example.org; dmarc=pass header.from=example.org")
    assert not _dk("mx.example.org; dkim=pass header.d=example.org; dmarc=fail header.from=example.org",
                   "mx.example.org; dmarc=pass header.from=example.org")


def test_another_authserv_id_ends_the_run():
    # on top: not ours at all (unchanged behaviour)
    assert not _dk("other.example.net; dkim=pass header.d=example.org; dmarc=pass header.from=example.org",
                   "mx.example.org; dkim=pass header.d=example.org; dmarc=pass header.from=example.org")
    # below a foreign header the trusted id is not believed any more
    assert not _dk("mx.example.org; dkim=pass header.d=example.org",
                   "relay.example.net; arc=none",
                   "mx.example.org; dmarc=pass header.from=example.org")


# --- C5 (E.1): a header the parser chokes on is quarantined, the pass goes on ----------------------------------

def test_nested_comment_header_is_quarantined_and_next_uid_processed(email_live):
    nid, did = send(email_live)
    mb = email_live.mailbox
    bad = b"Content-Type: text/plain " + b"(" * 500 + b"\r\n" + reply_raw(did, message_id="<bad@example.org>")
    bad_uid = mb.add(bad)
    good_uid = mb.add(reply_raw(did))
    assert poll(email_live) == {"accepted": 1, "quarantined": 1, "duplicates": 0}
    assert mb.folders["Quarantine"] == [bad_uid] and mb.folders["Processed"] == [good_uid]
    assert email_live.service.db.scalar("SELECT reason FROM reply_quarantine") == "malformed_headers"


# --- C6 (E.2): part floods are refused before the full parse --------------------------------------------------

def _many_parts(did: str, n: int) -> bytes:
    m = rp.parse_message(reply_raw(did))
    for i in range(n):
        m.add_attachment(b"x", maintype="application", subtype="octet-stream", filename=f"f{i}.bin")
    return m.as_bytes(policy=policy.SMTP)


def test_part_flood_is_quarantined_too_complex(email_live):
    nid, did = send(email_live)
    mb = email_live.mailbox
    uid = mb.add(_many_parts(did, 300))
    assert poll(email_live)["quarantined"] == 1
    assert mb.folders["Quarantine"] == [uid]
    assert email_live.service.db.scalar("SELECT reason FROM reply_quarantine") == "too_complex"
    assert email_live.service.db.scalar("SELECT COUNT(*) FROM replies") == 0


def test_part_cap_stops_the_parser_early(monkeypatch):
    made = []
    real = EmailMessage.__init__

    def counting(self, *a, **kw):
        made.append(1)
        real(self, *a, **kw)
    raw = _many_parts(DLV, 5000)
    monkeypatch.setattr(EmailMessage, "__init__", counting)
    with pytest.raises(rp.TooComplex):
        rp.parse_reply(raw)
    assert len(made) < 300  # it never builds the 5000-part tree
    monkeypatch.undo()
    assert len(rp.parse_reply(_many_parts(DLV, 150)).dropped) == 140  # a modest mail still parses


def test_attachments_past_the_cap_are_not_decoded(monkeypatch):
    msg = rp.parse_message(_many_parts(DLV, 15))
    decoded = []
    real = EmailMessage.get_payload

    def counting(self, *a, **kw):
        if kw.get("decode") or (len(a) > 1 and a[1]):
            decoded.append(1)
        return real(self, *a, **kw)
    monkeypatch.setattr(EmailMessage, "get_payload", counting)
    kept, dropped = rp.extract_attachments(msg)
    assert len(kept) == rp.MAX_ATTACHMENTS and dropped == [f"f{i}.bin" for i in range(10, 15)]
    assert len(decoded) == rp.MAX_ATTACHMENTS


# --- C10 (E.6): no plain EXPUNGE; \Deleted mail skipped; quarantine recorded once -------------------------------

@pytest.fixture
def imap_cls(monkeypatch):
    class F(FakeIMAP):
        instances: list = []
        move_ok = False
        expunge_answer = ("NO", [b"no UIDPLUS"])

        def uid(self, cmd, *args):
            self.calls.append(("uid", cmd, *args))
            if cmd == "FETCH" and args[0] == "1:*":
                return ("OK", [b"1 (UID 7 RFC822.SIZE 1234 FLAGS (\\Seen))",
                               b"2 (UID 9 RFC822.SIZE 99 FLAGS (\\Seen \\Deleted))", b"3 (UID 11 RFC822.SIZE 5)"])
            if cmd == "MOVE":
                raise imaplib.IMAP4.error("MOVE not supported")
            if cmd == "EXPUNGE":
                return self.expunge_answer
            return ("OK", [b""])
    monkeypatch.setattr(inbound.imaplib, "IMAP4_SSL", F)
    return F


def test_list_skips_messages_flagged_deleted(imap_cls):
    mb = inbound.ImapMailbox("imap.test", 993, "u", "p")
    mb.open()
    try:
        assert mb.list() == [(b"7", 1234), (b"11", 5)]
    finally:
        mb.close()


def test_move_without_uid_expunge_leaves_message_flagged(imap_cls):
    mb = inbound.ImapMailbox("imap.test", 993, "u", "p")
    mb.open()
    mb.move(b"7", "Processed")  # a NO on UID EXPUNGE no longer ends the pass
    mb.close()
    calls = imap_cls.instances[0].calls
    assert ("uid", "COPY", b"7", '"Processed"') in calls and ("expunge",) not in calls
    assert ("uid", "STORE", b"7", "+FLAGS.SILENT", "(\\Deleted)") in calls


class StickyMailbox(FakeMailbox):
    """A server where the original stays behind after a move (what the old fallback could leave)."""

    def move(self, uid: bytes, folder: str) -> None:
        self.folders[folder].append(uid)


def test_quarantine_is_recorded_once_per_message(email_live):
    mb = StickyMailbox()
    email_live.mailbox = mb
    email_live.service.open_mailbox = lambda provider: mb
    nid, did = send(email_live)
    mb.add(reply_raw(did, message_id="<q@example.org>", auth_results=["mx.example.org; dkim=fail"]))
    mb.add(reply_raw(did, message_id=None, auth_results=["mx.example.org; dkim=fail"]))
    poll(email_live)
    poll(email_live)
    db = email_live.service.db
    assert db.scalar("SELECT COUNT(*) FROM reply_quarantine") == 2
    assert db.scalar("SELECT COUNT(*) FROM events WHERE type='reply.quarantined'") == 2


# --- C11 (E.7): per-message deadline; a message that keeps failing is skipped ----------------------------------

def test_session_deadline_rearmed_per_message(email_live):
    nid, did = send(email_live)
    mb = email_live.mailbox
    for i in range(3):
        mb.add(reply_raw(did, message_id=f"<m{i}@example.org>"))
    poll(email_live)
    assert mb.armed == 3


def test_imap_arm_restarts_the_timer(fake_imap_ok):
    mb = inbound.ImapMailbox("imap.test", 993, "u", "p", timeout=30)
    mb.open()
    first = mb._timer
    mb.arm()
    first.join(1)
    assert not first.is_alive() and mb._timer is not first and mb._timer.is_alive()
    mb.close()
    mb._timer.join(1)
    assert not mb._timer.is_alive()


@pytest.fixture
def fake_imap_ok(monkeypatch):
    class F(FakeIMAP):
        instances: list = []
    monkeypatch.setattr(inbound.imaplib, "IMAP4_SSL", F)
    return F


class FailingMoveMailbox(FakeMailbox):
    def __init__(self, bad: bytes) -> None:
        super().__init__()
        self.bad = bad

    def move(self, uid: bytes, folder: str) -> None:
        if uid == self.bad:
            raise inbound.MailboxError("copy to Quarantine failed: [OVERQUOTA]")
        super().move(uid, folder)


def test_uid_failing_three_times_is_skipped(email_live):
    mb = FailingMoveMailbox(b"1")
    email_live.mailbox = mb
    nid, did = send(email_live)
    mb.add(reply_raw(did, message_id="<spam@example.org>", auth_results=["mx.example.org; dkim=fail"]))
    good = mb.add(reply_raw(did))
    for _ in range(inbound.MAX_UID_FAILURES):
        with pytest.raises(inbound.MailboxError):
            poll(email_live)
    assert mb.folders["Processed"] == []
    assert poll(email_live)["accepted"] == 1  # head-of-line blocking is over
    assert mb.folders["Processed"] == [good]
    note = inbound.skipped_note(email_live.service.providers["mail"])
    assert note and "UID 1" in note and "OVERQUOTA" in note


# --- C12 (E.8): thread matching prefers a delivery that accepts the reply --------------------------------------

def test_reply_to_a_non_reply_follow_up_reaches_the_conversation(email_live):
    nid, did = send(email_live)
    _, did2 = send(email_live, in_reply_to=nid, reply=False, message="FYI")
    raw = reply_raw(did2).replace(f"References: <{did2}@example.org>".encode(),
                                  f"References: <{did}@example.org> <{did2}@example.org>".encode())
    head = rp.parse_headers(split_headers(raw))
    v = email_live.service.replies.classify(head, split_headers(raw), "mx.example.org", "mail")
    assert v.action == "accept" and v.delivery["id"] == did
    # nothing in the chain accepts replies: the newest known delivery still decides the reason
    only = reply_raw(did2)
    v = email_live.service.replies.classify(rp.parse_headers(split_headers(only)), split_headers(only),
                                            "mx.example.org", "mail")
    assert v.action == "quarantine" and v.reason == "not_reply_enabled" and v.delivery["id"] == did2


def test_thread_match_never_uses_another_providers_delivery(email_live):
    nid, did = send(email_live)
    raw = reply_raw(did)
    head = rp.parse_headers(split_headers(raw))
    v = email_live.service.replies.classify(head, split_headers(raw), "mx.example.org", "other-mail")
    assert v.action == "quarantine" and v.reason == "no_thread" and v.delivery is None


# --- C13 (E.9): what the reply checks prove ---------------------------------------------------------------------

def test_mcp_instructions_do_not_overstate_reply_checks():
    from notify.mcp_server import instructions
    text = " ".join(instructions("Europe/Amsterdam").split())
    assert "it was sent from the recipient's address and signed by their mail domain" in text
    assert "comes from the person" not in text
