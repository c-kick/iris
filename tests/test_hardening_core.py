"""Regression tests for the 2026-11 hardening pass: core delivery pipeline (review findings A.*, B.1)."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from notify.core import Conflict
from notify.db import Database, now_iso
from notify.providers.base import SendResult
from tests.conftest import wait_for


def submit(live, who="agent", **body):
    body.setdefault("targets", ["user:alex"])
    body.setdefault("message", "hello")
    return live.client(who).post("/v1/notifications", json=body)


def delivery(live, nid):
    return live.service.db.one("SELECT * FROM deliveries WHERE notification_id=?", (nid,))


def gate_sends(live):
    """Hold every Pushover send until the returned event is set (the send is 'in flight' meanwhile)."""
    prov = live.service.providers["pushover"]
    orig = prov.send
    gate = threading.Event()

    async def held(msg, address):
        while not gate.is_set():
            await asyncio.sleep(0.02)
        return await orig(msg, address)
    prov.send = held
    return gate


# ------------------------------------------------------------------ A.1 cancel while sending

def test_a1_cancel_during_send_recalls_the_emergency_alert(live, fake):
    gate = gate_sends(live)
    nid = submit(live, severity="critical").json()["id"]
    wait_for(lambda: delivery(live, nid)["state"] == "sending")
    r = live.client().post(f"/v1/notifications/{nid}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["result"] == "cancel_requested"
    gate.set()
    wait_for(lambda: fake.cancelled)  # the receipt was cancelled at Pushover: the phone stops ringing
    wait_for(lambda: delivery(live, nid)["state"] == "cancelled")


def test_a1_cancel_during_a_failing_send_is_never_retried(live, fake):
    gate = gate_sends(live)
    fake.responses.append((503, {"status": 0, "errors": ["busy"]}))
    nid = submit(live).json()["id"]
    wait_for(lambda: delivery(live, nid)["state"] == "sending")
    assert live.client().post(f"/v1/notifications/{nid}/cancel").status_code == 200
    gate.set()
    wait_for(lambda: delivery(live, nid)["state"] == "cancelled")
    live.fast_forward()
    live.service.wake.set()
    import time
    time.sleep(0.5)
    assert len(fake.messages) == 1  # the 503 attempt only; no second send of a cancelled message


def test_a1_queued_delivery_with_a_cancel_flag_is_cancelled_at_claim(live, fake):
    gate = gate_sends(live)
    nid = submit(live).json()["id"]
    wait_for(lambda: delivery(live, nid)["state"] == "sending")
    with live.service.db.tx() as c:  # as if a cancel had landed during an attempt that was then recovered
        c.execute("UPDATE deliveries SET state='queued', cancel_requested=1, lease_until=NULL,"
                  " next_attempt_at='2000-01-01T00:00:00.000000Z' WHERE notification_id=?", (nid,))
    live.service.wake.set()
    wait_for(lambda: delivery(live, nid)["state"] == "cancelled")
    gate.set()
    import time
    time.sleep(0.3)
    assert delivery(live, nid)["state"] == "cancelled"


# ------------------------------------------------------------------ A.3 lease fencing

def test_a3_result_of_a_superseded_claim_is_ignored(live, fake):
    gate = gate_sends(live)
    nid = submit(live).json()["id"]
    wait_for(lambda: delivery(live, nid)["state"] == "sending")
    old = delivery(live, nid)
    with live.service.db.tx() as c:  # the lease was recovered and the delivery claimed again
        c.execute("UPDATE deliveries SET attempts=attempts+1, lease_no=lease_no+1 WHERE id=?", (old["id"],))
    live.call(lambda: live.service.complete_send(old, SendResult(outcome="success", provider_message_id="stale"),
                                                  now_iso()))
    d = delivery(live, nid)
    assert d["state"] == "sending" and d["provider_message_id"] is None
    gate.set()


def test_a3_send_is_bounded_inside_the_lease(live, fake):
    prov = live.service.providers["pushover"]

    async def hang(msg, address):
        await asyncio.sleep(3600)
    prov.send = hang
    live.service.send_timeout = lambda provider: 0.3
    nid = submit(live).json()["id"]
    wait_for(lambda: live.service.db.one("SELECT * FROM attempts WHERE delivery_id=?", (delivery(live, nid)["id"],)))
    a = live.service.db.one("SELECT * FROM attempts WHERE delivery_id=?", (delivery(live, nid)["id"],))
    assert a["outcome"] == "retryable" and "outcome unknown" in a["error"]
    assert delivery(live, nid)["state"] == "queued"


# ------------------------------------------------------------------ A.9 crash loop cap

def test_a9_interrupted_send_at_max_attempts_fails_instead_of_resending(live, fake):
    gate = gate_sends(live)
    nid = submit(live).json()["id"]
    wait_for(lambda: delivery(live, nid)["state"] == "sending")
    with live.service.db.tx() as c:
        c.execute("UPDATE deliveries SET attempts=max_attempts, lease_until='2000-01-01T00:00:00.000000Z'"
                  " WHERE notification_id=?", (nid,))
    live.call(live.service.recover_leases)
    d = delivery(live, nid)
    assert d["state"] == "failed" and "interrupted" in d["last_error"]
    gate.set()


# ------------------------------------------------------------------ A.5 guarded transitions

def test_a5_transition_from_a_stale_row_raises_instead_of_overwriting(live, fake):
    nid = submit(live, severity="critical").json()["id"]
    wait_for(lambda: delivery(live, nid)["state"] == "dispatched")
    stale = delivery(live, nid)
    with live.service.db.tx() as c:
        c.execute("UPDATE deliveries SET state='delivered' WHERE id=?", (stale["id"],))
    with pytest.raises(Conflict):
        with live.service.db.tx() as c:
            live.service._transition(c, stale, "cancelled", {}, {"reason": "test"})
    assert delivery(live, nid)["state"] == "delivered"


# ------------------------------------------------------------------ A.10 rollback on failed COMMIT

class _CommitFails:
    def __init__(self, conn):
        self._conn = conn
        self.fail = True

    def execute(self, sql, *a):
        if sql == "COMMIT" and self.fail:
            self.fail = False
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_a10_failed_commit_leaves_the_connection_usable(tmp_path):
    db = Database(str(tmp_path / "x.sqlite3"))
    real = db.conn
    db.conn = _CommitFails(real)
    with pytest.raises(sqlite3.OperationalError):
        with db.tx() as c:
            c.execute("INSERT INTO meta(key, value) VALUES ('a', '1')")
    with db.tx() as c:  # old code: "cannot start a transaction within a transaction"
        c.execute("INSERT INTO meta(key, value) VALUES ('b', '2')")
    assert db.scalar("SELECT value FROM meta WHERE key='b'") == "2"
    assert db.scalar("SELECT value FROM meta WHERE key='a'") is None


# ------------------------------------------------------------------ A.4 removed provider

def _clone_delivery(live, nid, **changes):
    d = dict(delivery(live, nid))
    d.update(id="dlv_clone" + nid[-6:], state="queued", finalized_at=None, lease_until=None,
             next_attempt_at="2000-01-01T00:00:00.000000Z", **changes)
    cols = ",".join(d)
    with live.service.db.tx() as c:
        c.execute(f"INSERT INTO deliveries({cols}) VALUES ({','.join('?' * len(d))})", tuple(d.values()))
    return d["id"]


def test_a4_queued_delivery_of_a_removed_provider_fails(live, fake):
    nid = submit(live).json()["id"]
    wait_for(lambda: delivery(live, nid)["state"] == "dispatched")
    did = _clone_delivery(live, nid, provider="gone")
    live.service.wake.set()
    wait_for(lambda: live.service.db.one("SELECT state FROM deliveries WHERE id=?", (did,))["state"] == "failed")
    assert "no longer configured" in live.service.db.one("SELECT last_error FROM deliveries WHERE id=?",
                                                         (did,))["last_error"]


# ------------------------------------------------------------------ A.8 abandoned receipts

def test_a8_receipt_of_a_removed_provider_is_abandoned(live, fake):
    nid = submit(live, severity="critical").json()["id"]
    wait_for(lambda: delivery(live, nid)["state"] == "dispatched")
    with live.service.db.tx() as c:
        c.execute("UPDATE deliveries SET provider='gone', next_poll_at='2000-01-01T00:00:00.000000Z'"
                  " WHERE notification_id=?", (nid,))
    d = wait_for(lambda: (x := delivery(live, nid))["state"] == "expired" and x)
    assert d["finalized_at"] and "abandoned" in d["last_error"]


def test_a8_receipt_poll_give_up_finalizes_the_delivery(live, fake):
    nid = submit(live, severity="critical").json()["id"]
    d = wait_for(lambda: (x := delivery(live, nid))["state"] == "dispatched" and x)
    fake.receipts.pop(d["receipt"])  # Pushover answers 404 from now on
    with live.service.db.tx() as c:
        c.execute("UPDATE deliveries SET poll_errors=4 WHERE id=?", (d["id"],))
    live.fast_forward()
    d = wait_for(lambda: (x := delivery(live, nid))["state"] == "expired" and x)
    assert d["finalized_at"]


# ------------------------------------------------------------------ A.7 attachments not loaded for web push

def test_a7_adapter_without_attachments_gets_no_bytes(live, fake):
    from notify.providers.webpush import WebPushProvider
    assert WebPushProvider.attachment_data == "none"
    import base64
    nid = submit(live, attachments=[{"filename": "a.bin", "content_type": "application/octet-stream",
                                     "data_base64": base64.b64encode(b"x" * 1000).decode()}]).json()["id"]
    n = live.service.db.one("SELECT * FROM notifications WHERE id=?", (nid,))
    prov = live.service.providers["pushover"]
    prov.attachment_data = "none"
    try:
        msg = live.service.message_for(n, "dlv_x", "pushover", with_data=True)
    finally:
        del prov.attachment_data
    assert msg.attachments and all(a.data is None for a in msg.attachments)


# ------------------------------------------------------------------ A.2 events cursor

def test_a2_event_type_filter_still_moves_the_cursor(live, fake):
    for _ in range(6):
        submit(live)
    wait_for(lambda: len(fake.messages) >= 6)
    r = live.client().get("/v1/events", params={"types": "delivery.acknowledged", "limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == [] and body["cursor"] > 0


# ------------------------------------------------------------------ A.11 list filters

def test_a11_since_with_an_offset_is_compared_as_a_time(live, fake):
    nid = submit(live).json()["id"]
    wait_for(lambda: delivery(live, nid)["state"] == "dispatched")
    earlier = (datetime.now(timezone.utc) - timedelta(minutes=30)).astimezone(timezone(timedelta(hours=1)))
    r = live.client().get("/v1/notifications", params={"since": earlier.isoformat()})
    assert r.status_code == 200 and [n["id"] for n in r.json()["items"]] == [nid]


def test_a11_bad_since_and_unknown_before_are_422(live, fake):
    c = live.client()
    assert c.get("/v1/notifications", params={"since": "yesterday"}).status_code == 422
    assert c.get("/v1/notifications", params={"before": "ntf_doesnotexist"}).status_code == 422


# ------------------------------------------------------------------ A.12 / B.5 subscriptions

def test_b5_subscriptions_are_capped_per_client(live, fake):
    c = live.client("reader")
    for i in range(20):
        assert c.post("/v1/subscriptions", json={"url": f"http://hooks.test/{i}"}).status_code in (200, 201)
    assert c.post("/v1/subscriptions", json={"url": "http://hooks.test/x"}).status_code == 422


def test_b5_subscriptions_per_notification_are_capped_too(live, fake):
    nid = submit(live).json()["id"]
    c = live.client()
    codes = [c.post("/v1/subscriptions", json={"url": f"http://hooks.test/{i}", "notification_id": nid}).status_code
             for i in range(6)]
    assert codes[:5] == [201] * 5 and codes[5] == 422


def test_a6_short_outage_does_not_disable_a_subscription(server_factory, fake):
    live = server_factory(webhook_disable_after_failures=2)
    fake.webhook_status = 500
    sid = live.client().post("/v1/subscriptions", json={"url": "http://hooks.test/h"}).json()["id"]
    submit(live)
    wait_for(lambda: len(fake.webhooks) >= 3)
    sub = live.service.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))
    assert sub["consecutive_failures"] >= 2 and sub["active"] == 1
    with live.service.db.tx() as c:  # it has now been down for two days
        c.execute("UPDATE subscriptions SET failing_since='2000-01-01T00:00:00.000000Z' WHERE id=?", (sid,))
    submit(live)
    wait_for(lambda: live.service.db.one("SELECT active FROM subscriptions WHERE id=?", (sid,))["active"] == 0)


# ------------------------------------------------------------------ B.1 data cap

def test_b1_data_is_capped(live, fake):
    assert submit(live, data={"x": "y" * 20000}).status_code == 422
    assert submit(live, data={"x": "y" * 1000}).status_code == 202


# ------------------------------------------------------------------ stage 6 latent fixes

def test_latent_interrupted_cancelled_send_ends_cancelled(live, fake):
    gate = gate_sends(live)
    nid = submit(live).json()["id"]
    wait_for(lambda: delivery(live, nid)["state"] == "sending")
    with live.service.db.tx() as c:
        c.execute("UPDATE deliveries SET cancel_requested=1, attempts=max_attempts,"
                  " lease_until='2000-01-01T00:00:00.000000Z' WHERE notification_id=?", (nid,))
    live.call(live.service.recover_leases)
    assert delivery(live, nid)["state"] == "cancelled"
    gate.set()


def test_disabled_subscription_can_be_enabled_again(server_factory, fake):
    """0.13.2: the owner re-enables a disabled subscription; the backlog from the outage is not replayed (catch up
    with GET /v1/events instead)."""
    live = server_factory(webhook_disable_after_failures=1)
    fake.webhook_status = 500
    c = live.client()
    sid = c.post("/v1/subscriptions", json={"url": "http://hooks.test/h"}).json()["id"]
    with live.service.db.tx() as tx:
        tx.execute("UPDATE subscriptions SET failing_since='2000-01-01T00:00:00.000000Z' WHERE id=?", (sid,))
    submit(live)
    wait_for(lambda: live.service.db.one("SELECT active FROM subscriptions WHERE id=?", (sid,))["active"] == 0)
    submit(live)  # never queued for a disabled subscription
    fake.webhook_status = 200
    assert c.post(f"/v1/subscriptions/{sid}/enable").status_code == 200
    sub = live.service.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))
    assert sub["active"] == 1 and sub["consecutive_failures"] == 0 and sub["failing_since"] is None
    assert live.service.db.scalar("SELECT COUNT(*) FROM webhook_deliveries WHERE subscription_id=? AND state='pending'",
                                  (sid,)) == 0
    before = len(fake.webhooks)
    submit(live)
    wait_for(lambda: len(fake.webhooks) > before)
    assert c.post("/v1/subscriptions/sub_nope/enable").status_code == 404


def test_enable_is_owner_only(server_factory, fake):
    live = server_factory()
    sid = live.client().post("/v1/subscriptions", json={"url": "http://hooks.test/h"}).json()["id"]
    assert live.client("other").post(f"/v1/subscriptions/{sid}/enable").status_code == 404
    assert live.client("admin").post(f"/v1/subscriptions/{sid}/enable").status_code == 200
