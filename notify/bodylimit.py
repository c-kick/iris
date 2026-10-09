"""Pure-ASGI request body cap: Content-Length precheck plus byte counting for chunked bodies."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_ROUTES: dict[tuple[str, str], int] = {("POST", "/v1/notifications"): 15 << 20}


class _Rejected(Exception):
    pass


class BodyLimit:
    """Bodies over `large` bytes need a valid credential (checked before reading; `get_auth` returns the live
    Authenticator, without it any Authorization header passes), at most `large_concurrency` of them are read at
    once and each must arrive within `read_deadline` seconds, so unauthenticated, parallel or slow-drip uploads
    cannot exhaust memory or hold the slots."""

    def __init__(self, app: ASGIApp, default: int = 1 << 20, routes: dict[tuple[str, str], int] | None = None,
                 large: int = 1 << 20, large_concurrency: int = 2, get_auth: Callable[[], Any] | None = None,
                 read_deadline: float = 60.0):
        self.app = app
        self.default = default
        self.routes = DEFAULT_ROUTES if routes is None else routes
        self.large = large
        self.large_concurrency = large_concurrency
        self.get_auth = get_auth
        self.read_deadline = read_deadline
        self._sem: asyncio.Semaphore | None = None

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.large_concurrency)
        return self._sem

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cap = self.routes.get((scope["method"], scope["path"]), self.default)
        headers = scope.get("headers", [])
        declared = next((v for k, v in headers if k.lower() == b"content-length"), None)
        auth = next((v.decode("latin-1") for k, v in headers if k.lower() == b"authorization"), None)
        has_auth = auth is not None and (self.get_auth is None or self.get_auth().from_header(auth) is not None)
        size = int(declared) if declared is not None and declared.isdigit() else None
        if size is not None and size > cap:
            await self._reject(cap, scope, receive, send)
            return
        if size is not None and size > self.large and not has_auth:
            await self._unauthenticated(scope, receive, send)
            return
        seen = 0
        held = False  # holding a large-body slot
        reason = ""  # "", "large" (413), "auth" (401) or "slow" (408)
        complete = False  # the final body chunk has arrived
        deadline = 0.0  # monotonic time by which a large body must have arrived
        started = False  # a response (ours or the app's) has begun
        rejected = False  # that response is our 413/401

        async def acquire() -> None:
            nonlocal held
            nonlocal deadline
            if not held:
                await self._semaphore().acquire()
                held = True
                deadline = time.monotonic() + self.read_deadline

        async def limited_receive() -> Message:
            nonlocal seen, reason, complete
            if held and not complete:
                try:
                    if time.monotonic() > deadline:
                        raise asyncio.TimeoutError
                    msg = await asyncio.wait_for(receive(), max(0.001, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    reason = "slow"
                    raise _Rejected from None
            else:
                msg = await receive()
            if msg["type"] == "http.request":
                if not msg.get("more_body"):
                    complete = True  # fully read: later receive() calls (disconnect listener) are not ours to time
                seen += len(msg.get("body", b""))
                if seen > cap:
                    reason = "large"
                    raise _Rejected
                if seen > self.large and not has_auth:
                    reason = "auth"
                    raise _Rejected
                if seen > self.large:
                    await acquire()
            return msg

        async def reject() -> None:
            if reason == "auth":
                await self._unauthenticated(scope, receive, send)
            elif reason == "slow":
                await JSONResponse({"detail": "request body arrived too slowly"}, status_code=408,
                                   headers={"Connection": "close"})(scope, receive, send)
            else:
                await self._reject(cap, scope, receive, send)

        async def tracking_send(msg: Message) -> None:
            nonlocal started, rejected
            if reason:  # the app may swallow _Rejected and answer 400/500: its response is replaced
                if not started:
                    started = rejected = True
                    await reject()
                return
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            if size is not None and size > self.large:
                await acquire()
            await self.app(scope, limited_receive, tracking_send)
        except BaseException:
            if not reason or (started and not rejected):
                raise  # not ours, or the app's response is already underway: abort the connection
        finally:
            if held:
                self._semaphore().release()
        if reason and not started:
            await reject()

    @staticmethod
    async def _unauthenticated(scope: Scope, receive: Receive, send: Send) -> None:
        resp = JSONResponse({"detail": "authentication required"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer", "Connection": "close"})
        await resp(scope, receive, send)

    @staticmethod
    async def _reject(cap: int, scope: Scope, receive: Receive, send: Send) -> None:
        resp = JSONResponse({"detail": f"request body too large (max {cap} bytes)"}, status_code=413,
                            headers={"Connection": "close"})
        await resp(scope, receive, send)
