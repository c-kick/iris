"""Agent group chats: schema, the chat scope and its settings."""

from __future__ import annotations

import sqlite3

import pytest

from notify.db import SCHEMA_VERSION, Database
from tests.test_admin import H, fresh, setup_admin  # noqa: F401  (fixture re-export)

CHAT_TABLES = {"chat_groups", "chat_members", "chat_messages", "chat_wakes"}


def _tables(db: Database) -> set[str]:
    return {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}


def _group(db: Database, gid: str, state: str) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO chat_groups(id, host, folder, state, cap) VALUES (?, 'host-a', '/srv/app', ?, 10)",
                  (gid, state))


def test_tables_and_open_index(tmp_path):
    db = Database(tmp_path / "n.sqlite3")
    assert CHAT_TABLES <= _tables(db)
    _group(db, "g1", "open")
    with pytest.raises(sqlite3.IntegrityError):
        _group(db, "g2", "open")
    _group(db, "g3", "closed")
    _group(db, "g4", "closed")


def test_migration_from_v2(tmp_path):
    path = tmp_path / "n.sqlite3"
    db = Database(path)
    with db.tx() as c:  # turn it back into a schema-2 database
        for t in ("chat_wakes", "chat_messages", "chat_members", "chat_groups"):
            c.execute(f"DROP TABLE {t}")
        c.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    db.conn.close()
    db = Database(path)
    assert CHAT_TABLES <= _tables(db)
    assert db.scalar("SELECT value FROM meta WHERE key='schema_version'") == str(SCHEMA_VERSION) == "4"


def test_chat_settings(fresh):
    b = setup_admin(fresh())
    b.put("/admin/api/users/alex", json={"display_name": "Alex"}, headers=H)
    st = b.put("/admin/api/server", json={"chat_cap": 5, "chat_alert_targets": ["user:alex"]}, headers=H).json()
    assert st["config"]["server"]["chat_cap"] == 5
    assert st["config"]["server"]["chat_alert_targets"] == ["user:alex"]
    assert b.put("/admin/api/server", json={"chat_cap": 1}, headers=H).status_code == 422
    assert b.put("/admin/api/server", json={"chat_alert_targets": ["user:nobody"]}, headers=H).status_code == 422
    st = b.post("/admin/api/clients/agent-a", json={"scopes": ["chat"], "chat_host": "host-b"}, headers=H).json()
    assert st["config"]["clients"]["agent-a"]["chat_host"] == "host-b"
    st = b.put("/admin/api/clients/agent-a", json={"scopes": ["chat"], "chat_host": "host-c"}, headers=H).json()
    assert st["config"]["clients"]["agent-a"]["chat_host"] == "host-c"
    assert b.put("/admin/api/clients/agent-a", json={"scopes": ["chat"], "chat_host": "Host B"},
                 headers=H).status_code == 422
