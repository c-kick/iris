"""Pure-ASGI request body cap: Content-Length precheck plus byte counting for chunked bodies."""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_ROUTES: dict[tuple[str, str], int] = {("POST", "/v1/notifications"): 15 << 20}


class _TooLarge(Exception):
    pass


class BodyLimit:
    def __init__(self, app: ASGIApp, default: int = 1 << 20, routes: dict[tuple[str, str], int] | None = None):
        self.app = app
        self.default = default
        self.routes = DEFAULT_ROUTES if routes is None else routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cap = self.routes.get((scope["method"], scope["path"]), self.default)
        declared = next((v for k, v in scope.get("headers", []) if k.lower() == b"content-length"), None)
        if declared is not None and declared.isdigit() and int(declared) > cap:
            await self._reject(cap, scope, receive, send)
            return
        seen = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > cap:
                    raise _TooLarge
            return msg

        async def tracking_send(msg: Message) -> None:
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            if started:
                raise
            await self._reject(cap, scope, receive, send)

    @staticmethod
    async def _reject(cap: int, scope: Scope, receive: Receive, send: Send) -> None:
        resp = JSONResponse({"detail": f"request body too large (max {cap} bytes)"}, status_code=413,
                            headers={"Connection": "close"})
        await resp(scope, receive, send)
