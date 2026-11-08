"""Application factory: wires config, database, providers, workers, REST, admin UI and MCP."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .admin import router as admin_router
from .api import router
from .auth import Authenticator
from .chat_api import router as chat_router
from .chat_decide import router as chat_decide_router
from .bodylimit import BodyLimit
from .config import NotifyConfig
from .core import NotifyService
from .db import Database
from .mcp_server import SCOPE_KEY, build_mcp
from .providers import build_provider
from .registrations import router as registration_router
from .webpush_routes import router as webpush_router
from .store import ConfigStore, MasterKey
from .worker import Workers

log = logging.getLogger("notify.app")

ADMIN_UI = Path(__file__).parent / "admin_ui"


class MCPAuth:
    """Bearer auth in front of /mcp; stores the client in the ASGI scope for tools."""

    def __init__(self, app: ASGIApp, get_auth: Callable[[], Authenticator]):
        self.app = app
        self.get_auth = get_auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            client = self.get_auth().from_header(headers.get("authorization"))
            if client is None:
                resp = JSONResponse({"detail": "missing or invalid bearer token"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
                await resp(scope, receive, send)
                return
            scope[SCOPE_KEY] = client
        await self.app(scope, receive, send)


def create_app(config: NotifyConfig | None = None, db_path: str | None = None, key_path: str | None = None,
               transport: httpx.AsyncBaseTransport | None = None, start_workers: bool = True) -> FastAPI:
    """`config` seeds an empty database (tests); otherwise the database is authoritative and
    NOTIFY_CONFIG (YAML) is imported only when the database has no configuration yet."""
    db = Database(db_path or os.environ.get("NOTIFY_DB", "/data/notify.sqlite3"))
    from .providers.pushover import redact_callback_secrets
    if redact_callback_secrets(db):  # older attempt rows kept the callback URL, secret included
        log.warning("removed the callback secret from stored attempt rows")
    store = ConfigStore(db, MasterKey(key_path or os.environ.get("NOTIFY_KEY_FILE", "/secrets/master.key")))
    config = store.load(seed=config, seed_path=os.environ.get("NOTIFY_CONFIG", "/config/notify.yaml"))
    holder: dict[str, Any] = {}
    mcp = build_mcp(lambda: holder["service"])

    app = FastAPI(title="hnl-notify", version=__version__,
                  description="Unified notification service: provider-agnostic REST + MCP.")

    def apply_config(cfg: NotifyConfig) -> None:
        """Persist nothing; make `cfg` live for routing, providers and authentication."""
        service: NotifyService = holder["service"]
        providers = {name: build_provider(name, p.model_dump(), service.http) for name, p in cfg.providers.items()}
        service.reconfigure(cfg, providers)
        app.state.auth = Authenticator(cfg)
        app.state.config = cfg
        if app.state.workers:
            app.state.workers.check_devices()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(20.0, connect=10.0),
                                 headers={"User-Agent": f"hnl-notify/{__version__}"},
                                 limits=httpx.Limits(max_connections=10, max_keepalive_connections=4))
        cfg = app.state.config
        providers = {name: build_provider(name, p.model_dump(), http) for name, p in cfg.providers.items()}
        service = NotifyService(cfg, db, providers)
        service.http = http
        service.fetch_transport = transport  # attachment fetches never use the shared client
        holder["service"] = service
        app.state.loop = asyncio.get_running_loop()
        service.loop = app.state.loop  # lets reconfigure() wake the dispatcher from a worker thread
        app.state.service = service
        workers = Workers(service, http) if start_workers else None
        app.state.workers = workers
        for name, p in providers.items():
            if p.config_problems():
                log.warning("provider not ready", extra={"provider": name, "problems": p.config_problems()})
        if not store.admin_configured():
            code = store.new_setup_code()
            log.warning(f"admin setup required: open /admin and enter setup code {code}",
                        extra={"setup_code": code})
        if workers:
            workers.start()
            workers.check_devices()
        log.info("hnl-notify started", extra={"version": __version__, "devices": list(cfg.devices),
                                              "providers": list(providers), "clients": list(cfg.clients)})
        async with mcp.session_manager.run():
            try:
                yield
            finally:
                if workers:
                    await workers.stop()
                await http.aclose()
                db.close()
                log.info("hnl-notify stopped")

    app.router.lifespan_context = lifespan

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Any, exc: RequestValidationError) -> JSONResponse:
        # the default handler echoes each offending input, which can be a megabyte of base64
        return JSONResponse({"detail": [{k: v for k, v in e.items() if k not in ("input", "ctx", "url")}
                                        for e in exc.errors()]}, status_code=422)
    app.state.auth = Authenticator(config)
    app.state.config = config
    app.state.store = store
    app.state.apply_config = apply_config
    app.state.workers = None
    app.include_router(router)
    app.include_router(admin_router)
    app.include_router(registration_router)
    app.include_router(webpush_router)
    app.include_router(chat_router)
    app.include_router(chat_decide_router)
    app.mount("/admin/static", StaticFiles(directory=ADMIN_UI), name="admin-static")
    for route in mcp.streamable_http_app().routes:
        app.router.routes.append(route)
    app.add_middleware(MCPAuth, get_auth=lambda: app.state.auth)

    @app.middleware("http")
    async def admin_headers(request: Any, call_next: Any) -> Any:
        response = await call_next(request)
        if request.url.path.startswith("/admin"):
            response.headers["Content-Security-Policy"] = ADMIN_CSP
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    app.add_middleware(BodyLimit, get_auth=lambda: app.state.auth)  # added last = outermost: caps bodies before auth or routing
    return app


ADMIN_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
             "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
             "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
