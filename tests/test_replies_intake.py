from __future__ import annotations

from notify.db import Database


def test_reply_fields_and_conversation(live):
    with live.client() as c:
        r = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "pick", "reply": True})
        assert r.status_code == 202, r.text
        first = r.json()
        assert first["reply"] is True and first["conversation_id"] == first["id"] and first["in_reply_to"] is None
        r = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "next", "in_reply_to": first["id"]})
        assert r.status_code == 202, r.text
        nxt = r.json()
        assert nxt["conversation_id"] == first["id"] and nxt["in_reply_to"] == first["id"] and nxt["reply"] is False
        r = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "third", "in_reply_to": nxt["id"]})
        assert r.json()["conversation_id"] == first["id"]


def test_plain_notification_shape_unchanged(live):
    with live.client() as c:
        out = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "hi"}).json()
    assert not {"reply", "in_reply_to", "conversation_id"} & set(out)


def test_in_reply_to_other_client_or_unknown(live):
    with live.client("other") as o:
        theirs = o.post("/v1/notifications", json={"targets": ["user:alex"], "message": "x"}).json()["id"]
    with live.client() as c:
        for ref in (theirs, "ntf_doesnotexist"):
            r = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "y", "in_reply_to": ref})
            assert r.status_code == 422 and "in_reply_to" in r.text


def test_system_client_may_follow_up_any_notification(live):
    from notify.core import SYSTEM_CLIENT
    from notify.models import NotificationIn
    with live.client("other") as o:
        theirs = o.post("/v1/notifications", json={"targets": ["user:alex"], "message": "x"}).json()["id"]
    req = NotificationIn(targets=["user:alex"], message="auto", in_reply_to=theirs)
    req._auto_submitted = True
    out, _ = live.call(lambda: live.service.submit(SYSTEM_CLIENT, req))
    row = live.service.db.one("SELECT auto_submitted, conversation_id FROM notifications WHERE id=?", (out["id"],))
    assert row["auto_submitted"] == 1 and row["conversation_id"] == theirs


def test_migration_adds_columns_to_old_database(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE notifications (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, idempotency_key TEXT,"
                      " source TEXT NOT NULL, title TEXT, message TEXT NOT NULL, severity TEXT NOT NULL, url TEXT,"
                      " url_title TEXT, actions_json TEXT NOT NULL DEFAULT '[]', data_json TEXT,"
                      " tags_json TEXT NOT NULL DEFAULT '[]', targets_json TEXT NOT NULL,"
                      " require_ack INTEGER NOT NULL DEFAULT 0, expires_at TEXT, status TEXT NOT NULL,"
                      " routing_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);")
    con.close()
    db = Database(path)
    cols = {r[1] for r in db.query("PRAGMA table_info(notifications)")}
    assert {"reply_wanted", "in_reply_to", "conversation_id", "listener_seen_at", "auto_submitted"} <= cols
    assert "recipient_address" in {r[1] for r in db.query("PRAGMA table_info(deliveries)")}
    db.close()
