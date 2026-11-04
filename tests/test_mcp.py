from __future__ import annotations

import json

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from tests.conftest import TOKENS, wait_for


def run_session(live, who, fn):
    async def main():
        async with streamablehttp_client(f"{live.url}/mcp",
                                         headers={"Authorization": f"Bearer {TOKENS[who]}"}) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return await fn(session)
    return anyio.run(main)


def payload(result):
    assert not result.isError, result.content
    return json.loads(result.content[0].text)


def test_mcp_requires_token(live):
    r = httpx.post(f"{live.url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_mcp_tools_end_to_end(live, fake):
    async def flow(s: ClientSession):
        tools = {t.name for t in (await s.list_tools()).tools}
        targets = payload(await s.call_tool("list_targets", {}))
        preview = payload(await s.call_tool("preview_routing", {"targets": ["user:alex"], "severity": "critical"}))
        sent = payload(await s.call_tool("send_notification", {
            "targets": ["user:alex"], "title": "Agent", "message": "Build finished", "tags": ["ci"]}))
        return tools, targets, preview, sent

    tools, targets, preview, sent = run_session(live, "agent", flow)
    assert {"send_notification", "preview_routing", "get_notification", "list_notifications", "list_attention",
            "list_targets", "retry_notification", "cancel_notification"} <= tools
    assert targets["users"][0]["target"] == "user:alex"
    assert "acknowledged" in preview["deliveries"][0]["observable_states"]
    assert sent["created"] and sent["source"] == "mcp:agent"
    nid = sent["id"]
    wait_for(lambda: fake.messages)

    async def inspect(s: ClientSession):
        got = payload(await s.call_tool("get_notification", {"notification_id": nid, "include_attempts": True}))
        listed = await s.call_tool("list_notifications", {"tag": "ci"})
        attention = payload(await s.call_tool("list_attention", {}))
        cancel = await s.call_tool("cancel_notification", {"notification_id": nid})
        res = await s.read_resource(f"notify://notifications/{nid}")
        return got, listed, attention, cancel, res

    got, listed, attention, cancel, res = run_session(live, "agent", inspect)
    assert got["status"] == "dispatched" and got["deliveries"][0]["attempt_log"][0]["outcome"] == "success"
    assert json.loads(listed.content[0].text)["id"] == nid
    assert json.loads(listed.content[0].text)["sent_at"] == got["created_at"]
    assert attention["failed"] == []
    assert cancel.isError and "cannot recall" in cancel.content[0].text
    assert json.loads(res.contents[0].text)["id"] == nid


def test_mcp_scopes_enforced(live):
    async def flow(s: ClientSession):
        return await s.call_tool("send_notification", {"targets": ["user:alex"], "message": "x"})
    res = run_session(live, "reader", flow)
    assert res.isError and "lacks scope 'send'" in res.content[0].text


def test_mcp_advertises_email_and_attachments(live):
    """Clients that defer tool schemas only see the server instructions and tool descriptions up front."""
    async def flow(s: ClientSession):
        init = await s.initialize()
        tools = {t.name: t.description for t in (await s.list_tools()).tools}
        return init.instructions, tools

    instructions, tools = run_session(live, "agent", flow)
    for phrase in ("email", "kinds", "attachments", "image", "browser", "direct-only"):
        assert phrase in instructions
    for phrase in ("kind", "opt_in", "direct_only", "attachment"):
        assert phrase in tools["list_targets"]


def test_mcp_instructions_name_zone(monkeypatch, server_factory):
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    live = server_factory()

    async def flow(s: ClientSession):
        return (await s.initialize()).instructions

    text = run_session(live, "agent", flow)
    assert "without offset = Europe/Amsterdam" in text
    assert "update_scheduled_notification" in text


def test_mcp_schedule_update_cancel(live):
    async def schedule(s: ClientSession):
        return payload(await s.call_tool("send_notification", {
            "targets": ["user:alex"], "title": "Later", "message": "hello", "url": "https://example.org/x",
            "delay": "2h"}))

    sent = run_session(live, "sender", schedule)
    nid = sent["id"]
    assert sent["status"] == "scheduled"
    # the agent sees when it goes out, its lateness limit and who it would reach now
    assert sent["send_at"] and sent["deliver_by"] and sent["max_late_seconds"] == 3600
    assert sent["routing_preview"]["devices"][0]["device"] == "alex-phone"

    async def edit(s: ClientSession):
        upd = payload(await s.call_tool("update_scheduled_notification", {
            "notification_id": nid, "title": "Changed", "clear": ["url"]}))
        cancelled = payload(await s.call_tool("cancel_notification", {"notification_id": nid}))
        return upd, cancelled

    upd, cancelled = run_session(live, "sender", edit)
    assert upd["title"] == "Changed" and upd["status"] == "scheduled"
    assert cancelled["notification"]["status"] == "cancelled"
    assert live.service.db.scalar("SELECT url FROM notifications WHERE id=?", (nid,)) is None


def test_mcp_clear_rejects_required(live):
    async def flow(s: ClientSession):
        sent = payload(await s.call_tool("send_notification", {
            "targets": ["user:alex"], "message": "hello", "delay": "2h"}))
        return await s.call_tool("update_scheduled_notification", {
            "notification_id": sent["id"], "clear": ["message"]})

    res = run_session(live, "sender", flow)
    assert res.isError and "message" in res.content[0].text


def test_mcp_cancel_scope_send_or_manage(live, fake):
    from tests.conftest import wait_for
    with live.client("agent") as c:
        nid = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "now"}).json()["id"]
    wait_for(lambda: fake.messages)
    with live.service.db.tx() as c:
        c.execute("UPDATE notifications SET client_id='manager' WHERE id=?", (nid,))  # visibility is per owner
        c.execute("UPDATE deliveries SET state='queued', dispatched_at=NULL, next_attempt_at='2999-01-01T00:00:00Z'"
                  " WHERE notification_id=?", (nid,))

    async def cancel(s: ClientSession):
        return await s.call_tool("cancel_notification", {"notification_id": nid})

    assert run_session(live, "reader", cancel).isError  # read-only: refused
    res = run_session(live, "manager", cancel)
    assert not res.isError, res.content
