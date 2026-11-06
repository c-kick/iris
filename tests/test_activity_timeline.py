"""The admin Activity list: everything sent and received in one paged list."""

from __future__ import annotations

import httpx
import pytest

from tests.mailfactory import reply_raw
from tests.test_replies_core import deliver, send
from tests.test_schedule_release import sched


def session(srv) -> httpx.Client:
    """A browser with a valid signed-in admin cookie."""
    return httpx.Client(base_url=srv.url, cookies={"notify_admin": srv.app.state.store.new_session()}, timeout=10)


def timeline(srv, **params) -> dict:
    with session(srv) as a:
        r = a.get("/admin/api/timeline", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def post(srv, title, source=None) -> str:
    with srv.client() as c:
        body = {"targets": ["user:alex"], "message": f"body of {title}", "title": title}
        r = c.post("/v1/notifications", json={**body, **({"source": source} if source else {})})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def set_time(srv, table, row_id, at) -> None:
    column = "created_at" if table == "notifications" else "received_at"
    with srv.service.db.tx() as c:
        c.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (at, row_id))


def conversation(srv) -> dict:
    """A notification, a genuine reply to it and a rejected mail, at fixed and distinct times."""
    nid, did = send(srv, source="asker")
    rid = deliver(srv, reply_raw(did))
    assert deliver(srv, reply_raw(did, message_id="<m@x>", sender="mallory@example.org")) is None
    qid = srv.service.db.scalar("SELECT id FROM reply_quarantine")
    set_time(srv, "notifications", nid, "2026-01-01T10:00:00.000000Z")
    set_time(srv, "replies", rid, "2026-01-01T11:00:00.000000Z")
    set_time(srv, "reply_quarantine", qid, "2026-01-01T12:00:00.000000Z")
    return {"nid": nid, "rid": rid, "qid": qid}


def test_sent_received_and_rejected_share_one_list_newest_first(email_live):
    ids = conversation(email_live)
    items = timeline(email_live)["items"]
    assert [i["kind"] for i in items] == ["rejected", "reply", "sent"]
    assert items[1]["id"] == ids["rid"] and items[2]["id"] == ids["nid"]
    assert [i["at"] for i in items] == ["2026-01-01T12:00:00.000000Z", "2026-01-01T11:00:00.000000Z",
                                        "2026-01-01T10:00:00.000000Z"]


def test_rows_carry_what_the_page_shows(email_live):
    conversation(email_live)
    rejected, reply, sent = timeline(email_live)["items"]
    assert sent["message"] == "Pick" and sent["deliveries"] and sent["source"] == "asker"
    assert reply["text"] == "Go with B." and reply["from"] == "alex@example.org"
    assert reply["notification_id"] == sent["id"] and reply["notification_title"] == "Pick"
    assert rejected["from_address"] == "mallory@example.org" and rejected["reason"] == "sender_mismatch"


def test_direction_filter(email_live):
    conversation(email_live)
    assert [i["kind"] for i in timeline(email_live, direction="sent")["items"]] == ["sent"]
    assert [i["kind"] for i in timeline(email_live, direction="received")["items"]] == ["rejected", "reply"]
    with session(email_live) as a:
        assert a.get("/admin/api/timeline", params={"direction": "sideways"}).status_code == 422


def test_source_filter_keeps_the_exchange_and_drops_rejected_mail(email_live):
    ids = conversation(email_live)
    other = post(email_live, "other", source="someone-else")
    items = timeline(email_live, source="asker")["items"]
    assert [i["id"] for i in items] == [ids["rid"], ids["nid"]]
    assert other not in [i["id"] for i in items]
    assert timeline(email_live)["sources"] == ["asker", "someone-else"]


def test_received_filter_with_a_source_shows_only_its_replies(email_live):
    ids = conversation(email_live)
    assert [i["id"] for i in timeline(email_live, direction="received", source="asker")["items"]] == [ids["rid"]]


@pytest.fixture
def seven(live) -> list[str]:
    """Seven sent notifications, oldest first, a minute apart."""
    ids = [post(live, f"n{i}") for i in range(7)]
    for i, nid in enumerate(ids):
        set_time(live, "notifications", nid, f"2026-01-01T10:0{i}:00.000000Z")
    return ids


def test_paging_older_and_newer(live, seven):
    first = timeline(live, limit=3)
    assert [i["id"] for i in first["items"]] == seven[6:3:-1]
    assert first["newer"] is None and first["older"]
    second = timeline(live, limit=3, before=first["older"])
    assert [i["id"] for i in second["items"]] == seven[3:0:-1]
    third = timeline(live, limit=3, before=second["older"])
    assert [i["id"] for i in third["items"]] == [seven[0]]
    assert third["older"] is None and third["newer"]
    back = timeline(live, limit=3, after=third["newer"])
    assert [i["id"] for i in back["items"]] == seven[3:0:-1]
    assert back["older"] and back["newer"]


def test_a_short_newer_page_fills_up_to_the_newest(live, seven):
    first = timeline(live, limit=3)
    second = timeline(live, limit=3, before=first["older"])
    # only three are newer than this cursor: exactly the first page, not a partial one
    assert timeline(live, limit=3, after=second["newer"])["items"] == first["items"]
    near_top = f"{first['items'][1]['at']}|{first['items'][1]['id']}"
    page = timeline(live, limit=3, after=near_top)
    assert [i["id"] for i in page["items"]] == seven[6:3:-1] and page["newer"] is None


def test_arrivals_do_not_shift_the_older_page(live, seven):
    first = timeline(live, limit=3)
    post(live, "arrives while you read")
    second = timeline(live, limit=3, before=first["older"])
    assert [i["id"] for i in second["items"]] == seven[3:0:-1]


def test_paging_across_kinds_loses_nothing(email_live):
    ids = conversation(email_live)
    seen, cursor = [], None
    while True:
        page = timeline(email_live, limit=1, **({"before": cursor} if cursor else {}))
        seen += [i["kind"] for i in page["items"]]
        cursor = page["older"]
        if cursor is None:
            break
    assert seen == ["rejected", "reply", "sent"] and ids


def test_equal_times_page_without_gaps(live):
    ids = [post(live, f"same{i}") for i in range(4)]
    for nid in ids:
        set_time(live, "notifications", nid, "2026-01-01T10:00:00.000000Z")
    seen, cursor = [], None
    while True:
        page = timeline(live, limit=1, **({"before": cursor} if cursor else {}))
        seen += [i["id"] for i in page["items"]]
        cursor = page["older"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(ids) and len(set(seen)) == 4


@pytest.mark.parametrize("params", [{"before": "nonsense"}, {"after": "2026-01-01|"},
                                    {"before": "2026-01-01T10:00:00.000000Z|ntf_a", "after": "2026-01-01T10:00:00.000000Z|ntf_b"}])
def test_bad_cursors_are_refused(live, params):
    with session(live) as a:
        assert a.get("/admin/api/timeline", params=params).status_code == 422


def test_a_page_is_capped(live, seven):
    assert len(timeline(live, limit=1000)["items"]) == 7
    with session(live) as a:
        assert a.get("/admin/api/timeline", params={"limit": 0}).status_code == 200


def test_history_order_is_by_send_time(live):
    """A scheduled notification sits where it went out, not where it was created."""
    reminder = sched(live, title="planned long ago", delay="1d")
    newer = post(live, "created later, sent earlier")
    live.call(lambda: live.service.scheduler.release(reminder))
    assert [i["id"] for i in timeline(live)["items"]] == [reminder, newer]
    assert timeline(live)["items"][0]["at"] == timeline(live)["items"][0]["sent_at"]


def test_what_waits_or_was_cancelled_while_waiting_is_not_listed(live):
    waiting = sched(live, title="waits", delay="1d")
    cancelled = sched(live, title="cancelled", delay="1d")
    with live.client() as c:
        assert c.post(f"/v1/notifications/{cancelled}/cancel").status_code == 200
    listed = [i["id"] for i in timeline(live)["items"]]
    assert waiting not in listed and cancelled not in listed
    assert timeline(live)["sources"] == []


def test_find_by_id(email_live):
    ids = conversation(email_live)
    found = timeline(email_live, find=ids["nid"])
    assert [i["id"] for i in found["items"]] == [ids["nid"]] and found["older"] is None and found["newer"] is None
    assert [i["id"] for i in timeline(email_live, find=f"  {ids['rid']} ")["items"]] == [ids["rid"]]


def test_find_ignores_the_filters(email_live):
    ids = conversation(email_live)
    assert [i["id"] for i in timeline(email_live, find=ids["rid"], direction="sent", source="nobody")["items"]] == [ids["rid"]]


@pytest.mark.parametrize("needle", ["ntf_nope", "rpl_nope", "Pick", "ntf_", "%"])
def test_find_only_matches_a_whole_id(email_live, needle):
    conversation(email_live)
    assert timeline(email_live, find=needle)["items"] == []


def test_find_shows_a_waiting_notification_as_unsent(live):
    waiting = sched(live, title="waits", delay="1d")
    item = timeline(live, find=waiting)["items"][0]
    assert item["id"] == waiting and item["unsent"] is True and item["sent_at"] is None and item["status"] == "scheduled"


def test_find_shows_a_cancelled_waiting_notification_as_unsent(live):
    cancelled = sched(live, title="cancelled", delay="1d")
    with live.client() as c:
        c.post(f"/v1/notifications/{cancelled}/cancel")
    assert timeline(live, find=cancelled)["items"][0]["unsent"] is True


def test_sent_rows_are_not_marked_unsent(live):
    post(live, "plain")
    assert timeline(live)["items"][0]["unsent"] is False


def test_mailbox_status_comes_with_the_list(email_live):
    assert "inbound" in timeline(email_live) and timeline(email_live)["timezone"]


def test_bearer_admin_gets_states_and_times_only(email_live):
    ids = conversation(email_live)
    with email_live.client("admin") as a:
        r = a.get("/admin/api/timeline")
        found = a.get("/admin/api/timeline", params={"find": ids["rid"]})
    assert r.status_code == 200, r.text
    rejected, reply, _ = r.json()["items"]
    for item in (reply, found.json()["items"][0]):
        assert item["text"] is None and item["subject"] is None and item["from"] is None
        assert item["attachments"] == [] and item["dropped_attachments"] == []
        assert item["state"] and item["at"]
    assert rejected["from_address"] is None and rejected["subject"] is None and rejected["message_id"] is None
    assert rejected["reason"] and rejected["at"]


def test_agents_cannot_read_the_timeline(email_live):
    with email_live.client("agent") as c:
        assert c.get("/admin/api/timeline").status_code in (401, 403)
