"""0.18.0: Codex sessions hosted by the shared `codex app-server` process.

Every such session reports the same pid (the server's), so a pid tells neither which session ended nor whether it
is still open, and Codex does not always run its SessionEnd hook on /exit. Such members are flagged shared_process:
same-process replacement does not apply to them.

0.18.0 to 0.20.x also removed such a member as "idle" after 2 quiet hours; that removed dormant members that were
still listening, so it was reverted. Members it left as idle come back at their next activity or chat call.
"""

from __future__ import annotations

from datetime import timedelta


from tests.test_chat_core import AGENT, FOLDER, svc  # noqa: F401

QUIET = 2 * 3600  # the former idle window

SERVER_PID, SERVER_START = 4242, "srv-start"


def cx(svc, sid, shared=True, folder=FOLDER):
    return svc.chat.join(AGENT, kind="codex", session_id=sid, folder=folder, pid=SERVER_PID, pid_start=SERVER_START,
                         shared_process=shared)


def live(svc):
    return {r["id"] for r in svc.db.query("SELECT id FROM chat_members WHERE left_at IS NULL")}


def notes(svc, gid):
    return [r["text"] for r in svc.db.query("SELECT text FROM chat_messages WHERE group_id=? AND author='system'"
                                            " ORDER BY rowid", (gid,))]


def alive(*members):
    return [{"member_id": m["member_id"], "pid": SERVER_PID, "pid_start": SERVER_START} for m in members]


def at(svc, seconds):
    t0 = svc.chat.now()
    svc.chat.now = lambda: t0 + timedelta(seconds=seconds)


def test_sessions_on_the_shared_server_do_not_replace_each_other(svc):
    a, b = cx(svc, "cx-1"), cx(svc, "cx-2")
    assert live(svc) == {a["member_id"], b["member_id"]}
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_members WHERE shared_process=1") == 2
    # an unflagged process is still "a new session in the same window"
    c = cx(svc, "cx-3", shared=False)
    d = cx(svc, "cx-4", shared=False)
    assert c["member_id"] not in live(svc) and d["member_id"] in live(svc)


def test_shared_presences_do_not_replace_each_other(svc):
    for sid in ("p1", "p2"):
        svc.chat.presence(AGENT, kind="codex", session_id=sid, folder="/srv/off", pid=SERVER_PID,
                          pid_start=SERVER_START, shared_process=True)
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_presence WHERE shared_process=1") == 2


def test_a_quiet_shared_member_stays_while_its_server_is_alive(svc):
    a, b = cx(svc, "cx-1"), cx(svc, "cx-2")  # a listens, never prompted
    at(svc, QUIET * 12)
    svc.chat.heartbeat(AGENT, alive(a, b))
    assert live(svc) == {a["member_id"], b["member_id"]}
    assert not any("quiet for" in n for n in notes(svc, a["group_id"]))
    svc.chat.post(a["member_handle"], "still here")


def test_a_quiet_shared_presence_stays(svc):
    svc.chat.presence(AGENT, kind="codex", session_id="p1", folder="/srv/off", pid=SERVER_PID,
                      pid_start=SERVER_START, shared_process=True)
    pid_ = svc.db.scalar("SELECT id FROM chat_presence")
    at(svc, QUIET * 12)
    svc.chat.heartbeat(AGENT, [{"member_id": pid_, "pid": SERVER_PID, "pid_start": SERVER_START}])
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_presence") == 1


def test_an_unshared_member_is_never_left_for_being_quiet(svc):
    m = svc.chat.join(AGENT, kind="claude", session_id="cl-1", folder=FOLDER, pid=77, pid_start="s77")
    at(svc, QUIET * 5)
    svc.chat.heartbeat(AGENT, [{"member_id": m["member_id"], "pid": 77, "pid_start": "s77"}])
    assert m["member_id"] in live(svc)


def _idle(svc, a, keep=None):
    """A member 0.18.0 to 0.20.x left as idle (the row the upgrade finds)."""
    svc.chat.remove_member(a["member_id"], "idle", keep_open=True)
    assert a["member_id"] not in live(svc)


def test_activity_takes_an_idle_member_back(svc):
    a, keep = cx(svc, "cx-1"), cx(svc, "cx-keep")
    _idle(svc, a, keep)
    svc.chat.set_status(AGENT, kind="codex", session_id="cx-1", status="busy")
    assert a["member_id"] in live(svc)
    m = svc.db.one("SELECT name, marker, status FROM chat_members WHERE id=?", (a["member_id"],))
    assert m["name"] == a["name"] and m["marker"] == a["marker"] and m["status"] == "busy"
    assert notes(svc, a["group_id"])[-1] == f"Codex {a['name']} is back"
    svc.chat.post(a["member_handle"], "the old handle works again")


def test_a_chat_call_with_the_handle_takes_an_idle_member_back(svc):
    a, keep = cx(svc, "cx-1"), cx(svc, "cx-keep")
    _idle(svc, a, keep)
    svc.chat.read(a["member_handle"])
    assert a["member_id"] in live(svc)


def test_no_take_back_when_the_group_closed_or_the_session_joined_again(svc):
    a, keep = cx(svc, "cx-1"), cx(svc, "cx-keep")
    _idle(svc, a, keep)
    again = cx(svc, "cx-1")  # the session started over (SessionStart)
    svc.chat.set_status(AGENT, kind="codex", session_id="cx-1", status="busy")
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_members WHERE session_id='cx-1' AND left_at IS NULL") == 1
    assert again["member_id"] in live(svc)
    svc.chat.remove_member(again["member_id"], "ended")
    svc.chat.remove_member(keep["member_id"], "ended")  # the group closes
    assert svc.db.scalar("SELECT state FROM chat_groups") == "closed"
    import pytest
    from notify.core import NotFound
    with pytest.raises(NotFound):  # a closed group takes nobody back
        svc.chat.set_status(AGENT, kind="codex", session_id="cx-1", status="busy")
    assert live(svc) == set()


def test_presence_turned_member_keeps_the_flag(svc):
    out = svc.chat.presence(AGENT, kind="codex", session_id="p1", folder="/srv/off", pid=SERVER_PID,
                            pid_start=SERVER_START, shared_process=True)
    svc.chat.open_adhoc(out["member_handle"], "need a second opinion")
    assert svc.db.scalar("SELECT shared_process FROM chat_members WHERE session_id='p1'") == 1


def test_api_accepts_the_flag(svc):
    from notify.chat_api import JoinIn
    assert JoinIn(kind="codex", session_id="s", folder="/x", pid=1, pid_start="a", shared_process=True).shared_process
    assert JoinIn(kind="codex", session_id="s", folder="/x").shared_process is False


def test_a_lone_member_going_idle_can_come_back(svc):
    a = cx(svc, "cx-1")
    _idle(svc, a)
    assert a["member_id"] not in live(svc)
    assert svc.db.scalar("SELECT state FROM chat_groups") == "open", "kept open for its return"
    svc.chat.set_status(AGENT, kind="codex", session_id="cx-1", status="busy")
    assert a["member_id"] in live(svc)


def test_quiet_group_with_only_an_idle_member_closes_after_a_week_not_at_once(svc):
    a = cx(svc, "cx-1")
    _idle(svc, a)
    svc.chat.close_quiet_groups(svc.chat.now())
    assert svc.db.scalar("SELECT state FROM chat_groups") == "open"
    svc.chat.close_quiet_groups(svc.chat.now() + timedelta(days=8))
    assert svc.db.scalar("SELECT state FROM chat_groups") == "closed"


def test_stale_shared_member_comes_back_beside_a_live_sibling(svc):
    a, b = cx(svc, "cx-1"), cx(svc, "cx-2")
    svc.chat.remove_member(a["member_id"], "stale")
    svc.chat.set_status(AGENT, kind="codex", session_id="cx-2", status="busy")
    svc.chat.heartbeat(AGENT, alive(a, b))
    assert a["member_id"] in live(svc), "a sibling on the same server pid is not 'the same process joined again'"


def test_ending_an_idle_member_does_not_bring_it_back_first(svc):
    a, keep = cx(svc, "cx-1"), cx(svc, "cx-keep")
    _idle(svc, a, keep)
    svc.chat.leave(a["member_handle"], "ended")
    assert a["member_id"] not in live(svc)
    assert not any(n.endswith(" is back") for n in notes(svc, a["group_id"]))


def test_a_lone_idle_member_ending_closes_its_group(svc):
    a = cx(svc, "cx-1")
    _idle(svc, a)
    svc.chat.leave(a["member_handle"], "ended")
    assert svc.db.scalar("SELECT state FROM chat_groups") == "closed"
