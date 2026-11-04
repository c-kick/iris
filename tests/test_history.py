"""The history shows what was sent at the moment it was sent, whether it was scheduled or not."""

from __future__ import annotations

from tests.test_schedule_release import get, row, sched, set_times


def send(srv, title):
    with srv.client() as c:
        r = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "hello", "title": title})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def release(srv, nid):
    return srv.call(lambda: srv.service.scheduler.release(nid))


def history(srv, **params):
    with srv.client() as c:
        r = c.get("/v1/notifications", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def ids(srv, **params):
    return [n["id"] for n in history(srv, **params)["items"]]


def test_released_notification_sits_at_its_send_time(live):
    reminder = sched(live, title="planned long ago", delay="1d")
    newer = send(live, "created after the reminder, sent before it")
    release(live, reminder)
    assert ids(live) == [reminder, newer]


def test_sent_at_is_when_it_went_out(live):
    reminder = sched(live, delay="1d")
    plain = send(live, "immediate")
    release(live, reminder)
    by_id = {n["id"]: n for n in history(live)["items"]}
    assert by_id[reminder]["sent_at"] == row(live, reminder)["released_at"]
    assert by_id[reminder]["sent_at"] != row(live, reminder)["created_at"]
    assert by_id[plain]["sent_at"] == row(live, plain)["created_at"]


def test_not_sent_yet_is_not_in_the_history(live):
    waiting = sched(live, delay="1d")
    sent = send(live, "immediate")
    assert ids(live) == [sent]
    assert ids(live, status="scheduled") == [waiting]


def test_cancelled_before_sending_is_not_in_the_history(live):
    gone = sched(live, delay="1d")
    with live.client() as c:
        assert c.post(f"/v1/notifications/{gone}/cancel").status_code == 200
    assert ids(live) == []
    assert ids(live, status="cancelled") == [gone]  # still findable when asked for by status
    assert get(live, gone)["status"] == "cancelled"


def test_missed_release_stays_in_the_history(live):
    missed = sched(live, delay="1d")
    set_times(live, missed, late=7200, limit=60)
    assert release(live, missed) == "expired"
    assert ids(live) == [missed]


def test_since_counts_from_the_send_time(live):
    reminder = sched(live, delay="1d")
    plain = send(live, "immediate")
    cut = row(live, plain)["created_at"]
    release(live, reminder)
    assert ids(live, since=cut) == [reminder, plain]
    assert ids(live, since=row(live, reminder)["released_at"]) == [reminder]


def test_paging_follows_the_send_time_without_gaps(live):
    first = sched(live, delay="1d")
    second = sched(live, delay="1d")
    plain = [send(live, f"immediate {i}") for i in range(2)]
    release(live, second)
    release(live, first)
    seen, cursor = [], None
    while True:
        page = history(live, limit=2, **({"before": cursor} if cursor else {}))
        seen += [n["id"] for n in page["items"]]
        cursor = page["next_before"]
        if not cursor:
            break
    assert seen == [first, second, plain[1], plain[0]]
