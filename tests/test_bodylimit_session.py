"""Owner file uploads: the admin page signs in with a cookie, and BodyLimit lets a valid session send a large body
on the upload route only (checked before the body is read)."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from notify.bodylimit import BodyLimit
from tests.test_bodylimit import echo

UPLOAD = re.compile(r"^/admin/api/chats/[^/]+/files$")


def call(method: str, path: str, size: int, cookie: str | None = None, auth: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        wrapped = BodyLimit(echo, default=100, unauthenticated_max=50, large=80,
                            session_routes=[("PUT", UPLOAD, 1000)], session_ok=lambda sid: sid == "good")
        headers = {}
        if cookie is not None:
            headers["cookie"] = f"other=1; notify_admin={cookie}"
        if auth is not None:
            headers["authorization"] = auth
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://t") as c:
            return await c.request(method, path, content=b"a" * size, headers=headers)
    return asyncio.run(run())


def test_valid_session_may_upload_up_to_the_route_cap():
    assert call("PUT", "/admin/api/chats/cg_1/files", 1000, cookie="good").status_code == 200
    assert call("PUT", "/admin/api/chats/cg_1/files", 1001, cookie="good").status_code == 413


def test_no_or_bad_session_stays_unauthenticated():
    assert call("PUT", "/admin/api/chats/cg_1/files", 60).status_code == 401
    assert call("PUT", "/admin/api/chats/cg_1/files", 60, cookie="bad").status_code == 401


def test_session_counts_only_on_the_upload_route():
    assert call("POST", "/admin/api/chats/cg_1/messages", 60, cookie="good").status_code == 401
    assert call("PUT", "/admin/api/chats/cg_1/files/x", 60, cookie="good").status_code == 401
    assert call("GET", "/admin/api/chats/cg_1/files", 60, cookie="good").status_code == 401


def test_session_check_is_not_called_elsewhere():
    calls: list[Any] = []

    async def run() -> int:
        wrapped = BodyLimit(echo, default=100, session_routes=[("PUT", UPLOAD, 1000)],
                            session_ok=lambda sid: calls.append(sid) or True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://t") as c:
            return (await c.post("/x", content=b"a", headers={"cookie": "notify_admin=x"})).status_code
    assert asyncio.run(run()) == 200 and calls == []
