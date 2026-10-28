"""Scheduling: submitting with send_at / delay (REST, live server)."""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime, timedelta

from notify import schedule
from notify.db import parse_iso
from tests.conftest import PNG

PNG_B64 = base64.b64encode(PNG).decode()


def submit(srv, who="agent", **body):
    body.setdefault("targets", ["user:alex"])
    body.setdefault("message", "hello")
    with srv.client(who) as c:
        return c.post("/v1/notifications", json=body)


def inline(data=PNG_B64):
    return {"filename": "door.png", "content_type": "image/png", "data_base64": data}


def at(hours=1.0):
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


def test_schedule_by_delay(live, fake):
    r = submit(live, delay="1h")
    assert r.status_code == 202, r.text
    n = r.json()
    assert n["status"] == "scheduled" and n["deliveries"] == []
    send = parse_iso(n["send_at"])
    assert abs((send - datetime.now(UTC)).total_seconds() - 3600) < 30
    assert parse_iso(n["deliver_by"]) - send == timedelta(hours=1) and n["max_late_seconds"] == 3600
    assert n["released_at"] is None
    assert n["routing_preview"]["devices"][0]["device"] == "alex-phone"
    assert n["routing_preview"]["suppressed"] == []
    time.sleep(1.5)
    assert fake.messages == []
    got = live.client().get(f"/v1/notifications/{n['id']}").json()
    assert got["status"] == "scheduled" and got["routing_preview"] == n["routing_preview"]


def test_immediate_threshold(live, fake):
    r = submit(live, delay=3)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "pending" and "send_at" not in r.json()
    from tests.conftest import wait_for
    wait_for(lambda: fake.messages)


def test_past_and_horizon_422(live):
    assert submit(live, send_at=at(-1)).status_code == 422
    assert submit(live, send_at=at(366 * 24 + 5)).status_code == 422
    assert submit(live, delay="400d").status_code == 422


def test_bad_delay_is_422(live):
    r = submit(live, delay="abc")
    assert r.status_code == 422, r.text


def test_naive_expires_uses_zone_only_when_scheduled(monkeypatch, server_factory):
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    srv = server_factory()
    r = submit(srv, send_at="2026-12-01T07:30", expires_at="2026-12-01T08:00")
    assert r.status_code == 202, r.text
    row = srv.service.db.one("SELECT send_at, expires_at FROM notifications WHERE id=?", (r.json()["id"],))
    assert parse_iso(row["send_at"]) == datetime(2026, 12, 1, 6, 30, tzinfo=UTC)
    assert parse_iso(row["expires_at"]) == datetime(2026, 12, 1, 7, 0, tzinfo=UTC)
    exp = (datetime.now(UTC) + timedelta(hours=2)).replace(tzinfo=None, microsecond=0)
    r = submit(srv, expires_at=exp.isoformat())
    assert r.status_code == 202, r.text
    assert parse_iso(r.json()["expires_at"]) == exp.replace(tzinfo=UTC)


def test_expires_before_send_422(live):
    r = submit(live, send_at=at(2), expires_at=at(1))
    assert r.status_code == 422 and "after the send time" in r.text


def test_ttl_counts_from_send_time(live):
    n = submit(live, delay="1h", ttl_seconds=600).json()
    assert parse_iso(n["expires_at"]) - parse_iso(n["send_at"]) == timedelta(seconds=600)
    row = live.service.db.one("SELECT ttl_seconds, explicit_expires_at FROM notifications WHERE id=?", (n["id"],))
    assert row["ttl_seconds"] == 600 and row["explicit_expires_at"] is None
    n = submit(live, delay="1h", ttl_seconds=600, expires_at=at(1.5)).json()
    assert parse_iso(n["expires_at"]) - parse_iso(n["send_at"]) == timedelta(seconds=600)
    row = live.service.db.one("SELECT explicit_expires_at FROM notifications WHERE id=?", (n["id"],))
    assert row["explicit_expires_at"] is not None


def test_caps(live, monkeypatch):
    monkeypatch.setattr(schedule, "MAX_SCHEDULED", 3)
    for _ in range(3):
        assert submit(live, delay="1h").status_code == 202
    r = submit(live, delay="1h")
    assert r.status_code == 422 and "3 scheduled" in r.text
    assert submit(live, "other", delay="1h").status_code == 202  # per client
    assert submit(live, "admin", delay="1h").status_code == 202  # admin exempt


def test_byte_cap(live, monkeypatch):
    monkeypatch.setattr(schedule, "MAX_SCHEDULED_BYTES", 1024)
    big = base64.b64encode(b"\x00" * 600).decode()
    a = {"filename": "a.bin", "content_type": "application/octet-stream", "data_base64": big}
    assert submit(live, delay="1h", attachments=[a]).status_code == 202
    r = submit(live, delay="1h", attachments=[a])
    assert r.status_code == 422 and "attachments" in r.text
    assert submit(live, "admin", delay="1h", attachments=[a]).status_code == 202


def test_attachments_stored_when_preview_empty(live):
    r = submit(live, targets=["device:spare"], delay="1h", attachments=[inline()])
    assert r.status_code == 202, r.text
    assert r.json()["routing_preview"]["devices"] == []
    row = live.service.db.one("SELECT data IS NOT NULL AS has FROM attachments WHERE notification_id=?",
                              (r.json()["id"],))
    assert row["has"] == 1


def test_url_attachment_fetched_when_scheduled(server_factory, fake):
    live = server_factory(attachment_url_hosts=["files.test"])
    r = submit(live, targets=["device:spare"], delay="1h",
               attachments=[{"url": "https://files.test/door.png"}])
    assert r.status_code == 202, r.text
    assert fake.file_requests == ["/door.png"]
    row = live.service.db.one("SELECT data IS NOT NULL AS has FROM attachments WHERE notification_id=?",
                              (r.json()["id"],))
    assert row["has"] == 1


def test_follow_up_of_scheduled_rejected(live):
    n = submit(live, delay="1h").json()
    r = submit(live, delay="2h", in_reply_to=n["id"])
    assert r.status_code == 422 and "has not been sent yet" in r.text


def test_replay_different_send_time_409(live):
    key = "k-1"
    first = submit(live, send_at=at(1), idempotency_key=key)
    assert first.status_code == 202
    same = submit(live, send_at=first.json()["send_at"], idempotency_key=key)
    assert same.status_code == 200 and same.json()["id"] == first.json()["id"]
    r = submit(live, send_at=at(3), idempotency_key=key)
    assert r.status_code == 409 and "different send time" in r.text
    assert submit(live, idempotency_key=key).status_code == 409  # now immediate: also a different time
    d1 = submit(live, delay="1h", idempotency_key="k-2")
    time.sleep(1.1)
    assert submit(live, delay="1h", idempotency_key="k-2").status_code == 200  # a delay is stable on replay


def test_scheduled_event_type(live):
    n = submit(live, delay="1h").json()
    ev = live.client().get(f"/v1/notifications/{n['id']}/events").json()["items"]
    types = [e["type"] for e in ev]
    assert "notification.scheduled" in types and "notification.created" not in types
    assert not any(t.startswith("delivery.") for t in types)
    sched = next(e for e in ev if e["type"] == "notification.scheduled")
    assert sched["data"]["send_at"] == n["send_at"]


def test_reply_handle_at_schedule(email_live):
    r = submit(email_live, targets=["device:alex-email"], kinds=["email"], reply=True, delay="1h")
    assert r.status_code == 202, r.text
    assert r.json()["reply_handle"]
    assert r.json()["routing_preview"]["devices"][0]["device"] == "alex-email"


def test_dry_run_returns_send_at(live):
    r = submit(live, delay="1h", dry_run=True)
    assert r.status_code == 200 and r.json()["send_at"] and r.json()["deliveries"]
    assert live.client().get("/v1/notifications").json()["items"] == []
