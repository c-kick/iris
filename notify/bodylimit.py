"""Pure-ASGI request body cap: Content-Length precheck plus byte counting for chunked bodies."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# /mcp: a group_post may carry 5 MiB of files as base64 (~6.7 MB) plus its envelope; each tool still enforces its own
# limit (send_notification: 256 KiB inline)
DEFAULT_ROUTES: dict[tuple[str, str], int] = {("POST", "/v1/notifications"): 15 << 20, ("POST", "/mcp"): 15 << 19}
# a body above `large` on these routes also needs one of these scopes (any valid token is not enough)
DEFAULT_LARGE_SCOPES: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/v1/notifications"): frozenset({"send", "admin"}),
    ("POST", "/mcp"): frozenset({"chat", "admin"})}  # only a chat post has a reason to be that large
# without a valid bearer/basic credential a body is at most this big: JSON is parsed before the route's auth
# dependency runs (~25x memory), and nothing public (registration, login, callbacks, decide pages, browser
# push, admin session forms) needs more
UNAUTHENTICATED_MAX = 64 << 10
# routes the signed-in admin page sends large bodies to (method, full path pattern, cap): there a valid admin session
# cookie counts as a credential. Only the owner's chat file upload, a raw body that is never parsed as JSON.
SESSION_COOKIE = "notify_admin"
DEFAULT_SESSION_ROUTES: list[tuple[str, "re.Pattern[str]", int]] = [
    ("PUT", re.compile(r"^/admin/api/chats/[A-Za-z0-9_-]{1,64}/files$"), 10 << 20)]


def _cookie(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    for k, v in headers:
        if k.lower() == b"cookie":
            for part in v.decode("latin-1").split(";"):
                key, _, val = part.strip().partition("=")
                if key == name:
                    return val
    return None


class _Rejected(Exception):
    pass


class BodyLimit:
    """Bodies over `large` bytes need a valid credential (checked before reading; `get_auth` returns the live
    Authenticator, without it any Authorization header passes), at most `large_concurrency` of them are read at
    once and each must arrive within `read_deadline` seconds, so unauthenticated, parallel or slow-drip uploads
    cannot exhaust memory or hold the slots."""

    def __init__(self, app: ASGIApp, default: int = 1 << 20, routes: dict[tuple[str, str], int] | None = None,
                 large: int = 1 << 20, large_concurrency: int = 2, get_auth: Callable[[], Any] | None = None,
                 read_deadline: float = 60.0, unauthenticated_max: int = UNAUTHENTICATED_MAX,
                 large_scopes: dict[tuple[str, str], frozenset[str]] | None = None,
                 session_routes: list[tuple[str, "re.Pattern[str]", int]] | None = None,
                 session_ok: Callable[[str | None], bool] | None = None):
        self.app = app
        self.default = default
        self.routes = DEFAULT_ROUTES if routes is None else routes
        self.large = large
        self.large_concurrency = large_concurrency
        self.get_auth = get_auth
        self.read_deadline = read_deadline
        self.unauthenticated_max = unauthenticated_max
        self.large_scopes = DEFAULT_LARGE_SCOPES if large_scopes is None else large_scopes
        self.session_routes = DEFAULT_SESSION_ROUTES if session_routes is None else session_routes
        self.session_ok = session_ok  # without it no session route is open
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
        who = None
        if auth is not None and self.get_auth is not None:
            who = self.get_auth().from_header(auth)
        has_auth = auth is not None and (self.get_auth is None or who is not None)
        need = self.large_scopes.get((scope["method"], scope["path"]))
        large_ok = has_auth and (need is None or who is None or bool({getattr(s, "value", s) for s in who.scopes} & need))
        if auth is None and self.session_ok is not None:
            route = next((c for m, rx, c in self.session_routes if m == scope["method"] and rx.match(scope["path"])), None)
            if route is not None and self.session_ok(_cookie(headers, SESSION_COOKIE)):
                cap, has_auth, large_ok = route, True, True
        size = int(declared) if declared is not None and declared.isdigit() else None
        if size is not None and size > cap:
            await self._reject(cap, scope, receive, send)
            return
        if size is not None and size > self.unauthenticated_max and not has_auth:
            await self._unauthenticated(scope, receive, send)
            return
        if size is not None and size > self.large and not large_ok:
            await self._forbidden(scope, receive, send)
            return
        seen = 0
        held = False  # holding a large-body slot
        reason = ""  # "", "large" (413), "auth" (401), "scope" (403) or "slow" (408)
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
                if seen > self.unauthenticated_max and not has_auth:
                    reason = "auth"
                    raise _Rejected
                if seen > self.large and not large_ok:
                    reason = "scope"
                    raise _Rejected
                if seen > self.large:
                    await acquire()
            return msg

        async def reject() -> None:
            if reason == "auth":
                await self._unauthenticated(scope, receive, send)
            elif reason == "scope":
                await self._forbidden(scope, receive, send)
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
    async def _forbidden(scope: Scope, receive: Receive, send: Send) -> None:
        resp = JSONResponse({"detail": "a body this large needs a token with the send scope"}, status_code=403,
                            headers={"Connection": "close"})
        await resp(scope, receive, send)

    @staticmethod
    async def _reject(cap: int, scope: Scope, receive: Receive, send: Send) -> None:
        resp = JSONResponse({"detail": f"request body too large (max {cap} bytes)"}, status_code=413,
                            headers={"Connection": "close"})
        await resp(scope, receive, send)
