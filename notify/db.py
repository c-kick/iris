"""SQLite persistence: schema, connection and time helpers.

One connection, serialised with a lock. All access happens from the asyncio
event loop (plus short CLI use); statements are small, so blocking is brief.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    idempotency_key TEXT,
    source TEXT NOT NULL,
    title TEXT,
    message TEXT NOT NULL,
    severity TEXT NOT NULL,
    url TEXT,
    url_title TEXT,
    actions_json TEXT NOT NULL DEFAULT '[]',
    data_json TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    targets_json TEXT NOT NULL,
    require_ack INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    status TEXT NOT NULL,
    routing_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (client_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_notifications_created ON notifications(created_at);
CREATE INDEX IF NOT EXISTS ix_notifications_status ON notifications(status);

CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY,
    notification_id TEXT NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    device TEXT NOT NULL,
    provider TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    next_attempt_at TEXT,
    lease_until TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    tracking TEXT NOT NULL DEFAULT 'none',
    observable_states_json TEXT NOT NULL,
    capability_gaps_json TEXT NOT NULL DEFAULT '[]',
    provider_message_id TEXT,
    receipt TEXT,
    next_poll_at TEXT,
    poll_errors INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    dispatched_at TEXT,
    delivered_at TEXT,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    interacted_at TEXT,
    interaction_json TEXT,
    finalized_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_deliveries_due ON deliveries(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_deliveries_poll ON deliveries(tracking, next_poll_at);
CREATE INDEX IF NOT EXISTS ix_deliveries_notification ON deliveries(notification_id);
CREATE INDEX IF NOT EXISTS ix_deliveries_receipt ON deliveries(receipt);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'send',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    http_status INTEGER,
    request_json TEXT,
    response_body TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_attempts_delivery ON attempts(delivery_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    notification_id TEXT REFERENCES notifications(id) ON DELETE CASCADE,
    delivery_id TEXT,
    client_id TEXT,
    source TEXT,
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_notification ON events(notification_id);

CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    events_json TEXT NOT NULL,
    notification_id TEXT,
    source TEXT,
    tag TEXT,
    description TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_failure_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_status INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE (subscription_id, event_id)
);
CREATE INDEX IF NOT EXISTS ix_webhook_due ON webhook_deliveries(state, next_attempt_at);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS enrollments (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    owner TEXT NOT NULL,
    code_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    device TEXT
);
CREATE INDEX IF NOT EXISTS ix_enrollments_code ON enrollments(code_sha256);

CREATE TABLE IF NOT EXISTS registrations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    requested_scopes_json TEXT NOT NULL,
    granted_scopes_json TEXT,
    token_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    source_ip TEXT,
    user_agent TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_sha256 TEXT
);
CREATE INDEX IF NOT EXISTS ix_registrations_status ON registrations(status, created_at);
CREATE INDEX IF NOT EXISTS ix_registrations_token ON registrations(token_sha256);

CREATE TABLE IF NOT EXISTS attachments (
    notification_id TEXT NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    source TEXT NOT NULL,
    source_host TEXT,
    data BLOB,
    purged_at TEXT,
    PRIMARY KEY (notification_id, idx)
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    id_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replies (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    notification_id TEXT NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    delivery_id TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    from_address TEXT NOT NULL,
    subject TEXT,
    received_at TEXT NOT NULL,
    text TEXT NOT NULL,
    stripped INTEGER NOT NULL DEFAULT 1,
    truncated INTEGER NOT NULL DEFAULT 0,
    dropped_attachments_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL,
    claimed_at TEXT,
    claimed_by TEXT,
    waiter_id TEXT,
    autoreply_notification_id TEXT,
    late_ok INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_replies_conversation ON replies(conversation_id, state, received_at);

CREATE TABLE IF NOT EXISTS reply_attachments (
    reply_id TEXT NOT NULL REFERENCES replies(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    data BLOB,
    purged_at TEXT,
    PRIMARY KEY (reply_id, idx)
);

CREATE TABLE IF NOT EXISTS reply_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    from_address TEXT,
    subject TEXT,
    message_id TEXT,
    reason TEXT NOT NULL,
    notification_id TEXT
);
"""


# --- time -------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_iso() -> str:
    return iso(utcnow())  # type: ignore[return-value]


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def in_seconds(seconds: float, base: datetime | None = None) -> str:
    return iso((base or utcnow()) + timedelta(seconds=seconds))  # type: ignore[return-value]


def new_id(prefix: str) -> str:
    """Time-sortable id: prefix + 12 hex ms timestamp + 10 hex random."""
    return f"{prefix}_{int(time.time() * 1000):012x}{secrets.token_hex(5)}"


def dumps(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False, default=str)


def loads(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    return json.loads(s)


# --- database ---------------------------------------------------------------

class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA journal_size_limit=67108864")
        self.conn.executescript(SCHEMA)
        self._add_missing_columns({
            "registrations": {"decision_sha256": "TEXT"},
            "notifications": {"reply_wanted": "INTEGER NOT NULL DEFAULT 0", "in_reply_to": "TEXT",
                              "conversation_id": "TEXT", "listener_seen_at": "TEXT",
                              "auto_submitted": "INTEGER NOT NULL DEFAULT 0",
                              # 0.8.0 scheduling; release_problem = reason text, null = none
                              "send_at": "TEXT", "max_late_seconds": "INTEGER", "deliver_by": "TEXT",
                              "ttl_seconds": "INTEGER", "explicit_expires_at": "TEXT", "released_at": "TEXT",
                              "dismissed_at": "TEXT", "release_problem": "TEXT"},
            "deliveries": {"recipient_address": "TEXT", "dismissed_at": "TEXT"},
            "replies": {"late_ok": "INTEGER NOT NULL DEFAULT 0"},
        })
        self._add_conversations()
        had_adhoc = "adhoc" in {r[1] for r in self.conn.execute("PRAGMA table_info(chat_groups)")}
        self._add_chat()
        self._add_chat_history()
        self._add_missing_columns({"chat_wakes": {"attempts": "INTEGER NOT NULL DEFAULT 0", "retry_after": "TEXT"},
                                  "chat_groups": {"owner_watch_until": "TEXT", "paused_at": "TEXT",
                                                  "adhoc": "INTEGER NOT NULL DEFAULT 0"},
                                  "chat_messages": {"in_reply_to": "TEXT", "claimed_by_member_id": "TEXT", "claimed_at": "TEXT",
                                                    "kind": "TEXT NOT NULL DEFAULT 'request'"},  # 0.14.0: request, fyi, all
                                  "chat_members": {"last_read_rowid": "INTEGER", "acted_rowid": "INTEGER",
                                                   "name": "TEXT", "status": "TEXT", "status_at": "TEXT",
                                                   "active_at": "TEXT", "marker": "TEXT", "intent": "TEXT"},  # 0.14.0
                                  "subscriptions": {"failing_since": "TEXT"},
                                  # 0.13.2: bumped by every claim, never reset (a retry resets attempts)
                                  "deliveries": {"lease_no": "INTEGER NOT NULL DEFAULT 0"},
                                  "replies": {"collected_at": "TEXT"}})  # 0.13.1: a waiter printed it  # 0.13.1: start of a failure streak
        # 0.13.2: webhooks of clients deleted before tombstoning are re-owned the same way (see admin delete_client)
        self.conn.execute("UPDATE subscriptions SET client_id=client_id || '#deleted' WHERE active=0"
                          " AND last_error='the API client was deleted' AND client_id NOT LIKE '%#deleted'")
        # members from before 0.13.0's read cursor owe nothing: start them at their group's newest message
        self.conn.execute("UPDATE chat_members SET last_read_rowid=(SELECT COALESCE(MAX(rowid), 0) FROM chat_messages"
                          " WHERE group_id=chat_members.group_id) WHERE last_read_rowid IS NULL")
        with self.tx() as c:  # 0.14.0: who a message @mentions; folder opt-in; sessions present; invites
            c.execute("CREATE TABLE IF NOT EXISTS chat_mentions (message_id TEXT NOT NULL REFERENCES chat_messages(id),"
                      " member_id TEXT NOT NULL REFERENCES chat_members(id), PRIMARY KEY(message_id, member_id))")
            c.execute("CREATE TABLE IF NOT EXISTS chat_folders (host TEXT NOT NULL, folder TEXT NOT NULL,"
                      " enabled_at TEXT, enabled_by TEXT, PRIMARY KEY(host, folder))")
            c.execute("CREATE TABLE IF NOT EXISTS chat_known_folders (host TEXT NOT NULL, folder TEXT NOT NULL,"
                      " PRIMARY KEY(host, folder))")
            c.execute("CREATE TABLE IF NOT EXISTS chat_presence (id TEXT PRIMARY KEY, host TEXT NOT NULL,"
                      " folder TEXT NOT NULL, kind TEXT NOT NULL, session_id TEXT NOT NULL, pid INTEGER, pid_start TEXT,"
                      " client_id TEXT, handle_sha256 TEXT NOT NULL, seen_at TEXT, started_at TEXT, status TEXT,"
                      " status_at TEXT, active_at TEXT)")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_chat_presence_session ON chat_presence(host, kind, session_id)")
            c.execute("CREATE TABLE IF NOT EXISTS chat_invites (id TEXT PRIMARY KEY, group_id TEXT NOT NULL,"
                      " presence_id TEXT NOT NULL, message_id TEXT NOT NULL, state TEXT NOT NULL, waiter_id TEXT,"
                      " claimed_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, retry_after TEXT, created_at TEXT,"
                      " finished_at TEXT)")
        if not had_adhoc:  # 0.14.0: a group open at the upgrade stays open as an ad-hoc group until it empties
            self.conn.execute("UPDATE chat_groups SET adhoc=1 WHERE state='open'")
        self._name_open_members()
        # after the columns exist (an index in SCHEMA would fail on a pre-0.6.0 database)
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_notifications_conversation ON notifications(conversation_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_notifications_scheduled ON notifications(status, send_at)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_chat_members_name ON chat_members(group_id, name)")
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def _add_conversations(self) -> None:
        """0.7.0: the conversations table and its one-time backfill. Not in SCHEMA: executescript autocommits, so a
        crash between CREATE and the backfill would skip the backfill. DDL is transactional here, so both or neither."""
        with self.tx() as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'").fetchone():
                return
            c.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, handle_sha256 TEXT, handle_notification_id TEXT,"
                      " legacy INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
            # every existing reply conversation keeps the old token rule; its unclaimed replies (late_ok 0) stay put
            c.execute("INSERT INTO conversations(id, legacy, created_at) SELECT DISTINCT conversation_id, 1, ?"
                      " FROM notifications WHERE reply_wanted=1 AND conversation_id IS NOT NULL", (iso(utcnow()),))

    def _add_chat(self) -> None:
        """Agent group chats (schema 3). Not in SCHEMA, like conversations: one transaction, all or nothing."""
        with self.tx() as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_groups'").fetchone():
                return
            c.execute("CREATE TABLE chat_groups (id TEXT PRIMARY KEY, host TEXT NOT NULL, folder TEXT NOT NULL,"
                      " state TEXT NOT NULL, opened_at TEXT, closed_at TEXT, paused INTEGER NOT NULL DEFAULT 0,"
                      " agent_streak INTEGER NOT NULL DEFAULT 0, cap INTEGER NOT NULL, owner_seen_at TEXT)")
            c.execute("CREATE UNIQUE INDEX ux_chat_groups_open ON chat_groups(host, folder) WHERE state='open'")
            c.execute("CREATE TABLE chat_members (id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES chat_groups(id),"
                      " kind TEXT CHECK(kind IN ('claude','codex')), session_id TEXT NOT NULL, pid INTEGER,"
                      " pid_start TEXT, client_id TEXT, handle_sha256 TEXT NOT NULL, joined_at TEXT, last_seen_at TEXT,"
                      " left_at TEXT, leave_reason TEXT)")
            c.execute("CREATE TABLE chat_messages (id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES chat_groups(id),"
                      " author_member_id TEXT, author TEXT CHECK(author IN ('claude','codex','owner','system')),"
                      " text TEXT NOT NULL, created_at TEXT)")
            c.execute("CREATE TABLE chat_wakes (id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES chat_messages(id),"
                      " member_id TEXT NOT NULL REFERENCES chat_members(id),"
                      " state TEXT CHECK(state IN ('pending','claimed','delivered','skipped')), waiter_id TEXT,"
                      " claimed_at TEXT, finished_at TEXT, reason TEXT)")
            c.execute("CREATE INDEX ix_chat_wakes_member_state ON chat_wakes(member_id, state)")

    def _add_chat_history(self) -> None:
        """Owner-approved reads of earlier closed chats (group_history). Additive, created for old databases too."""
        with self.tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS chat_history_requests (id TEXT PRIMARY KEY,"
                      " group_id TEXT NOT NULL REFERENCES chat_groups(id),"
                      " member_id TEXT NOT NULL REFERENCES chat_members(id), reason TEXT NOT NULL,"
                      " state TEXT NOT NULL CHECK(state IN ('pending','approved','denied','expired','used')),"
                      " decision_sha256 TEXT, message_id TEXT, created_at TEXT NOT NULL, decided_at TEXT, used_at TEXT)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_chat_history_member ON chat_history_requests(member_id, state)")

    def _name_open_members(self) -> None:
        """0.14.0: members still in an open group get a name, with a line in the chat ("Claude mem_... is now Wren")."""
        from .chat_names import pick
        with self.tx() as c:
            rows = c.execute("SELECT m.id, m.kind, m.group_id FROM chat_members m JOIN chat_groups g ON g.id=m.group_id"
                             " WHERE g.state='open' AND m.left_at IS NULL AND m.name IS NULL ORDER BY m.rowid").fetchall()
            for r in rows:
                taken = [x[0] for x in c.execute("SELECT name FROM chat_members WHERE group_id=? AND left_at IS NULL",
                                                 (r["group_id"],))]
                name = pick(taken, [])
                c.execute("UPDATE chat_members SET name=? WHERE id=?", (name, r["id"]))
                c.execute("INSERT INTO chat_messages(id, group_id, author_member_id, author, text, created_at)"
                          " VALUES (?, ?, NULL, 'system', ?, ?)",
                          (new_id("msg"), r["group_id"], f"{(r['kind'] or 'agent').capitalize()} {r['id']} is now {name}",
                           iso(utcnow())))

    def _add_missing_columns(self, columns: dict[str, dict[str, str]]) -> None:
        """Additive migrations for databases created by older versions."""
        for table, cols in columns.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                try:
                    self.conn.execute("COMMIT")
                except BaseException:
                    if self.conn.in_transaction:  # e.g. disk full: never leave the shared connection mid-transaction
                        self.conn.execute("ROLLBACK")
                    raise

    def query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Any = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Any = ()) -> Any:
        row = self.one(sql, params)
        return None if row is None else row[0]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
