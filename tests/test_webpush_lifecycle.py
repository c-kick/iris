"""Core handling of `report` tracking: browser reports, early reports, lifetime sweep, attention."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import httpx
import pytest

from notify.app import create_app
from notify.config import NotifyConfig
from notify.db import iso, utcnow
from notify.providers.base import SendResult
from notify.webpush_crypto import generate_vapid_keys
from tests.conftest import LiveServer, make_config, wait_for
from tests.test_attachments_api import get, submit

UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
PRIV, PUB = generate_vapid_keys()


def webpush_config() -> NotifyConfig:
    d = make_config(external_url="https://notify.example.org:8931").model_dump(mode="json", exclude_none=True)
    d["providers"]["browser"] = {"type": "webpush", "vapid_private_key": PRIV, "vapid_public_key": PUB,
                                 "contact": "mailto:alex@example.org"}
    d["devices"]["alex-browser"] = {"provider": "browser", "owner": "alex", "address": {
        "endpoint": "https://fcm.googleapis.com/fcm/send/abc", "p256dh": UA_PUBLIC, "auth": "BTBZMqHH6r4Tts7J_aSIgg"}}  # ggignore: RFC 8291 test vector
    d["users"]["alex"]["devices"] = ["alex-browser"]
    return NotifyConfig.model_validate(d)


@pytest.fixture
def wp(tmp_path, fake):
    app = create_app(webpush_config(), db_path=str(tmp_path / "wp.sqlite3"), key_path=str(tmp_path / "master.key"),
                     transport=httpx.MockTransport(fake.handler))
    srv = LiveServer(app, fake)
    srv.start()
    yield srv
    srv.stop()


def delivery(wp, nid) -> Any:
    return wp.service.db.one("SELECT * FROM deliveries WHERE notification_id=?", (nid,))


def sent(wp, **body):
    r = submit(wp, targets=["device:alex-browser"], **body)
    assert r.status_code == 202, r.text
    nid = r.json()["id"]
    wait_for(lambda: delivery(wp, nid)["state"] == "dispatched")
    return nid, delivery(wp, nid)["id"]


def report(wp, did, kind):
    wp.call(lambda: wp.service.apply_report(did, kind, "alex-browser"))


def test_shown_then_clicked(wp):
    nid, did = sent(wp)
    report(wp, did, "shown")
    assert delivery(wp, nid)["state"] == "delivered"
    report(wp, did, "clicked")
    d = delivery(wp, nid)
    assert d["state"] == "acknowledged" and d["acknowledged_by"] == "alex-browser" and d["delivered_at"]


def test_clicked_implies_shown(wp):
    nid, did = sent(wp)
    report(wp, did, "clicked")
    d = delivery(wp, nid)
    assert d["state"] == "acknowledged" and d["delivered_at"]


def test_report_before_dispatch_is_applied(wp):
    provider = wp.service.providers["browser"]

    async def early(msg, address):
        wp.service.apply_report(msg.delivery_id, "shown", "alex-browser")
        return SendResult(outcome="success", http_status=201)
    provider.send = early
    r = submit(wp, targets=["device:alex-browser"])
    nid = r.json()["id"]
    wait_for(lambda: delivery(wp, nid)["state"] == "delivered")
    assert get(wp, nid)["status"] == "delivered"


def test_early_click_ends_acknowledged(wp):
    provider = wp.service.providers["browser"]

    async def early(msg, address):
        wp.service.apply_report(msg.delivery_id, "clicked", "alex-browser")
        return SendResult(outcome="success", http_status=201)
    provider.send = early
    nid = submit(wp, targets=["device:alex-browser"]).json()["id"]
    wait_for(lambda: delivery(wp, nid)["state"] == "acknowledged")
    assert delivery(wp, nid)["acknowledged_by"] == "alex-browser"


def test_report_for_other_states_is_ignored(wp):
    nid, did = sent(wp)
    report(wp, did, "clicked")
    report(wp, did, "shown")
    report(wp, "dlv_missing", "shown")
    assert delivery(wp, nid)["state"] == "acknowledged"


def age_dispatch(wp, nid, days=2):
    with wp.service.db.tx() as c:
        c.execute("UPDATE deliveries SET dispatched_at=? WHERE notification_id=?",
                  (iso(utcnow() - timedelta(days=days)), nid))


def test_unshown_push_expires(wp):
    nid, _ = sent(wp)
    assert wp.call(wp.service.expire_reports) == 0
    age_dispatch(wp, nid)
    assert wp.call(wp.service.expire_reports) == 1
    assert delivery(wp, nid)["state"] == "expired"
    assert get(wp, nid)["status"] in ("expired", "failed")


def test_unacked_high_expires_but_normal_delivered_finalizes(wp):
    n1, d1 = sent(wp, severity="high")
    n2, d2 = sent(wp, message="calm")
    for n, d in ((n1, d1), (n2, d2)):
        report(wp, d, "shown")
        age_dispatch(wp, n)
    wp.call(wp.service.expire_reports)
    high, normal = delivery(wp, n1), delivery(wp, n2)
    assert high["state"] == "expired" and high["delivered_at"]
    assert normal["state"] == "delivered" and normal["finalized_at"]
    assert get(wp, n2)["status"] == "delivered"
    assert wp.call(wp.service.expire_reports) == 0


def test_attention_awaits_ack_only_for_high(wp):
    n1, d1 = sent(wp, message="calm")
    n2, d2 = sent(wp, severity="high")
    report(wp, d1, "shown")
    report(wp, d2, "shown")
    waiting = wp.client().get("/v1/attention").json()["awaiting_acknowledgement"]
    assert [w["delivery_id"] for w in waiting] == [d2]


def test_report_delivery_not_final_at_dispatch(wp):
    nid, _ = sent(wp)
    assert delivery(wp, nid)["finalized_at"] is None


def test_report_racing_the_dispatch_commit_is_not_lost(wp):
    """The dispatch commit lands just before apply_report's transaction opens."""
    from notify.db import Database
    svc = wp.service
    provider = svc.providers["browser"]
    real_tx = Database.tx
    armed = {"on": False}

    async def send(msg, address):
        armed["on"] = True
        svc.apply_report(msg.delivery_id, "shown", "alex-browser")
        return SendResult(outcome="success", http_status=201)

    def tx(self):
        if armed["on"]:
            armed["on"] = False
            svc.complete_send(svc.db.one("SELECT * FROM deliveries WHERE state='sending'"),
                              SendResult(outcome="success", http_status=201), iso(utcnow()))
        return real_tx(self)

    provider.send = send
    Database.tx = tx
    try:
        nid = submit(wp, targets=["device:alex-browser"], severity="high").json()["id"]
        wait_for(lambda: delivery(wp, nid)["state"] == "delivered")
    finally:
        Database.tx = real_tx


def test_rejected_device_stays_not_ready_across_rebuild(wp):
    def ready() -> bool:
        return next(d for d in wp.service.targets()["devices"] if d["target"] == "device:alex-browser")["ready"]

    assert ready()
    wp.fake.push_responses += [(410, {}), (410, {})]  # the push, then the alert to the same (only) device
    nid = submit(wp, targets=["device:alex-browser"]).json()["id"]
    wait_for(lambda: delivery(wp, nid)["state"] == "failed")
    wait_for(lambda: wp.service.db.one("SELECT 1 FROM meta WHERE key='device_invalid:alex-browser'"))
    wait_for(lambda: not wp.fake.push_responses)
    time.sleep(0.3)
    assert not ready()
    wp.call(lambda: wp.app.state.apply_config(wp.app.state.config))  # providers are rebuilt, memory is lost
    assert not ready()
    again = submit(wp, targets=["device:alex-browser"]).json()
    assert any("rejected" in g for g in get(wp, again["id"])["deliveries"][0]["capability_gaps"])
    wait_for(lambda: delivery(wp, again["id"])["state"] == "dispatched")
    wait_for(ready)
