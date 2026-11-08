from __future__ import annotations

import asyncio
import email
import email.policy
import smtplib
import socket
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from aiosmtpd.controller import Controller

import notify.providers.email as email_mod
from notify.app import create_app
from notify.config import NotifyConfig
from notify.models import Severity
from notify.providers.base import AttachmentData, OutboundMessage, Provider
from notify.providers.email import EmailProvider, _header_safe
from tests.conftest import PNG, LiveServer, make_config, wait_for

GOOD = {"type": "email", "host": "mail.test", "port": 25, "security": "none", "from_address": "hnl@example.org",
        "subject_prefix": "[home]"}
TO = {"to": "alex@example.org"}


def prov(**over: Any) -> EmailProvider:
    return EmailProvider("mail", {**GOOD, **over}, None)  # type: ignore[arg-type]


def msg(title: str | None = "Door", message: str = "Open\nsecond", severity: Severity = Severity.normal,
        url: str | None = None, actions: list[dict] | None = None, attachments: list[AttachmentData] | None = None):
    return OutboundMessage("ntf_1", "dlv_1", title, message, severity, url, "Look" if url else None, actions or [], [],
                           False, datetime(2026, 10, 9, 12, 0, tzinfo=UTC), None, attachments=attachments or [])


def parsed(m: OutboundMessage, p: EmailProvider | None = None):
    return email.message_from_bytes((p or prov()).build_message(m, TO).as_bytes(), policy=email.policy.default)


def test_headers_and_parts():
    em = parsed(msg(severity=Severity.high, url="https://x.example/a?b=1&c=2"))
    assert em["Subject"] == "[home] Door"
    assert em["From"] == "Iris <hnl@example.org>"
    assert em["To"] == "alex@example.org"
    assert em["Message-ID"] == "<dlv_1@example.org>"
    assert em["Importance"] == "high" and em["X-Priority"] == "1"
    assert "Look: https://x.example/a?b=1&c=2" in em.get_body(("plain",)).get_content()
    assert '<a href="https://x.example/a?b=1&amp;c=2"' in em.get_body(("html",)).get_content()


def test_subject_falls_back_to_first_line_and_low_importance():
    em = parsed(msg(title=None, severity=Severity.low))
    assert em["Subject"] == "[home] Open" and em["Importance"] == "low"


def test_non_http_links_are_text_only():
    em = parsed(msg(url="javascript:alert(1)", actions=[{"id": "a", "label": "Go", "url": "ftp://h/x"}]))
    body = em.get_body(("html",)).get_content()
    assert "href=" not in body and "javascript:alert(1)" in body


def test_hostile_header_values():
    att = AttachmentData("a\r\nb.png", "image/png", len(PNG), PNG)
    em = parsed(msg(title="Door\r\nBcc: evil@x.org 🚪", attachments=[att]))
    assert em["Subject"] == "[home] Door Bcc: evil@x.org 🚪"
    assert em["Bcc"] is None
    assert not any(line.startswith(b"Bcc:") for line in prov().build_message(
        msg(title="Door\r\nBcc: evil@x.org", attachments=[att]), TO).as_bytes().splitlines())
    assert next(em.iter_attachments()).get_filename() == "a b.png"


@pytest.mark.parametrize("bad", ["\x00", " ", "x" * 2000, "a\u2028b\x85c", "\r\n\r\n", "http://[::1", "http://[x"])
def test_hostile_strings_never_raise(bad):
    att = AttachmentData(bad, "", 3, b"abc")
    p = prov(subject_prefix=bad, from_name=bad)
    m = msg(title=bad, message=bad, url=bad, actions=[{"id": "a", "label": bad, "url": bad}], attachments=[att])
    p.plan(m, TO)
    em = p.build_message(m, TO)
    assert b"\r\nBcc:" not in em.as_bytes()
    assert not any(c in str(em["From"]) for c in "\r\n")
    name = next(em.iter_attachments()).get_filename() or ""
    assert not any(c in name for c in "\r\n")
    assert _header_safe(None) == ""


def test_unknown_attachment_without_data_skipped_and_plan_ok():
    p = prov()
    m = msg(attachments=[AttachmentData("x", "", None)])
    assert list(p.build_message(m, TO).iter_attachments()) == []
    assert p.plan(m, TO).tracking == "none"


def test_plan_gaps():
    m = msg(url="javascript:x", actions=[{"id": "n"}, {"id": "f", "url": "ftp://x"}])
    m.require_ack = True
    gaps = " | ".join(prov().plan(m, TO).capability_gaps)
    assert "acknowledgement" in gaps and "'n' dropped (no url)" in gaps and "shown as text" in gaps


def test_config_problems():
    assert prov().config_problems() == []
    assert any("host" in p for p in EmailProvider("m", {**GOOD, "host": ""}, None).config_problems())  # type: ignore[arg-type]
    assert any("unencrypted" in p for p in prov(username="u").config_problems())
    assert any("from_address" in p for p in prov(from_address="nope").config_problems())
    assert any("security" in p for p in prov(security="ssl").config_problems())
    assert any("port" in p for p in prov(port=70000).config_problems())
    assert any("unknown settings: bogus" in p for p in prov(bogus=1).config_problems())
    assert prov(timeout_seconds=1).timeout == 10 and prov(timeout_seconds=9999).send_deadline_seconds == 300.0
    assert prov().device_problems(TO) == [] and prov().device_problems({"to": "a b@c.d"}) and prov().device_problems({})


def test_base_test_connection_unsupported():
    class P(Provider):
        config_problems = capabilities = plan = send = lambda *a, **k: None  # type: ignore[assignment]
    assert asyncio.run(P.test_connection(None)) == {"supported": False}  # type: ignore[arg-type]


class FakeSock:
    closed = False

    def settimeout(self, t: float) -> None:
        pass

    def shutdown(self, how: int) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


class FakeSMTP:
    features: dict[str, str] = {}
    starttls_offered = True
    login_exc: Exception | None = None
    send_exc: Exception | None = None
    connect_exc: Exception | None = None
    sent: list[bytes] = []

    contexts: list[Any] = []
    block_send = False

    def __init__(self, host: str, port: int, timeout: float = 0, context: Any = None):
        if self.connect_exc:
            raise self.connect_exc
        self.contexts.append(context)
        self.sock = FakeSock()
        self.esmtp_features = dict(self.features)

    def ehlo(self): pass
    def has_extn(self, n): return self.starttls_offered
    def starttls(self, context=None): pass
    def noop(self): return (250, b"ok")
    def quit(self): pass
    def close(self): pass

    def login(self, u, p):
        if self.login_exc:
            raise self.login_exc

    def sendmail(self, f, to, raw):
        while self.block_send:
            if self.sock.closed:
                raise OSError("socket closed")
            time.sleep(0.02)
        if self.send_exc:
            raise self.send_exc
        self.sent.append(raw)


@pytest.fixture
def fake_smtp(monkeypatch):
    class F(FakeSMTP):
        features: dict[str, str] = {}
        sent: list[bytes] = []
        contexts: list[Any] = []
        quits = 0

        def quit(self):
            type(self).quits += 1
    monkeypatch.setattr(email_mod.smtplib, "SMTP", F)
    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", F)
    return F


def send(p: EmailProvider, m: OutboundMessage | None = None):
    return asyncio.run(p.send(m or msg(), TO))


def test_accepted(fake_smtp):
    r = send(prov())
    assert r.outcome == "success" and r.provider_message_id == "<dlv_1@example.org>"
    assert r.request_summary["to_domain"] == "…@example.org" and "alex" not in str(r.request_summary)
    assert len(fake_smtp.sent) == 1


@pytest.mark.parametrize("exc,outcome", [
    (smtplib.SMTPRecipientsRefused({"x": (550, b"no")}), "permanent"),
    (smtplib.SMTPRecipientsRefused({"x": (450, b"later")}), "retryable"),
    (smtplib.SMTPDataError(554, b"spam"), "permanent"),
    (smtplib.SMTPSenderRefused(451, b"slow", "a@b.c"), "retryable"),
    (smtplib.SMTPServerDisconnected("gone"), "retryable"),
    (TimeoutError("t"), "retryable"),
])
def test_send_error_mapping(fake_smtp, exc, outcome):
    fake_smtp.send_exc = exc
    assert send(prov()).outcome == outcome


def test_not_supported_other_than_starttls(fake_smtp):
    fake_smtp.login_exc = smtplib.SMTPNotSupportedError("SMTP AUTH extension not supported by server.")
    r = send(prov(security="starttls", username="u", password="pw"))
    assert r.outcome == "retryable" and "STARTTLS" not in r.error and "AUTH" in r.error
    out = asyncio.run(prov(security="starttls", username="u", password="pw").test_connection())
    assert out["ok"] is False and "STARTTLS" not in out["error"] and "AUTH" in out["error"]
    fake_smtp.starttls_offered = False
    assert "STARTTLS" in asyncio.run(prov().__class__("m", {**GOOD, "security": "starttls"}, None).test_connection())["error"]


def test_tls_path_and_verify_off(fake_smtp):
    assert send(prov(security="tls", verify_tls=False)).outcome == "success"
    ctx = fake_smtp.contexts[-1]
    assert ctx.check_hostname is False and ctx.verify_mode == email_mod.ssl.CERT_NONE
    assert send(prov(security="tls")).outcome == "success"
    assert fake_smtp.contexts[-1].check_hostname is True


def test_size_checked_before_login_and_quits(fake_smtp):
    fake_smtp.features = {"size": "1000"}
    fake_smtp.login_exc = smtplib.SMTPAuthenticationError(535, b"bad")
    r = send(prov(security="starttls", username="u", password="pw"), msg(message="x" * 5000))
    assert r.outcome == "permanent" and fake_smtp.quits == 1


def test_non_numeric_size_ignored(fake_smtp):
    fake_smtp.features = {"size": "abc"}
    assert send(prov()).outcome == "success"
    assert asyncio.run(prov().test_connection())["size_limit"] is None


def test_short_password_not_scrubbed(fake_smtp):
    fake_smtp.login_exc = smtplib.SMTPAuthenticationError(535, b"login")
    p = prov(security="starttls", username="u", password="log")
    assert "login rejected" in asyncio.run(p.test_connection())["error"]


def test_hard_deadline_stops_stalled_send(fake_smtp):
    fake_smtp.block_send = True
    p = prov()
    p.timeout = 1
    t0 = time.monotonic()
    r = send(p)
    assert time.monotonic() - t0 < 5
    assert r.outcome == "retryable" and "exceeded 1 s" in r.error


def test_slow_connect_past_deadline_never_ehlos(fake_smtp):
    calls: list[str] = []
    orig_init = fake_smtp.__init__

    def slow_init(self, *a, **k):
        orig_init(self, *a, **k)
        time.sleep(1.3)
    fake_smtp.__init__ = slow_init
    fake_smtp.ehlo = lambda self: calls.append("ehlo")
    p = prov()
    p.timeout = 1
    r = send(p)
    assert r.outcome == "retryable" and "exceeded 1 s" in r.error
    assert calls == [] and fake_smtp.sent == []


def test_message_is_built_off_the_event_loop(fake_smtp, monkeypatch):
    import threading
    where: list[str] = []
    orig = EmailProvider.build_message

    def spy(self, m, a):
        where.append(threading.current_thread().name)
        return orig(self, m, a)
    monkeypatch.setattr(EmailProvider, "build_message", spy)
    assert send(prov()).outcome == "success" and where and where[0] != threading.main_thread().name

    def boom(self, m, a):
        raise ValueError("bad")
    monkeypatch.setattr(EmailProvider, "build_message", boom)
    r = send(prov())
    assert r.outcome == "permanent" and "could not build" in r.error


def test_auth_failure_retries_later(fake_smtp):
    fake_smtp.login_exc = smtplib.SMTPAuthenticationError(535, b"bad")
    r = send(prov(security="starttls", username="u", password="secret"))
    assert r.outcome == "retryable" and r.retry_after == 900 and "login rejected" in r.error
    assert "secret" not in r.error


def test_no_starttls(fake_smtp):
    fake_smtp.starttls_offered = False
    r = send(prov(security="starttls"))
    assert r.outcome == "retryable" and "STARTTLS" in r.error


def test_connection_refused(fake_smtp):
    fake_smtp.connect_exc = ConnectionRefusedError("no")
    assert send(prov()).outcome == "retryable"


def test_size_limit(fake_smtp):
    fake_smtp.features = {"size": "1000"}
    r = send(prov(), msg(message="x" * 5000))
    assert r.outcome == "permanent" and "server accepts at most" in r.error and not fake_smtp.sent


def test_config_problem_is_retryable_without_connecting(fake_smtp):
    r = send(prov(host=""))
    assert r.outcome == "retryable" and "host" in r.error


def test_connection_test(fake_smtp):
    fake_smtp.features = {"size": "5000"}
    assert asyncio.run(prov().test_connection()) == {"supported": True, "ok": True, "tls": "none", "size_limit": 5000}
    fake_smtp.login_exc = smtplib.SMTPAuthenticationError(535, b"bad")
    out = asyncio.run(prov(security="starttls", username="u", password="hunter2").test_connection())
    assert out["ok"] is False and "hunter2" not in str(out)


def test_deadline_exceeded(fake_smtp, monkeypatch):
    p = prov()
    left = p._deadline()
    monkeypatch.setattr(email_mod.time, "monotonic", lambda: 1e12)
    with pytest.raises(TimeoutError, match="exceeded 60 s"):
        left()


class Collect:
    def __init__(self):
        self.envelopes = []

    async def handle_DATA(self, server, session, envelope):
        self.envelopes.append(envelope)
        return "250 OK"


def test_real_smtp_server_with_attachment():
    h = Collect()
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    c = Controller(h, hostname="127.0.0.1", port=port)
    c.start()
    try:
        p = prov(host="127.0.0.1", port=port)
        att = AttachmentData("pic.png", "image/png", len(PNG), PNG)
        r = send(p, msg(attachments=[att]))
        assert r.outcome == "success", r.error
    finally:
        c.stop()
    env = h.envelopes[0]
    assert env.rcpt_tos == ["alex@example.org"] and env.mail_from == "hnl@example.org"
    got = email.message_from_bytes(env.original_content, policy=email.policy.default)
    atts = list(got.iter_attachments())
    assert len(atts) == 1 and atts[0].get_content() == PNG and atts[0].get_filename() == "pic.png"


def test_slow_smtp_not_reclaimed(tmp_path, fake, monkeypatch):
    calls: list[float] = []

    def slow(self, raw, sender, to):
        calls.append(time.monotonic())
        time.sleep(12)
        from notify.providers.base import SendResult
        return SendResult(outcome="success")
    monkeypatch.setattr(EmailProvider, "_send_sync", slow)
    d = make_config(lease_seconds=10).model_dump(mode="json", exclude_none=True)
    d["providers"]["mail"] = dict(GOOD)
    d["devices"]["alex-email"] = {"provider": "mail", "owner": "alex", "address": dict(TO)}
    d["users"]["alex"]["devices"] = ["alex-phone", "alex-email"]
    app = create_app(NotifyConfig.model_validate(d), db_path=str(tmp_path / "n.sqlite3"),
                     key_path=str(tmp_path / "master.key"), transport=httpx.MockTransport(fake.handler))
    srv = LiveServer(app, fake)
    srv.start()
    try:
        with srv.client() as c:
            r = c.post("/v1/notifications", json={"targets": ["device:alex-email"], "message": "hi", "kinds": ["email"]})
            assert r.status_code == 202, r.text
            nid = r.json()["id"]
            wait_for(lambda: c.get(f"/v1/notifications/{nid}").json()["deliveries"][0]["state"] == "dispatched", timeout=30)
        time.sleep(1)
        assert len(calls) == 1
        assert srv.service.db.one("SELECT COUNT(*) n FROM attempts")["n"] == 1
    finally:
        srv.stop()
