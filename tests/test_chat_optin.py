"""0.14.0 group chats beta: folder opt-in, presence, ad-hoc groups, invites, report-in and intent."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

from notify.chat import MemberGone
from notify.core import Conflict, Forbidden, Invalid
from notify.db import Database
from tests.test_admin import H, fresh  # noqa: F401  (fixture re-export)
from tests.test_chat_admin import ctx  # noqa: F401
from tests.test_chat_api import chatlive  # noqa: F401
from tests.test_chat_core import AGENT, FOLDER, OTHER_HOST, build, svc  # noqa: F401
from tests.test_notify_chat import Env, hook


def run(coro):
    return asyncio.run(coro)


def pres(svc, sid, folder=FOLDER, client=AGENT, kind="claude", pid=100):
    return svc.chat.presence(client, kind=kind, session_id=sid, folder=folder, pid=pid, pid_start="x")


def groups(svc):
    return svc.db.query("SELECT * FROM chat_groups ORDER BY rowid")


def sysnotes(svc, gid):
    return [r["text"] for r in svc.db.query("SELECT text FROM chat_messages WHERE group_id=? AND author='system'"
                                            " ORDER BY rowid", (gid,))]


def test_disabled_folder_is_presence_only(svc):
    r = pres(svc, "s1")
    assert r["state"] == "present" and r["member_handle"].startswith(r["member_id"] + ":")
    assert "Group chats are off" in r["context"] and "group_open" in r["context"] and r["member_handle"] in r["context"]
    assert len(r["context"]) < 300 and "marker" not in r
    assert groups(svc) == [] and svc.db.scalar("SELECT COUNT(*) FROM chat_presence") == 1
    again = pres(svc, "s1")  # the same session again: same id, fresh handle
    assert again["member_id"] == r["member_id"] and again["member_handle"] != r["member_handle"]
    with pytest.raises(MemberGone):
        svc.chat.presence_from_handle(r["member_handle"])


def test_enabled_folder_joins_with_report_in(svc):
    svc.chat.set_folder("host-a", FOLDER, True)
    r = pres(svc, "s1")
    assert r["state"] == "joined" and r["name"] and r["marker"]
    c = r["context"]
    assert r["member_handle"] in c and r["marker"] in c and "Before you start, post one line" in c
    assert "at most about 15 words" in c and "If a member objects" in c
    other = pres(svc, "s2", kind="codex")
    assert f"{r['name']} (Claude, unknown)" in other["context"]


def test_open_group_joins_even_when_disabled(svc):
    svc.chat.set_folder("host-a", FOLDER, True)
    a = pres(svc, "s1")
    svc.chat.set_folder("host-a", FOLDER, False)  # off: the open group stays until it empties
    b = pres(svc, "s2")
    assert b["state"] == "joined" and b["group_id"] == a["group_id"]
    svc.chat.leave(a["member_handle"])
    svc.chat.leave(b["member_handle"])
    assert pres(svc, "s3")["state"] == "present"


def test_group_open_invites_same_folder_only(svc):
    a = pres(svc, "s1")
    b = pres(svc, "s2", kind="codex")
    elsewhere = pres(svc, "s3", folder="/srv/other")
    other_host = pres(svc, "s4", client=OTHER_HOST)
    out = svc.chat.open_adhoc(a["member_handle"], "split the refactor between us")
    assert out["state"] == "joined" and out["member_id"] == a["member_id"]
    g = groups(svc)[0]
    assert g["adhoc"] == 1 and g["folder"] == FOLDER and g["host"] == "host-a"
    svc.chat.post(a["member_handle"], "the old handle works")  # same id and handle as the presence
    inv = svc.db.query("SELECT * FROM chat_invites")
    assert [i["presence_id"] for i in inv] == [b["member_id"]]
    notes = sysnotes(svc, g["id"])
    assert notes[0] == f"Claude {out['name']} opened the chat: split the refactor between us"
    assert notes[1].startswith(f"{out['name']} (Claude) opened a group chat here: split the refactor")
    assert "group_join" in notes[1]
    for p in (elsewhere, other_host):
        assert svc.db.scalar("SELECT COUNT(*) FROM chat_invites WHERE presence_id=?", (p["member_id"],)) == 0


def test_invite_goes_through_the_relay(svc):
    a, b = pres(svc, "s1"), pres(svc, "s2", kind="codex")
    svc.chat.open_adhoc(a["member_handle"], "need a hand")
    svc.chat.pause_owner(groups(svc)[0]["id"])  # invites go out while paused too
    w = run(svc.chat.claim_host(AGENT, "relay", 0))
    assert w["wake_id"].startswith("inv_") and w["member_id"] == b["member_id"] and w["kind"] == "codex"
    assert w["session_id"] == "s2" and "group_join" in w["text"] and w["text"].startswith("[hnl-notify group chat")
    svc.chat.release(AGENT, w["wake_id"], "relay")
    assert svc.db.scalar("SELECT state FROM chat_invites") == "pending"
    with svc.db.tx() as c:
        c.execute("UPDATE chat_invites SET retry_after=NULL")
    w = run(svc.chat.claim_host(AGENT, "relay", 0))
    svc.chat.confirm(AGENT, w["wake_id"], "relay")
    assert svc.db.scalar("SELECT state FROM chat_invites") == "delivered"
    assert run(svc.chat.claim_host(AGENT, "relay", 0)) is None


def test_group_join_after_invite(svc):
    a, b = pres(svc, "s1"), pres(svc, "s2", kind="codex")
    with pytest.raises(Invalid, match="no group chat is open"):
        svc.chat.join_open(b["member_handle"])
    svc.chat.open_adhoc(a["member_handle"], "pairing")
    j = svc.chat.join_open(b["member_handle"])
    assert j["state"] == "joined" and j["member_id"] == b["member_id"] and j["marker"]
    assert "saying you joined (no plan)" in j["context"] and "Before you start" not in j["context"]
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_invites") == 0
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_presence") == 0
    with pytest.raises(MemberGone):
        svc.chat.join_open(b["member_handle"])  # a member now, not a presence


def test_second_open_joins_and_new_session_auto_joins(svc):
    a, b = pres(svc, "s1"), pres(svc, "s2")
    svc.chat.open_adhoc(a["member_handle"], "x")
    out = svc.chat.open_adhoc(b["member_handle"], "y")
    assert out["note"].startswith("a group was already open") and len(groups(svc)) == 1
    assert pres(svc, "s3")["state"] == "joined"  # the folder's open group
    again = svc.chat.open_adhoc(a["member_handle"], "z")  # already a member
    assert again["already_member"] is True


def test_open_needs_reason_and_own_host(svc):
    a = pres(svc, "s1")
    with pytest.raises(Invalid):
        svc.chat.open_adhoc(a["member_handle"], "  ")
    with pytest.raises(Invalid):
        svc.chat.open_adhoc(a["member_handle"], "x" * 301)
    with pytest.raises(Forbidden):
        svc.chat.check_handle_host(OTHER_HOST, a["member_handle"])


def test_presence_ends_with_the_session(svc):
    a = pres(svc, "s1")
    svc.chat.leave(a["member_handle"], "ended")
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_presence") == 0


def test_presence_heartbeat_and_stale_sweep(svc):
    from datetime import timedelta
    a = pres(svc, "s1", pid=4242)
    hb = svc.chat.heartbeat(AGENT, [])
    assert {"member_id": a["member_id"], "kind": "claude", "pid": 4242, "pid_start": "x"} in hb["members"]
    t0 = svc.chat.now()
    svc.chat.started_at = t0 - timedelta(hours=1)
    svc.chat.relay_seen["host-a"] = (t0 - timedelta(minutes=10), t0)
    svc.chat.heartbeat(AGENT, [{"member_id": a["member_id"]}])
    svc.chat.sweep(t0 + timedelta(seconds=60))
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_presence") == 1
    svc.chat.relay_seen["host-a"] = (t0 - timedelta(minutes=10), t0 + timedelta(minutes=5))
    svc.chat.sweep(t0 + timedelta(minutes=5))
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_presence") == 0


def test_presence_status_carries_into_member(svc):
    a, b = pres(svc, "s1"), pres(svc, "s2")
    svc.chat.set_status(AGENT, kind="claude", session_id="s2", status="busy")
    svc.chat.open_adhoc(a["member_handle"], "x")
    j = svc.chat.join_open(b["member_handle"])
    assert svc.db.scalar("SELECT status FROM chat_members WHERE id=?", (j["member_id"],)) == "busy"


def test_intent_first_post_and_update(svc):
    svc.chat.set_folder("host-a", FOLDER, True)
    a, b = pres(svc, "s1"), pres(svc, "s2")
    svc.chat.post(a["member_handle"], "Juniper: fixing the flaky\nlogin test " + "x" * 200)
    svc.chat.post(a["member_handle"], "second message is not the plan")
    m = {x["member_id"]: x for x in svc.chat.read(b["member_handle"])["members"]}[a["member_id"]]
    assert m["intent"].startswith("Juniper: fixing the flaky login test x") and len(m["intent"]) == 120
    svc.chat.post(a["member_handle"], "now: docs", intent=True)
    m = {x["member_id"]: x for x in svc.chat.read(b["member_handle"])["members"]}[a["member_id"]]
    assert m["intent"] == "now: docs"


def test_set_folder_checks_host(svc):
    with pytest.raises(Invalid):
        svc.chat.set_folder("host-zz", FOLDER, True)
    with pytest.raises(Invalid):
        svc.chat.set_folder("host-a", "relative/path", True)


def test_owner_open_invites_present_sessions(svc):
    a, b = pres(svc, "s1"), pres(svc, "s2")
    out = svc.chat.open_owner("host-a", FOLDER)
    assert out["invited"] == 2
    assert sysnotes(svc, out["group_id"])[0].startswith("The owner opened a group chat here.")
    with pytest.raises(Conflict):
        svc.chat.open_owner("host-a", FOLDER)
    svc.chat.join_open(a["member_handle"])
    del b


def test_folders_listing(svc):
    pres(svc, "s1", folder="/srv/present")
    svc.chat.set_folder("host-a", "/srv/on", True)
    svc.chat.set_folder("host-a", "/srv/off", True)
    svc.chat.set_folder("host-a", "/srv/off", False)  # switched off again: still listed
    f = {x["folder"]: x for x in svc.chat.folders()}
    assert f["/srv/present"]["present"] == 1 and not f["/srv/present"]["enabled"]
    assert f["/srv/on"]["enabled"] and f["/srv/on"]["enabled_at"]
    assert "/srv/off" in f and not f["/srv/off"]["enabled"]


def test_migration_keeps_open_group_as_adhoc(tmp_path):
    s = build(tmp_path)
    s.chat.join(AGENT, kind="claude", session_id="s1", folder=FOLDER, pid=1, pid_start="x")
    with s.db.tx() as c:
        c.execute("ALTER TABLE chat_groups DROP COLUMN adhoc")
        c.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    s.db.close()
    db = Database(tmp_path / "n.sqlite3")
    assert db.scalar("SELECT adhoc FROM chat_groups") == 1
    assert db.scalar("SELECT COUNT(*) FROM chat_folders") == 0  # no folder switched on by the upgrade
    db.close()


def test_rest_presence_and_compat_join(chatlive, tmp_path):  # noqa: F811
    chatlive.service.chat.auto_enable = False
    c = chatlive.client("hosta")
    body = {"kind": "claude", "session_id": "s1", "folder": FOLDER, "pid": 5, "pid_start": "x"}
    r = c.post("/v1/chat/presence", json=body)
    assert r.status_code == 201 and r.json()["state"] == "present"
    r = c.post("/v1/chat/join", json={**body, "session_id": "s2"})  # an old hook script
    assert r.status_code == 201 and r.json()["state"] == "present"
    chatlive.service.chat.set_folder("host-a", FOLDER, True)
    r = c.post("/v1/chat/presence", json={**body, "session_id": "s3"})
    assert r.json()["state"] == "joined"


def test_notify_chat_start_present_then_end(chatlive, tmp_path):  # noqa: F811
    chatlive.service.chat.auto_enable = False
    e = Env(tmp_path, chatlive)
    h = hook(tmp_path)
    p = e.run("start", "claude", h, CLAUDE_PID=str(os.getpid()))
    out = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
    assert p.returncode == 0 and "Group chats are off" in out
    assert json.loads((e.state / "claude-sess-1.json").read_text())["state"] == "present"
    assert e.run("end", "claude", h).returncode == 0
    assert chatlive.service.db.scalar("SELECT COUNT(*) FROM chat_presence") == 0


def test_mcp_group_open_and_join(chatlive):  # noqa: F811
    from tests.test_mcp import payload, run_session
    chatlive.service.chat.auto_enable = False
    c = chatlive.client("hosta")
    body = {"kind": "claude", "session_id": "s1", "folder": FOLDER, "pid": 5, "pid_start": "x"}
    a = c.post("/v1/chat/presence", json=body).json()
    b = c.post("/v1/chat/presence", json={**body, "session_id": "s2"}).json()

    async def go(session):
        o = await session.call_tool("group_open", {"member_handle": a["member_handle"], "reason": "pair up"})
        j = await session.call_tool("group_join", {"member_handle": b["member_handle"]})
        return payload(o), payload(j)

    o, j = run_session(chatlive, "hosta", go)
    assert o["state"] == "joined" and j["state"] == "joined" and o["group_id"] == j["group_id"]


def test_admin_folder_api(ctx):  # noqa: F811
    srv, b, bearer = ctx
    srv.service.chat.presence(__import__("notify.core", fromlist=["Client"]).Client(
        "agent-a", frozenset()), kind="claude", session_id="s1", folder="/srv/one", pid=None, pid_start=None)
    got = b.get("/admin/api/chats/folders").json()
    assert got["hosts"] == ["host-a", "host-b"] and got["folders"][0]["present"] == 1
    body = {"host": "host-a", "folder": "/srv/one", "enabled": True}
    assert httpx.put(srv.url + "/admin/api/chats/folders", json=body, headers={**bearer, **H}).status_code == 403
    assert b.put("/admin/api/chats/folders", json=body, headers=H).json()["enabled"] is True
    assert b.put("/admin/api/chats/folders", json={**body, "host": "nope"}, headers=H).status_code == 422
    assert b.get("/admin/api/chats/folders").json()["folders"][0]["enabled"] is True
    b.put("/admin/api/chats/folders", json={**body, "enabled": False}, headers=H)
    r = b.post("/admin/api/chats/folders/open", json=body, headers=H)
    assert r.status_code == 201 and r.json()["invited"] == 1
    assert b.post("/admin/api/chats/folders/open", json=body, headers=H).status_code == 409
    assert httpx.post(srv.url + "/admin/api/chats/folders/open", json=body,
                      headers={**bearer, **H}).status_code == 403
    listed = b.get("/admin/api/chats").json()["folders"][0]
    assert listed["presences"] == [{"kind": "claude", "since": listed["presences"][0]["since"]}]
    assert listed["adhoc"] is True and listed["enabled"] is False
