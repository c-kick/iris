"""Agent group chats: REST endpoints and MCP tools."""

from __future__ import annotations

import httpx
import pytest

from notify.app import create_app
from notify.auth import hash_token
from notify.chat import MEMBER_GONE
from notify.config import NotifyConfig
from tests.conftest import TOKENS, FakePushover, LiveServer, fake_resolve, make_config
from tests.test_mcp import payload, run_session

CHAT_TOKENS = {"hosta": "tok-hosta", "bot": "tok-bot", "hostb": "tok-hostb"}
FOLDER = "/srv/app"


@pytest.fixture
def chatlive(tmp_path, fake):
    cfg = make_config().model_dump(mode="json")
    for name, host in (("hosta", "host-a"), ("bot", None), ("hostb", "host-b")):
        c = {"token_sha256": hash_token(CHAT_TOKENS[name]), "scopes": ["chat"]}
        if host:
            c["chat_host"] = host
        cfg["clients"][name] = c
    app = create_app(NotifyConfig.model_validate(cfg), db_path=str(tmp_path / "n.sqlite3"),
                     key_path=str(tmp_path / "master.key"), transport=httpx.MockTransport(fake.handler))
    srv = LiveServer(app, fake)
    srv.start()
    srv.service.resolve = fake_resolve
    TOKENS.update(CHAT_TOKENS)
    yield srv
    for k in CHAT_TOKENS:
        TOKENS.pop(k, None)
    srv.stop()


def join(srv, who="hosta", sid="s1", folder=FOLDER):
    r = srv.client(who).post("/v1/chat/join", json={"kind": "claude", "session_id": sid, "folder": folder,
                                                    "pid": 5, "pid_start": "x"})
    assert r.status_code == 201, r.text
    return r.json()


def test_join_leave_roundtrip_rest(chatlive):
    j = join(chatlive)
    assert j["member_handle"] and [m["member_id"] for m in j["members"]] == [j["member_id"]]
    c = chatlive.client("hosta")
    r = c.post("/v1/chat/leave", headers={"X-Member-Handle": j["member_handle"]}, json={"reason": "ended"})
    assert r.status_code == 204
    again = c.post("/v1/chat/leave", headers={"X-Member-Handle": j["member_handle"]})
    assert again.status_code == 404 and again.json()["detail"] == MEMBER_GONE
    assert c.post("/v1/chat/heartbeat", json={"alive": []}).json() == {"members": []}


def test_handle_not_in_url(chatlive):
    j = join(chatlive)
    r = chatlive.client("hosta").post(f"/v1/chat/leave?member_handle={j['member_handle']}")
    assert r.status_code == 422
    assert j["member_handle"] not in r.text
    assert chatlive.service.db.scalar("SELECT COUNT(*) FROM chat_members WHERE left_at IS NULL") == 1


def test_claim_204_then_200(chatlive):
    a, b = join(chatlive, sid="s1"), join(chatlive, sid="s2")
    c = chatlive.client("hosta")
    assert c.post("/v1/chat/wakes/claim", params={"waiter": "w1", "wait": 0}).status_code == 204
    chatlive.service.chat.post(a["member_handle"], "hello")
    r = c.post("/v1/chat/wakes/claim", params={"waiter": "w1", "wait": 0})
    assert r.status_code == 200 and r.json()["member_id"] == b["member_id"]
    wid = r.json()["wake_id"]
    assert c.post(f"/v1/chat/wakes/{wid}/confirm", params={"waiter": "other"}).status_code == 404
    assert c.post(f"/v1/chat/wakes/{wid}/confirm", params={"waiter": "w1"}).status_code == 204
    # another host's token sees nothing; a client without a chat host is refused
    assert chatlive.client("hostb").post("/v1/chat/wakes/claim", params={"waiter": "w", "wait": 0}).status_code == 204
    assert chatlive.client("bot").post("/v1/chat/wakes/claim", params={"waiter": "w", "wait": 0}).status_code == 403


def test_scope_required(chatlive):
    for who in ("agent", "reader", "sender"):
        c = chatlive.client(who)
        assert c.post("/v1/chat/join", json={"kind": "claude", "session_id": "s", "folder": FOLDER}).status_code == 403
        assert c.post("/v1/chat/leave", headers={"X-Member-Handle": "x"}).status_code == 403
        assert c.post("/v1/chat/heartbeat", json={"alive": []}).status_code == 403
        assert c.post("/v1/chat/wakes/claim", params={"waiter": "w", "wait": 0}).status_code == 403
        assert c.post("/v1/chat/wakes/x/confirm", params={"waiter": "w"}).status_code == 403
    assert httpx.post(f"{chatlive.url}/v1/chat/heartbeat", json={"alive": []}).status_code == 401


def test_mcp_post_from_other_token(chatlive):
    j = join(chatlive, sid="s1")
    j2 = join(chatlive, sid="s2")

    async def flow(s):
        posted = payload(await s.call_tool("group_post", {"member_handle": j["member_handle"], "text": "hi"}))
        read = payload(await s.call_tool("group_read", {"member_handle": j2["member_handle"]}))
        tools = {t.name for t in (await s.list_tools()).tools}
        return posted, read, tools

    posted, read, tools = run_session(chatlive, "bot", flow)
    assert {"group_post", "group_read", "group_leave"} <= tools
    assert posted["message_id"] and [m["id"] for m in read["messages"] if m["author"] != "system"] == [posted["message_id"]]

    async def leave(s):
        return payload(await s.call_tool("group_leave", {"member_handle": j["member_handle"]}))

    assert run_session(chatlive, "bot", leave) == {"left": True}


def test_mcp_post_wrong_handle(chatlive):
    j = join(chatlive)
    tampered = j["member_handle"][:-3] + ("aaa" if not j["member_handle"].endswith("aaa") else "bbb")

    async def flow(s):
        return [await s.call_tool("group_post", {"member_handle": h, "text": "x"})
                for h in (tampered, "garbage", "x" * 500)]

    for r in run_session(chatlive, "bot", flow):
        assert r.isError and r.content[0].text.endswith(": " + MEMBER_GONE)
    assert chatlive.service.db.scalar("SELECT COUNT(*) FROM chat_messages WHERE author<>'system'") == 0

    chatlive.client("hosta").post("/v1/chat/leave", headers={"X-Member-Handle": j["member_handle"]})

    async def dead(s):
        return await s.call_tool("group_read", {"member_handle": j["member_handle"]})

    r = run_session(chatlive, "bot", dead)
    assert r.isError and r.content[0].text.endswith(": " + MEMBER_GONE)


def test_mcp_needs_chat_scope(chatlive):
    j = join(chatlive)

    async def flow(s):
        return await s.call_tool("group_post", {"member_handle": j["member_handle"], "text": "x"})

    r = run_session(chatlive, "agent", flow)
    assert r.isError and "lacks scope 'chat'" in r.content[0].text


def test_events_visibility_unchanged(chatlive):
    j = join(chatlive, sid="s1")
    join(chatlive, sid="s2")
    chatlive.service.chat.post(j["member_handle"], "secret")
    assert chatlive.service.db.scalar("SELECT COUNT(*) FROM events WHERE type='group.message'") == 1
    r = chatlive.client("agent").get("/v1/events", params={"after": 0})
    assert r.status_code == 200
    assert not [e for e in r.json()["items"] if e["type"].startswith("group.")]
