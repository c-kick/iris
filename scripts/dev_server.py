"""Run Iris locally against a fake Pushover (nothing leaves the host).

    .venv/bin/python scripts/dev_server.py --dir /tmp/notify-dev --port 18931 [--demo] [--password PW]

--demo seeds a Pushover provider, an iPhone, a household group, two channels,
a few API clients and some delivered/failed history so every admin screen has
content. --password pre-creates the admin password (otherwise the setup code
is printed). For UI iteration only; never point it at the live data directory.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notify.app import create_app  # noqa: E402
from notify.auth import hash_token  # noqa: E402
from notify.config import NotifyConfig  # noqa: E402
from notify.core import Client  # noqa: E402
from notify.db import now_iso  # noqa: E402
from notify.models import Scope  # noqa: E402

FAKE_TOKEN = "a" * 30
FAKE_USER = "u" * 30


def fake_pushover(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    form = dict(parse_qsl(request.content.decode())) if request.content else {}
    if path.endswith("/users/validate.json"):
        ok = form.get("user") == FAKE_USER
        return httpx.Response(200 if ok else 400, json={"status": 1, "devices": ["iphone", "ipad"]} if ok
                              else {"status": 0, "errors": ["user key is invalid"]})
    if path.endswith("/messages.json"):
        if "fail" in form.get("message", ""):
            return httpx.Response(400, json={"status": 0, "errors": ["device name is not valid for user"]})
        body = {"status": 1, "request": "dev"}
        if form.get("priority") == "2":
            body["receipt"] = "r" * 30
        return httpx.Response(200, json=body, headers={"X-Limit-App-Limit": "10000", "X-Limit-App-Remaining": "9876",
                                                       "X-Limit-App-Reset": "1790000000"})
    if "/receipts/" in path:
        return httpx.Response(200, json={"status": 1, "acknowledged": 1, "acknowledged_at": 1790000000,
                                         "acknowledged_by_device": "iphone", "last_delivered_at": 1790000000,
                                         "expired": 0, "expires_at": 1790003600})
    return httpx.Response(404, json={"status": 0, "errors": ["unknown"]})


def demo_config() -> NotifyConfig:
    return NotifyConfig.model_validate({
        "providers": {"pushover": {"type": "pushover", "app_token": FAKE_TOKEN}},
        "devices": {"alex-phone": {"provider": "pushover", "owner": "alex", "description": "Alex's phone",
                                     "address": {"user_key": FAKE_USER, "device": "iphone"}}},
        "users": {"alex": {"display_name": "Alex", "devices": ["alex-phone"]}},
        "groups": {"household": {"description": "Everyone at home", "members": ["user:alex"]}},
        "channels": {"security": {"description": "Doors, alarms, leaks", "targets": ["group:household"]},
                     "infra": {"description": "Server and network alerts", "targets": ["user:alex"],
                               "min_severity": "high"}},
        "clients": {n: {"token_sha256": hash_token(f"dev-{n}"), "scopes": s, "description": d} for n, s, d in [
            ("home-assistant", ["send", "read"], "Automations and rest_command"),
            ("node-red", ["send"], None),
            ("agents", ["send", "read", "manage", "chat"], "Claude Code and Codex via MCP")]}
        | {"agent-a": {"token_sha256": hash_token("dev-agent-a"), "scopes": ["chat"], "chat_host": "host-a",
                       "description": "Agent sessions on host-a"},
           "agent-b": {"token_sha256": hash_token("dev-agent-b"), "scopes": ["chat"], "chat_host": "host-b"}},
    })


async def seed_history(url: str) -> None:
    async with httpx.AsyncClient(base_url=url, headers={"Authorization": "Bearer dev-home-assistant"}) as c:
        for body in [
            {"targets": ["user:alex"], "title": "Washing machine", "message": "The laundry is done", "tags": ["laundry"]},
            {"targets": ["channel:security"], "title": "Front door", "message": "Open for 10 minutes",
             "severity": "high"},
            {"targets": ["channel:security"], "title": "Water leak in the attic", "message": "Sensor reports water",
             "severity": "critical"},
            {"targets": ["user:alex"], "title": "Backup", "message": "fail: NAS backup", "severity": "low"},
            {"targets": ["channel:infra"], "title": "Disk 71%", "message": "Below the channel floor"},
            {"targets": ["user:alex"], "title": "Weekly report", "severity": "low",
             "message": "\n".join(f"Line {i} of the weekly report, which is long enough to need Show more."
                                  for i in range(1, 13))},
            *({"targets": ["user:alex"], "title": f"Sensor check {i}", "message": f"Reading {i} is fine",
               "severity": "low", "source": "sensors"} for i in range(1, 31)),
        ]:
            await c.post("/v1/notifications", json=body)


def seed_inbound(app) -> None:
    """Replies and rejected mail, so the Activity list has received rows; a hostile body shows they stay text."""
    svc = app.state.service
    n = svc.db.one("SELECT id FROM notifications WHERE title='Washing machine'")
    d = svc.db.one("SELECT id FROM deliveries WHERE notification_id=?", (n["id"],))
    now = now_iso()
    with svc.db.tx() as c:
        for i, text in enumerate(["Thanks, I'll get it.", "<img src=x onerror=\"document.title='hacked'\"><b>bold?</b>"]):
            c.execute("INSERT INTO replies(id, conversation_id, notification_id, delivery_id, message_id, from_address,"
                      " subject, received_at, text, stripped, truncated, dropped_attachments_json, state)"
                      " VALUES (?,?,?,?,?,?,?,?,?,1,0,'[]','claimed')",
                      (f"rpl_dev{i}", n["id"], n["id"], d["id"], f"<dev{i}@example.org>", "alex@example.org",
                       "Re: Washing machine", now, text))
        c.execute("INSERT INTO reply_quarantine(received_at, from_address, subject, message_id, reason)"
                  " VALUES (?,?,?,?,?)", (now, "mallory@example.org", "Re: Washing machine", "<evil@example.org>",
                                          "sender_mismatch"))


def seed_chats(app) -> None:
    """Three group chats: an open one with Claude and Codex (incl. a notice line), a paused open one, a closed one."""
    chat = app.state.service.chat
    a, b = Client("agent-a", frozenset({Scope.chat})), Client("agent-b", frozenset({Scope.chat}))

    def join(client, kind, sid, folder):
        return chat.join(client, kind=kind, session_id=sid, folder=folder, pid=None, pid_start=None)

    # two earlier sessions of /srv/app, both closed: one with messages, one opened and closed without a word
    old1 = join(a, "claude", "demo-old-1", "/srv/app")
    chat.post(old1["member_handle"], "Plan: keep `reply_handle` until the clients moved.")
    chat.post_owner(old1["group_id"], "Fine, no rename yet.")
    chat.leave(old1["member_handle"], "ended")
    old2 = join(a, "claude", "demo-old-2", "/srv/app")
    chat.leave(old2["member_handle"], "ended")
    cl = join(a, "claude", "demo-claude", "/srv/app")
    cx = join(a, "codex", "demo-codex", "/srv/app")
    # which model each runs, as the status hook would report it (before they post: messages keep it)
    chat.set_status(a, kind="claude", session_id="demo-claude", status="free", model="claude-fable-5-2")
    chat.set_status(a, kind="codex", session_id="demo-codex", status="free", model="gpt-6.1-sol")
    chat.post(cx["member_handle"], "Heads-up: the API renames `reply_handle` to `thread_key`. Your client reads the old name.")
    chat.post(cl["member_handle"], "I have the client open, I'll take it.\nCodex, are you fine with that?")
    yes = chat.post(cx["member_handle"], "Yes, go ahead. <b>Not bold</b>: this stays text.")
    chat.read(cl["member_handle"])  # both active since: the messages so far show two ticks
    chat.read(cx["member_handle"])
    with chat.db.tx() as c:
        chat._add_message(c, cl["group_id"], "system", None, "Codex reconnected")
    chat.post(cl["member_handle"], "Done: the client reads both names, tests pass.")
    # the notice of a notification an agent sent, and the bell on the folder until the owner opens it
    chat.note_notification(a, cl["member_handle"], kinds=None, title="Client reads both names", deliveries=1)
    chat.post(cl["member_handle"], "Which tests did you run?", in_reply_to=yes["message_id"])
    chat.post(cx["member_handle"], f"@{cl['name']} can you also update the docs page?")
    chat.post(cx["member_handle"], "I'm rebasing main in a minute; expect a force-push on my branch only.", fyi=True)
    c3 = join(a, "claude", "demo-claude-b", "/srv/app")  # more members: each gets its own colour
    c4 = join(a, "codex", "demo-codex-b", "/srv/app")
    chat.post(c3["member_handle"], "I can take the migration script if nobody has it.")
    chat.post(c4["member_handle"], "Docs are mine; I'll update the API page after the rename lands.", fyi=True)
    chat.set_status(a, kind="claude", session_id="demo-claude", status="busy")
    chat.set_status(a, kind="codex", session_id="demo-codex", status="waiting")
    thanks = chat.post_owner(cl["group_id"], "Thanks. Commit it, don't deploy.")["message_id"]
    with chat.db.tx() as c:  # in both sessions; Claude claims it: one green tick
        c.execute("UPDATE chat_wakes SET state='delivered' WHERE message_id=?", (thanks,))
    chat.claim(cl["member_handle"], thanks)
    chat.history(cl["member_handle"], "Why was the rename postponed? I need the earlier decision.")  # a pending request
    # a paused group: the agents hit the cap, the last message went to nobody
    p1 = join(b, "claude", "demo-claude-2", "/srv/tools")
    p2 = join(b, "codex", "demo-codex-2", "/srv/tools")
    chat.post(p1["member_handle"], "Should we split the importer?")
    with chat.db.tx() as c:
        c.execute("UPDATE chat_groups SET paused=1 WHERE id=?", (p1["group_id"],))
        chat._add_message(c, p1["group_id"], "system", None, "paused after 10 agent messages; waiting for the owner")
    chat.post(p2["member_handle"], "Yes, but not before the release.")
    # 0.14.0: /srv/app has chats on; a session present in /srv/docs (chats off there); a second session in /srv/app
    chat.set_folder("host-a", "/srv/app", True)
    chat.set_folder("host-b", "/srv/planned", True)  # switched on before any agent started there: editable
    chat.presence(a, kind="codex", session_id="demo-present", folder="/srv/docs", pid=None, pid_start=None)
    # a closed group
    c1 = join(a, "claude", "demo-claude-3", "/srv/old-project")
    chat.post(c1["member_handle"], "Nothing left to do here.")
    chat.leave(c1["member_handle"], "ended")


async def keep_demo_chats_alive(app) -> None:
    """The worker marks members without a heartbeat as stale after 3 minutes; the demo has no relay."""
    while True:
        await asyncio.sleep(30)
        with app.state.service.db.tx() as c:
            c.execute("UPDATE chat_members SET last_seen_at=? WHERE left_at IS NULL", (now_iso(),))
            c.execute("UPDATE chat_presence SET seen_at=?", (now_iso(),))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--port", type=int, default=18931)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--password")
    args = ap.parse_args()
    d = Path(args.dir)
    d.mkdir(parents=True, exist_ok=True)
    os.environ["NOTIFY_CONFIG"] = str(d / "none.yaml")
    app = create_app(demo_config() if args.demo else None, db_path=str(d / "notify.sqlite3"),
                     key_path=str(d / "master.key"), transport=httpx.MockTransport(fake_pushover))
    if args.password and not app.state.store.admin_configured():
        app.state.store.set_password(args.password)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))

    async def run() -> None:
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        if args.demo and not app.state.service.list(Client("dev", frozenset({Scope.admin})), limit=1):
            await seed_history(f"http://127.0.0.1:{args.port}")
            seed_inbound(app)
            seed_chats(app)
        if args.demo:
            asyncio.create_task(keep_demo_chats_alive(app))
        print(f"Iris dev server on http://127.0.0.1:{args.port}/admin", flush=True)
        await task

    asyncio.run(run())


if __name__ == "__main__":
    main()
