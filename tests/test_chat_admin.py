"""Agent group chats: the admin API behind the Chats page."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.test_admin import H, fresh, setup_admin  # noqa: F401  (fixture re-export)


@pytest.fixture
def ctx(fresh):  # noqa: F811
    srv = fresh()
    b = setup_admin(srv)
    b.post("/admin/api/clients/ops", json={"scopes": ["admin"]}, headers=H)
    ops = b.post("/admin/api/clients/ops2", json={"scopes": ["admin"]}, headers=H).json()["token"]
    for n, h in (("a", "host-a"), ("b", "host-b")):
        b.post(f"/admin/api/clients/agent-{n}", json={"scopes": ["chat"], "chat_host": h}, headers=H)
    return srv, b, {"Authorization": f"Bearer {ops}"}


def _join(srv, host, folder, sid):
    """A member straight in the database, through the service (no agent token needed)."""
    from notify.core import Client
    from notify.models import Scope
    srv.app.state.config.clients  # config is live
    return srv.service.chat.join(Client(f"agent-{host[-1]}", frozenset({Scope.chat})), kind="claude",
                                 session_id=sid, folder=folder, pid=None, pid_start=None)


def test_admin_list_groups_order(ctx):
    srv, b, bearer = ctx
    o1 = _join(srv, "host-a", "/srv/one", "s1")
    o2 = _join(srv, "host-a", "/srv/two", "s2")
    c1 = _join(srv, "host-a", "/srv/old", "s3")
    c2 = _join(srv, "host-b", "/srv/older", "s4")
    chat = srv.service.chat
    chat.leave(c1["member_handle"])
    chat.leave(c2["member_handle"])
    with srv.service.db.tx() as cx:  # fixed times: the order must not depend on the clock tick
        for gid, col, ts in ((o1["group_id"], "opened_at", "2026-11-01T10:00:00Z"),
                             (o2["group_id"], "opened_at", "2026-11-02T10:00:00Z"),
                             (c1["group_id"], "closed_at", "2026-11-03T10:00:00Z"),
                             (c2["group_id"], "closed_at", "2026-11-01T10:00:00Z")):
            cx.execute(f"UPDATE chat_groups SET {col}=? WHERE id=?", (ts, gid))
    chat.post(o1["member_handle"], "hello\nworld " + "x" * 200)
    groups = b.get("/admin/api/chats").json()["groups"]
    assert [g["folder"] for g in groups] == ["/srv/two", "/srv/one", "/srv/old", "/srv/older"]
    assert [g["state"] for g in groups] == ["open", "open", "closed", "closed"]
    one = next(g for g in groups if g["folder"] == "/srv/one")
    assert one["unread"] == 1 and one["last_message"]["author"] == "claude"
    assert one["last_message"]["text_preview"].startswith("hello world x")
    assert len(one["last_message"]["text_preview"]) <= 120
    assert set(one["members"][0]) == {"id", "name", "kind", "joined_at", "last_seen_at", "left_at", "status",
                                      "status_since", "quiet_minutes", "claims", "oldest_claim", "intent", "model", "model_id", "tmux", "resettable", "reset_note", "is_lead"}
    assert [g["id"] for g in b.get("/admin/api/chats?host=host-b").json()["groups"]] == [groups[3]["id"]]
    assert len(b.get("/admin/api/chats?host=host-a&folder=/srv/two").json()["groups"]) == 1
    # seen clears unread
    assert b.post(f"/admin/api/chats/{one['id']}/seen", headers=H).status_code == 200
    assert next(g for g in b.get("/admin/api/chats").json()["groups"] if g["id"] == one["id"])["unread"] == 0


def test_owner_post_needs_session(ctx):
    srv, b, bearer = ctx
    j = _join(srv, "host-a", "/srv/one", "s1")
    r = httpx.post(f"{srv.url}/admin/api/chats/{j['group_id']}/messages", json={"text": "hi"},
                   headers={**bearer, **H})
    assert r.status_code == 403
    assert httpx.post(f"{srv.url}/admin/api/chats/{j['group_id']}/seen", headers={**bearer, **H}).status_code == 403
    assert srv.service.db.scalar("SELECT COUNT(*) FROM chat_messages WHERE author='owner'") == 0


def test_owner_post_needs_header(ctx):
    srv, b, _ = ctx
    j = _join(srv, "host-a", "/srv/one", "s1")
    assert b.post(f"/admin/api/chats/{j['group_id']}/messages", json={"text": "hi"}).status_code == 403
    assert b.post(f"/admin/api/chats/{j['group_id']}/seen").status_code == 403
    r = b.post(f"/admin/api/chats/{j['group_id']}/messages", json={"text": "hi"}, headers=H)
    assert r.status_code == 201 and len(r.json()["woken"]) == 1 and r.json()["paused"] is False
    assert b.post(f"/admin/api/chats/{j['group_id']}/messages", json={"text": "  "}, headers=H).status_code == 422
    assert b.post("/admin/api/chats/grp_nope/messages", json={"text": "x"}, headers=H).status_code == 404


def test_owner_post_closed_409(ctx):
    srv, b, _ = ctx
    j = _join(srv, "host-a", "/srv/one", "s1")
    srv.service.chat.leave(j["member_handle"])
    r = b.post(f"/admin/api/chats/{j['group_id']}/messages", json={"text": "late"}, headers=H)
    assert r.status_code == 409


def test_messages_raw_text_and_wake_summary(ctx):
    srv, b, bearer = ctx
    a = _join(srv, "host-a", "/srv/one", "s1")
    c = _join(srv, "host-a", "/srv/one", "s2")
    d = _join(srv, "host-a", "/srv/one", "s3")
    chat = srv.service.chat
    first = chat.post(a["member_handle"], "<b>raw</b> text")["message_id"]
    # c's wake delivered, d's skipped
    with srv.service.db.tx() as cx:
        cx.execute("UPDATE chat_wakes SET state='delivered' WHERE message_id=? AND member_id=?", (first, c["member_id"]))
        cx.execute("UPDATE chat_wakes SET state='claimed' WHERE message_id=? AND member_id=?", (first, d["member_id"]))
    second = chat.post_owner(a["group_id"], "ok")["message_id"]
    gid = a["group_id"]
    msgs = [m for m in b.get(f"/admin/api/chats/{gid}/messages").json()["messages"] if m["author"] != "system"]
    assert [m["id"] for m in msgs] == [first, second]
    assert msgs[0]["text"] == "<b>raw</b> text" and not any(h in msgs[0]["text"] for h in ("Iris group chat", "hnl-notify group chat", "[end "))
    assert msgs[0]["wakes"] == {"delivered": 1, "skipped": 0, "pending": 1}  # claimed counts as pending
    assert msgs[1]["author"] == "owner" and msgs[1]["wakes"]["pending"] == 3
    with srv.service.db.tx() as cx:
        cx.execute("UPDATE chat_wakes SET state='skipped', reason='member gone' WHERE message_id=?", (second,))
    after = b.get(f"/admin/api/chats/{gid}/messages?after={first}").json()
    assert [m["id"] for m in after["messages"]] == [second]
    assert after["messages"][0]["wakes"] == {"delivered": 0, "skipped": 3, "pending": 0}
    assert after["group"]["state"] == "open"
    assert b.get("/admin/api/chats/grp_nope/messages").status_code == 404


def test_bearer_admin_reads_without_text(ctx):
    srv, b, bearer = ctx
    a = _join(srv, "host-a", "/srv/one", "s1")
    srv.service.chat.post(a["member_handle"], "private words")
    r = httpx.get(f"{srv.url}/admin/api/chats/{a['group_id']}/messages", headers=bearer)
    assert r.status_code == 200
    m = [x for x in r.json()["messages"] if x["author"] != "system"][0]
    assert "text" not in m and m["author"] == "claude" and m["created_at"] and m["wakes"]
    g = httpx.get(f"{srv.url}/admin/api/chats", headers=bearer).json()["groups"][0]
    assert g["last_message"]["text_preview"] is None and g["last_message"]["author"] == "claude"
    assert "private words" not in r.text and "private words" not in httpx.get(f"{srv.url}/admin/api/chats",
                                                                               headers=bearer).text


def test_client_update_keeps_or_clears_chat_host(ctx):
    srv, b, _ = ctx
    cl = lambda: srv.app.state.config.clients["agent-a"].chat_host  # noqa: E731
    assert cl() == "host-a"
    b.put("/admin/api/clients/agent-a", json={"scopes": ["chat"], "description": "x"}, headers=H)
    assert cl() == "host-a"  # omitted = keep
    b.put("/admin/api/clients/agent-a", json={"scopes": ["chat"], "chat_host": "host-a"}, headers=H)
    assert cl() == "host-a"
    b.put("/admin/api/clients/agent-a", json={"scopes": ["chat"], "chat_host": ""}, headers=H)
    assert cl() is None  # empty = clear


def test_watching_pause_resume_need_session(ctx):
    srv, b, bearer = ctx
    j = _join(srv, "host-a", "/srv/one", "s1")
    base = f"/admin/api/chats/{j['group_id']}"
    for act in ("watching", "pause", "resume"):
        assert httpx.post(f"{srv.url}{base}/{act}", headers={**bearer, **H}).status_code == 403
        assert b.post(f"{base}/{act}").status_code == 403
    db = srv.service.db
    assert b.post(f"{base}/watching", headers=H).status_code == 204
    assert db.scalar("SELECT owner_watch_until FROM chat_groups") is not None
    assert b.post(f"{base}/pause", headers=H).status_code == 204
    assert db.scalar("SELECT paused FROM chat_groups") == 1
    assert b.post(f"{base}/resume", headers=H).status_code == 204
    assert db.scalar("SELECT paused FROM chat_groups") == 0
    assert b.post("/admin/api/chats/grp_nope/pause", headers=H).status_code == 404
    srv.service.chat.leave(j["member_handle"])
    for act in ("watching", "pause", "resume"):
        assert b.post(f"{base}/{act}", headers=H).status_code == 409


def test_admin_list_one_entry_per_folder(ctx):
    srv, b, bearer = ctx
    chat = srv.service.chat
    first = _join(srv, "host-a", "/srv/one", "s1")
    chat.post(first["member_handle"], "first session talk")
    chat.leave(first["member_handle"])
    second = _join(srv, "host-a", "/srv/one", "s2")
    chat.leave(second["member_handle"])  # opened and closed, nobody said anything
    third = _join(srv, "host-a", "/srv/one", "s3")
    chat.post(third["member_handle"], "current talk")
    other = _join(srv, "host-b", "/srv/one", "s4")  # same folder name on another host is another thread
    data = b.get("/admin/api/chats").json()
    assert len(data["groups"]) == 4  # the flat list still has every group
    assert len(data["folders"]) == 2
    mine = next(f for f in data["folders"] if f["host"] == "host-a")
    assert mine["id"] == third["group_id"] and mine["state"] == "open" and mine["group_count"] == 3
    assert mine["earlier_count"] == 2 and mine["unread"] == 1 and mine["last_message"]["author"] == "claude"
    assert mine["last_message"]["text_preview"] == "current talk"
    assert next(f for f in data["folders"] if f["host"] == "host-b")["id"] == other["group_id"]
    # newest activity first
    stamps = [f["activity_at"] for f in data["folders"]]
    assert stamps == sorted(stamps, reverse=True)
    info = b.get("/admin/api/chats/folder", params={"host": "host-a", "folder": "/srv/one"}).json()
    assert [g["id"] for g in info["groups"]] == [first["group_id"], second["group_id"], third["group_id"]]
    assert [g["message_count"] for g in info["groups"]] == [1, 0, 1]
    assert [g["state"] for g in info["groups"]] == ["closed", "closed", "open"]
    assert "text" not in json.dumps(info) and "talk" not in json.dumps(info)
    assert b.get("/admin/api/chats/folder", params={"host": "host-a", "folder": "/nope"}).status_code == 404
    # a folder whose newest group is closed is still one entry, shown closed
    chat.leave(third["member_handle"])
    mine = next(f for f in b.get("/admin/api/chats").json()["folders"] if f["host"] == "host-a")
    assert mine["state"] == "closed" and mine["id"] == third["group_id"]


def test_admin_folders_redacted_for_bearer(ctx):
    srv, b, bearer = ctx
    chat = srv.service.chat
    j = _join(srv, "host-a", "/srv/one", "s1")
    chat.post(j["member_handle"], "private words")
    chat.history(j["member_handle"], "private reason")
    r = httpx.get(f"{srv.url}/admin/api/chats", headers=bearer)
    assert r.status_code == 200
    assert "private" not in r.text
    assert r.json()["folders"][0]["last_message"]["text_preview"] is None
    assert r.json()["folders"][0]["pending_history_requests"] == 1
    m = httpx.get(f"{srv.url}/admin/api/chats/{j['group_id']}/messages", headers=bearer)
    assert "private" not in m.text and all("text" not in x for x in m.json()["messages"])
    assert all("reason" not in x.get("history_request", {}) for x in m.json()["messages"])
    assert "private" not in httpx.get(f"{srv.url}/admin/api/chats/folder",
                                      params={"host": "host-a", "folder": "/srv/one"}, headers=bearer).text
    assert "private reason" in json.dumps(b.get(f"/admin/api/chats/{j['group_id']}/messages").json())
