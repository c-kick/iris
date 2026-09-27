"""Runtime configuration held in SQLite, secrets encrypted at rest.

The admin UI edits this. A YAML file (NOTIFY_CONFIG) is only a one-time seed
for an empty database. Secret fields (declared by each provider adapter, plus
server.callback_secret) are stored as AES-256-GCM ciphertext; the 32-byte key
lives in a separate 0600 file (NOTIFY_KEY_FILE) generated on first start.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import ConfigError, NotifyConfig, load_config
from .db import Database, dumps, in_seconds, loads, now_iso
from .providers import REGISTRY

log = logging.getLogger("notify.store")

ENC = "$enc"
SESSION_DAYS = 14


class MasterKey:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.exists():
            raw = self.path.read_bytes()
            if len(raw) != 32:
                raise RuntimeError(f"{self.path}: master key must be exactly 32 bytes")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            raw = secrets.token_bytes(32)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            log.warning("generated new master key", extra={"path": str(self.path)})
        self._aead = AESGCM(raw)

    def encrypt(self, plaintext: str) -> str:
        nonce = secrets.token_bytes(12)
        return base64.b64encode(nonce + self._aead.encrypt(nonce, plaintext.encode(), b"hnl-notify")).decode()

    def decrypt(self, token: str) -> str:
        raw = base64.b64decode(token)
        return self._aead.decrypt(raw[:12], raw[12:], b"hnl-notify").decode()


def secret_paths(cfg: dict[str, Any]) -> list[tuple[str, ...]]:
    """Paths of secret leaves in a config dict."""
    paths: list[tuple[str, ...]] = [("server", "callback_secret")]
    providers = cfg.get("providers") or {}
    for pname, p in providers.items():
        cls = REGISTRY.get((p or {}).get("type"))
        if cls:
            paths += [("providers", pname, k) for k in cls.secret_settings]
    for dname, d in (cfg.get("devices") or {}).items():
        ptype = (providers.get((d or {}).get("provider")) or {}).get("type")
        cls = REGISTRY.get(ptype)
        if cls:
            paths += [("devices", dname, "address", k) for k in cls.secret_address_fields]
    return paths


def _get(d: dict[str, Any], path: tuple[str, ...]) -> Any:
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _set(d: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    for k in path[:-1]:
        if not isinstance(d.get(k), dict):
            return
        d = d[k]
    d[path[-1]] = value


def mask(value: Any) -> dict[str, Any]:
    s = str(value or "")
    return {"set": bool(s), "hint": ("…" + s[-4:]) if len(s) >= 12 else ("set" if s else "")}


class ConfigStore:
    def __init__(self, db: Database, key: MasterKey):
        self.db = db
        self.key = key

    # --- config ---
    def load(self, seed: NotifyConfig | None = None, seed_path: str | None = None) -> NotifyConfig:
        row = self.db.one("SELECT value FROM settings WHERE key='config'")
        if row is not None:
            data = loads(row["value"], {})
            for path in secret_paths(data):
                v = _get(data, path)
                if isinstance(v, dict) and ENC in v:
                    _set(data, path, self.key.decrypt(v[ENC]))
            return NotifyConfig.model_validate(data)
        if seed is None and seed_path and Path(seed_path).exists():
            seed = load_config(seed_path)
            log.info("imported seed configuration", extra={"path": seed_path, "missing_env": seed.missing_env})
        cfg = seed or NotifyConfig()
        self.save(cfg)
        return cfg

    def save(self, cfg: NotifyConfig) -> None:
        data = cfg.model_dump(mode="json", exclude={"missing_env"})
        for path in secret_paths(data):
            v = _get(data, path)
            if v:
                _set(data, path, {ENC: self.key.encrypt(str(v))})
        with self.db.tx() as c:
            c.execute("INSERT INTO settings(key, value, updated_at) VALUES ('config', ?, ?)"
                      " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                      (dumps(data), now_iso()))

    @staticmethod
    def masked(cfg: NotifyConfig) -> dict[str, Any]:
        data = cfg.model_dump(mode="json", exclude={"missing_env"})
        for path in secret_paths(data):
            _set(data, path, mask(_get(data, path)))
        return data

    # --- admin credentials ---
    def admin_configured(self) -> bool:
        return self.db.one("SELECT 1 FROM settings WHERE key='admin'") is not None

    @staticmethod
    def _hash_password(password: str, salt: bytes) -> str:
        return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, maxmem=64 * 1024 * 1024).hex()

    def set_password(self, password: str) -> None:
        salt = secrets.token_bytes(16)
        value = dumps({"salt": salt.hex(), "hash": self._hash_password(password, salt)})
        with self.db.tx() as c:
            c.execute("INSERT INTO settings(key, value, updated_at) VALUES ('admin', ?, ?)"
                      " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                      (value, now_iso()))
            c.execute("DELETE FROM settings WHERE key='setup'")
            c.execute("DELETE FROM admin_sessions")

    def check_password(self, password: str) -> bool:
        row = self.db.one("SELECT value FROM settings WHERE key='admin'")
        if row is None:
            return False
        v = loads(row["value"])
        return hmac.compare_digest(self._hash_password(password, bytes.fromhex(v["salt"])), v["hash"])

    def new_setup_code(self) -> str:
        code = "-".join(secrets.token_hex(3) for _ in range(3))
        with self.db.tx() as c:
            c.execute("INSERT INTO settings(key, value, updated_at) VALUES ('setup', ?, ?)"
                      " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                      (hashlib.sha256(code.encode()).hexdigest(), now_iso()))
        return code

    def check_setup_code(self, code: str) -> bool:
        row = self.db.one("SELECT value FROM settings WHERE key='setup'")
        return row is not None and hmac.compare_digest(
            hashlib.sha256(code.strip().lower().encode()).hexdigest(), row["value"])

    def reset_admin(self) -> str:
        with self.db.tx() as c:
            c.execute("DELETE FROM settings WHERE key='admin'")
            c.execute("DELETE FROM admin_sessions")
        return self.new_setup_code()

    # --- sessions ---
    def new_session(self) -> str:
        sid = secrets.token_urlsafe(32)
        with self.db.tx() as c:
            c.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now_iso(),))
            c.execute("INSERT INTO admin_sessions(id_hash, created_at, expires_at) VALUES (?,?,?)",
                      (hashlib.sha256(sid.encode()).hexdigest(), now_iso(), in_seconds(SESSION_DAYS * 86400)))
        return sid

    def session_valid(self, sid: str | None) -> bool:
        if not sid:
            return False
        row = self.db.one("SELECT expires_at FROM admin_sessions WHERE id_hash=?",
                          (hashlib.sha256(sid.encode()).hexdigest(),))
        return row is not None and row["expires_at"] > now_iso()

    def end_session(self, sid: str | None) -> None:
        if sid:
            with self.db.tx() as c:
                c.execute("DELETE FROM admin_sessions WHERE id_hash=?", (hashlib.sha256(sid.encode()).hexdigest(),))


def edit_config(cfg: NotifyConfig, mutate: Any) -> NotifyConfig:
    """Apply `mutate(dict)` to a plaintext copy and re-validate. Raises ConfigError."""
    data = copy.deepcopy(cfg.model_dump(mode="json", exclude={"missing_env"}))
    mutate(data)
    try:
        return NotifyConfig.model_validate(data)
    except Exception as e:
        raise ConfigError(_readable(e)) from e


def _readable(e: Exception) -> str:
    errors = getattr(e, "errors", None)
    if callable(errors):
        parts = []
        for err in errors():
            loc = ".".join(str(x) for x in err.get("loc", ()))
            msg = str(err.get("msg", "")).removeprefix("Value error, ")
            parts.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(parts)
    return str(e)
