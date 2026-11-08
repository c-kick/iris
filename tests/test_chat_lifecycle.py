"""0.14.0 group chats beta: same-process rejoin, rejoin after stale, pruning open groups."""

from __future__ import annotations

from datetime import timedelta

from notify.db import iso, utcnow
from tests.test_chat_core import AGENT, FOLDER, build, svc  # noqa: F401


def join(svc, sid, pid=None, start=None, kind="claude", folder=FOLDER):
    pid = pid if pid is not None else 1000 + int.from_bytes(sid.encode()[-2:], "big")  # one process per session
    return svc.chat.join(AGENT, kind=kind, session_id=sid, folder=folder, pid=pid, pid_start=start or f"st-{sid}")


def sysnotes(svc, gid):
    return [r["text"] for r in svc.db.query("SELECT text FROM chat_messages WHERE group_id=? AND author='system'"
                                            " ORDER BY rowid", (gid,))]


def live(svc):
    return {r["id"] for r in svc.db.query("SELECT id FROM chat_members WHERE left_at IS NULL")}


def test_same_process_new_session_replaces(svc):
    a = join(svc, "s1", pid=10, start="p10")
    b = join(svc, "s2", pid=20, start="p20")
    req = svc.chat.post(b["member_handle"], "please")["message_id"]
    svc.chat.claim(a["member_handle"], req)
    a2 = join(svc, "s1-new", pid=10, start="p10")  # /clear or /new in the same window
    assert live(svc) == {b["member_id"], a2["member_id"]}
    row = svc.db.one("SELECT leave_reason FROM chat_members WHERE id=?", (a["member_id"],))
    assert row["leave_reason"] == "replaced"
    notes = sysnotes(svc, a["group_id"])
    assert f"Claude {a['name']} left the chat (new session in the same window)" in notes
    assert f"Claude {a['name']} released {req}" in notes  # claims released as on any leave
    assert a2["name"] != a["name"]


def test_other_pid_or_start_or_kind_is_not_replaced(svc):
    a = join(svc, "s1", pid=10, start="111")
    join(svc, "s2", pid=10, start="222")  # pid reused by another process
    join(svc, "s3", pid=10, start="111", kind="codex")
    svc.chat.join(AGENT, kind="claude", session_id="s4", folder=FOLDER, pid=None, pid_start=None)
    assert a["member_id"] in live(svc) and len(live(svc)) == 4


def test_other_client_cannot_replace(svc):
    from notify.core import Client
    from notify.models import Scope
    a = join(svc, "s1", pid=10, start="p10")
    other = Client("agent-a2", frozenset({Scope.chat}))  # same host, another client
    svc.chat.join(other, kind="claude", session_id="evil", folder=FOLDER, pid=10, pid_start="p10")
    assert a["member_id"] in live(svc)


def test_same_process_presence_replaced(svc):
    svc.chat.presence(AGENT, kind="codex", session_id="p1", folder="/srv/off", pid=7, pid_start="s")
    svc.chat.presence(AGENT, kind="codex", session_id="p2", folder="/srv/off", pid=7, pid_start="s")
    assert [r["session_id"] for r in svc.db.query("SELECT session_id FROM chat_presence")] == ["p2"]


def _stale(svc, m, minutes_ago=5):
    svc.chat.remove_member(m["member_id"], "stale")
    with svc.db.tx() as c:
        c.execute("UPDATE chat_members SET left_at=? WHERE id=?",
                  (iso(utcnow() - timedelta(minutes=minutes_ago)), m["member_id"]))


def test_rejoin_after_stale(svc):
    a, b = join(svc, "s1", pid=10, start="t1"), join(svc, "s2", pid=20, start="t2")
    _stale(svc, a)
    hb = svc.chat.heartbeat(AGENT, [])
    assert {"member_id": a["member_id"], "kind": "claude", "pid": 10, "pid_start": "t1"} in hb["members"]
    svc.chat.heartbeat(AGENT, [{"member_id": a["member_id"], "pid": 10, "pid_start": "t1"}])
    assert a["member_id"] in live(svc)
    assert sysnotes(svc, a["group_id"])[-1] == f"Claude {a['name']} is back"
    svc.chat.post(a["member_handle"], "the old handle still works")
    assert svc.db.scalar("SELECT COUNT(*) FROM events WHERE type='group.member_rejoined'") == 1
    m = svc.db.one("SELECT name, marker FROM chat_members WHERE id=?", (a["member_id"],))
    assert m["name"] == a["name"] and m["marker"] == a["marker"]
    del b


def test_no_rejoin_wrong_start_or_too_late_or_closed(svc):
    a, b = join(svc, "s1", pid=10, start="t1"), join(svc, "s2", pid=20, start="t2")
    _stale(svc, a)
    svc.chat.heartbeat(AGENT, [{"member_id": a["member_id"], "pid": 10, "pid_start": "other"}])
    assert a["member_id"] not in live(svc)
    _stale(svc, b, minutes_ago=61)
    svc.chat.heartbeat(AGENT, [{"member_id": b["member_id"], "pid": 20, "pid_start": "t2"}])
    assert b["member_id"] not in live(svc)  # also: the group closed when b left
    assert svc.db.scalar("SELECT state FROM chat_groups") == "closed"
    hb = svc.chat.heartbeat(AGENT, [])
    assert hb["members"] == []


def test_rejoin_takes_a_new_name_if_taken(svc, monkeypatch):
    from notify import chat_names
    monkeypatch.setattr(chat_names, "NAMES", ("Wren", "Fern", "Moss"))
    a, b = join(svc, "s1", pid=10, start="t1"), join(svc, "s2", pid=20, start="t2")
    _stale(svc, a)
    svc.chat.rename_owner(b["member_id"], a["name"])
    svc.chat.heartbeat(AGENT, [{"member_id": a["member_id"], "pid": 10, "pid_start": "t1"}])
    names = [r["name"] for r in svc.db.query("SELECT name FROM chat_members WHERE left_at IS NULL")]
    assert len(set(n.lower() for n in names)) == 2


def test_quiet_group_closes_after_a_week(svc):
    a, b = join(svc, "s1"), join(svc, "s2")
    other = join(svc, "s3", folder="/srv/busy")
    old = iso(utcnow() - timedelta(days=8))
    with svc.db.tx() as c:
        c.execute("UPDATE chat_members SET last_seen_at=? WHERE group_id=?", (old, a["group_id"]))
    svc.chat.close_quiet_groups(utcnow())
    assert svc.db.scalar("SELECT state FROM chat_groups WHERE id=?", (a["group_id"],)) == "closed"
    assert svc.db.scalar("SELECT state FROM chat_groups WHERE id=?", (other["group_id"],)) == "open"
    assert "closed: nobody seen for 7 days" in sysnotes(svc, a["group_id"])
    del b


def test_empty_owner_group_closes_after_a_week(svc):
    out = svc.chat.open_owner("host-a", "/srv/empty")
    with svc.db.tx() as c:
        c.execute("UPDATE chat_groups SET opened_at=? WHERE id=?", (iso(utcnow() - timedelta(days=8)), out["group_id"]))
    svc.chat.sweep()
    assert svc.db.scalar("SELECT state FROM chat_groups WHERE id=?", (out["group_id"],)) == "closed"


def test_retention_prunes_open_groups(tmp_path):
    s = build(tmp_path, retention_days=30)
    a, b = join(s, "s1"), join(s, "s2")
    ids = [s.chat.post(a["member_handle"], f"m{i}")["message_id"] for i in range(4)]
    s.chat.claim(b["member_handle"], ids[1])
    mention = s.chat.post(a["member_handle"], f"@{b['name']} x")["message_id"]
    s.chat.unclaim(b["member_handle"], mention)
    newest = s.chat.post(a["member_handle"], "newest")["message_id"]
    first = s.db.scalar("SELECT id FROM chat_messages WHERE group_id=? ORDER BY rowid LIMIT 1", (a["group_id"],))
    with s.db.tx() as c:
        c.execute("UPDATE chat_messages SET created_at=?", (iso(utcnow() - timedelta(days=40)),))
    s.retention_sweep()
    left = {r["id"] for r in s.db.query("SELECT id FROM chat_messages WHERE group_id=?", (a["group_id"],))}
    assert left == {first, ids[1], newest}
    assert s.db.scalar("SELECT COUNT(*) FROM chat_mentions") == 0
    assert s.db.scalar("SELECT COUNT(*) FROM chat_wakes WHERE message_id NOT IN (SELECT id FROM chat_messages)") == 0
    s.chat.post(a["member_handle"], "still works")
    s.db.close()


def test_solo_member_clear_keeps_the_group(svc):
    a = join(svc, "s1", pid=10, start="p10")
    a2 = join(svc, "s1-new", pid=10, start="p10")
    assert a2["group_id"] == a["group_id"]
    assert svc.db.scalar("SELECT state FROM chat_groups WHERE id=?", (a["group_id"],)) == "open"
    assert "closed" not in sysnotes(svc, a["group_id"])


def test_stale_rejoin_refused_when_session_joined_again(svc):
    a, b = join(svc, "s1", pid=10, start="t1"), join(svc, "s2", pid=20, start="t2")
    _stale(svc, a)
    again = join(svc, "s1", pid=10, start="t1")  # SessionStart again: a new member
    svc.chat.heartbeat(AGENT, [{"member_id": a["member_id"], "pid": 10, "pid_start": "t1"}])
    assert a["member_id"] not in live(svc) and again["member_id"] in live(svc)
    del b
