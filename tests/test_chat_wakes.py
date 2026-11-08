"""Agent group chats: wake claims, confirmation, liveness heartbeat and sweeps."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from notify.chat import CLAIM_TIMEOUT_SECONDS, STALE_SECONDS
from notify.core import Forbidden, NotFound
from notify.db import iso
from tests.test_chat_core import AGENT, NOHOST, OTHER_HOST, build, join


class Clock:
    def __init__(self):
        self.t = datetime.now(timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


@pytest.fixture
def svc(tmp_path):
    s = build(tmp_path)
    s.chat.now = Clock()
    s.chat.started_at = s.chat.now()  # the grace after start-up counts from the fake clock
    yield s
    s.db.close()


def run(coro):
    return asyncio.run(coro)


def two(svc):
    a = join(svc, "s1")
    b = join(svc, "s2", kind="codex")
    return a, b


def wake_state(svc, wid):
    return svc.db.one("SELECT * FROM chat_wakes WHERE id=?", (wid,))


def test_claim_host_all_kinds(svc):
    a, b = two(svc)
    svc.chat.post(a["member_handle"], "hello")
    c = join(svc, "s3", kind="claude")
    svc.chat.post(b["member_handle"], "second")
    got = [run(svc.chat.claim_host(AGENT, f"w{i}", 0)) for i in range(3)]
    assert all(got)
    assert [g["kind"] for g in got[:1]] == ["codex"]
    assert {g["member_id"] for g in got} == {a["member_id"], b["member_id"], c["member_id"]}
    for g in got:
        assert set(g) == {"wake_id", "message_id", "member_id", "kind", "session_id", "pid", "pid_start", "text"}
        assert g["pid"] == 100 and g["text"].startswith("[hnl-notify group chat")
    assert run(svc.chat.claim_host(AGENT, "w9", 0)) is None
    assert run(svc.chat.claim_host(OTHER_HOST, "w9", 0)) is None


def test_claim_host_timeout_none(svc):
    assert run(svc.chat.claim_host(AGENT, "w", 0.05)) is None


def test_claim_host_requires_host(svc):
    with pytest.raises(Forbidden):
        run(svc.chat.claim_host(NOHOST, "w", 0))
    with pytest.raises(Forbidden):
        svc.chat.heartbeat(NOHOST, [])


def test_confirm_delivered(svc):
    a, b = two(svc)
    svc.chat.post(a["member_handle"], "hello")
    g = run(svc.chat.claim_host(AGENT, "w", 0))
    svc.chat.confirm(AGENT, g["wake_id"], "w")
    r = wake_state(svc, g["wake_id"])
    assert r["state"] == "delivered" and r["finished_at"]


def test_confirm_wrong_waiter_or_host(svc):
    a, b = two(svc)
    svc.chat.post(a["member_handle"], "hello")
    g = run(svc.chat.claim_host(AGENT, "w", 0))
    for client, waiter in ((AGENT, "other"), (OTHER_HOST, "w")):
        with pytest.raises(NotFound):
            svc.chat.confirm(client, g["wake_id"], waiter)
    assert wake_state(svc, g["wake_id"])["state"] == "claimed"
    with pytest.raises(NotFound):
        svc.chat.confirm(AGENT, "wak_nope", "w")


def test_unconfirmed_claim_returns(svc):
    a, b = two(svc)
    svc.chat.post(a["member_handle"], "hello")
    g = run(svc.chat.claim_host(AGENT, "w1", 0))
    svc.chat.now.advance(CLAIM_TIMEOUT_SECONDS - 1)
    svc.chat.heartbeat(AGENT, [{"member_id": a["member_id"]}, {"member_id": b["member_id"]}])
    svc.chat.sweep()
    assert wake_state(svc, g["wake_id"])["state"] == "claimed"
    svc.chat.now.advance(2)
    svc.chat.sweep()
    r = wake_state(svc, g["wake_id"])
    assert r["state"] == "pending" and r["waiter_id"] is None
    again = run(svc.chat.claim_host(AGENT, "w2", 0))
    assert again["wake_id"] == g["wake_id"]
    with pytest.raises(NotFound):  # the first waiter lost its claim
        svc.chat.confirm(AGENT, g["wake_id"], "w1")


def test_same_waiter_gets_held_wake(svc):
    a, b = two(svc)
    svc.chat.post(a["member_handle"], "one")
    svc.chat.post(a["member_handle"], "two")
    g1 = run(svc.chat.claim_host(AGENT, "w", 0))
    g2 = run(svc.chat.claim_host(AGENT, "w", 0))
    assert g1["wake_id"] == g2["wake_id"]
    other = run(svc.chat.claim_host(AGENT, "x", 0))
    assert other["wake_id"] != g1["wake_id"]


def test_long_turn_not_stale(svc):
    a, b = two(svc)
    alive = [{"member_id": a["member_id"], "pid": 100, "pid_start": "x"},
             {"member_id": b["member_id"], "pid": 100, "pid_start": "x"}]
    for _ in range(60):  # 30 minutes
        svc.chat.now.advance(30)
        svc.chat.heartbeat(AGENT, alive)
        svc.chat.sweep()
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_members WHERE left_at IS NULL") == 2


def test_missing_heartbeat_stale(svc):
    a, b = two(svc)
    svc.chat.post(a["member_handle"], "hello")
    for _ in range(6):  # the relay reports every 30 s, only a
        svc.chat.heartbeat(AGENT, [{"member_id": a["member_id"]}])
        svc.chat.now.advance(30)
    svc.chat.heartbeat(AGENT, [{"member_id": a["member_id"]}])
    svc.chat.now.advance(2)
    svc.chat.sweep()
    m = svc.db.one("SELECT * FROM chat_members WHERE id=?", (b["member_id"],))
    assert m["left_at"] and m["leave_reason"] == "stale"
    assert svc.db.one("SELECT state FROM chat_groups WHERE id=?", (a["group_id"],))["state"] == "open"
    w = svc.db.one("SELECT * FROM chat_wakes WHERE member_id=?", (b["member_id"],))
    assert (w["state"], w["reason"]) == ("skipped", "member gone")
    for _ in range(7):  # the relay goes on reporting, now without a
        svc.chat.now.advance(30)
        svc.chat.heartbeat(AGENT, [])
    svc.chat.sweep()
    assert svc.db.one("SELECT state FROM chat_groups WHERE id=?", (a["group_id"],))["state"] == "closed"


def test_claimed_wake_of_departed_member_skipped(svc):
    a, b = two(svc)
    svc.chat.post(a["member_handle"], "hello")
    g = run(svc.chat.claim_host(AGENT, "w", 0))
    svc.chat.leave(b["member_handle"])
    r = wake_state(svc, g["wake_id"])
    assert (r["state"], r["reason"]) == ("skipped", "member gone")
    # a claimed wake that slipped through (e.g. written before this fix) is cleaned by the sweep
    with svc.db.tx() as c:
        c.execute("UPDATE chat_wakes SET state='claimed', waiter_id='w', finished_at=NULL WHERE id=?", (g["wake_id"],))
    svc.chat.sweep()
    assert wake_state(svc, g["wake_id"])["state"] == "skipped"
    assert run(svc.chat.claim_host(AGENT, "w", 0)) is None


def test_heartbeat_wrong_host_ignored(svc):
    a, b = two(svc)
    before = svc.db.one("SELECT last_seen_at FROM chat_members WHERE id=?", (a["member_id"],))["last_seen_at"]
    svc.chat.now.advance(100)
    out = svc.chat.heartbeat(OTHER_HOST, [{"member_id": a["member_id"]}])
    assert out == {"members": []}
    assert svc.db.one("SELECT last_seen_at FROM chat_members WHERE id=?", (a["member_id"],))["last_seen_at"] == before


def test_heartbeat_lists_members(svc):
    a, b = two(svc)
    out = svc.chat.heartbeat(AGENT, [])
    assert [m["member_id"] for m in out["members"]] == [a["member_id"], b["member_id"]]
    assert set(out["members"][0]) == {"member_id", "kind", "pid", "pid_start"}  # no session ids (review D.5)


def test_release_backoff_and_give_up(svc):
    from notify.chat import MAX_WAKE_ATTEMPTS
    a, b = two(svc)
    svc.chat.post(a["member_handle"], "hello")
    other = join(svc, "s9")
    delays = []
    for n in range(1, MAX_WAKE_ATTEMPTS + 1):
        g = run(svc.chat.claim_host(AGENT, "w", 0))
        assert g is not None
        svc.chat.release(AGENT, g["wake_id"], "w")
        r = wake_state(svc, g["wake_id"])
        assert r["attempts"] == n
        if n < MAX_WAKE_ATTEMPTS:
            assert r["state"] == "pending" and r["waiter_id"] is None
            delays.append((datetime.fromisoformat(r["retry_after"].replace("Z", "+00:00")) - svc.chat.now()).total_seconds())
            # not claimable until retry_after (other pending wakes are: the other member's wake)
            first = run(svc.chat.claim_host(AGENT, "w", 0))
            assert first is None or first["wake_id"] != g["wake_id"]
            if first is not None:
                svc.chat.release(AGENT, first["wake_id"], "w")
            svc.chat.now.advance(delays[-1] + 1)
    assert delays == pytest.approx([30, 60, 120, 240], abs=1)
    assert r["state"] == "skipped" and r["reason"] == "undeliverable" and r["finished_at"]
    assert other  # silence unused


def test_release_wrong_waiter_or_host(svc):
    a, b = two(svc)
    svc.chat.post(a["member_handle"], "hello")
    g = run(svc.chat.claim_host(AGENT, "w1", 0))
    for client, waiter in ((AGENT, "w2"), (OTHER_HOST, "w1")):
        with pytest.raises(NotFound):
            svc.chat.release(client, g["wake_id"], waiter)
    with pytest.raises(Forbidden):
        svc.chat.release(NOHOST, g["wake_id"], "w1")
    r = wake_state(svc, g["wake_id"])
    assert r["state"] == "claimed" and r["attempts"] == 0


def test_no_stale_sweep_right_after_start(svc):
    a, b = two(svc)
    started = svc.chat.started_at
    with svc.db.tx() as c:
        c.execute("UPDATE chat_members SET last_seen_at=?", (iso(started - timedelta(minutes=10)),))
    for t in (0, 60):  # the relay reports, without these members
        svc.chat.now.t = started + timedelta(seconds=t)
        svc.chat.heartbeat(AGENT, [])
    svc.chat.sweep()
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_members WHERE left_at IS NULL") == 2
    for t in (120, STALE_SECONDS + 1):
        svc.chat.now.t = started + timedelta(seconds=t)
        svc.chat.heartbeat(AGENT, [])
    svc.chat.sweep()
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_members WHERE left_at IS NULL") == 0


def test_claim_cut_off_by_a_restart_is_redelivered(tmp_path):
    """0.13.2 (instead of a 503 at shutdown): a claim whose response was lost in a restart is handed to the same
    relay again at once, and a long-poll cancelled while waiting claims nothing."""
    s1 = build(tmp_path)
    a, b = two(s1)
    s1.chat.post(a["member_handle"], "hello")
    lost = run(s1.chat.claim_host(AGENT, "relay-host-a", 0))  # committed, but the relay never saw the answer
    s1.db.close()

    s2 = build(tmp_path)  # the service came back on the same database
    try:
        again = run(s2.chat.claim_host(AGENT, "relay-host-a", 0))
        assert again["wake_id"] == lost["wake_id"] and again["text"] == lost["text"]
        s2.chat.confirm(AGENT, again["wake_id"], "relay-host-a")
        assert wake_state(s2, again["wake_id"])["state"] == "delivered"

        async def cancelled_poll():
            task = asyncio.create_task(s2.chat.claim_host(AGENT, "relay-host-a", 30))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        run(cancelled_poll())
        assert s2.db.scalar("SELECT COUNT(*) FROM chat_wakes WHERE state='claimed'") == 0
        s2.chat.post(b["member_handle"], "after the restart")
        got = run(s2.chat.claim_host(AGENT, "relay-host-a", 0))
        assert got is not None and got["member_id"] == a["member_id"]
    finally:
        s2.db.close()
