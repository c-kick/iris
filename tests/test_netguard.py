"""0.13.2: webhooks and attachment fetches check the address they actually connect to (no DNS-rebinding window)."""

from __future__ import annotations

import asyncio
import http.server
import threading

import httpcore
import httpx
import pytest

from notify import netguard


class Recorder(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connected: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append((host, port))
        raise httpcore.ConnectError("stop here")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


def _resolver(mapping):
    async def resolve(host, port):
        return mapping[host]
    return resolve


def test_rebinding_name_is_refused_before_connecting():
    inner = Recorder()
    backend = netguard.GuardedBackend(_resolver({"hooks.test": ["127.0.0.1"]}), inner)
    with pytest.raises(netguard.AddressNotAllowed):
        asyncio.run(backend.connect_tcp("hooks.test", 80))
    assert inner.connected == []


def test_mixed_answer_is_refused():
    inner = Recorder()
    backend = netguard.GuardedBackend(_resolver({"hooks.test": ["192.168.5.5", "169.254.169.254"]}), inner)
    with pytest.raises(netguard.AddressNotAllowed):
        asyncio.run(backend.connect_tcp("hooks.test", 80))
    assert inner.connected == []


def test_connects_to_the_checked_address_not_the_name():
    inner = Recorder()
    backend = netguard.GuardedBackend(_resolver({"hooks.test": ["192.168.5.5", "192.168.5.6"]}), inner)
    with pytest.raises(httpcore.ConnectError):
        asyncio.run(backend.connect_tcp("hooks.test", 8080))
    assert inner.connected == [("192.168.5.5", 8080), ("192.168.5.6", 8080)]


def test_literal_addresses():
    inner = Recorder()
    backend = netguard.GuardedBackend(_resolver({}), inner)
    for denied in ("127.0.0.1", "::1", "[::ffff:127.0.0.1]", "0.0.0.0"):
        with pytest.raises(netguard.AddressNotAllowed):
            asyncio.run(backend.connect_tcp(denied, 80))
    assert inner.connected == []


def test_real_request_to_loopback_sends_nothing():
    """End to end through httpx: a server on 127.0.0.1 never sees the request."""
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    async def go():
        transport = netguard.GuardedTransport(_resolver({"rebind.test": ["127.0.0.1"]}))
        async with httpx.AsyncClient(transport=transport, trust_env=False) as c:
            with pytest.raises(httpx.ConnectError) as e:
                await c.post(f"http://rebind.test:{port}/hook", content=b"{}")
            assert netguard.refused(e.value)
            with pytest.raises(httpx.ConnectError):
                await c.post(f"http://127.0.0.1:{port}/hook", content=b"{}")
    try:
        asyncio.run(go())
    finally:
        srv.shutdown()
    assert hits == []
