"""Scheduling: retention and attachment purge count from the effective time, never the creation time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from notify.db import iso
from tests.test_schedule_release import PNG_B64, row, sched, set_times

ATT = [{"filename": "door.png", "content_type": "image/png", "data_base64": PNG_B64}]


def days_ago(n):
    return iso(datetime.now(UTC) - timedelta(days=n))


def backdate(srv, nid, created=None, **cols):
    sets = dict(cols)
    if created is not None:
        sets["created_at"] = created
    with srv.service.db.tx() as c:
        c.execute(f"UPDATE notifications SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                  (*sets.values(), nid))


def has_bytes(srv, nid):
    return srv.service.db.one("SELECT data IS NOT NULL AS has FROM attachments WHERE notification_id=?",
                              (nid,))["has"] == 1


def test_scheduled_far_ahead_survives_sweep(live):
    nid = sched(live, targets=["device:spare"], delay="10d", attachments=ATT)
    backdate(live, nid, created=days_ago(200))
    live.call(live.service.retention_sweep)
    assert row(live, nid) is not None and has_bytes(live, nid)


def test_missed_survives(email_live):
    live = email_live
    nid = sched(live, targets=["device:alex-email"], kinds=["email"], reply=True, attachments=ATT)
    conv = "SELECT 1 FROM conversations WHERE id=?"
    assert live.service.db.one(conv, (nid,)) is not None
    set_times(live, nid, late=7200, limit=60)
    assert live.call(lambda: live.service.scheduler.release(nid)) == "expired"
    assert row(live, nid)["release_problem"].startswith("missed")
    backdate(live, nid, created=days_ago(200))
    live.call(live.service.retention_sweep)
    assert row(live, nid) is not None and has_bytes(live, nid)
    assert live.service.db.one(conv, (nid,)) is not None


def test_released_old_is_swept(live):
    nid = sched(live, targets=["device:spare"], attachments=ATT)
    set_times(live, nid, late=7200, limit=60)
    assert live.call(lambda: live.service.scheduler.release(nid)) == "expired"
    backdate(live, nid, created=days_ago(200), released_at=days_ago(100))
    live.call(live.service.retention_sweep)
    assert row(live, nid) is None


def test_cancelled_scheduled_row_loses_bytes_at_once_and_ages_from_the_cancel(live):
    nid = sched(live, targets=["device:spare"], delay="10d", attachments=ATT)
    assert has_bytes(live, nid)
    with live.client() as c:
        assert c.post(f"/v1/notifications/{nid}/cancel").status_code == 200
    assert not has_bytes(live, nid)
    a = live.service.db.one("SELECT purged_at FROM attachments WHERE notification_id=?", (nid,))
    assert a["purged_at"] is not None and row(live, nid)["released_at"] is not None
    live.call(live.service.retention_sweep)
    assert row(live, nid) is not None  # send_at is 10 days ahead, but the cancel is what counts: still young
    backdate(live, nid, released_at=days_ago(100))
    live.call(live.service.retention_sweep)
    assert row(live, nid) is None


def test_kept_released_row_loses_bytes_after_attachment_retention(live):
    nid = sched(live, targets=["device:spare"], attachments=ATT)
    set_times(live, nid, late=7200, limit=60)
    assert live.call(lambda: live.service.scheduler.release(nid)) == "expired"
    backdate(live, nid, released_at=days_ago(3))
    live.call(live.service.retention_sweep)
    assert has_bytes(live, nid)  # younger than attachment_retention_days (7)
    backdate(live, nid, released_at=days_ago(10))
    live.call(live.service.retention_sweep)
    assert row(live, nid) is not None and not has_bytes(live, nid)  # kept (90 days), bytes gone
