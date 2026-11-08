"""0.14.0 group chats beta: posts allowed while paused, and folding held wakes on resume."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from notify.core import Invalid, TooMany
from tests.test_admin import H, fresh  # noqa: F401  (fixture re-export)
from tests.test_chat_admin import _join, ctx  # noqa: F401
from tests.test_chat_api import chatlive  # noqa: F401
from tests.test_chat_core import AGENT, build, join, svc  # noqa: F401


def pending(svc, member_id):
    return [r["message_id"] for r in svc.db.query("SELECT message_id FROM chat_wakes WHERE member_id=?"
                                                  " AND state='pending' ORDER BY rowid", (member_id,))]


def test_posts_while_paused_are_limited(svc):
    a, b = join(svc, "s1"), join(svc, "s2")
    svc.chat.pause_owner(a["group_id"])
    for i in range(5):  # the default allowance
        svc.chat.post(a["member_handle"], f"still here {i}")
    with pytest.raises(TooMany, match=r"group paused: wait for the owner \(limit 5 posts while paused\)"):
        svc.chat.post(a["member_handle"], "one more")
    svc.chat.post(b["member_handle"], "b has its own allowance")
    svc.chat.post_owner(a["group_id"], "go on")  # the owner is never limited, and resumes the group
    for i in range(3):
        svc.chat.post(a["member_handle"], f"after {i}")  # not paused: no limit
    svc.chat.pause_owner(a["group_id"])
    svc.chat.post(a["member_handle"], "counter was reset on resume")


def test_setting_and_override(tmp_path):
    s = build(tmp_path, chat_paused_posts=0)
    a, _ = join(s, "s1"), join(s, "s2")
    s.chat.pause_owner(a["group_id"])
    with pytest.raises(TooMany, match="limit 0"):
        s.chat.post(a["member_handle"], "x")
    assert s.chat.set_paused_posts(a["group_id"], 2) == {"paused_posts": 2, "effective": 2}
    s.chat.post(a["member_handle"], "1")
    s.chat.post(a["member_handle"], "2")
    with pytest.raises(TooMany, match="limit 2"):
        s.chat.post(a["member_handle"], "3")
    assert s.chat.set_paused_posts(a["group_id"], None)["effective"] == 0
    for bad in (-1, 51, True, "3"):
        with pytest.raises(Invalid):
            s.chat.set_paused_posts(a["group_id"], bad)
    s.db.close()


def test_resume_folds_older_held_wakes(tmp_path):
    s = build(tmp_path, chat_resume_wakes=3, chat_paused_posts=50)
    a, b = join(s, "s1"), join(s, "s2")
    s.chat.pause_owner(a["group_id"])
    ids = [s.chat.post(a["member_handle"], f"m{i}")["message_id"] for i in range(7)]
    before = s.db.scalar("SELECT last_read_rowid FROM chat_members WHERE id=?", (b["member_id"],))
    s.chat.resume_owner(a["group_id"])
    got = pending(s, b["member_id"])
    note = s.db.one("SELECT text FROM chat_messages WHERE id=?", (got[0],))["text"]
    assert note.startswith(f"Claude {b['name']}: while the group was paused, 4 earlier messages arrived")
    assert "group_read since=" in note
    assert got[1:] == ids[4:]  # the newest three, after the notice
    folded = s.db.query("SELECT message_id FROM chat_wakes WHERE member_id=? AND reason='paused, folded'"
                        " ORDER BY rowid", (b["member_id"],))
    assert [r["message_id"] for r in folded] == ids[:4]
    assert s.db.scalar("SELECT last_read_rowid FROM chat_members WHERE id=?", (b["member_id"],)) == before
    for _ in range(4):  # deliver the notice and the three: the folded ones stay unread
        w = asyncio.run(s.chat.claim_host(AGENT, "relay", 0))
        s.chat.confirm(AGENT, w["wake_id"], "relay")
    unread = [m["id"] for m in s.chat.read(b["member_handle"])["messages"]]
    assert ids[:4] == unread[:4]
    s.db.close()


def test_resume_without_folding_when_few(svc):
    a, b = join(svc, "s1"), join(svc, "s2")
    svc.chat.pause_owner(a["group_id"])
    ids = [svc.chat.post(a["member_handle"], f"m{i}")["message_id"] for i in range(2)]
    svc.chat.resume_owner(a["group_id"])
    assert pending(svc, b["member_id"]) == ids


def test_owner_message_resume_folds_too(tmp_path):
    s = build(tmp_path, chat_resume_wakes=1, chat_paused_posts=50)
    a, b = join(s, "s1"), join(s, "s2")
    s.chat.pause_owner(a["group_id"])
    ids = [s.chat.post(a["member_handle"], f"m{i}")["message_id"] for i in range(3)]
    o = s.chat.post_owner(a["group_id"], "carry on")["message_id"]
    got = pending(s, b["member_id"])
    assert len(got) == 3 and got[1:] == [ids[2], o]
    s.db.close()


def test_mcp_429_text(chatlive):  # noqa: F811
    from tests.test_mcp import run_session
    c = chatlive.client("hosta")
    body = {"kind": "claude", "session_id": "s1", "folder": "/srv/app"}
    a = c.post("/v1/chat/presence", json=body).json()
    c.post("/v1/chat/presence", json={**body, "session_id": "s2"})
    chatlive.service.chat.set_paused_posts(a["group_id"], 0)
    chatlive.service.chat.pause_owner(a["group_id"])

    async def go(session):
        return await session.call_tool("group_post", {"member_handle": a["member_handle"], "text": "x"})

    r = run_session(chatlive, "hosta", go)
    assert r.isError and "group paused: wait for the owner (limit 0 posts while paused)" in r.content[0].text


def test_admin_settings_and_group_override(ctx):  # noqa: F811
    srv, b, bearer = ctx
    st = b.put("/admin/api/server", json={"chat_paused_posts": 7, "chat_resume_wakes": 30}, headers=H).json()
    assert st["config"]["server"]["chat_paused_posts"] == 7 and st["config"]["server"]["chat_resume_wakes"] == 30
    assert b.put("/admin/api/server", json={"chat_paused_posts": 51}, headers=H).status_code == 422
    assert b.put("/admin/api/server", json={"chat_resume_wakes": 0}, headers=H).status_code == 422
    assert httpx.put(srv.url + "/admin/api/server", json={"chat_paused_posts": 3},
                     headers={**bearer, **H}).status_code == 403
    j = _join(srv, "host-a", "/srv/one", "s1")
    url = f"/admin/api/chats/{j['group_id']}/settings"
    assert httpx.put(srv.url + url, json={"paused_posts": 1}, headers={**bearer, **H}).status_code == 403
    assert b.put(url, json={"paused_posts": 1}, headers=H).json() == {"paused_posts": 1, "effective": 1}
    assert b.put(url, json={"paused_posts": 99}, headers=H).status_code == 422
    g = b.get("/admin/api/chats").json()["folders"][0]
    assert g["paused_posts"] == 1 and g["paused_posts_effective"] == 1
    b.put(url, json={"paused_posts": None}, headers=H)
    assert b.get("/admin/api/chats").json()["folders"][0]["paused_posts_effective"] == 7
