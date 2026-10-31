"""Web push provider: encrypted, VAPID-signed delivery to browser push services."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx

from notify.models import ACKNOWLEDGED, DELIVERED, Severity
from notify.providers.base import AttachmentData, OutboundMessage
from notify.providers.webpush import WebPushProvider
from notify.webpush_crypto import generate_vapid_keys
from tests.conftest import FakePushover

UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
ADDRESS = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc123", "p256dh": UA_PUBLIC, "auth": AUTH}
PRIV, PUB = generate_vapid_keys()


def _provider(fake: FakePushover, **settings) -> WebPushProvider:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return WebPushProvider("browser", {"vapid_private_key": PRIV, "vapid_public_key": PUB,
                                       "contact": "mailto:alex@example.org", **settings}, http)


def _msg(**kw) -> OutboundMessage:
    base = dict(notification_id="ntf_1", delivery_id="dlv_1", title="T", message="hi", severity=Severity.normal,
                url=None, url_title=None, actions=[], tags=[], require_ack=False, created_at=datetime.now(UTC),
                expires_at=None, external_url="https://notify.example.org")
    base.update(kw)
    return OutboundMessage(**base)


def _send(p, msg=None, address=ADDRESS):
    return asyncio.run(p.send(msg or _msg(), address))


def test_send_encrypts_and_signs():
    fake = FakePushover()
    r = _send(_provider(fake))
    assert r.outcome == "success" and r.address_check is not None and r.address_check.valid
    (req,) = fake.push_requests
    assert req.headers["content-encoding"] == "aes128gcm"
    assert req.headers["ttl"] == "86400"
    assert req.headers["urgency"] == "normal"
    assert req.headers["authorization"].startswith("vapid t=")
    assert len(req.content) <= 4096
    assert r.request_summary["endpoint"] == "fcm.googleapis.com/…abc123"
    assert r.request_summary["bytes"] == len(req.content)


def test_plan_tracking_report_and_attachment_gap():
    p = _provider(FakePushover())
    msg = _msg(attachments=[AttachmentData("a.png", "image/png", 10)])
    plan = p.plan(msg, ADDRESS)
    assert plan.tracking == "report"
    assert DELIVERED in plan.observable_states and ACKNOWLEDGED in plan.observable_states
    assert plan.capability_gaps == ["attachments: not supported on browser push"]


def test_gone_subscription_flags_device():
    fake = FakePushover(push_responses=[(410, {})])
    p = _provider(fake)
    r = _send(p)
    assert r.outcome == "permanent"
    assert r.address_check is not None and r.address_check.valid is False
    assert "re-enroll" in r.address_check.problem
    assert p.address_problems(ADDRESS)
    assert _send(p).outcome == "success"
    assert p.address_problems(ADDRESS) == []


def test_429_retry_after():
    fake = FakePushover(push_responses=[(429, {"Retry-After": "120"})])
    r = _send(_provider(fake))
    assert r.outcome == "retryable" and r.retry_after == 120


def test_5xx_retryable_and_other_4xx_permanent():
    fake = FakePushover(push_responses=[(503, {}), (400, {}), (413, {})])
    p = _provider(fake)
    assert [_send(p).outcome for _ in range(3)] == ["retryable", "permanent", "permanent"]


def test_ssrf_endpoint_refused():
    fake = FakePushover()
    p = _provider(fake)
    bad = {**ADDRESS, "endpoint": "https://192.168.1.10/x"}
    r = _send(p, address=bad)
    assert r.outcome == "permanent" and fake.push_requests == []
    assert p.device_problems(bad)
    assert p.device_problems({**ADDRESS, "endpoint": "http://fcm.googleapis.com/x"})
    assert p.device_problems(ADDRESS) == []


def test_bad_keys_are_permanent_not_a_crash():
    fake = FakePushover()
    r = _send(_provider(fake), address={**ADDRESS, "p256dh": "AAAA"})
    assert r.outcome == "permanent" and r.error and fake.push_requests == []


def test_payload_fits_4096():
    p = _provider(FakePushover())
    msg = _msg(title="t" * 250, message="\U0001F600" * 1024, severity=Severity.high)
    plain = p.build_payload(msg)
    data = json.loads(plain)
    assert len(plain) <= 3993 and data["b"].endswith("…") and data["ri"] is True and data["v"] == 1
    assert data["r"] == "https://notify.example.org/push/report" and data["k"] == p.report_token("dlv_1")
    assert data["id"] == "dlv_1" and data["s"] == "high" and data["u"] is None
    from notify.webpush_crypto import encrypt
    assert len(encrypt(plain, UA_PUBLIC, AUTH)) <= 4096


def test_payload_url_from_action():
    p = _provider(FakePushover())
    msg = _msg(actions=[{"id": "x", "label": "X"}, {"id": "o", "label": "Open", "url": "https://example.org/o"}])
    assert json.loads(p.build_payload(msg))["u"] == "https://example.org/o"


def test_ttl_from_expiry_and_urgency():
    fake = FakePushover()
    p = _provider(fake)
    _send(p, _msg(severity=Severity.min, expires_at=datetime.now(UTC) + timedelta(seconds=600)))
    req = fake.push_requests[0]
    assert req.headers["urgency"] == "very-low" and 595 <= int(req.headers["ttl"]) <= 600


def test_report_token_roundtrip():
    p = _provider(FakePushover())
    t = p.report_token("dlv_1")
    assert len(t) == 22 and p.report_ok("dlv_1", t)
    assert not p.report_ok("dlv_2", t) and not p.report_ok("dlv_1", "x" * 22)
    other = WebPushProvider("o", {"vapid_private_key": generate_vapid_keys()[0], "contact": "mailto:a@example.org"},
                            p.http)
    assert not other.report_ok("dlv_1", t)


def test_no_https_external_url_fails():
    fake = FakePushover()
    p = _provider(fake)
    for url in (None, "http://notify.example.org"):
        r = _send(p, _msg(external_url=url))
        assert r.outcome == "permanent" and "https external URL" in r.error
    assert fake.push_requests == []


def test_config_problems():
    p = _provider(FakePushover(), contact="alex@example.org")
    assert any("mailto" in x for x in p.config_problems())
    http = httpx.AsyncClient()
    assert WebPushProvider("b", {"contact": "mailto:a@example.org"}, http).config_problems()


def test_retry_after_is_finite_and_clamped():
    for value, expected in (("inf", None), ("nan", None), ("-5", None), ("abc", None), ("1e999", None),
                            ("99999", 3600), ("0", 0), ("120", 120)):
        fake = FakePushover(push_responses=[(429, {"Retry-After": value})])
        r = _send(_provider(fake))
        assert r.outcome == "retryable" and r.retry_after == expected, value


def test_corrupt_private_key_is_a_config_problem():
    p = _provider(FakePushover(), vapid_private_key="AAAA")
    assert any("vapid_private_key" in x for x in p.config_problems())
    assert _provider(FakePushover()).config_problems() == []


def test_vapid_signing_failure_is_permanent(monkeypatch):
    import notify.providers.webpush as mod

    def boom(*a, **k):
        raise ValueError("bad key")
    monkeypatch.setattr(mod, "vapid_authorization", boom)
    fake = FakePushover()
    r = _send(_provider(fake))
    assert r.outcome == "permanent" and fake.push_requests == []
