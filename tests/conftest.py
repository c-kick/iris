"""Test harness: real uvicorn server in a thread, fake Pushover API via httpx.MockTransport."""

from __future__ import annotations

import asyncio
import email
import email.policy
import inspect
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qsl

import httpx
import pytest
import uvicorn

from notify.app import create_app
from notify.auth import hash_token
from notify.config import NotifyConfig

APP_TOKEN = "a" * 30
USER_KEY = "u" * 30
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 56
TOKENS = {"sender": "tok-sender", "reader": "tok-reader", "agent": "tok-agent", "admin": "tok-admin",
          "other": "tok-other"}


@dataclass
class FakePushover:
    """Minimal stand-in for api.pushover.net with scriptable failures."""
    messages: list[dict[str, str]] = field(default_factory=list)
    responses: list[tuple[int, dict[str, Any]]] = field(default_factory=list)  # queued overrides for messages.json
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    webhooks: list[httpx.Request] = field(default_factory=list)
    webhook_status: int = 200
    file_requests: list[str] = field(default_factory=list)  # paths fetched from files.test
    lock: threading.Lock = field(default_factory=threading.Lock)

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            if request.url.host == "hooks.test":
                self.webhooks.append(request)
                return httpx.Response(self.webhook_status)
            path = request.url.path
            if request.url.host == "files.test":
                self.file_requests.append(path)
                if path == "/missing":
                    return httpx.Response(404, content=b"not here")
                if path == "/big":
                    return httpx.Response(200, content=b"x" * (10 * 1024 * 1024 + 1),
                                          headers={"content-type": "application/octet-stream"})
                return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
            if path.endswith("/users/validate.json"):
                form = dict(parse_qsl(request.content.decode()))
                if form.get("user") == USER_KEY and form.get("token") == APP_TOKEN:
                    return httpx.Response(200, json={"status": 1, "group": 0, "devices": ["iphone", "ipad"]})
                return httpx.Response(400, json={"status": 0, "errors": ["user key is invalid"]})
            if path.endswith("/messages.json"):
                form = dict(parse_qsl(request.content.decode()))
                self.messages.append(form)
                if self.responses:
                    status, body = self.responses.pop(0)
                    return httpx.Response(status, json=body)
                body: dict[str, Any] = {"status": 1, "request": f"req-{len(self.messages)}"}
                if form.get("priority") == "2":
                    receipt = f"r{len(self.messages):029d}"
                    body["receipt"] = receipt
                    self.receipts[receipt] = {"status": 1, "acknowledged": 0, "acknowledged_at": 0,
                                              "last_delivered_at": 0, "expired": 0, "expires_at": 0}
                return httpx.Response(200, json=body, headers={"X-Limit-App-Limit": "10000",
                                                               "X-Limit-App-Remaining": str(10000 - len(self.messages)),
                                                               "X-Limit-App-Reset": "1790000000"})
            if "/receipts/" in path and path.endswith("/cancel.json"):
                receipt = path.split("/receipts/")[1].split("/")[0]
                self.cancelled.append(receipt)
                return httpx.Response(200, json={"status": 1})
            if "/receipts/" in path:
                receipt = path.split("/receipts/")[1].removesuffix(".json")
                if receipt not in self.receipts:
                    return httpx.Response(404, json={"status": 0, "errors": ["receipt not found"]})
                return httpx.Response(200, json=self.receipts[receipt])
            return httpx.Response(404, json={"status": 0, "errors": ["unknown endpoint"]})


async def fake_resolve(host: str) -> list[str]:
    return ["192.168.1.10"]


def make_config(**server: Any) -> NotifyConfig:
    return NotifyConfig.model_validate({
        "server": {"retry_base_seconds": 5, **server},
        "providers": {"pushover": {"type": "pushover", "app_token": APP_TOKEN, "api_base": "https://pushover.test/1"}},
        "devices": {
            "alex-phone": {"provider": "pushover", "owner": "alex", "description": "iPhone",
                             "address": {"user_key": USER_KEY, "device": "iphone"}},
            "spare": {"provider": "pushover", "enabled": False, "address": {"user_key": USER_KEY}},
        },
        "users": {"alex": {"display_name": "Alex", "devices": ["alex-phone"]}},
        "groups": {"household": {"members": ["user:alex", "device:alex-phone"]}},
        "channels": {"infra": {"targets": ["group:household"], "min_severity": "high"}},
        "clients": {
            "sender": {"token_sha256": hash_token(TOKENS["sender"]), "scopes": ["send"]},
            "reader": {"token_sha256": hash_token(TOKENS["reader"]), "scopes": ["read"]},
            "agent": {"token_sha256": hash_token(TOKENS["agent"]), "scopes": ["send", "read", "manage"]},
            "other": {"token_sha256": hash_token(TOKENS["other"]), "scopes": ["send", "read", "manage"]},
            "admin": {"token_sha256": hash_token(TOKENS["admin"]), "scopes": ["admin"]},
        },
    })


class LiveServer:
    def __init__(self, app: Any, fake: FakePushover):
        self.app = app
        self.fake = fake
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_config=None,
                                                    access_log=False, lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        self.app.state.workers.receipt_pause = 0.05

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=20)

    @property
    def service(self) -> Any:
        return self.app.state.service

    def client(self, who: str = "agent") -> httpx.Client:
        return httpx.Client(base_url=self.url, headers={"Authorization": f"Bearer {TOKENS[who]}"}, timeout=10)

    def call(self, fn: Callable[[], Any], timeout: float = 70.0) -> Any:
        """Run fn (sync, or returning an awaitable) on the server's event loop, as the app itself would."""
        async def run() -> Any:
            res = fn()
            return await res if inspect.isawaitable(res) else res
        return asyncio.run_coroutine_threadsafe(run(), self.app.state.loop).result(timeout)

    def fast_forward(self) -> None:
        """Make every waiting retry / receipt poll due now."""
        with self.service.db.tx() as c:
            c.execute("UPDATE deliveries SET next_attempt_at='2000-01-01T00:00:00.000000Z' WHERE state='queued'")
            c.execute("UPDATE deliveries SET next_poll_at='2000-01-01T00:00:00.000000Z' WHERE next_poll_at IS NOT NULL")
            c.execute("UPDATE webhook_deliveries SET next_attempt_at='2000-01-01T00:00:00.000000Z' WHERE state='pending'")


def wait_for(fn: Callable[[], Any], timeout: float = 10.0, interval: float = 0.05) -> Any:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


@pytest.fixture
def fake() -> FakePushover:
    return FakePushover()


@pytest.fixture
def server_factory(tmp_path, fake):
    servers: list[LiveServer] = []

    def make(**server_cfg: Any) -> LiveServer:
        app = create_app(make_config(**server_cfg), db_path=str(tmp_path / f"notify{len(servers)}.sqlite3"),
                         key_path=str(tmp_path / "master.key"), transport=httpx.MockTransport(fake.handler))
        srv = LiveServer(app, fake)
        srv.start()
        srv.service.resolve = fake_resolve  # files.test does not resolve
        servers.append(srv)
        return srv

    yield make
    for s in servers:
        s.stop()


@pytest.fixture
def live(server_factory) -> LiveServer:
    return server_factory()


MAIL = {"type": "email", "host": "mail.test", "port": 25, "security": "none", "from_address": "hnl@example.org"}
REPLY_MAILBOX = {"reply_address": "replies@example.org", "imap_host": "imap.test", "imap_user": "replies",
                 "imap_password": "imap-secret", "authserv_id": "mx.example.org"}


def make_email_config(reply_mailbox: bool = True, **server: Any) -> NotifyConfig:
    d = make_config(**server).model_dump(mode="json", exclude_none=True)
    d["providers"]["mail"] = {**MAIL, **(REPLY_MAILBOX if reply_mailbox else {})}
    d["devices"]["alex-email"] = {"provider": "mail", "owner": "alex", "address": {"to": "alex@example.org"}}
    d["users"]["alex"]["devices"] = ["alex-phone", "alex-email"]
    return NotifyConfig.model_validate(d)


@dataclass
class SentMail:
    raws: list[bytes] = field(default_factory=list)

    def last(self) -> Any:
        return email.message_from_bytes(self.raws[-1], policy=email.policy.default)


@pytest.fixture
def sent_mail(monkeypatch) -> SentMail:
    from notify.providers.base import SendResult
    from notify.providers.email import EmailProvider
    box = SentMail()

    def capture(self: Any, raw: bytes, sender: str, to: str) -> SendResult:
        box.raws.append(raw)
        return SendResult(outcome="success")
    monkeypatch.setattr(EmailProvider, "_send_sync", capture)
    return box


@pytest.fixture
def email_factory(tmp_path, fake, sent_mail):
    servers: list[LiveServer] = []

    def make(reply_mailbox: bool = True, **server_cfg: Any) -> LiveServer:
        app = create_app(make_email_config(reply_mailbox, **server_cfg),
                         db_path=str(tmp_path / f"mail{len(servers)}.sqlite3"),
                         key_path=str(tmp_path / "master.key"), transport=httpx.MockTransport(fake.handler))
        srv = LiveServer(app, fake)
        srv.start()
        srv.service.resolve = fake_resolve
        srv.mailbox = None  # replaced by a FakeMailbox once tests/mailfactory.py exists (Task 5)
        servers.append(srv)
        return srv

    yield make
    for s in servers:
        s.stop()


@pytest.fixture
def email_live(email_factory) -> LiveServer:
    return email_factory()
