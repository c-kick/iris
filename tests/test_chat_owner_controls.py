"""Group chats: owner watching suspends the cap; Pause now and Resume."""

from __future__ import annotations

from datetime import timedelta

from tests.test_chat_core import build, join


def pair(tmp_path, cap=3):
    s = build(tmp_path, chat_cap=cap)
    a, b = join(s, "s1"), join(s, "s2")
    clock = {"t": s.chat.now()}
    s.chat.now = lambda: clock["t"]
    return s, a, b, clock


def owner_lines(s):
    return [r["text"] for r in s.db.query("SELECT text FROM chat_messages WHERE author='system'"
                                           " AND text NOT LIKE '% the chat%' ORDER BY rowid")]


def states(s, msg_id):
    return [r["state"] for r in s.db.query("SELECT state FROM chat_wakes WHERE message_id=?", (msg_id,))]


def test_watching_disables_cap(tmp_path):
    s, a, b, _ = pair(tmp_path)
    s.chat.owner_watching(a["group_id"])
    ids = [s.chat.post(a["member_handle"] if i % 2 else b["member_handle"], str(i)) for i in range(5)]
    assert not any(r["paused"] for r in ids)
    assert all(states(s, r["message_id"]) == ["pending"] for r in ids)
    assert s.db.scalar("SELECT agent_streak FROM chat_groups") == 0
    s.db.close()


def test_watching_lapses_counting_restarts(tmp_path):
    s, a, b, clock = pair(tmp_path)
    s.chat.owner_watching(a["group_id"])
    s.chat.post(a["member_handle"], "1")
    clock["t"] += timedelta(seconds=31)
    r = [s.chat.post(a["member_handle"], str(i)) for i in range(3)]
    assert [x["paused"] for x in r] == [False, False, True]
    s.db.close()


def test_watching_does_not_unpause(tmp_path):
    s, a, b, _ = pair(tmp_path, cap=2)
    s.chat.post(a["member_handle"], "1")
    assert s.chat.post(b["member_handle"], "2")["paused"]
    s.chat.owner_watching(a["group_id"])
    assert s.db.scalar("SELECT paused FROM chat_groups") == 1
    r = s.chat.post(a["member_handle"], "3")
    assert r["paused"] and states(s, r["message_id"]) == ["skipped"]
    s.db.close()


def test_pause_now_and_resume_requeues(tmp_path):
    s, a, b, _ = pair(tmp_path, cap=50)
    g = a["group_id"]
    s.chat.post(a["member_handle"], "before")  # wake pending, untouched
    s.chat.pause_owner(g)
    held = s.chat.post(a["member_handle"], "held")
    assert held["paused"] and states(s, held["message_id"]) == ["skipped"]
    other = s.chat.post(b["member_handle"], "other")["message_id"]  # a wake skipped for another reason
    with s.db.tx() as c:
        c.execute("UPDATE chat_wakes SET reason='undeliverable' WHERE message_id=?", (other,))
    s.chat.resume_owner(g)
    assert states(s, held["message_id"]) == ["pending"]
    assert states(s, other) == ["skipped"]
    w = s.db.one("SELECT * FROM chat_wakes WHERE message_id=?", (held["message_id"],))
    assert w["retry_after"] is None and w["attempts"] == 0
    assert s.db.scalar("SELECT paused FROM chat_groups") == 0
    assert s.db.scalar("SELECT agent_streak FROM chat_groups") == 0
    assert owner_lines(s) == ["paused by the owner", "resumed by the owner"]
    assert s.db.scalar("SELECT COUNT(*) FROM events WHERE type='group.paused'") == 1
    assert s.db.scalar("SELECT COUNT(*) FROM events WHERE type='group.unpaused'") == 1
    s.db.close()


def test_resume_requeues_only_wakes_since_the_pause(tmp_path):
    s, a, b, _ = pair(tmp_path, cap=2)
    g = a["group_id"]
    s.chat.post(a["member_handle"], "1")
    first = s.chat.post(b["member_handle"], "2")  # cap pause, skipped
    s.chat.post_owner(g, "go")  # owner post unpauses; the first skipped wake stays skipped
    s.chat.pause_owner(g)
    s.chat.resume_owner(g)
    assert states(s, first["message_id"]) == ["skipped"]
    s.db.close()


def test_pause_resume_noops(tmp_path):
    s, a, b, _ = pair(tmp_path)
    g = a["group_id"]
    s.chat.resume_owner(g)  # not paused
    s.chat.pause_owner(g)
    s.chat.pause_owner(g)  # already paused
    n = len(owner_lines(s))
    assert n == 1 and s.db.scalar("SELECT COUNT(*) FROM events WHERE type='group.paused'") == 1
    s.chat.resume_owner(g)
    s.chat.resume_owner(g)
    assert len(owner_lines(s)) == 2
    s.db.close()


def test_owner_post_unpause_writes_line(tmp_path):
    s, a, b, _ = pair(tmp_path)
    s.chat.pause_owner(a["group_id"])
    r = s.chat.post_owner(a["group_id"], "go on")
    assert r["paused"] is False and len(r["woken"]) == 2
    texts = owner_lines(s)
    assert texts == ["paused by the owner", "resumed by the owner's message"]
    s.db.close()


def sys_lines(s):
    return [r["text"] for r in s.db.query("SELECT text FROM chat_messages WHERE author='system' ORDER BY rowid")]


def test_join_leave_system_lines(tmp_path):
    from datetime import timedelta as td
    s = build(tmp_path)
    a = join(s, "s1")
    b = join(s, "s2", kind="codex")
    a = join(s, "s1")  # same session again: same member, fresh handle, nothing written
    assert sys_lines(s) == [f"Claude {a['member_id']} joined the chat", f"Codex {b['member_id']} joined the chat"]
    s.chat.leave(a["member_handle"])
    assert sys_lines(s)[-1] == f"Claude {a['member_id']} left the chat"
    clock = s.chat.now()
    s.chat.now = lambda: clock + td(seconds=1000)
    s.chat.sweep()
    lines = sys_lines(s)
    assert lines[-2:] == [f"Codex {b['member_id']} left the chat (no longer running)", "closed"]
    assert s.db.scalar("SELECT COUNT(*) FROM chat_wakes") == 0
    assert s.db.scalar("SELECT agent_streak FROM chat_groups") == 0
    s.db.close()


def test_resume_does_not_requeue_wakes_of_members_who_left(tmp_path):
    s, a, b, _ = pair(tmp_path, cap=50)
    g = a["group_id"]
    s.chat.pause_owner(g)
    held = s.chat.post(a["member_handle"], "held")  # skipped for b
    assert states(s, held["message_id"]) == ["skipped"]
    s.chat.leave(b["member_handle"], "left")
    s.chat.resume_owner(g)
    assert states(s, held["message_id"]) == ["skipped"]
    s.db.close()
