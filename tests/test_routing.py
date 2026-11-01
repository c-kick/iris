"""Routing by delivery kind with per-device opt-in."""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from notify.app import create_app
from notify.config import NotifyConfig
from notify.models import NotificationIn, Severity
from notify.providers import REGISTRY
from notify.providers.pushover import PushoverProvider
from notify.routing import Router

from tests.conftest import APP_TOKEN, USER_KEY, LiveServer, make_config


class FakeMail(PushoverProvider):
    type = "fakemail"
    kind = "email"


@pytest.fixture(autouse=True)
def fakemail(monkeypatch):
    monkeypatch.setitem(REGISTRY, "fakemail", FakeMail)


def cfg_dict(**dev_over) -> dict:
    addr = {"user_key": USER_KEY}
    devices = {
        "alex-phone": {"provider": "pushover", "owner": "alex", "address": addr},
        "alex-email": {"provider": "mail", "owner": "alex", "address": addr},
        "sam-phone": {"provider": "pushover", "owner": "sam", "address": addr},
    }
    for k, v in dev_over.items():
        devices[k.replace("_", "-")].update(v)
    return {
        "providers": {"pushover": {"type": "pushover", "app_token": APP_TOKEN},
                      "mail": {"type": "fakemail", "app_token": APP_TOKEN}},
        "devices": devices,
        "users": {"alex": {}, "sam": {}},
        "groups": {"household": {"members": ["user:alex", "user:sam", "device:alex-email"]}},
    }


def router(**dev_over) -> Router:
    return Router(NotifyConfig.model_validate(cfg_dict(**dev_over)))


def devs(plan) -> set[str]:
    return set(plan.devices)


def test_default_skips_opt_in_email():
    plan = router().resolve(["user:alex"], Severity.normal)
    assert devs(plan) == {"alex-phone"}
    assert plan.suppressed == []


def test_kinds_email_only():
    assert devs(router().resolve(["user:alex"], Severity.normal, ["email"])) == {"alex-email"}


def test_kinds_both():
    assert devs(router().resolve(["user:alex"], Severity.normal, ["push", "email"])) == {"alex-phone", "alex-email"}


def test_top_level_device_bypasses_opt_in():
    assert devs(router().resolve(["device:alex-email"], Severity.normal)) == {"alex-email"}


def test_group_member_device_does_not_bypass():
    assert devs(router().resolve(["group:household"], Severity.normal)) == {"alex-phone", "sam-phone"}


def test_no_email_device_suppressed():
    plan = router().resolve(["user:sam"], Severity.normal, ["email"])
    assert plan.devices == {}
    assert plan.suppressed == [{"target": "user:sam", "via": "user:sam", "reason": "no email device"}]


def test_top_level_device_kind_not_requested():
    plan = router().resolve(["device:alex-email"], Severity.normal, ["push"])
    assert plan.devices == {}
    reasons = [s["reason"] for s in plan.suppressed]
    assert "device kind email not requested" in reasons
    assert "no push device" in reasons


def test_opt_in_override():
    r = router(alex_email={"opt_in": False}, alex_phone={"opt_in": True})
    assert devs(r.resolve(["user:alex"], Severity.normal)) == {"alex-email"}


DIRECT_NOTE = {"target": "device:alex-email", "via": "user:alex",
               "reason": "direct-only device: name it as device:alex-email"}


def test_direct_only_skipped_via_user():
    plan = router(alex_email={"direct_only": True}).resolve(["user:alex"], Severity.normal, ["email"])
    assert plan.devices == {}
    assert plan.suppressed == [DIRECT_NOTE]


def test_direct_only_silent_when_kind_not_requested():
    plan = router(alex_email={"direct_only": True}).resolve(["user:alex"], Severity.normal)
    assert devs(plan) == {"alex-phone"}
    assert plan.suppressed == []


def test_direct_only_reached_when_named():
    r = router(alex_email={"direct_only": True})
    assert devs(r.resolve(["device:alex-email"], Severity.normal, ["email"])) == {"alex-email"}
    plan = r.resolve(["user:alex", "device:alex-email"], Severity.normal, ["push", "email"])
    assert devs(plan) == {"alex-phone", "alex-email"}
    assert not any(s["reason"].startswith("direct-only") for s in plan.suppressed)


def test_direct_only_reached_as_group_member_not_via_user_member():
    r = router(alex_email={"direct_only": True}, sam_phone={"direct_only": True})
    plan = r.resolve(["group:household"], Severity.normal, ["push", "email"])
    assert devs(plan) == {"alex-phone", "alex-email"}
    assert plan.suppressed == [{"target": "device:sam-phone", "via": "group:household",
                                "reason": "direct-only device: name it as device:sam-phone"}]


def test_direct_only_push_device_via_user():
    plan = router(alex_phone={"direct_only": True}).resolve(["user:alex"], Severity.normal)
    assert plan.devices == {}
    assert [s["reason"] for s in plan.suppressed] == ["direct-only device: name it as device:alex-phone"]


def test_kinds_validation():
    with pytest.raises(ValidationError):
        NotificationIn(targets=["x"], message="m", kinds=[])
    assert NotificationIn(targets=["x"], message="m", kinds=["email", "email"]).kinds == ["email"]
    with pytest.raises(ValidationError):
        NotificationIn(targets=["x"], message="m", kinds=["sms"])
    assert NotificationIn(targets=["x"], message="m").kinds is None


@pytest.fixture
def mail_live(tmp_path, fake):
    d = make_config().model_dump(mode="json", exclude_none=True)
    d["providers"]["mail"] = {"type": "fakemail", "app_token": APP_TOKEN, "api_base": "https://pushover.test/1"}
    d["devices"]["alex-email"] = {"provider": "mail", "owner": "alex", "address": {"user_key": USER_KEY}}
    d["users"]["alex"]["devices"] = ["alex-phone", "alex-email"]
    app = create_app(NotifyConfig.model_validate(d), db_path=str(tmp_path / "n.sqlite3"),
                     key_path=str(tmp_path / "master.key"), transport=httpx.MockTransport(fake.handler))
    srv = LiveServer(app, fake)
    srv.start()
    yield srv
    srv.stop()


def test_existing_sender_unchanged_with_email_device(mail_live):
    with mail_live.client() as c:
        r = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "hi"})
        assert r.status_code == 202, r.text
        n = c.get(f"/v1/notifications/{r.json()['id']}").json()
        assert [d["device"] for d in n["deliveries"]] == ["alex-phone"]
        assert "kinds" not in mail_live.service.db.one(
            "SELECT routing_json FROM notifications WHERE id=?", (n["id"],))["routing_json"]
        r = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "hi", "kinds": ["email"]})
        assert [d["device"] for d in r.json()["deliveries"]] == ["alex-email"]


def test_targets_show_kind_and_opt_in(mail_live):
    with mail_live.client() as c:
        devices = {d["target"]: d for d in c.get("/v1/targets").json()["devices"]}
        assert (devices["device:alex-phone"]["kind"], devices["device:alex-phone"]["opt_in"]) == ("push", False)
        assert (devices["device:alex-email"]["kind"], devices["device:alex-email"]["opt_in"]) == ("email", True)
        assert devices["device:alex-email"]["direct_only"] is False


def test_arr_kinds_param(mail_live):
    url = f"{mail_live.url}/v1/ingest/arr"
    auth = ("radarr", "tok-sender")
    r = httpx.post(url, params={"targets": "user:alex", "kinds": "email"}, json={"eventType": "Test"}, auth=auth, timeout=10)
    assert r.status_code == 202, r.text
    assert [d["device"] for d in r.json()["deliveries"]] == ["alex-email"]
    r = httpx.post(url, params={"targets": "user:alex", "kinds": "sms"}, json={"eventType": "Test"}, auth=auth, timeout=10)
    assert r.status_code == 422


def test_channel_floor_cut_adds_no_kind_entries():
    d = cfg_dict()
    d["channels"] = {"alerts": {"targets": ["user:alex"], "min_severity": "high"}}
    r = Router(NotifyConfig.model_validate(d))
    plan = r.resolve(["channel:alerts"], Severity.normal, ["email"])
    assert devs(plan) == set()
    assert [s["reason"] for s in plan.suppressed] == ["below channel min_severity high"]
    plan = r.resolve(["channel:alerts"], Severity.high, ["email"])
    assert devs(plan) == {"alex-email"}


def test_mcp_kinds_email_routes_only_email(mail_live):
    from tests.test_mcp import payload, run_session

    async def flow(s):
        return payload(await s.call_tool("send_notification", {"targets": ["user:alex"], "message": "m",
                                                               "kinds": ["email"]}))
    sent = run_session(mail_live, "agent", flow)
    assert [d["device"] for d in sent["deliveries"]] == ["alex-email"]
    assert '"kinds":["email"]' in mail_live.service.db.one(
        "SELECT routing_json FROM notifications WHERE id=?", (sent["id"],))["routing_json"]
