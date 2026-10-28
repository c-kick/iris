"""Scheduling: edit, send now and cancel of scheduled notifications (REST, live server)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from notify.db import parse_iso
from tests.conftest import wait_for


def sched(srv, who="agent", **body):
    body.setdefault("targets", ["user:alex"])
    body.setdefault("message", "hello")
    if "send_at" not in body:
        body.setdefault("delay", "1h")
    with srv.client(who) as c:
        r = c.post("/v1/notifications", json=body)
    assert r.status_code == 202, r.text
    return r.json()["id"]


def patch(srv, nid, who="agent", **body):
    with srv.client(who) as c:
        return c.patch(f"/v1/notifications/{nid}", json=body)


def row(srv, nid):
    return dict(srv.service.db.one("SELECT * FROM notifications WHERE id=?", (nid,)))


def at(hours):
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


def test_reschedule_moves_ttl(live):
    nid = sched(live, ttl_seconds=600)
    before = row(live, nid)
    r = patch(live, nid, send_at=at(25))
    assert r.status_code == 200, r.text
    after = row(live, nid)
    assert parse_iso(after["send_at"]) - parse_iso(before["send_at"]) > timedelta(hours=23)
    assert parse_iso(after["expires_at"]) - parse_iso(after["send_at"]) == timedelta(seconds=600)
    assert parse_iso(after["deliver_by"]) - parse_iso(after["send_at"]) == timedelta(seconds=3600)
    assert r.json()["status"] == "scheduled" and r.json()["routing_preview"]["devices"]


def test_explicit_expiry_stays_and_blocks(live):
    nid = sched(live, expires_at=at(3))
    assert patch(live, nid, delay="2h").status_code == 200
    assert row(live, nid)["expires_at"] == row(live, nid)["explicit_expires_at"]
    before = row(live, nid)
    r = patch(live, nid, send_at=at(5))
    assert r.status_code == 422 and "expires_at" in r.text
    assert row(live, nid) == before
    assert patch(live, nid, send_at=at(5), expires_at=None).status_code == 200
    assert row(live, nid)["expires_at"] is None


def test_edit_content(live):
    nid = sched(live, title="old", url="https://example.org/a")
    r = patch(live, nid, title="new", message="changed", severity="high", targets=["device:alex-phone"],
              tags=["x"], require_ack=True)
    assert r.status_code == 200, r.text
    n = r.json()
    assert (n["title"], n["message"], n["severity"], n["targets"], n["tags"], n["require_ack"]) == (
        "new", "changed", "high", ["device:alex-phone"], ["x"], True)
    assert n["url"] == "https://example.org/a"  # omitted = unchanged
    ev = [e for e in live.client().get(f"/v1/notifications/{nid}").json()["events"]
          if e["type"] == "notification.updated"]
    assert len(ev) == 1
    d = ev[0]["data"]
    assert d["by"] == "agent" and set(d["fields"]) == {"title", "message", "severity", "targets", "tags", "require_ack"}
    assert d["fields"] == sorted(d["fields"]) and "send_at" not in d


def test_event_carries_moved_send_at(live):
    nid = sched(live)
    old = row(live, nid)["send_at"]
    assert patch(live, nid, send_at=at(5)).status_code == 200
    ev = next(e for e in live.client().get(f"/v1/notifications/{nid}").json()["events"]
              if e["type"] == "notification.updated")
    assert ev["data"]["send_at"] == {"from": old, "to": row(live, nid)["send_at"]}


def test_null_clears_url(live):
    nid = sched(live, url="https://example.org/a", url_title="t")
    r = patch(live, nid, url=None, url_title=None)
    assert r.status_code == 200 and r.json()["url"] is None and r.json()["url_title"] is None


def test_null_on_required_field_422(live):
    nid = sched(live)
    before = row(live, nid)
    assert patch(live, nid, message=None).status_code == 422
    assert patch(live, nid, targets=None).status_code == 422
    assert row(live, nid) == before


def test_bad_tags_and_kinds_422_row_unchanged(live):
    nid = sched(live)
    before = row(live, nid)
    assert patch(live, nid, tags=["x" * 65]).status_code == 422
    assert patch(live, nid, tags=[""]).status_code == 422
    assert patch(live, nid, kinds=[]).status_code == 422
    assert row(live, nid) == before
    r = patch(live, nid, kinds=["push", "push"])
    assert r.status_code == 200
    assert row(live, nid)["routing_json"].count('"push"') == 1


def test_unknown_target_leaves_row_unchanged(live):
    nid = sched(live)
    before = row(live, nid)
    r = patch(live, nid, message="changed", targets=["user:nobody"])
    assert r.status_code == 422
    assert row(live, nid) == before
    assert not [e for e in live.client().get(f"/v1/notifications/{nid}").json()["events"]
                if e["type"] == "notification.updated"]


def test_past_and_horizon_422(live):
    nid = sched(live)
    before = row(live, nid)
    assert patch(live, nid, send_at=at(-1)).status_code == 422
    assert patch(live, nid, delay="400d").status_code == 422
    assert row(live, nid) == before


def test_naive_send_at_read_in_local_zone(live, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    nid = sched(live)
    tokyo = (datetime.now(UTC) + timedelta(hours=5) + timedelta(hours=9)).replace(tzinfo=None)
    assert patch(live, nid, send_at=tokyo.isoformat()).status_code == 200
    assert abs((parse_iso(row(live, nid)["send_at"]) - datetime.now(UTC)).total_seconds() - 5 * 3600) < 30


def test_send_now(live, fake):
    nid = sched(live)
    r = patch(live, nid, send_now=True)
    assert r.status_code == 200, r.text
    wait_for(lambda: row(live, nid)["status"] != "scheduled", timeout=2)
    wait_for(lambda: fake.messages, timeout=5)
    assert row(live, nid)["released_at"] is not None


def test_send_now_with_time_is_422(live):
    nid = sched(live)
    assert patch(live, nid, send_now=True, delay="2h").status_code == 422


def test_patch_after_release_409(live):
    nid = sched(live)
    assert patch(live, nid, send_now=True).status_code == 200
    wait_for(lambda: row(live, nid)["status"] != "scheduled")
    r = patch(live, nid, message="late")
    assert r.status_code == 409
    assert r.json()["detail"]["error"].startswith("notification is ")
    assert r.json()["detail"]["results"]["status"] == row(live, nid)["status"]


def test_sender_scope_can_cancel_own_scheduled(live):
    nid = sched(live, "sender")
    r = live.client("sender").post(f"/v1/notifications/{nid}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["notification"]["status"] == "cancelled"
    assert r.json()["results"] == [{"result": "cancelled"}]
    assert row(live, nid)["status"] == "cancelled"
    assert "notification.status_changed" in [e["type"] for e in
                                             live.client("admin").get(f"/v1/notifications/{nid}").json()["events"]]


def test_sender_scope_can_edit_own_scheduled(live):
    nid = sched(live, "sender")
    assert patch(live, nid, "sender", message="x").status_code == 200


def test_sender_scope_cannot_cancel_sent(live, fake):
    with live.client("sender") as c:
        nid = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "now"}).json()["id"]
        wait_for(lambda: fake.messages)
        assert c.post(f"/v1/notifications/{nid}/cancel").status_code == 403


def test_cancel_scheduled_twice_is_409(live):
    nid = sched(live)
    assert live.client().post(f"/v1/notifications/{nid}/cancel").status_code == 200
    assert live.client().post(f"/v1/notifications/{nid}/cancel").status_code == 409


def test_other_client_cannot_touch(live):
    nid = sched(live)
    assert patch(live, nid, "other", message="x").status_code == 404
    assert live.client("other").post(f"/v1/notifications/{nid}/cancel").status_code == 404
    assert row(live, nid)["status"] == "scheduled" and row(live, nid)["message"] == "hello"


def test_sender_cannot_cancel_foreign_scheduled(live):
    nid = sched(live)
    assert live.client("sender").post(f"/v1/notifications/{nid}/cancel").status_code in (403, 404)
    assert row(live, nid)["status"] == "scheduled"


def test_admin_can_edit_and_cancel_any(live):
    nid = sched(live)
    assert patch(live, nid, "admin", message="by admin").status_code == 200
    assert row(live, nid)["message"] == "by admin"
    r = live.client("admin").post(f"/v1/notifications/{nid}/cancel")
    assert r.status_code == 200 and row(live, nid)["status"] == "cancelled"


def test_patch_unknown_field_and_missing_row(live):
    nid = sched(live)
    assert patch(live, nid, bogus=1).status_code == 422
    assert patch(live, "ntf_nope", message="x").status_code == 404


def test_concurrent_edits_serialise(live):
    nid = sched(live, expires_at=at(10))
    results: list[int] = []

    def go(**body):
        results.append(patch(live, nid, **body).status_code)

    ts = [threading.Thread(target=go, kwargs={"send_at": at(12)}),
          threading.Thread(target=go, kwargs={"expires_at": at(11)})]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    r = row(live, nid)
    assert r["expires_at"] is None or parse_iso(r["expires_at"]) > parse_iso(r["send_at"]) or 422 in results
    if r["expires_at"] is not None:
        assert parse_iso(r["expires_at"]) > parse_iso(r["send_at"])
