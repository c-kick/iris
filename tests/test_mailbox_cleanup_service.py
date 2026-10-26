"""Mailbox cleanup as a service: one lock per mailbox, the daily worker, the size alert and the admin API."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta

import httpx

import notify.providers.email_inbound as inbound
from notify.configops import commit_config
from notify.mailbox_cleanup import MIB
from tests.conftest import wait_for
from tests.mailfactory import FakeMailbox
from tests.test_admin import H, _session_client
from tests.test_mailbox_cleanup import NOW, FakeServer


def fill(srv, folder="Processed", n=3, days_old=40, size=10, start=1):
    for i in range(start, start + n):
        srv.cleanup_server.add(folder, i, days_old, size)


def cfg(srv, **section):
    srv.call(lambda: commit_config(srv.app, lambda d: d["mailbox_cleanup"].__setitem__("mail", section)))


def poll(srv, box=None):
    p = srv.service.providers["mail"]
    return srv.call(lambda: inbound.poll_once(srv.service, p, box or srv.mailbox))


def settle(workers, n=2):
    """Wait until the cleanup worker has finished n full passes after now (no fixed sleeps)."""
    for _ in range(n):
        h = workers.heartbeat.get("cleanup")
        wait_for(lambda: workers.heartbeat.get("cleanup") != h, timeout=15)


def in_thread(fn):
    out = {}
    t = threading.Thread(target=lambda: out.update(r=fn()))
    t.start()
    return t, out


def alerts(srv):
    return srv.service.db.query("SELECT * FROM notifications WHERE tags_json LIKE '%mailbox-cleanup%'")


class BlockingBox(FakeMailbox):
    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.entered = threading.Event()

    def open(self):
        self.entered.set()
        self.gate.wait(15)
        super().open()


# ----------------------------------------------------------------------------------------------- locking

def test_poll_and_cleanup_never_overlap(email_live):
    fill(email_live)
    gate = email_live.cleanup_server.gate = threading.Event()
    email_live.cleanup_server.block_on = "FETCH"
    with _session_client(email_live) as a:
        t, out = in_thread(lambda: a.post("/admin/api/mailbox/mail/cleanup", headers=H, timeout=30))
        wait_for(lambda: email_live.cleanup_server.sessions)
        time.sleep(0.3)
        done = []
        pt, _ = in_thread(lambda: (poll(email_live), done.append(time.monotonic())))
        time.sleep(0.5)
        assert not done  # the poll waits for the cleanup step
        released = time.monotonic()
        gate.set()
        pt.join(10)
        t.join(20)
    assert done and done[0] >= released and out["r"].status_code == 200


def test_cleanup_waits_for_blocked_poll(email_live):
    fill(email_live)
    box = BlockingBox()
    email_live.service.open_mailbox = lambda p: box
    pt, _ = in_thread(lambda: poll(email_live, box))
    assert box.entered.wait(5)
    with _session_client(email_live) as a:
        t, out = in_thread(lambda: a.post("/admin/api/mailbox/mail/cleanup", headers=H, timeout=30))
        time.sleep(0.6)
        assert "r" not in out
        box.gate.set()
        pt.join(10)
        t.join(20)
    assert out["r"].status_code == 200 and out["r"].json()["folders"]["Processed"]["removed_age"] == 3


def test_manual_cleanup_busy_409(email_live):
    email_live.service.cleanup_busy_wait = 0.3
    lock = email_live.call(lambda: email_live.service.mailbox_lock("mail"))
    email_live.call(lambda: lock.acquire())
    try:
        with _session_client(email_live) as a:
            r = a.post("/admin/api/mailbox/mail/cleanup", headers=H)
        assert r.status_code == 409 and "mailbox busy, try again" in r.text
    finally:
        email_live.call(lambda: lock.release())


def test_cancel_keeps_lock_until_thread_returns(email_live):
    fill(email_live, n=1)
    email_live.cleanup_server.gate = threading.Event()
    email_live.cleanup_server.block_on = "STORE"
    svc = email_live.service

    async def go():
        task = asyncio.ensure_future(svc.run_mailbox_cleanup("mail", dry_run=False, by="t", deadline_seconds=None))
        lock = svc.mailbox_lock("mail")
        for _ in range(100):
            await asyncio.sleep(0.05)
            if email_live.cleanup_server.sessions and email_live.cleanup_server.sessions[0].selected == "Processed":
                break
        await asyncio.sleep(0.2)  # now inside the blocked batch
        task.cancel()
        await asyncio.sleep(0.3)
        held = lock.locked()
        email_live.cleanup_server.gate.set()
        await asyncio.wait({task})
        return held, lock.locked()
    held, after = email_live.call(go)
    assert held is True and after is False


# ----------------------------------------------------------------------------------------------- admin API

def test_cache_invalidated_after_put_and_post(email_live):
    fill(email_live)
    with _session_client(email_live) as a:
        first = a.get("/admin/api/mailbox").json()["providers"][0]
        assert first["settings"]["processed_days"] == 30 and first["preview"]["folders"]["Processed"]["removed_age"] == 3
        body = {**first["settings"], "processed_days": 0}
        assert a.put("/admin/api/mailbox/mail", json=body, headers=H).status_code == 200
        second = a.get("/admin/api/mailbox").json()["providers"][0]
        assert second["settings"]["processed_days"] == 0 and second["preview"]["folders"]["Processed"]["removed_age"] == 0
        assert a.post("/admin/api/mailbox/mail/cleanup", headers=H).status_code == 200
        third = a.get("/admin/api/mailbox").json()["providers"][0]
        assert third["status"]["last_by"] == "admin-ui"


def test_put_and_post_need_session(email_live):
    fill(email_live)
    body = {"enabled": True, "processed_days": 10}
    with email_live.client("admin") as b:
        assert b.get("/admin/api/mailbox").status_code == 200
        assert b.put("/admin/api/mailbox/mail", json=body).status_code == 403
        assert b.post("/admin/api/mailbox/mail/cleanup").status_code == 403
    with _session_client(email_live) as a:
        assert a.get("/admin/api/mailbox").status_code == 200
        assert a.put("/admin/api/mailbox/mail", json=body, headers=H).status_code == 200
        assert a.post("/admin/api/mailbox/mail/cleanup", headers=H).status_code == 200
        assert a.put("/admin/api/mailbox/nope", json=body, headers=H).status_code == 404
        assert a.post("/admin/api/mailbox/nope/cleanup", headers=H).status_code == 404
    assert len(email_live.cleanup_server.mail["Processed"]) == 0


def test_cleanup_event_names_client(email_live):
    fill(email_live)
    with _session_client(email_live) as a:
        a.get("/admin/api/mailbox")  # a preview writes no event
        assert a.post("/admin/api/mailbox/mail/cleanup", headers=H).status_code == 200
    rows = email_live.service.db.query("SELECT * FROM events WHERE type='mailbox.cleanup'")
    assert len(rows) == 1 and rows[0]["client_id"] == "admin-ui" and rows[0]["notification_id"] is None
    import json
    data = json.loads(rows[0]["data_json"])
    assert rows[0]["source"] == "hnl-notify" and data["provider"] == "mail" and data["dry_run"] is False
    assert data["removed"]["Processed"]["removed_age"] == 3


def test_refused_provider_does_nothing(email_live):
    fill(email_live)
    srv = email_live.service
    srv.providers["mail"].reply_processed = "INBOX"
    out = email_live.call(lambda: srv.run_mailbox_cleanup("mail", dry_run=False, by="t", deadline_seconds=None))
    assert out["refused"] and not any("STORE" in c or "EXPUNGE" in c for _, _, c in email_live.cleanup_server.log)
    assert len(email_live.cleanup_server.mail["Processed"]) == 3


def test_open_failure_is_recorded_masked(email_live):
    email_live.cleanup_server.login_error = "denied for PW"
    out = email_live.call(lambda: email_live.service.run_mailbox_cleanup("mail", dry_run=False, by="t",
                                                                          deadline_seconds=None))
    assert out["error"] and "imap-secret" not in out["error"] and "***" in out["error"]
    st = email_live.service.cleanup_status["mail"]
    assert st["last_error"] == out["error"] and st["last_success"] is None


# ----------------------------------------------------------------------------------------------- alert

def over_cap(srv):
    fill(srv, n=3, days_old=1, size=MIB)
    cfg(srv, processed_days=0, processed_max_mib=1)


def run_real(srv):
    return srv.call(lambda: srv.service.run_mailbox_cleanup("mail", dry_run=False, by="t", deadline_seconds=None))


def test_cap_hit_sends_one_alert_per_day(email_live):
    over_cap(email_live)
    run_real(email_live)
    fill(email_live, n=2, days_old=1, size=MIB, start=10)
    run_real(email_live)
    rows = alerts(email_live)
    assert len(rows) == 1 and rows[0]["title"] == "Reply mailbox: Processed over its size cap"
    assert rows[0]["severity"] == "normal" and "went over its 1 MiB cap" in rows[0]["message"]
    assert rows[0]["idempotency_key"].startswith("mailbox-cleanup:mail:Processed:")


def test_alert_links_to_the_mailbox_page(email_live):
    def ext(d):
        d["server"]["external_url"] = "http://notify.lan:8930/"
    email_live.call(lambda: commit_config(email_live.app, ext))
    over_cap(email_live)
    run_real(email_live)
    row = alerts(email_live)[0]
    assert row["url"] == "http://notify.lan:8930/admin#mailbox" and row["url_title"] == "Open the reply mailbox"


def test_alert_targets_override(email_live):
    over_cap(email_live)
    cfg(email_live, processed_days=0, processed_max_mib=1, alert_targets=["device:alex-email"])
    run_real(email_live)
    assert "alex-email" in alerts(email_live)[0]["targets_json"]


def test_no_targets_no_alert_and_warning(email_live):
    def strip(d):
        d["users"] = {}
        for dev in d["devices"].values():
            dev["owner"] = None
        d["groups"]["household"]["members"] = ["device:alex-phone"]
    email_live.call(lambda: commit_config(email_live.app, strip))
    over_cap(email_live)
    run_real(email_live)
    assert alerts(email_live) == []
    with email_live.client("admin") as b:
        prov = b.get("/admin/api/mailbox").json()["providers"][0]
    assert prov["alert_warning"]


# ----------------------------------------------------------------------------------------------- worker

def test_disabled_skips_automatic_but_manual_works(email_live):
    fill(email_live)
    cfg(email_live, enabled=False)
    w = email_live.app.state.workers
    w.cleanup_check_seconds, w.cleanup_start_delay = 0.05, 0.0
    settle(w)
    assert len(email_live.cleanup_server.mail["Processed"]) == 3
    with _session_client(email_live) as a:
        assert a.post("/admin/api/mailbox/mail/cleanup", headers=H).status_code == 200
    assert len(email_live.cleanup_server.mail["Processed"]) == 0


def test_automatic_run_once_a_day_with_backoff(email_live):
    fill(email_live)
    w = email_live.app.state.workers
    now = [1000.0]
    w.clock = lambda: now[0]
    w.cleanup_check_seconds, w.cleanup_start_delay = 0.05, 100.0
    w.cleanup_interval, w.cleanup_backoff_max = 1000.0, 400.0
    runs = lambda: email_live.service.cleanup_status.get("mail", {}).get("last_run")
    settle(w)
    assert runs() is None  # before the start delay
    now[0] = 1101.0
    wait_for(lambda: runs(), timeout=10)
    first = runs()
    assert len(email_live.cleanup_server.mail["Processed"]) == 0
    now[0] = 1500.0
    settle(w)
    assert runs() == first  # not again before the interval
    email_live.cleanup_server.login_error = "nope"
    now[0] = 2200.0
    wait_for(lambda: email_live.service.cleanup_status["mail"]["last_error"], timeout=10)
    failed = runs()
    now[0] = 2300.0
    settle(w)
    assert runs() == failed  # first back-off is 600 s, capped at 400: not yet due at +100
    now[0] = 2700.0
    wait_for(lambda: runs() != failed, timeout=10)


def test_failing_cleanup_does_not_stop_polling(email_live):
    email_live.cleanup_server.login_error = "nope"
    w = email_live.app.state.workers
    w.cleanup_check_seconds, w.cleanup_start_delay = 0.05, 0.0
    w.inbound_interval = 0.1
    wait_for(lambda: email_live.service.cleanup_status.get("mail", {}).get("last_error"))
    wait_for(lambda: email_live.service.inbound_status.get("mail", {}).get("last_success"))


def test_settings_event_names_client_and_events_do_not_fan_out(email_live):
    with email_live.service.db.tx() as c:
        c.execute("INSERT INTO subscriptions(id, client_id, url, secret, events_json, created_at)"
                  " VALUES ('sub_x', 'admin', 'https://hooks.example.org/x', 's', '[\"*\"]', '2026-01-01T00:00:00Z')")
    fill(email_live)
    body = {"enabled": True, "processed_days": 10, "alert_targets": []}
    with _session_client(email_live) as a:
        assert a.put("/admin/api/mailbox/mail", json=body, headers=H).status_code == 200
        assert a.post("/admin/api/mailbox/mail/cleanup", headers=H).status_code == 200
    import json
    row = email_live.service.db.one("SELECT * FROM events WHERE type='mailbox.settings'")
    data = json.loads(row["data_json"])
    assert row["client_id"] == "admin-ui" and row["notification_id"] is None
    assert data["provider"] == "mail" and data["settings"]["processed_days"] == 10
    assert email_live.service.db.scalar("SELECT COUNT(*) FROM events WHERE type LIKE 'mailbox.%'") == 2
    assert email_live.service.db.scalar("SELECT COUNT(*) FROM webhook_deliveries") == 0


def test_poll_cancel_keeps_lock_until_thread_returns(email_live):
    box = BlockingBox()
    svc = email_live.service
    provider = svc.providers["mail"]

    async def go():
        task = asyncio.ensure_future(inbound.poll_once(svc, provider, box))
        lock = svc.mailbox_lock("mail")
        await asyncio.sleep(0.3)
        assert box.entered.is_set()
        task.cancel()
        await asyncio.sleep(0.3)
        held = lock.locked()
        box.gate.set()
        await asyncio.wait({task})
        return held, lock.locked()
    held, after = email_live.call(go)
    assert held is True and after is False


def test_unexpected_worker_exception_backs_off(email_live):
    svc, w = email_live.service, email_live.app.state.workers
    calls = []

    async def boom(name, **kw):
        calls.append(w.clock())
        raise RuntimeError("bug with imap-secret")
    svc.run_mailbox_cleanup = boom
    now = [1000.0]
    w.clock = lambda: now[0]
    w.cleanup_check_seconds, w.cleanup_start_delay = 0.05, 0.0
    wait_for(lambda: calls)
    settle(w)
    assert len(calls) == 1  # no retry on every pass
    st = svc.cleanup_status["mail"]
    assert "RuntimeError" in st["last_error"] and "imap-secret" not in st["last_error"]
    now[0] = 1700.0  # past the first 600 s back-off
    wait_for(lambda: len(calls) == 2)


def test_run_locked_creates_call_under_the_lock():
    from notify.providers.email_inbound import run_locked

    async def go():
        lock, calls = asyncio.Lock(), []

        async def work():
            calls.append(1)
            return 5
        await lock.acquire()
        queued = asyncio.ensure_future(run_locked(lock, work))
        await asyncio.sleep(0.05)
        queued.cancel()
        try:
            await queued
        except asyncio.CancelledError:
            pass
        lock.release()
        assert calls == []  # the call was never created for a caller that never got the lock
        assert await run_locked(lock, work) == 5
    asyncio.run(go())


def test_run_locked_retrieves_exception_after_cancel():
    import gc
    from notify.providers.email_inbound import run_locked
    seen = []

    async def go():
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: seen.append(ctx))
        lock = asyncio.Lock()

        async def boom():
            await asyncio.sleep(0.2)
            raise RuntimeError("late failure")
        t = asyncio.ensure_future(run_locked(lock, boom))
        await asyncio.sleep(0.05)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        assert not lock.locked()
    asyncio.run(go())
    gc.collect()
    assert seen == []
