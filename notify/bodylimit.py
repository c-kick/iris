"""Pure-ASGI request body cap: Content-Length precheck plus byte counting for chunked bodies."""

from __future__ import annotations

import asyncio

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_ROUTES: dict[tuple[str, str], int] = {("POST", "/v1/notifications"): 15 << 20}


class _Rejected(Exception):
    pass


class BodyLimit:
    """Bodies over `large` bytes need an Authorization header (checked before reading) and at most
    `large_concurrency` of them are read at once, so unauthenticated or parallel uploads cannot exhaust memory."""

    def __init__(self, app: ASGIApp, default: int = 1 << 20, routes: dict[tuple[str, str], int] | None = None,
                 large: int = 1 << 20, large_concurrency: int = 2):
        self.app = app
        self.default = default
        self.routes = DEFAULT_ROUTES if routes is None else routes
        self.large = large
        self.large_concurrency = large_concurrency
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
        has_auth = any(k.lower() == b"authorization" for k, _ in headers)
        size = int(declared) if declared is not None and declared.isdigit() else None
        if size is not None and size > cap:
            await self._reject(cap, scope, receive, send)
            return
        if size is not None and size > self.large and not has_auth:
            await self._unauthenticated(scope, receive, send)
            return
        seen = 0
        held = False  # holding a large-body slot
        reason = ""  # "", "large" (413) or "auth" (401)
        started = False  # a response (ours or the app's) has begun
        rejected = False  # that response is our 413/401

        async def acquire() -> None:
            nonlocal held
            if not held:
                await self._semaphore().acquire()
                held = True

        async def limited_receive() -> Message:
            nonlocal seen, reason
            msg = await receive()
            if msg["type"] == "http.request":
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
