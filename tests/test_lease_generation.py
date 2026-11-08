"""0.13.2: a send run whose lease was recovered never finishes the delivery of a later run, even after a manual
retry reset the attempt count (the fence is a lease number that never goes back)."""

from __future__ import annotations

from notify.core import Client, NotifyService
from notify.db import Database, iso, utcnow
from notify.models import NotificationIn, Scope
from notify.providers.base import SendResult

from tests.conftest import make_config
from tests.test_attachments_api import SlowProvider


def test_stale_run_cannot_finish_a_retried_delivery(tmp_path):
    db = Database(tmp_path / "x.sqlite3")
    svc = NotifyService(make_config(), db, {"pushover": SlowProvider("pushover", {}, None)})  # type: ignore[arg-type]
    client = Client("agent", frozenset({Scope.send, Scope.read, Scope.manage}))
    n, _ = svc.submit(client, NotificationIn(targets=["user:alex"], message="x"))
    nid = n["id"]
    db.conn.execute("UPDATE deliveries SET max_attempts=1")
    (stale,) = svc.claim_due("pushover", 5)  # run A: attempt 1
    db.conn.execute("UPDATE deliveries SET lease_until=?", (iso(utcnow()),))
    svc.recover_leases()  # A's lease expired on its only attempt: failed
    assert db.one("SELECT state FROM deliveries")["state"] == "failed"
    svc.retry(client, nid)  # attempts back to 0
    (fresh,) = svc.claim_due("pushover", 5)  # run B: attempt 1 again
    assert fresh["attempts"] == stale["attempts"]
    svc.complete_send(stale, SendResult(outcome="success"), iso(utcnow()))  # A finishes late
    d = db.one("SELECT state, lease_no FROM deliveries")
    assert d["state"] == "sending"  # B still owns it
    assert d["lease_no"] == fresh["lease_no"] > stale["lease_no"]
    svc.complete_send(fresh, SendResult(outcome="success"), iso(utcnow()))
    assert db.one("SELECT state FROM deliveries")["state"] == "dispatched"
    db.close()
