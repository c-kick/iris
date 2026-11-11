"""Context reset from the chat (spec 2026-11-11-chat-reset-design.md), and mentions inside code are text."""

from __future__ import annotations

from notify.chat import find_mentions


def test_mentions_inside_code_are_text():
    assert find_mentions("ask `@all /clear` like this") == []
    assert find_mentions("`code @Wren` but @Fern acts") == ["Fern"]
    assert find_mentions("```\n@all\n```\n@Wren please") == ["Wren"]
    assert find_mentions("~~~\n@all\n~~~") == []
    assert find_mentions("@Wren and `x` and @Fern") == ["Wren", "Fern"]


import asyncio  # noqa: E402

from tests.test_chat_core import AGENT, OTHER_HOST, join, svc  # noqa: E402,F401


def _lines(svc, gid):
    return [r["text"] for r in svc.db.query("SELECT text FROM chat_messages WHERE group_id=? AND author='system'"
                                            " ORDER BY rowid", (gid,))]


def _free(svc, sid, tmux="work:1.0", kind="claude"):
    svc.chat.set_status(AGENT, kind=kind, session_id=sid, status="free", tmux=tmux)


def _claim(svc, client=AGENT, resets=True):
    return asyncio.run(svc.chat.claim_host(client, "relay-a", 0, resets=resets))


def test_a_clear_message_becomes_resets_not_a_message(svc):  # noqa: F811
    a, b = join(svc, "s1"), join(svc, "s2")
    gid = a["group_id"]
    _free(svc, "s1"), _free(svc, "s2")
    out = svc.chat.post_owner(gid, f"/clear @{a['name']}")
    assert out["resets"] == [{"member_id": a["member_id"], "name": a["name"], "state": "pending"}]
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_messages WHERE group_id=? AND author='owner'", (gid,)) == 0
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_wakes") == 0, "nobody is woken by it"
    assert _lines(svc, gid)[-1] == f"You asked to reset: {a['name']}"
    out = svc.chat.post_owner(gid, "@all /clear")
    assert {r["member_id"] for r in out["resets"]} == {a["member_id"], b["member_id"]}


def test_who_cannot_be_reset_is_told_at_once(svc):  # noqa: F811
    a, b, c = join(svc, "s1"), join(svc, "s2"), join(svc, "s3")
    gid = a["group_id"]
    _free(svc, "s1")
    svc.chat.set_status(AGENT, kind="claude", session_id="s2", status="busy", tmux="work:2.0")
    svc.chat.set_status(AGENT, kind="claude", session_id="s3", status="free")  # never reported a tmux place
    out = svc.chat.post_owner(gid, "/clear @all")
    states = {r["name"]: r["state"] for r in out["resets"]}
    assert states == {a["name"]: "pending", b["name"]: "busy", c["name"]: "not_tmux"}
    lines = _lines(svc, gid)
    assert f"Claude {b['name']}: not reset (busy; try again when it is free)" in lines
    assert f"Claude {c['name']}: not reset (not running in tmux)" in lines


def test_only_a_relay_that_asks_gets_resets(svc):  # noqa: F811
    a = join(svc, "s1")
    _free(svc, "s1")
    svc.chat.post_owner(a["group_id"], "/clear @all")
    assert _claim(svc, resets=False) is None, "an older relay never sees a reset"
    assert _claim(svc, OTHER_HOST) is None, "only the member's own host"
    r = _claim(svc)
    assert r["type"] == "reset" and r["member_id"] == a["member_id"] and r["kind"] == "claude"
    assert r["pid"] == 100 and r["folder"] == "/srv/app" and "text" not in r


def test_outcomes_become_lines(svc):  # noqa: F811
    a, b = join(svc, "s1"), join(svc, "s2")
    gid = a["group_id"]
    _free(svc, "s1"), _free(svc, "s2")
    svc.chat.post_owner(gid, "/clear @all")
    r1, = [_claim(svc)]
    svc.chat.reset_done(AGENT, r1["reset_id"], "relay-a", "typed")
    r2 = _claim(svc)
    svc.chat.reset_done(AGENT, r2["reset_id"], "relay-a", "reset")
    lines = _lines(svc, gid)
    assert any(ln.endswith(": not reset (its input line was not empty, or could not be read; nothing was sent)")
               for ln in lines)
    assert any(ln.endswith(": context cleared") for ln in lines)
    assert _claim(svc) is None


def test_after_a_reset_the_new_session_keeps_name_and_colour(svc):  # noqa: F811
    a = join(svc, "s1")
    gid = a["group_id"]
    _free(svc, "s1")
    svc.chat.post_owner(gid, f"/clear @{a['name']}")
    r = _claim(svc)
    svc.chat.reset_done(AGENT, r["reset_id"], "relay-a", "reset")
    svc.chat.leave(a["member_handle"], "ended")  # Claude's SessionEnd on /clear
    assert svc.db.scalar("SELECT left_at FROM chat_members WHERE id=?", (a["member_id"],)) is None
    # the new session of the same process: pid 100, the same pid_start
    n = svc.chat.join(AGENT, kind="claude", session_id="s1-new", folder="/srv/app", pid=100, pid_start="x-s1")
    assert (n["member_id"], n["name"]) == (a["member_id"], a["name"]), "same member: same name and colour"
    assert svc.db.scalar("SELECT session_id FROM chat_members WHERE id=?", (a["member_id"],)) == "s1-new"
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_members WHERE group_id=?", (gid,)) == 1
    assert _lines(svc, gid)[-1] == f"Claude {a['name']} started a fresh context"


def test_a_codex_in_a_shared_app_server_is_taken_over_by_folder(svc):  # noqa: F811
    a = svc.chat.join(AGENT, kind="codex", session_id="t1", folder="/srv/app", pid=500, pid_start="srv",
                      shared_process=True)
    _free(svc, "t1", kind="codex")
    svc.chat.post_owner(a["group_id"], "/clear @all")
    svc.chat.reset_done(AGENT, _claim(svc)["reset_id"], "relay-a", "reset")
    n = svc.chat.join(AGENT, kind="codex", session_id="t2", folder="/srv/app", pid=500, pid_start="srv",
                      shared_process=True)
    assert n["member_id"] == a["member_id"]


def test_no_new_session_within_three_minutes_means_it_left(svc):  # noqa: F811
    from datetime import timedelta
    a = join(svc, "s1")
    _free(svc, "s1")
    svc.chat.post_owner(a["group_id"], "/clear @all")
    svc.chat.reset_done(AGENT, _claim(svc)["reset_id"], "relay-a", "reset")
    svc.chat.leave(a["member_handle"], "ended")
    later = svc.chat.now() + timedelta(minutes=4)
    svc.chat.now = lambda: later
    svc.chat.heartbeat(AGENT, [])
    assert svc.db.scalar("SELECT leave_reason FROM chat_members WHERE id=?", (a["member_id"],)) == "ended"


def test_agents_cannot_ask_for_a_reset(svc):  # noqa: F811
    a, b = join(svc, "s1"), join(svc, "s2")
    _free(svc, "s1"), _free(svc, "s2")
    out = svc.chat.post(a["member_handle"], f"/clear @{b['name']}")
    assert "resets" not in out
    assert _claim(svc) is not None and _claim(svc).get("type") != "reset"


def test_page_offers_the_reset_and_marks_who_cannot():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "notify/admin_ui/app.js").read_text()
    fn = js[js.index("function chatMembersHtml"):js.index("function chatNameStyle")]
    assert "m.resettable === false" in fn and "disabled" in fn and "no reset · " in fn
    assert 'data-reset="${esc(m.id)}"' in fn
    assert 'async function chatReset' in js and 'confirm(`Clear the context of ${name}?' in js
    assert '[data-reset]' in js[js.index("roster.onclick"):]


def test_an_existing_session_rejoining_steals_nothing(svc):  # noqa: F811
    a = svc.chat.join(AGENT, kind="codex", session_id="t1", folder="/srv/app", pid=500, pid_start="srv",
                      shared_process=True)
    b = svc.chat.join(AGENT, kind="codex", session_id="t9", folder="/srv/app", pid=500, pid_start="srv",
                      shared_process=True)
    _free(svc, "t1", kind="codex")
    svc.chat.post_owner(a["group_id"], f"/clear @{a['name']}")
    svc.chat.reset_done(AGENT, _claim(svc)["reset_id"], "relay-a", "reset")
    again = svc.chat.join(AGENT, kind="codex", session_id="t9", folder="/srv/app", pid=500, pid_start="srv",
                          shared_process=True)  # b joins again (a resume): it stays b
    assert again["member_id"] == b["member_id"]
    assert svc.db.scalar("SELECT session_id FROM chat_members WHERE id=?", (a["member_id"],)) == "t1"


def test_a_comma_is_no_clear_message(svc):  # noqa: F811
    a = join(svc, "s1")
    _free(svc, "s1")
    out = svc.chat.post_owner(a["group_id"], f"/clear,@{a['name']}")
    assert "resets" not in out


def test_claim_tells_the_relay_how_many_codex_members_there_are(svc):  # noqa: F811
    a = svc.chat.join(AGENT, kind="codex", session_id="t1", folder="/srv/app", pid=500, pid_start="srv",
                      shared_process=True)
    svc.chat.join(AGENT, kind="codex", session_id="t2", folder="/srv/app", pid=500, pid_start="srv",
                  shared_process=True)
    _free(svc, "t1", kind="codex")
    svc.chat.post_owner(a["group_id"], f"/clear @{a['name']}")
    assert _claim(svc)["codex_members"] == 2
