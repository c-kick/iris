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
    assert em["From"] == "hnl-notify <hnl@example.org>"
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


@pytest.mark.parametrize("bad", ["\x00", " ", "x" * 2000, "a b\x85c", "\r\n\r\n"])
def test_hostile_strings_never_raise(bad):
    att = AttachmentData(bad, "", 3, b"abc")
    p = prov(subject_prefix=bad, from_name=bad)
    raw = p.build_message(msg(title=bad, message=bad, attachments=[att]), TO).as_bytes()
    assert b"\r\nBcc:" not in raw
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


class FakeSMTP:
    features: dict[str, str] = {}
    starttls_offered = True
    login_exc: Exception | None = None
    send_exc: Exception | None = None
    connect_exc: Exception | None = None
    sent: list[bytes] = []

    class sock:
        @staticmethod
        def settimeout(t: float) -> None:
            pass

    def __init__(self, host: str, port: int, timeout: float = 0, context: Any = None):
        if self.connect_exc:
            raise self.connect_exc
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
        if self.send_exc:
            raise self.send_exc
        self.sent.append(raw)


@pytest.fixture
def fake_smtp(monkeypatch):
    class F(FakeSMTP):
        features: dict[str, str] = {}
        sent: list[bytes] = []
    monkeypatch.setattr(email_mod.smtplib, "SMTP", F)
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
