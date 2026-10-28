"""Scheduling: release problems in attention, dismiss and retry, health and metrics (live server)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from notify.db import iso
from tests.conftest import wait_for
from tests.test_schedule_release import get, make_due, row, sched, set_times


def attention(srv, who="agent"):
    with srv.client(who) as c:
        return c.get("/v1/attention").json()


def missed(srv, who="agent"):
    nid = sched(srv, who=who)
    make_due(srv, nid, late=7200, limit=60)
    wait_for(lambda: row(srv, nid)["status"] == "expired")
    return nid


def test_missed_listed_and_dismissed(live):
    nid = missed(live)
    item = attention(live)["release_problems"][0]
    assert item["notification_id"] == nid and item["reason"].startswith("missed:")
    assert item["status"] == "expired" and item["client"] == "agent" and item["at"] and item["send_at"]
    before = get(live, nid)
    with live.client("admin") as c:
        assert c.post(f"/admin/api/notifications/{nid}/dismiss").json() == {"dismissed": 1}
        assert c.post(f"/admin/api/notifications/{nid}/dismiss").status_code == 409
    assert attention(live)["release_problems"] == []
    after = get(live, nid)
    assert after == before and after["status"] == "expired"


def test_retry_missed_sends(live, fake):
    nid = missed(live)
    assert fake.messages == []
    with live.client() as c:
        r = c.post(f"/v1/notifications/{nid}/retry")
    assert r.status_code == 200 and r.json()["results"] == [{"result": "released"}]
    wait_for(lambda: get(live, nid)["status"] == "dispatched")
    assert row(live, nid)["release_problem"] is None and len(fake.messages) == 1
    assert attention(live)["release_problems"] == []


def test_retry_expired_409(live, fake):
    nid = missed(live)
    with live.service.db.tx() as c:
        c.execute("UPDATE notifications SET expires_at=? WHERE id=?",
                  (iso(datetime.now(UTC) - timedelta(seconds=5)), nid))
    with live.client() as c:
        r = c.post(f"/v1/notifications/{nid}/retry")
    assert r.status_code == 409 and "expired" in r.text
    assert fake.messages == []


def test_overdue_in_attention_and_health(live, monkeypatch):
    monkeypatch.setattr(live.service.scheduler, "release_due", lambda: 0)
    nid = sched(live)
    with live.client() as c:
        assert c.get("/health/ready").json()["checks"]["scheduler"] == "ok"
    set_times(live, nid, late=120)
    item = attention(live)["release_problems"][0]
    assert item["notification_id"] == nid and item["reason"].startswith("overdue: still waiting 1")
    assert item["status"] == "scheduled"
    with live.client() as c:
        r = c.get("/health/ready")
    assert r.status_code == 503 and r.json()["checks"]["scheduler"] == "1 overdue"


def test_metrics_lines(live):
    with live.client() as c:
        assert "notify_scheduled_next_seconds" not in c.get("/metrics").text
    nid = sched(live)
    with live.client() as c:
        text = c.get("/metrics").text
    assert "notify_scheduled_overdue 0" in text
    line = next(ln for ln in text.splitlines() if ln.startswith("notify_scheduled_next_seconds "))
    assert 50 < float(line.split()[1]) <= 61
    assert nid


def test_problems_own_only(live):
    nid = missed(live)
    assert attention(live, "other")["release_problems"] == []
    assert attention(live, "admin")["release_problems"][0]["notification_id"] == nid
