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
        assert r.status_code == 413, (r.status_code, r.text, r.headers)


def _chunks(total: int):
    def gen():
        for _ in range(total >> 16):
            yield b"x" * (1 << 16)
    return gen()


def test_live_chunked_mcp_over_cap(live):
    with live.client() as c:
        r = c.post("/mcp", content=_chunks(2 << 20), headers={
            "Authorization": f"Bearer {TOKENS['agent']}", "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"})
        assert r.status_code == 413, (r.status_code, r.text, r.headers)


def test_live_chunked_notifications_over_cap(live):
    with live.client() as c:
        r = c.post("/v1/notifications", content=_chunks(16 << 20), headers={"Content-Type": "application/json"})
        assert r.status_code == 413, (r.status_code, r.text, r.headers)


def test_large_body_needs_authorization_before_reading():
    reached: list[int] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(1)
        await echo(scope, receive, send)

    async def run(headers: dict[str, str], content: Any) -> httpx.Response:
        wrapped = BodyLimit(app, default=100, routes={("POST", "/v1/notifications"): 1000}, large=500)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://t") as c:
            return await c.post("/v1/notifications", content=content, headers=headers)
    r = asyncio.run(run({}, b"a" * 600))
    assert r.status_code == 401 and r.json() == {"detail": "authentication required"} and reached == []
    assert asyncio.run(run({"Authorization": "Bearer x"}, b"a" * 600)).text == "600"
    assert asyncio.run(run({}, b"a" * 400)).text == "400"  # small unauthenticated requests are untouched

    async def gen():
        for _ in range(3):
            yield b"a" * 300
    reached.clear()
    r = asyncio.run(run({}, gen()))  # chunked: refused as soon as it crosses the threshold
    assert r.status_code == 401


def test_large_bodies_are_read_two_at_a_time():
    running = peak = 0

    async def slow(scope: Any, receive: Any, send: Any) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.1)
        running -= 1
        await echo(scope, receive, send)

    async def run() -> list[int]:
        wrapped = BodyLimit(slow, default=100, routes={("POST", "/x"): 1000}, large=500)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://t") as c:
            h = {"Authorization": "Bearer x"}
            small = asyncio.create_task(c.post("/x", content=b"a" * 10, headers=h))
            rs = await asyncio.gather(*[c.post("/x", content=b"a" * 600, headers=h) for _ in range(5)], small)
            return [r.status_code for r in rs]
    assert asyncio.run(run()) == [200] * 6
    assert peak <= 3  # two large + the small one; five large never ran together


def test_live_unauthenticated_large_post_refused(live):
    r = httpx.post(f"{live.url}/v1/notifications", content=b"x" * (2 << 20), timeout=10)
    assert r.status_code == 401 and r.json() == {"detail": "authentication required"}


class FakeAuth:
    def from_header(self, header):
        return object() if header == "Bearer good" else None


def test_large_body_credential_is_validated_before_reading():
    reached: list[int] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(1)
        await echo(scope, receive, send)

    async def run(token: str) -> httpx.Response:
        wrapped = BodyLimit(app, default=100, routes={("POST", "/x"): 1000}, large=500, get_auth=FakeAuth)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://t") as c:
            return await c.post("/x", content=b"a" * 600, headers={"Authorization": f"Bearer {token}"})
    r = asyncio.run(run("bogus"))
    assert r.status_code == 401 and reached == []
    assert asyncio.run(run("good")).text == "600"


def test_slow_drip_large_body_times_out_and_frees_the_slot():
    async def run() -> list[int]:
        wrapped = BodyLimit(echo, default=100, routes={("POST", "/x"): 1000}, large=500, large_concurrency=1,
                            read_deadline=0.3)

        async def drip():
            yield b"a" * 400
            await asyncio.sleep(2)
            yield b"a" * 400
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://t") as c:
            h = {"Authorization": "Bearer x", "Content-Length": "800"}
            slow = await c.post("/x", content=drip(), headers=h)
            fast = await c.post("/x", content=b"a" * 600, headers={"Authorization": "Bearer x"})
            return [slow.status_code, fast.status_code]
    assert asyncio.run(run()) == [408, 200]


def test_live_bogus_token_on_large_post_refused(live):
    r = httpx.post(f"{live.url}/v1/notifications", content=b"x" * (2 << 20), timeout=10,
                   headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_deadline_does_not_apply_after_the_body_is_complete():
    async def app(scope: Any, receive: Any, send: Any) -> None:
        while (await receive()).get("more_body"):  # the whole body
            pass
        await asyncio.sleep(0.4)  # slower than the read deadline
        listener = asyncio.create_task(receive())  # disconnect listener: must not be timed
        await asyncio.sleep(0.05)
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        listener.cancel()

    async def run() -> int:
        wrapped = BodyLimit(app, default=100, routes={("POST", "/x"): 1000}, large=500, read_deadline=0.2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://t") as c:
            return (await c.post("/x", content=b"a" * 600, headers={"Authorization": "Bearer x"})).status_code
    assert asyncio.run(run()) == 202
