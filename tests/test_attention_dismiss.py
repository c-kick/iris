from __future__ import annotations

from tests.conftest import wait_for


def failed(live, fake) -> str:
    fake.responses.append((400, {"status": 0, "errors": ["user identifier is invalid"], "user": "invalid"}))
    with live.client() as c:
        nid = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "x", "title": "Broken"}).json()["id"]
        wait_for(lambda: c.get(f"/v1/notifications/{nid}").json()["status"] == "failed")
    return nid


def test_failed_item_has_time_and_can_be_dismissed(live, fake):
    nid = failed(live, fake)
    with live.client("admin") as a:
        item = a.get("/admin/api/activity").json()["attention"]["failed"][0]
        assert item["notification_id"] == nid and item["at"] and item["at"] >= item["created_at"]
        r = a.post(f"/admin/api/notifications/{nid}/dismiss")
        assert r.status_code == 200 and r.json()["dismissed"] == 1
        assert a.get("/admin/api/activity").json()["attention"]["failed"] == []
        assert a.post(f"/admin/api/notifications/{nid}/dismiss").status_code == 409
    with live.client() as c:
        assert c.get("/v1/attention").json()["failed"] == []  # agents no longer see it either
        assert c.get(f"/v1/notifications/{nid}").json()["status"] == "failed"  # history unchanged


def test_retry_clears_dismissal(live, fake):
    nid = failed(live, fake)
    with live.client("admin") as a:
        a.post(f"/admin/api/notifications/{nid}/dismiss")
    fake.responses.append((400, {"status": 0, "errors": ["still invalid"], "user": "invalid"}))
    with live.client() as c:
        assert c.post(f"/v1/notifications/{nid}/retry").status_code == 200
        wait_for(lambda: c.get("/v1/attention").json()["failed"])
        assert c.get("/v1/attention").json()["failed"][0]["last_error"] == "still invalid"


def test_dismiss_needs_admin_and_unknown_is_404(live, fake):
    nid = failed(live, fake)
    with live.client() as c:
        assert c.post(f"/admin/api/notifications/{nid}/dismiss").status_code in (401, 403)
    with live.client("admin") as a:
        assert a.post("/admin/api/notifications/ntf_nothere/dismiss").status_code == 404
