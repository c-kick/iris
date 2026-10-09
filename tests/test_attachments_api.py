from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from typing import Any

from notify.core import Client, NotifyService
from notify.db import Database, iso, parse_iso, utcnow
from notify.models import NotificationIn, Scope
from notify.providers.base import DeliveryPlan, OutboundMessage, Provider, SendResult
from tests.conftest import PNG, make_config, wait_for
from tests.test_mcp import run_session

PNG_B64 = base64.b64encode(PNG).decode()


def get(live, nid):
    return live.client().get(f"/v1/notifications/{nid}").json()


def submit(live, **body):
    body.setdefault("targets", ["user:alex"])
    body.setdefault("message", "hello")
    return live.client().post("/v1/notifications", json=body)


def inline(name="door.png", data=PNG_B64, ct="image/png"):
    return {"filename": name, "content_type": ct, "data_base64": data}


def count(live):
    return len(live.client().get("/v1/notifications").json()["items"])


def age(live, nid, days=8):
    with live.service.db.tx() as c:
        c.execute("UPDATE notifications SET created_at=? WHERE id=?", (iso(utcnow() - timedelta(days=days)), nid))


def test_inline_attachment_is_stored(live):
    r = submit(live, attachments=[inline()])
    assert r.status_code == 202, r.text
    n = get(live, r.json()["id"])
    assert n["attachments"] == [{"filename": "door.png", "content_type": "image/png", "size": len(PNG),
                                 "sha256": hashlib.sha256(PNG).hexdigest(), "source": "inline", "purged": False}]
    assert r.json()["attachments"][0]["filename"] == "door.png"
    row = live.service.db.one("SELECT * FROM attachments WHERE notification_id=?", (n["id"],))
    assert bytes(row["data"]) == PNG and row["idx"] == 0 and row["source_host"] is None


def test_url_attachment_requires_allowlist(live, fake):
    r = submit(live, attachments=[{"url": "http://files.test/snap/door.png"}])
    assert r.status_code == 422 and "not enabled" in r.text
    assert fake.file_requests == []


def test_url_attachment_fetched_from_allowed_host(server_factory, fake):
    live = server_factory(attachment_url_hosts=["files.test"])
    r = submit(live, attachments=[{"url": "http://files.test/snap/door.png?token=secret"}])
    assert r.status_code == 202, r.text
    a = get(live, r.json()["id"])["attachments"][0]
    assert a["source"] == "url" and a["filename"] == "door.png" and a["content_type"] == "image/png"
    assert a["size"] == len(PNG)
    row = live.service.db.one("SELECT * FROM attachments")
    assert row["source_host"] == "files.test" and bytes(row["data"]) == PNG
    assert "secret" not in str(dict(row))
    assert fake.file_requests == ["/snap/door.png"]


def test_url_fetch_error_stores_nothing(server_factory, fake):
    live = server_factory(attachment_url_hosts=["files.test"])
    before = count(live)
    r = submit(live, attachments=[{"url": "http://files.test/missing"}])
    assert r.status_code == 422 and "HTTP 404" in r.text
    assert count(live) == before
    assert live.service.db.scalar("SELECT COUNT(*) FROM attachments") == 0


def test_idempotent_replay_does_not_fetch_again(server_factory, fake):
    live = server_factory(attachment_url_hosts=["files.test"])
    body = {"attachments": [{"url": "http://files.test/a.png"}], "idempotency_key": "k1"}
    first = submit(live, **body)
    second = submit(live, **body)
    assert first.status_code == 202 and second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert fake.file_requests == ["/a.png"]


def test_dry_run_with_url_does_not_fetch(server_factory, fake):
    live = server_factory(attachment_url_hosts=["files.test"])
    r = submit(live, attachments=[{"url": "http://files.test/a.png"}], dry_run=True)
    assert r.status_code == 200 and r.json()["dry_run"] is True
    assert fake.file_requests == []
    r = submit(live, attachments=[{"url": "http://other.test/a.png"}], dry_run=True)
    assert r.status_code == 422 and "not in the allowed list" in r.text
    r = submit(live, attachments=[inline(data="!!!")], dry_run=True)
    assert r.status_code == 422 and "not valid base64" in r.text


def test_suppressed_notification_does_not_fetch(server_factory, fake):
    live = server_factory(attachment_url_hosts=["files.test"])
    r = submit(live, targets=["channel:infra"], severity="normal",
               attachments=[{"url": "http://files.test/a.png"}, inline()])
    assert r.status_code == 202, r.text
    n = get(live, r.json()["id"])
    assert n["status"] == "suppressed" and fake.file_requests == []
    assert [a["source"] for a in n["attachments"]] == ["url", "inline"]
    assert live.service.db.scalar("SELECT COUNT(*) FROM attachments WHERE data IS NOT NULL") == 0


def test_no_attachments_key_without_attachments(live):
    r = submit(live)
    assert "attachments" not in r.json()
    assert "attachments" not in get(live, r.json()["id"])
    assert "attachments" not in live.client().get("/v1/notifications").json()["items"][0]


def test_retention_purges_attachment_data(live):
    nid = submit(live, attachments=[inline()]).json()["id"]
    wait_for(lambda: get(live, nid)["status"] == "dispatched")
    age(live, nid)
    assert live.service.retention_sweep() == 0
    row = live.service.db.one("SELECT data, purged_at FROM attachments WHERE notification_id=?", (nid,))
    assert row["data"] is None and row["purged_at"]
    assert get(live, nid)["attachments"][0]["purged"] is True


def test_retention_keeps_data_while_delivery_pending(live):
    nid = submit(live, attachments=[inline()], targets=["device:alex-phone"]).json()["id"]
    with live.service.db.tx() as c:  # park it in the queue
        c.execute("UPDATE deliveries SET state='queued', next_attempt_at='2999-01-01T00:00:00.000000Z',"
                  " finalized_at=NULL WHERE notification_id=?", (nid,))
    age(live, nid)
    live.service.retention_sweep()
    assert live.service.db.scalar("SELECT data IS NOT NULL FROM attachments WHERE notification_id=?", (nid,)) == 1


def test_retry_after_purge_reports_gap(live, fake):
    fake.responses.append((400, {"status": 0, "errors": ["x"]}))
    nid = submit(live, attachments=[inline()]).json()["id"]
    wait_for(lambda: get(live, nid)["status"] == "failed")
    age(live, nid)
    live.service.retention_sweep()
    r = live.client().post(f"/v1/notifications/{nid}/retry")
    assert r.status_code == 200, r.text
    d = get(live, nid)["deliveries"][0]
    assert "attachments purged after 7 days: 1 not sent" in d["capability_gaps"]


def test_mcp_inline_limit(live):
    big = base64.b64encode(b"\0" * (300 * 1024)).decode()

    async def flow(s):
        return await s.call_tool("send_notification", {
            "targets": ["user:alex"], "message": "x",
            "attachments": [{"filename": "a.bin", "data_base64": big}]})
    res = run_session(live, "agent", flow)
    assert res.isError and "256 KiB" in res.content[0].text


def test_mcp_small_inline_attachment(live):
    async def flow(s):
        return await s.call_tool("send_notification", {
            "targets": ["user:alex"], "message": "x", "attachments": [inline()]})
    res = run_session(live, "agent", flow)
    assert not res.isError, res.content
    assert live.service.db.scalar("SELECT COUNT(*) FROM attachments") == 1


class SlowProvider(Provider):
    type = "slow"
    kind = "push"
    send_deadline_seconds = 120

    def config_problems(self) -> list[str]:
        return []

    def capabilities(self) -> dict[str, Any]:
        return {}

    def plan(self, msg: OutboundMessage, address: dict[str, Any]) -> DeliveryPlan:
        return DeliveryPlan(tracking="none", observable_states=[])

    async def send(self, msg: OutboundMessage, address: dict[str, Any]) -> SendResult:
        return SendResult(outcome="success")


def test_claim_due_lease_covers_send_deadline(tmp_path):
    db = Database(tmp_path / "x.sqlite3")
    svc = NotifyService(make_config(), db, {"pushover": SlowProvider("pushover", {}, None)})  # type: ignore[arg-type]
    svc.submit(Client("agent", frozenset({Scope.send})), NotificationIn(targets=["user:alex"], message="x"))
    (d,) = svc.claim_due("pushover", 5)
    assert parse_iso(d["lease_until"]) >= utcnow() + timedelta(seconds=149)
    db.close()


def test_sync_submit_rejects_url_attachments(tmp_path):
    db = Database(tmp_path / "x.sqlite3")
    svc = NotifyService(make_config(attachment_url_hosts=["files.test"]), db,
                        {"pushover": SlowProvider("pushover", {}, None)})  # type: ignore[arg-type]
    req = NotificationIn(targets=["user:alex"], message="x", attachments=[{"url": "http://files.test/a.png"}])
    try:
        svc.submit(Client("agent", frozenset({Scope.send})), req)
    except Exception as e:
        assert type(e).__name__ == "Invalid"
    else:
        raise AssertionError("expected Invalid")
    db.close()


def test_replay_answered_before_attachment_checks(server_factory, fake):
    live = server_factory(attachment_url_hosts=["files.test"])
    body = {"attachments": [{"url": "http://files.test/a.png"}], "idempotency_key": "k2"}
    first = submit(live, **body)
    assert first.status_code == 202, first.text
    live.service.config.server.attachment_url_hosts = []
    again = submit(live, **body)
    assert again.status_code == 200, again.text
    assert again.headers["Idempotent-Replay"] == "true" and again.json()["id"] == first.json()["id"]
    assert fake.file_requests == ["/a.png"]


def slow_prepare(monkeypatch, before=None):
    import asyncio

    from notify import attachments
    real = attachments.prepare

    async def prepare(*a, **kw):
        if before:
            before()
        await asyncio.sleep(1.2)
        return await real(*a, **kw)
    monkeypatch.setattr(attachments, "prepare", prepare)


def test_expiry_rechecked_after_fetch(server_factory, monkeypatch):
    live = server_factory(attachment_url_hosts=["files.test"])
    slow_prepare(monkeypatch)
    r = submit(live, ttl_seconds=1, attachments=[{"url": "http://files.test/a.png"}])
    assert r.status_code == 422 and "expired while attachments were fetched" in r.text
    assert live.service.db.scalar("SELECT COUNT(*) FROM notifications") == 0


def test_device_removed_during_fetch(server_factory, monkeypatch):
    live = server_factory(attachment_url_hosts=["files.test"])

    def drop_device():
        cfg = live.service.config.model_copy(deep=True)
        del cfg.devices["alex-phone"]
        live.service.config = cfg
    slow_prepare(monkeypatch, drop_device)
    r = submit(live, attachments=[{"url": "http://files.test/a.png"}])
    assert r.status_code == 422 and "configuration changed" in r.text
