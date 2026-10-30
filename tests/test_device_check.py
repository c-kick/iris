"""Pushover device names are checked before a push: Pushover sends a message for an unknown device
name to every other device on the account, so such a push fails instead, and an admin hears about it once."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx

from notify.models import Severity
from notify.providers.base import OutboundMessage
from notify.providers.pushover import PushoverProvider
from tests.conftest import APP_TOKEN, USER_KEY, FakePushover, wait_for
from tests.test_attachments_api import get, submit

ADDRESS = {"user_key": USER_KEY, "device": "iphone"}


def _provider(fake: FakePushover, **settings) -> PushoverProvider:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return PushoverProvider("pushover", {"app_token": APP_TOKEN, "api_base": "https://pushover.test/1", **settings},
                            http)


def _msg() -> OutboundMessage:
    return OutboundMessage("ntf_1", "dlv_1", "T", "hi", Severity.normal, None, None, [], [], False,
                           datetime.now(UTC), None)


def _send(p: PushoverProvider, address=ADDRESS):
    return asyncio.run(p.send(_msg(), address))


def test_unknown_device_fails_without_sending():
    fake = FakePushover(devices=["ipad"])
    p = _provider(fake)
    r = _send(p)
    assert r.outcome == "permanent"
    assert "'iphone'" in r.error and "device name is not valid" in r.error
    assert r.address_check is not None and r.address_check.valid is False
    assert fake.messages == []
    assert p.address_problems(ADDRESS) and "'iphone'" in p.address_problems(ADDRESS)[0]


def test_valid_device_is_checked_once_per_interval():
    fake = FakePushover()
    p = _provider(fake)
    first, second = _send(p), _send(p)
    assert first.outcome == second.outcome == "success"
    assert first.address_check is not None and first.address_check.valid
    assert second.address_check is None  # cached
    assert len(fake.validations) == 1 and fake.validations[0]["device"] == "iphone"
    assert len(fake.messages) == 2


def test_stale_check_is_repeated():
    fake = FakePushover()
    p = _provider(fake, device_check_seconds=0)
    _send(p), _send(p)
    assert len(fake.validations) == 2


def test_invalid_answer_is_not_cached():
    fake = FakePushover(devices=["ipad"])
    p = _provider(fake)
    assert _send(p).outcome == "permanent"
    fake.devices = ["iphone"]
    r = _send(p)
    assert r.outcome == "success" and r.address_check.valid
    assert p.address_problems(ADDRESS) == []
    assert len(fake.messages) == 1


def test_check_trouble_does_not_hold_the_push():
    fake = FakePushover(validate_status=500)
    p = _provider(fake)
    r = _send(p)
    assert r.outcome == "success" and r.address_check is None
    assert len(fake.messages) == 1


def test_known_bad_device_stays_blocked_when_the_check_gets_no_answer():
    fake = FakePushover(devices=["ipad"])
    p = _provider(fake)
    assert _send(p).outcome == "permanent"
    fake.validate_status = 500
    r = _send(p)
    assert r.outcome == "permanent" and r.address_check is None  # still flagged, nothing new learned
    assert fake.messages == []


def test_address_without_device_is_not_checked():
    fake = FakePushover()
    p = _provider(fake)
    assert _send(p, {"user_key": USER_KEY}).outcome == "success"
    assert fake.validations == []


def _device(live, name="alex-phone"):
    return next(d for d in live.client("admin").get("/v1/targets").json()["devices"]
                if d["target"] == f"device:{name}")


def _alerts(live):
    return live.service.db.query("SELECT * FROM notifications WHERE source='hnl-notify/device-check'")


def test_invalid_device_fails_delivery_flags_device_and_alerts_once(live, fake):
    wait_for(lambda: fake.validations)  # the start-up check passed and is cached
    live.service.providers["pushover"]._checked.clear()
    fake.devices = ["ipad"]
    nid = submit(live).json()["id"]
    d = wait_for(lambda: (x := get(live, nid)["deliveries"][0])["state"] == "failed" and x)
    assert "'iphone'" in d["last_error"]
    assert fake.messages == []
    dev = _device(live)
    assert dev["ready"] is False and any("'iphone'" in p for p in dev["problems"])
    alerts = wait_for(lambda: _alerts(live))
    assert len(alerts) == 1 and "alex-phone" in alerts[0]["title"]

    nid2 = submit(live).json()["id"]
    wait_for(lambda: get(live, nid2)["deliveries"][0]["state"] == "failed")
    assert len(_alerts(live)) == 1  # still the same problem: no second alert

    # fixed on the Pushover side: the next push checks again, sends, and clears the flag
    fake.devices = ["iphone"]
    nid3 = submit(live).json()["id"]
    wait_for(lambda: get(live, nid3)["deliveries"][0]["state"] == "dispatched")
    assert _device(live)["ready"] is True

    # broken again later: a new alert
    live.service.providers["pushover"]._checked.clear()
    fake.devices = []
    nid4 = submit(live).json()["id"]
    wait_for(lambda: get(live, nid4)["deliveries"][0]["state"] == "failed")
    wait_for(lambda: len(_alerts(live)) == 2)


def test_devices_are_checked_at_start_and_after_a_config_change(live, fake):
    assert wait_for(lambda: [v for v in fake.validations if v.get("device") == "iphone"])
    fake.devices = ["ipad"]
    before = len(fake.validations)
    live.call(lambda: live.app.state.apply_config(live.app.state.config))
    wait_for(lambda: len(fake.validations) > before)
    assert wait_for(lambda: _device(live)["ready"] is False)
    assert wait_for(lambda: _alerts(live))
