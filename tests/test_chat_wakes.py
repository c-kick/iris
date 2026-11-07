"""Agent group chats: wake claims, confirmation, liveness heartbeat and sweeps."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from notify.chat import CLAIM_TIMEOUT_SECONDS, STALE_SECONDS
from notify.core import Forbidden, NotFound
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
        assert set(g) == {"wake_id", "message_id", "member_id", "kind", "session_id", "pid", "text"}
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
    svc.chat.now.advance(STALE_SECONDS - 1)
    svc.chat.heartbeat(AGENT, [{"member_id": a["member_id"]}])
    svc.chat.now.advance(2)
    svc.chat.sweep()
    m = svc.db.one("SELECT * FROM chat_members WHERE id=?", (b["member_id"],))
    assert m["left_at"] and m["leave_reason"] == "stale"
    assert svc.db.one("SELECT state FROM chat_groups WHERE id=?", (a["group_id"],))["state"] == "open"
    w = svc.db.one("SELECT * FROM chat_wakes WHERE member_id=?", (b["member_id"],))
    assert (w["state"], w["reason"]) == ("skipped", "member gone")
    svc.chat.now.advance(STALE_SECONDS)
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
    assert set(out["members"][0]) == {"member_id", "kind", "session_id", "pid", "pid_start"}
