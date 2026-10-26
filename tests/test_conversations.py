"""Conversations table: backfill of pre-0.7.0 reply conversations and retention."""

from __future__ import annotations

from datetime import timedelta

from notify.core import NotifyService
from notify.db import Database, iso, utcnow
from tests.conftest import make_config

OLD = iso(utcnow() - timedelta(days=200))
NEW = iso(utcnow())


def add_notification(db, nid, *, reply=False, conv=None, created=NEW):
    with db.tx() as c:
        c.execute("INSERT INTO notifications(id, client_id, source, message, severity, targets_json, status,"
                  " created_at, updated_at, reply_wanted, conversation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (nid, "agent", "test", "m", "normal", "[]", "delivered", created, created, int(reply),
                   conv or (nid if reply else None)))


def legacy_db(path) -> Database:
    """A database as 0.6.2 left it: reply conversation ntf_a with an unclaimed reply, plain ntf_b, no table."""
    db = Database(path)
    add_notification(db, "ntf_a", reply=True)
    add_notification(db, "ntf_b")
    with db.tx() as c:
        c.execute("INSERT INTO deliveries(id, notification_id, device, provider, state, max_attempts,"
                  " observable_states_json, created_at, updated_at)"
                  " VALUES ('dlv_a','ntf_a','alex-email','mail','dispatched',3,'[]',?,?)", (NEW, NEW))
        c.execute("INSERT INTO replies(id, conversation_id, notification_id, delivery_id, message_id, from_address,"
                  " received_at, text, state) VALUES ('rpl_a','ntf_a','ntf_a','dlv_a','<a@x>','alex@example.org',?,"
                  "'hi','unclaimed')", (NEW,))
        c.execute("DROP TABLE conversations")
    db.close()
    return Database(path)


def test_backfill_marks_existing_reply_conversations_legacy(tmp_path):
    db = legacy_db(tmp_path / "n.sqlite3")
    rows = db.query("SELECT * FROM conversations")
    assert [(r["id"], r["legacy"], r["handle_sha256"]) for r in rows] == [("ntf_a", 1, None)]
    assert db.one("SELECT late_ok FROM replies WHERE id='rpl_a'")["late_ok"] == 0
    db.close()


def test_backfill_runs_once(tmp_path):
    path = tmp_path / "n.sqlite3"
    db = legacy_db(path)
    before = dict(db.one("SELECT * FROM conversations WHERE id='ntf_a'"))
    add_notification(db, "ntf_d", reply=True)
    db.close()
    db = Database(path)
    assert db.one("SELECT 1 FROM conversations WHERE id='ntf_d'") is None
    assert dict(db.one("SELECT * FROM conversations WHERE id='ntf_a'")) == before
    db.close()


def test_retention_keeps_conversation_while_followups_live(tmp_path):
    db = Database(tmp_path / "n.sqlite3")
    svc = NotifyService(make_config(retention_days=90), db, {})
    add_notification(db, "ntf_a", reply=True, created=OLD)
    add_notification(db, "ntf_c", conv="ntf_a")
    with db.tx() as c:
        c.execute("INSERT INTO conversations(id, legacy, created_at) VALUES ('ntf_a', 0, ?)", (OLD,))
    svc.retention_sweep()
    assert db.one("SELECT 1 FROM notifications WHERE id='ntf_a'") is None
    assert db.one("SELECT 1 FROM conversations WHERE id='ntf_a'") is not None
    with db.tx() as c:
        c.execute("UPDATE notifications SET created_at=? WHERE id='ntf_c'", (OLD,))
    svc.retention_sweep()
    assert db.one("SELECT 1 FROM conversations WHERE id='ntf_a'") is None
    db.close()


def test_retention_prunes_old_mailbox_events_only(tmp_path):
    db = Database(tmp_path / "n.sqlite3")
    svc = NotifyService(make_config(retention_days=90), db, {})
    with db.tx() as c:
        for typ, created in (("mailbox.cleanup", OLD), ("mailbox.settings", OLD), ("mailbox.cleanup", NEW),
                             ("other.event", OLD)):
            c.execute("INSERT INTO events(type, notification_id, client_id, source, data_json, created_at)"
                      " VALUES (?, NULL, 'x', 'hnl-notify', '{}', ?)", (typ, created))
    svc.retention_sweep()
    rows = [(r["type"], r["created_at"]) for r in db.query("SELECT type, created_at FROM events ORDER BY id")]
    assert rows == [("mailbox.cleanup", NEW), ("other.event", OLD)]
    db.close()
