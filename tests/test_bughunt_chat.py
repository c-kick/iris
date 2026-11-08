"""Bug hunt 2 (chats, relay, notify-wait, browser push routes) at bb0294f.

Every test asserts the CORRECT behaviour; a failing test is a confirmed bug (see the BH-2NN report).
"""

from __future__ import annotations

import threading

import pytest

from notify.chat import MAX_WAKE_ATTEMPTS
from tests.conftest import TOKENS
from tests.test_chat_api import chatlive  # noqa: F401  (fixture)
from tests.test_chat_api import join as rest_join
from tests.test_chat_core import AGENT, join
from tests.test_hardening_chat import deliver_reply, drain, load, run, wake_of
from tests.test_hardening_chat import svc  # noqa: F401  (fixture: chat_cap=3, fake clock)
from tests.test_replies_core import HANDLES, send


# --------------------------------------------------------------------------- BH-201
def test_bh201_message_read_while_paused_is_not_woken_again_on_resume(svc):  # noqa: F811
    """README: 'A message returned by group_read is not woken into the session again.' A held wake (skipped,
    reason 'paused') is not dropped by group_read, so Resume re-pends it and the reader gets the message twice."""
    a, b = join(svc, "s1"), join(svc, "s2")
    svc.chat.post(a["member_handle"], "one")
    svc.chat.post(b["member_handle"], "two")
    cap = svc.chat.post(a["member_handle"], "three")  # chat_cap=3: this one pauses the group
    assert cap["paused"] is True
    assert wake_of(svc, cap["message_id"], b["member_id"])["reason"] == "paused"
    got = [m["id"] for m in svc.chat.read(b["member_handle"])["messages"]]
    assert cap["message_id"] in got  # b has read it now
    svc.chat.resume_owner(a["group_id"])
    delivered_to_b = []
    while (g := run(svc.chat.claim_host(AGENT, "w", 0))) is not None:
        svc.chat.confirm(AGENT, g["wake_id"], "w")
        if g["member_id"] == b["member_id"]:
            delivered_to_b.append(g["message_id"])
    assert cap["message_id"] not in delivered_to_b


def test_bh201_pause_now_then_read_then_owner_post_no_second_wake(svc):  # noqa: F811
    a, b = join(svc, "s1"), join(svc, "s2")
    m = svc.chat.post(a["member_handle"], "queued")["message_id"]
    svc.chat.pause_owner(a["group_id"])  # holds the queued wake
    assert m in [x["id"] for x in svc.chat.read(b["member_handle"])["messages"]]
    svc.chat.post_owner(a["group_id"], "go on")  # resumes and re-pends what is held
    assert m not in drain(svc)


# --------------------------------------------------------------------------- BH-205
def test_bh205_group_read_does_not_return_a_message_already_woken(svc):  # noqa: F811
    """README: group_read without arguments returns unread messages, 'not those already delivered to you as a
    wake'. One undeliverable (skipped) wake keeps confirm() from moving the read mark, so later delivered
    messages come back from group_read."""
    a, b = join(svc, "s1"), join(svc, "s2")
    m1 = svc.chat.post(a["member_handle"], "one")["message_id"]
    for _ in range(MAX_WAKE_ATTEMPTS):  # m1 cannot be delivered (e.g. codex queue keeps failing)
        with svc.db.tx() as c:
            c.execute("UPDATE chat_wakes SET retry_after=NULL WHERE message_id=?", (m1,))
        g = run(svc.chat.claim_host(AGENT, "w", 0))
        assert g["message_id"] == m1
        svc.chat.release(AGENT, g["wake_id"], "w")
    assert wake_of(svc, m1, b["member_id"])["reason"] == "undeliverable"
    m2 = svc.chat.post(a["member_handle"], "two")["message_id"]
    assert drain(svc) == [m2]  # m2 reached b's session as a wake
    got = [x["id"] for x in svc.chat.read(b["member_handle"])["messages"]]
    assert m2 not in got


# --------------------------------------------------------------------------- BH-204 (REST leave host binding)
def test_bh204_rest_leave_rejects_handle_of_another_host(chatlive):  # noqa: F811
    """B12 binds MCP group tools to the token's chat host; POST /v1/chat/leave still takes any host's handle."""
    j = rest_join(chatlive, sid="s1")  # host-a member
    r = chatlive.client("hostb").post("/v1/chat/leave", headers={"X-Member-Handle": j["member_handle"]},
                                      json={"reason": "left"})
    assert r.status_code == 403
    assert chatlive.service.db.scalar("SELECT left_at FROM chat_members WHERE id=?", (j["member_id"],)) is None


# --------------------------------------------------------------------------- notify-wait
@pytest.fixture
def nw(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    mod = load("notify-wait", "notify_wait_bughunt")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return mod


def test_bh202_lost_cache_does_not_reprint_an_old_reply(nw, email_live, monkeypatch, tmp_path, capsys):
    """The waiter id with an empty cache is fixed per handle, and the server keeps a collected reply 'claimed' by
    that waiter forever. A run with no cache file (cache cleaned, another host, another HOME/XDG_CACHE_HOME)
    is handed the first reply again and exits 0 as if it were a new answer."""
    nid, did = send(email_live)
    deliver_reply(email_live, did)
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    assert nw.main([HANDLES[nid], "--url", email_live.url, "--timeout", "5s"]) == 0
    assert "Go with B." in capsys.readouterr().out
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "fresh-cache"))  # e.g. ~/.cache cleaned, other host
    rc = nw.main([HANDLES[nid], "--url", email_live.url, "--timeout", "1s"])
    out = capsys.readouterr().out
    assert rc == 2 and "Go with B." not in out


def test_bh203_two_concurrent_runs_do_not_both_print_one_reply(nw, email_live, monkeypatch, capsys):
    """Two notify-wait runs for one handle at the same time derive the same waiter id (HMAC of the secret and the
    same cached last reply), so the server hands the one reply to both; before 0.13.1 each run had its own waiter."""
    nid, did = send(email_live)
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    rcs: list[int] = []

    def one():
        rcs.append(nw.main([HANDLES[nid], "--url", email_live.url, "--timeout", "4s"]))

    ts = [threading.Thread(target=one) for _ in range(2)]
    for t in ts:
        t.start()
    threading.Event().wait(0.5)  # both are long-polling (time.sleep is patched out)
    deliver_reply(email_live, did)
    for t in ts:
        t.join(30)
    out = capsys.readouterr().out
    assert out.count("Go with B.") == 1, out
    assert sorted(rcs) == [0, 2]
