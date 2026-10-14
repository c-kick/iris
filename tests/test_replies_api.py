from __future__ import annotations

import threading
import time

import notify.providers.email_inbound as inbound
from tests.conftest import PNG, wait_for
from tests.mailfactory import reply_raw
from tests.test_mcp import payload, run_session


def send(srv) -> tuple[str, str]:
    with srv.client() as c:
        out = c.post("/v1/notifications", json={"targets": ["device:alex-email"], "message": "Pick", "title": "Pick",
                                                 "kinds": ["email"], "reply": True}).json()
        wait_for(lambda: c.get(f"/v1/notifications/{out['id']}").json()["deliveries"][0]["state"] == "dispatched")
    return out["id"], out["deliveries"][0]["id"]


def arrive(srv, raw: bytes) -> None:
    srv.mailbox.add(raw)
    srv.call(lambda: inbound.poll_once(srv.service, srv.service.providers["mail"], srv.mailbox))


def test_claim_long_poll_and_download(email_live):
    nid, did = send(email_live)
    out: dict = {}

    def wait() -> None:
        with email_live.client() as c:
            out["r"] = c.post(f"/v1/notifications/{nid}/replies/claim", params={"waiter": "abcdefgh12", "wait": 20},
                              timeout=30)
    t = threading.Thread(target=wait)
    t.start()
    time.sleep(0.5)
    arrive(email_live, reply_raw(did, attachment=PNG))
    t.join(15)
    r = out["r"]
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "Go with B." and body["notification_title"] == "Pick"
    with email_live.client() as c:
        again = c.post(f"/v1/notifications/{nid}/replies/claim", params={"waiter": "abcdefgh12", "wait": 0})
        assert again.json()["id"] == body["id"]
        assert c.post(f"/v1/notifications/{nid}/replies/claim", params={"waiter": "zzzzzzzz12", "wait": 0}).status_code == 204
        a = c.get(body["attachments"][0]["url"])
        assert a.status_code == 200 and a.content == PNG and a.headers["content-type"] == "image/png"
        assert "attachment; filename*=UTF-8''shot.png" == a.headers["content-disposition"]
        assert c.get(f"/v1/notifications/{nid}/replies").json()["items"][0]["state"] == "claimed"
    with email_live.client("other") as o:
        assert o.get(body["attachments"][0]["url"]).status_code == 404
        assert o.post(f"/v1/notifications/{nid}/replies/claim", params={"waiter": "abcdefgh12", "wait": 0}).status_code == 404


def test_claim_validation_and_scope(email_live):
    nid, _ = send(email_live)
    with email_live.client() as c:
        assert c.post(f"/v1/notifications/{nid}/replies/claim", params={"waiter": "bad id!", "wait": 0}).status_code == 422
        assert c.post(f"/v1/notifications/{nid}/replies/claim", params={"waiter": "abcdefgh12", "wait": 99}).status_code == 422
    with email_live.client("sender") as s:
        assert s.post(f"/v1/notifications/{nid}/replies/claim", params={"waiter": "abcdefgh12"}).status_code == 403


def test_mcp_reply_send_and_get_replies(email_live):
    async def flow(s):
        sent = payload(await s.call_tool("send_notification", {
            "targets": ["device:alex-email"], "message": "Pick", "kinds": ["email"], "reply": True}))
        return sent
    sent = run_session(email_live, "agent", flow)
    assert sent["reply"] is True and sent["conversation_id"] == sent["id"]
    did = sent["deliveries"][0]["id"]
    with email_live.client() as c:
        wait_for(lambda: c.get(f"/v1/notifications/{sent['id']}").json()["deliveries"][0]["state"] == "dispatched")
    arrive(email_live, reply_raw(did))

    async def look(s):
        tools = {t.name for t in (await s.list_tools()).tools}
        return tools, payload(await s.call_tool("get_replies", {"notification_id": sent["id"]}))
    tools, replies = run_session(email_live, "agent", look)
    assert "get_replies" in tools and replies["items"][0]["state"] == "waiting"
    assert replies["items"][0]["text"] is None
    with email_live.client() as c:
        assert c.post(f"/v1/notifications/{sent['id']}/replies/claim",
                      params={"waiter": "abcdefgh12", "wait": 0}).status_code == 200
    _, replies = run_session(email_live, "agent", look)
    assert replies["items"][0]["text"] == "Go with B."


def test_attachment_download_hardening(email_live):
    nid, did = send(email_live)
    arrive(email_live, reply_raw(did, attachment=PNG))
    with email_live.client() as c:
        body = c.post(f"/v1/notifications/{nid}/replies/claim", params={"waiter": "abcdefgh12", "wait": 0}).json()
        url = body["attachments"][0]["url"]
        for stored in ("text/ж", "text/html", "TEXT/HTML", "image/svg+xml", "a/" + "b" * 200):
            with email_live.service.db.tx() as t:
                t.execute("UPDATE reply_attachments SET content_type=? WHERE reply_id=?", (stored, body["id"]))
            r = c.get(url)
            assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream", stored
        name = 'a"b\r\nX: y ж.png'
        with email_live.service.db.tx() as t:
            t.execute("UPDATE reply_attachments SET filename=? WHERE reply_id=?", (name, body["id"]))
        cd = c.get(url).headers["content-disposition"]
        assert cd == "attachment; filename*=UTF-8''a%22b%0D%0AX%3A%20y%20%D0%B6.png"
