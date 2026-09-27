"""CLI: python -m notify [serve | new-token NAME | check-config]."""

from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m notify")
    sub = ap.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="run the service (default)")
    serve.add_argument("--host", default=os.environ.get("NOTIFY_HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("NOTIFY_PORT", "8930")))
    tok = sub.add_parser("new-token", help="generate a client token and the hash to put in notify.yaml")
    tok.add_argument("name")
    tok.add_argument("--scopes", default="send,read", help="comma list of send,read,manage,admin")
    sub.add_parser("reset-admin", help="forget the admin password and print a new one-time setup code")
    chk = sub.add_parser("check-config", help="validate a YAML seed config (no secrets shown)")
    chk.add_argument("--config", default=os.environ.get("NOTIFY_CONFIG", "/config/notify.yaml"))
    args = ap.parse_args(argv)

    if args.cmd == "new-token":
        from .auth import hash_token, new_token
        token = new_token()
        scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
        print(f"# token for {args.name} (give this to the client; it is not stored anywhere):", file=sys.stderr)
        print(token, file=sys.stderr)
        print(f"# add to clients: in notify.yaml\n  {args.name}:\n    token_sha256: \"{hash_token(token)}\"\n"
              f"    scopes: [{', '.join(scopes)}]")
        return 0

    if args.cmd == "reset-admin":
        from .db import Database
        from .store import ConfigStore, MasterKey
        db = Database(os.environ.get("NOTIFY_DB", "/data/notify.sqlite3"))
        store = ConfigStore(db, MasterKey(os.environ.get("NOTIFY_KEY_FILE", "/secrets/master.key")))
        code = store.reset_admin()
        db.close()
        print(f"Admin password cleared and all sessions signed out. Open /admin and enter setup code: {code}")
        return 0

    if args.cmd == "check-config":
        import httpx

        from .config import ConfigError, load_config
        from .providers import build_provider
        try:
            cfg = load_config(args.config)
        except ConfigError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        http = httpx.AsyncClient()
        report = {"missing_env": cfg.missing_env, "providers": {}, "devices": {},
                  "users": list(cfg.users), "groups": list(cfg.groups), "channels": list(cfg.channels),
                  "clients": {n: [s.value for s in c.scopes] for n, c in cfg.clients.items()}}
        ok = True
        providers = {}
        for name, p in cfg.providers.items():
            providers[name] = prov = build_provider(name, p.model_dump(), http)
            report["providers"][name] = prov.config_problems() or "ok"
            ok &= not prov.config_problems()
        for name, d in cfg.devices.items():
            probs = providers[d.provider].device_problems(d.address)
            report["devices"][name] = probs or "ok"
            ok &= not probs
        print(json.dumps(report, indent=2))
        return 0 if ok else 2

    import warnings

    import uvicorn

    from .logs import setup_logging
    # emitted by the MCP SDK's settings model at import; harmless
    warnings.filterwarnings("ignore", message="Field 'lifespan' has an incomplete definition")
    setup_logging(os.environ.get("NOTIFY_LOG_LEVEL", "INFO"))
    from .app import create_app
    host = getattr(args, "host", os.environ.get("NOTIFY_HOST", "0.0.0.0"))
    port = getattr(args, "port", int(os.environ.get("NOTIFY_PORT", "8930")))
    uvicorn.run(create_app(), host=host, port=port, log_config=None, access_log=False,
                timeout_graceful_shutdown=15, proxy_headers=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
