"""Peak-memory probe for large attachments (run from the repo root, ideally under a cgroup cap):

    PYTHONPATH=. systemd-run --user --scope -q -p MemoryMax=320M -p MemorySwapMax=0 -- .venv/bin/python scripts/memprobe.py

Starts the app in-process with a fake Pushover transport and an aiosmtpd sink, sends a notification with
10 MiB of inline attachments to a group of one email and two Pushover devices (once, then three at once)
and prints peak RSS (ru_maxrss) before and after each step. The probe's own client and the SMTP sink share
the process, so the figure is an upper bound for the service alone.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import resource
import socket
import sys
import tempfile
import time

import httpx
from aiosmtpd.controller import Controller

from notify.app import create_app
from notify.auth import hash_token
from notify.config import NotifyConfig

TOKEN = "memprobe-token"
PNG_HEAD = b"\x89PNG\r\n\x1a\n"


def rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class Sink:
    received = 0

    async def handle_DATA(self, server, session, envelope):  # noqa: N802
        Sink.received += 1
        return "250 OK"


def pushover(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": 1, "request": "r"})


def _b64_file(f, size: int, head: bytes) -> None:
    """Stream `size` random bytes (after `head`) as base64 without holding them all."""
    f.write(base64.b64encode(head + os.urandom(3 * 1024 * 1024 - len(head))).decode() if size >= 3 * 1024 * 1024 else "")
    left = size - 3 * 1024 * 1024
    while left > 0:
        n = min(left, 3 * 1024 * 1024)
        f.write(base64.b64encode(os.urandom(n)).decode())
        left -= n


def body(path: str) -> bytes:
    """The request JSON (4 MiB image + 6 MiB pdf), built piecewise so the probe's own peak stays small."""
    with open(path, "w") as f:
        f.write('{"targets": ["group:all"], "kinds": ["push", "email"], "message": "memprobe", "attachments": ['
                '{"filename": "pic.png", "content_type": "image/png", "data_base64": "')
        _b64_file(f, 4 * 1024 * 1024, PNG_HEAD)
        f.write('"}, {"filename": "doc.pdf", "content_type": "application/pdf", "data_base64": "')
        _b64_file(f, 6 * 1024 * 1024, b"%PDF-1.4\n")
        f.write('"}]}')
    with open(path, "rb") as f:
        return f.read()


async def main() -> None:
    tmp = tempfile.mkdtemp()
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    ctl = Controller(Sink(), hostname="127.0.0.1", port=port)
    ctl.start()
    addr = {"user_key": "u" * 30}
    cfg = NotifyConfig.model_validate({
        "providers": {"pushover": {"type": "pushover", "app_token": "a" * 30, "api_base": "https://pushover.test/1"},
                      "mail": {"type": "email", "host": "127.0.0.1", "port": port, "security": "none",
                               "from_address": "hnl@example.org"}},
        "devices": {"p1": {"provider": "pushover", "address": addr}, "p2": {"provider": "pushover", "address": addr},
                    "m1": {"provider": "mail", "address": {"to": "alex@example.org"}}},
        "groups": {"all": {"members": ["device:p1", "device:p2", "device:m1"]}},
        "clients": {"probe": {"token_sha256": hash_token(TOKEN), "scopes": ["send", "read"]}},
    })
    app = create_app(cfg, db_path=f"{tmp}/n.sqlite3", key_path=f"{tmp}/master.key",
                     transport=httpx.MockTransport(pushover))
    payload = body(f"{tmp}/body.json")
    print(f"baseline peak RSS before app: {rss_mib():.0f} MiB (body {len(payload) / 1048576:.1f} MiB)")
    async with app.router.lifespan_context(app):
        svc = app.state.service
        print(f"after startup:                {rss_mib():.0f} MiB")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=120,
                                     headers={"Authorization": f"Bearer {TOKEN}"}) as c:
            for label, n in (("one request", 1), ("three concurrent requests", 3)):
                before = Sink.received
                rs = await asyncio.gather(*[c.post("/v1/notifications", content=payload,
                                                   headers={"Content-Type": "application/json"}) for _ in range(n)])
                assert all(r.status_code == 202 for r in rs), [r.text[:200] for r in rs]
                end = time.monotonic() + 120
                while time.monotonic() < end:
                    done = svc.db.scalar("SELECT COUNT(*) FROM deliveries WHERE state='dispatched'")
                    if done >= 3 * (n + (0 if label == "one request" else 1)) and Sink.received >= before + n:
                        break
                    await asyncio.sleep(0.2)
                else:
                    print("TIMEOUT waiting for dispatch", file=sys.stderr)
                states = svc.db.query("SELECT state, COUNT(*) n FROM deliveries GROUP BY state")
                print(f"{label}: peak RSS {rss_mib():.0f} MiB; deliveries { {r['state']: r['n'] for r in states} };"
                      f" emails received {Sink.received}")
    ctl.stop()


if __name__ == "__main__":
    asyncio.run(main())
