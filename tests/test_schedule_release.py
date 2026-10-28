"""Scheduling: the release door, per-row isolation and lateness up to dispatch (live server)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

from notify import schedule
from notify.admin import UI_CLIENT
from notify.core import SYSTEM_CLIENT
from notify.db import iso
from tests.conftest import PNG, wait_for

PNG_B64 = base64.b64encode(PNG).decode()


def sched(srv, who="agent", **body):
    body.setdefault("targets", ["user:alex"])
    body.setdefault("message", "hello")
    body.setdefault("delay", 61)
    with srv.client(who) as c:
        r = c.post("/v1/notifications", json=body)
    assert r.status_code == 202, r.text
    return r.json()["id"]


def set_times(srv, nid, late=0.0, limit=3600):
    """Pretend the notification was due `late` seconds ago with a lateness limit of `limit` seconds."""
    send = datetime.now(UTC) - timedelta(seconds=late)
    with srv.service.db.tx() as c:
        c.execute("UPDATE notifications SET send_at=?, max_late_seconds=?, deliver_by=? WHERE id=?",
                  (iso(send), limit, iso(send + timedelta(seconds=limit)), nid))


def row(srv, nid):
    return srv.service.db.one("SELECT * FROM notifications WHERE id=?", (nid,))


def get(srv, nid):
    with srv.client() as c:
        return c.get(f"/v1/notifications/{nid}").json()


def event_types(srv, nid):
    return [e["type"] for e in get(srv, nid)["events"]]


def make_due(srv, nid, **kw):
    set_times(srv, nid, **kw)
    srv.service.wake.set()


def test_internal_client_ids():
    assert schedule.UI_CLIENT_ID == UI_CLIENT.id == "admin-ui" and SYSTEM_CLIENT.id == "hnl-notify"


def test_released_when_due(live, fake):
    nid = sched(live)
    assert fake.messages == []
    make_due(live, nid, late=1)
    wait_for(lambda: get(live, nid)["status"] == "dispatched")
    assert len(fake.messages) == 1
    r = row(live, nid)
    assert r["released_at"] and r["release_problem"] is None
    ev = get(live, nid)["events"]
    created = next(e for e in ev if e["type"] == "notification.created")
    assert created["data"]["scheduled"] is True
    assert "notification.scheduled" in [e["type"] for e in ev]


def test_routing_decided_at_release(live, fake):
    nid = sched(live, targets=["device:spare"], attachments=[
        {"filename": "door.png", "content_type": "image/png", "data_base64": PNG_B64}])
    live.service.config.devices["spare"].enabled = True  # enabled after scheduling
    make_due(live, nid)
    wait_for(lambda: get(live, nid)["status"] == "dispatched")
    n = get(live, nid)
    assert [d["device"] for d in n["deliveries"]] == ["spare"]
    assert len(fake.messages) == 1  # the attachment travels as Pushover's picture


def test_restart_overdue_two(live, fake, monkeypatch):
    # release_due is exactly the start-up path: hold it off, build overdue rows, let one pass handle them
    monkeypatch.setattr(live.service.scheduler, "release_due", lambda: 0)
    a, b = sched(live), sched(live)
    set_times(live, a, late=600, limit=3600)
    set_times(live, b, late=7200, limit=3600)
    monkeypatch.undo()
    live.service.wake.set()
    wait_for(lambda: row(live, a)["status"] != "scheduled" and row(live, b)["status"] != "scheduled")
    wait_for(lambda: get(live, a)["status"] == "dispatched")
    rb = row(live, b)
    assert rb["status"] == "expired" and rb["release_problem"].startswith("missed: due at ")
    assert rb["released_at"] and "notification.missed" in event_types(live, b)
    assert len(fake.messages) == 1


def test_sender_gone(live, fake):
    nid = sched(live, who="other")
    del live.service.config.clients["other"]
    make_due(live, nid)
    wait_for(lambda: row(live, nid)["status"] == "failed")
    assert row(live, nid)["release_problem"] == "sender no longer exists" and fake.messages == []


def test_routing_error_at_release(live, fake):
    nid = sched(live)
    del live.service.config.users["alex"]
    make_due(live, nid)
    wait_for(lambda: row(live, nid)["status"] == "failed")
    assert row(live, nid)["release_problem"].startswith("routing failed")


def test_zero_devices_is_problem(live, fake):
    nid = sched(live, targets=["device:spare"])
    make_due(live, nid)
    wait_for(lambda: row(live, nid)["status"] == "suppressed")
    r = row(live, nid)
    assert r["release_problem"].startswith("routed to no device at send time") and "device disabled" in r["release_problem"]
    assert r["released_at"]


def test_bad_row_does_not_block(live, fake, monkeypatch):
    bad = sched(live, title="bad")
    set_times(live, bad)
    orig = live.service._plan_deliveries

    def plan(nrow, route, meta):
        if nrow["id"] == bad:
            raise RuntimeError("boom")
        return orig(nrow, route, meta)

    monkeypatch.setattr(live.service, "_plan_deliveries", plan)
    live.service.wake.set()
    with live.client() as c:
        c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "now"})
    wait_for(lambda: row(live, bad)["status"] == "failed")
    assert row(live, bad)["release_problem"] == "release error: RuntimeError: boom"
    wait_for(lambda: any(m["message"] == "now" for m in fake.messages))


def test_released_once(live, fake):
    nid = sched(live)
    set_times(live, nid)
    assert live.call(lambda: live.service.scheduler.release(nid)) in ("pending", "dispatched")
    again = live.call(lambda: live.service.scheduler.release(nid))
    assert again != "scheduled"
    assert live.service.db.scalar("SELECT COUNT(*) FROM deliveries WHERE notification_id=?", (nid,)) == 1
    assert row(live, nid)["status"] != "scheduled"


def test_deliver_by_bounds_retries(live, fake):
    fake.responses = [(500, {"status": 0})] * 5
    nid = sched(live)
    make_due(live, nid, late=1, limit=3600)
    wait_for(lambda: len(fake.messages) == 1 and get(live, nid)["deliveries"][0]["state"] == "queued")
    with live.service.db.tx() as c:  # the lateness limit passes while the retry waits
        c.execute("UPDATE notifications SET deliver_by=? WHERE id=?",
                  (iso(datetime.now(UTC) - timedelta(seconds=1)), nid))
    live.service.wake.set()
    wait_for(lambda: get(live, nid)["deliveries"][0]["state"] == "expired")
    assert row(live, nid)["release_problem"] == "not sent within its lateness limit"
    live.fast_forward()
    live.service.wake.set()
    import time
    time.sleep(1.5)
    assert len(fake.messages) == 1


def test_deliver_by_caps_backoff(live, fake):
    fake.responses = [(500, {"status": 0})]
    nid = sched(live)
    make_due(live, nid, late=1, limit=60)  # first retry backs off at least 5 s; limit 59 s ahead
    with live.service.db.tx() as c:
        c.execute("UPDATE notifications SET deliver_by=? WHERE id=?",
                  (iso(datetime.now(UTC) + timedelta(seconds=2)), nid))
    wait_for(lambda: get(live, nid)["deliveries"][0]["state"] == "expired")
    assert row(live, nid)["release_problem"] == "not sent within its lateness limit"


def test_deliver_by_not_sent_to_provider(live, fake):
    nid = sched(live, max_late_seconds=120)
    make_due(live, nid, limit=120)
    wait_for(lambda: fake.messages)
    assert "ttl" not in fake.messages[0]


def test_timestamp_is_release_time(live, fake):
    nid = sched(live)
    with live.service.db.tx() as c:
        c.execute("UPDATE notifications SET created_at=? WHERE id=?",
                  (iso(datetime.now(UTC) - timedelta(days=7)), nid))
    make_due(live, nid)
    wait_for(lambda: fake.messages)
    assert abs(int(fake.messages[0]["timestamp"]) - datetime.now(UTC).timestamp()) < 30


def test_reply_without_email_device_is_problem(email_live):
    nid = sched(email_live, reply=True, kinds=["push"])
    make_due(email_live, nid)
    wait_for(lambda: row(email_live, nid)["status"] != "scheduled")
    r = row(email_live, nid)
    assert r["release_problem"] == "reply requested but no email device received it"
    assert r["released_at"] and email_live.service.db.scalar(
        "SELECT COUNT(*) FROM deliveries WHERE notification_id=?", (nid,)) == 1


def test_manual_retry_clears_lateness_limit(live, fake):
    fake.responses = [(400, {"status": 0, "errors": ["nope"]})]
    nid = sched(live)
    make_due(live, nid, late=1)
    wait_for(lambda: get(live, nid)["deliveries"] and get(live, nid)["deliveries"][0]["state"] == "failed")
    with live.service.db.tx() as c:
        c.execute("UPDATE notifications SET deliver_by=? WHERE id=?",
                  (iso(datetime.now(UTC) - timedelta(seconds=5)), nid))
    with live.client() as c:
        assert c.post(f"/v1/notifications/{nid}/retry").status_code == 200
    wait_for(lambda: get(live, nid)["status"] == "dispatched")
    assert row(live, nid)["deliver_by"] is None


def test_release_wakes_bus_for_every_outcome(live):
    calls = []
    live.service._after_commit = lambda dispatch=False: calls.append(dispatch)
    nid = sched(live)
    set_times(live, nid, late=7200, limit=60)  # missed
    assert live.call(lambda: live.service.scheduler.release(nid)) == "expired"
    assert calls and calls[-1] is True
    del live.service._after_commit
