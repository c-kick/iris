"""Inside a container started on a copy of live data: print what the new version sees.

    docker exec <container> python /app/scripts/upgrade_probe.py   (or pipe via stdin)

Reads only: config summary (no secrets), schema tables, readiness. Sends nothing.
"""

import json
import os
import sqlite3
import urllib.request

db = sqlite3.connect(os.environ.get("NOTIFY_DB", "/data/notify.sqlite3"))
tables = sorted(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
cfg = json.loads(db.execute("SELECT value FROM settings WHERE key='config'").fetchone()[0])
counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("notifications", "registrations")}
try:
    ready = json.loads(urllib.request.urlopen("http://127.0.0.1:8930/health/ready", timeout=5).read())
except urllib.error.HTTPError as e:  # 503 when not ready still carries the JSON body
    ready = json.loads(e.read())
print(json.dumps({
    "tables": tables,
    "counts": counts,
    "devices": sorted(cfg.get("devices", {})),
    "users": sorted(cfg.get("users", {})),
    "clients": sorted(cfg.get("clients", {})),
    "registration_block_stored": "registration" in cfg,
    "ready": ready,
}, indent=1))
