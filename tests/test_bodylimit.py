"""Request body limit middleware: Content-Length precheck and streamed byte counting."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from notify.bodylimit import BodyLimit
from tests.conftest import TOKENS


async def echo(scope: Any, receive: Any, send: Any) -> None:
    n = 0
    while True:
        msg = await receive()
        n += len(msg.get("body", b""))
        if not msg.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": str(n).encode()})


def call(method: str, path: str, content: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        wrapped = BodyLimit(echo, default=100, routes={("POST", "/v1/notifications"): 1000})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://t") as c:
            return await c.request(method, path, content=content, headers=headers)
    return asyncio.run(run())


def test_default_cap_by_content_length():
    r = call("POST", "/x", b"a" * 101)
    assert r.status_code == 413
    assert r.json() == {"detail": "request body too large (max 100 bytes)"}
    assert call("POST", "/x", b"a" * 100).status_code == 200


def test_route_cap():
    assert call("POST", "/v1/notifications", b"a" * 1000).status_code == 200
    assert call("POST", "/v1/notifications", b"a" * 1001).status_code == 413


def test_route_cap_is_method_specific():
    assert call("PUT", "/v1/notifications", b"a" * 101).status_code == 413


def test_chunked_body_over_cap():
    async def gen():
        for _ in range(3):
            yield b"a" * 50
    r = call("POST", "/x", gen())
    assert r.status_code == 413


def test_chunked_body_under_cap():
    async def gen():
        yield b"a" * 40
        yield b"a" * 40
    assert call("POST", "/x", gen()).text == "80"


def test_get_without_body():
    assert call("GET", "/x").status_code == 200


def test_live_mcp_oversized_body(live):
    with live.client() as c:
        r = c.post("/mcp", content=b"x" * (2 << 20), headers={"Authorization": f"Bearer {TOKENS['agent']}"})
        assert r.status_code == 413
