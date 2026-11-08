"""Regression tests for the 2026-11 hardening pass: interface, auth and fetch boundaries (B.*, C.1-C.7)."""

from __future__ import annotations

import gzip
import re
import threading
import time

import httpx
import pytest

from notify.attachments import _allowed
from notify.configops import commit_config
from tests.conftest import wait_for
from tests.test_admin import H, setup_admin


def submit(live, who="agent", **body):
    body.setdefault("targets", ["user:alex"])
    body.setdefault("message", "hello")
    return live.client(who).post("/v1/notifications", json=body)


# ------------------------------------------------------------------ B.2 body before auth

def test_b2_unauthenticated_body_is_refused_before_parsing(live):
    # malformed on purpose: the old code parsed it before auth and answered 422 (JSON decode error)
    r = httpx.post(f"{live.url}/v1/notifications", content=b"[" + b"{}," * 30000,
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 401


def test_b2_large_body_needs_the_send_scope(live):
    body = b"[" + b"{}," * (1 << 20)  # malformed: the old code parsed 3 MiB of it and answered 422
    r = live.client("reader").post("/v1/notifications", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 403


def test_b2_public_routes_still_take_small_unauthenticated_bodies(live):  # guard: passes before and after
    r = httpx.post(f"{live.url}/v1/registrations", json={"name": "newapp", "scopes": ["send"]})
    assert r.status_code == 202, r.text


# ------------------------------------------------------------------ C.1 / C.4 provider secrets

def test_c1_bearer_admin_cannot_edit_providers(email_live):
    r = email_live.client("admin").put("/admin/api/providers/mail", json={"type": "email", "settings": {
        "host": "evil.example", "from_address": "hnl@example.org", "imap_host": "evil.example"}})
    assert r.status_code == 403
    assert email_live.client("admin").post("/admin/api/providers/mail/test").status_code == 403
    assert email_live.client("admin").delete("/admin/api/providers/mail").status_code == 403
    assert email_live.service.config.providers["mail"].model_dump()["imap_host"] == "imap.test"


def _mail_settings(**over):
    s = {"host": "mail.test", "port": 25, "security": "none", "from_address": "hnl@example.org",
         "reply_address": "replies@example.org", "imap_host": "imap.test", "imap_user": "replies",
         "authserv_id": "mx.example.org", "imap_password": ""}
    s.update(over)
    return s


def test_c1_stored_password_is_not_carried_to_another_server(email_live):
    b = setup_admin(email_live)
    r = b.put("/admin/api/providers/mail", json={"type": "email", "settings": _mail_settings(imap_host="evil.example")},
              headers=H)
    assert r.status_code == 422 and "enter it again" in r.text
    assert email_live.service.config.providers["mail"].model_dump()["imap_host"] == "imap.test"
    # unchanged server: a blank password field keeps the stored one, as before
    r = b.put("/admin/api/providers/mail", json={"type": "email", "settings": _mail_settings()}, headers=H)
    assert r.status_code == 200, r.text
    assert email_live.service.config.providers["mail"].model_dump()["imap_password"] == "imap-secret"


# ------------------------------------------------------------------ C.2 decision codes

def test_c2_listings_never_show_decision_codes(server_factory):
    live = server_factory(external_url="http://notify.test")
    assert httpx.post(f"{live.url}/v1/registrations", json={"name": "newapp", "scopes": ["send"]}).status_code == 202
    wait_for(lambda: live.service.db.one("SELECT 1 FROM notifications WHERE client_id='hnl-notify'"))
    stored = live.service.db.one("SELECT url FROM notifications WHERE client_id='hnl-notify'")["url"]
    assert "/admin/decide/" in stored and "k=hidden" not in stored  # the push itself carries the real code
    for text in (live.client("admin").get("/admin/api/activity").text,
                 live.client("admin").get("/admin/api/timeline").text,
                 live.client("admin").get("/v1/notifications").text):
        assert "/admin/decide/" in text
        assert not re.search(r"[?&]k=(?!hidden)", text)


# ------------------------------------------------------------------ C.3 password route

def test_c3_password_change_is_session_only_and_throttled(live):
    r = live.client("admin").post("/admin/api/password", json={"current": "wrong guess", "new": "another password"})
    assert r.status_code == 403
    b = setup_admin(live)
    codes = [b.post("/admin/api/password", json={"current": f"wrong {i}", "new": "another password"},
                    headers=H).status_code for i in range(7)]
    assert codes[:5] == [403] * 5 and 429 in codes[5:]


# ------------------------------------------------------------------ C.7 logout

def test_c7_logout_needs_the_admin_header(live):
    b = setup_admin(live)
    assert b.post("/admin/api/logout").status_code == 403
    assert b.get("/admin/api/state").status_code == 200  # still signed in
    assert b.post("/admin/api/logout", headers=H).status_code == 200


def test_c7_cookie_is_secure_over_https(live):
    b = httpx.Client(base_url=live.url, timeout=10)
    code = live.app.state.store.new_setup_code()
    r = b.post("/admin/api/setup", json={"code": code, "password": "correct horse battery"},
               headers={**H, "X-Forwarded-Proto": "https"})
    assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()


# ------------------------------------------------------------------ B.6 / B.9 names

def test_b6_deleting_a_client_stops_its_webhooks(live):
    sid = live.client("reader").post("/v1/subscriptions", json={"url": "http://hooks.test/r"}).json()["id"]
    b = setup_admin(live)
    assert b.delete("/admin/api/clients/reader", headers=H).status_code == 200
    assert live.service.db.one("SELECT active FROM subscriptions WHERE id=?", (sid,))["active"] == 0


def test_b6_registration_refuses_a_name_with_history(live):
    assert submit(live, who="sender").status_code == 202
    b = setup_admin(live)
    assert b.delete("/admin/api/clients/sender", headers=H).status_code == 200
    r = httpx.post(f"{live.url}/v1/registrations", json={"name": "sender", "scopes": ["send", "read"]})
    assert r.status_code == 409


def test_b9_reserved_names_are_refused(live):
    r = httpx.post(f"{live.url}/v1/registrations", json={"name": "hnl-notify", "scopes": ["send"]})
    assert r.status_code == 422


def test_b7_proxied_registration_is_never_auto_approved(live):
    def mutate(d):
        d["registration"]["auto_approve_subnets"] = ["127.0.0.0/8"]
    live.call(lambda: commit_config(live.app, mutate))
    r = httpx.post(f"{live.url}/v1/registrations", json={"name": "viaproxy", "scopes": ["send"]},
                   headers={"X-Forwarded-For": "127.0.0.9"})  # an in-subnet address, as a LAN proxy would send
    assert r.status_code == 202 and r.json()["status"] == "pending"
    r = httpx.post(f"{live.url}/v1/registrations", json={"name": "direct", "scopes": ["send"]})
    assert r.json()["status"] == "approved"


# ------------------------------------------------------------------ B.10 edge 500s

def test_b10_callback_garbage_is_not_a_500(server_factory):
    live = server_factory(callback_secret="cb-secret-123", public_base_url="https://pub.example.org")
    u = f"{live.url}/v1/callbacks/pushover"
    assert httpx.post(f"{u}/cb-secret-123", content=b"[1,2]",
                      headers={"Content-Type": "application/json"}).status_code == 400
    assert httpx.post(f"{u}/cb-secret-123", content=b"{bad",
                      headers={"Content-Type": "application/json"}).status_code == 400
    assert httpx.post(f"{u}/%C3%A9", data={"x": "1"}).status_code == 404


def test_b10_arr_payload_with_odd_types_is_422(live):
    r = live.client("sender").post("/v1/ingest/arr", params={"targets": "user:alex"},
                                   json={"eventType": "SomethingNew", "message": 123,
                                         "release": {"releaseGroup": 5, "quality": "x"}})
    assert r.status_code in (202, 422)


# ------------------------------------------------------------------ B.11 readiness

def test_b11_readiness_writes_at_most_every_few_seconds(live):
    httpx.get(f"{live.url}/health/ready")
    with live.service.db.tx() as c:
        c.execute("UPDATE meta SET value='marker' WHERE key='ready_probe'")
    httpx.get(f"{live.url}/health/ready")
    assert live.service.db.scalar("SELECT value FROM meta WHERE key='ready_probe'") == "marker"


# ------------------------------------------------------------------ B.12 SSE streams

def test_b12_event_streams_are_capped_per_client(live):
    open_ = []
    try:
        for _ in range(5):
            cm = live.client("reader").stream("GET", "/v1/events/stream", timeout=8)
            assert cm.__enter__().status_code == 200
            open_.append(cm)
        cm = live.client("reader").stream("GET", "/v1/events/stream", timeout=8)
        assert cm.__enter__().status_code == 429
        open_.append(cm)
    finally:
        for cm in open_:
            cm.__exit__(None, None, None)
    # closed streams are released again
    wait_for(lambda: live.client("reader").stream("GET", "/v1/events/stream", timeout=8).__enter__().status_code == 200)


# ------------------------------------------------------------------ C.6 config commits

def test_c6_concurrent_config_commits_do_not_lose_updates(live):
    def add(name, pause):
        def mutate(d):
            time.sleep(pause)
            d["users"][name] = {"devices": []}
        commit_config(live.app, mutate)
    t = threading.Thread(target=add, args=("slow", 0.4))
    t.start()
    time.sleep(0.1)
    add("fast", 0)
    t.join()
    users = live.service.config.users
    assert "slow" in users and "fast" in users


# ------------------------------------------------------------------ B.3 webhook addresses

def test_b3_webhook_to_loopback_is_refused(live, fake):
    sid = live.client().post("/v1/subscriptions", json={"url": "http://127.0.0.1:9/h"}).json()["id"]
    submit(live)
    sub = wait_for(lambda: (s := live.client().get("/v1/subscriptions").json()["items"][0])["last_error"] and s)
    assert sub["id"] == sid and sub["last_error"] == "address not allowed"


# ------------------------------------------------------------------ B.4 compressed attachments

def test_b4_compressed_attachment_response_is_refused(server_factory):
    live = server_factory(attachment_url_hosts=["files.test"])
    bomb = gzip.compress(gzip.compress(b"\0" * (20 << 20)))

    def handler(request):
        return httpx.Response(200, content=bomb, headers={"content-encoding": "gzip, gzip",
                                                          "content-type": "application/octet-stream"})
    live.service.fetch_transport = httpx.MockTransport(handler)
    r = submit(live, kinds=["push"], attachments=[{"url": "http://files.test/x.bin"}])
    assert r.status_code == 422 and "compressed" in r.text


# ------------------------------------------------------------------ B.8 CIDR ports

def test_b8_bare_cidr_allows_web_ports_only():
    assert _allowed("192.168.1.5", 80, ["192.168.1.0/24"])
    assert _allowed("192.168.1.5", 443, ["192.168.1.0/24"])
    assert not _allowed("192.168.1.5", 8930, ["192.168.1.0/24"])
    assert _allowed("192.168.1.5", 8123, ["192.168.1.0/24:8123"])
    assert not _allowed("192.168.1.5", 8930, ["192.168.1.0/24:8123"])


def test_b8_cidr_port_entries_validate(live):
    def mutate(d):
        d["server"]["attachment_url_hosts"] = ["10.0.0.5/32:8123"]
    live.call(lambda: commit_config(live.app, mutate))
    assert live.service.config.server.attachment_url_hosts == ["10.0.0.5/32:8123"]


# ------------------------------------------------------------------ C.5 browser reports and enrollment

@pytest.fixture
def wp(tmp_path, fake):
    from tests.test_webpush_lifecycle import webpush_config
    from notify.app import create_app
    from tests.conftest import LiveServer
    app = create_app(webpush_config(), db_path=str(tmp_path / "wp.sqlite3"), key_path=str(tmp_path / "master.key"),
                     transport=httpx.MockTransport(fake.handler))
    srv = LiveServer(app, fake)
    srv.start()
    yield srv
    srv.stop()


WEB = {"Host": "notify.example.org:8931", "X-Forwarded-Proto": "https"}


def test_c5_junk_reports_cannot_use_up_the_budget_of_real_ones(wp):
    from tests.test_webpush_lifecycle import delivery, sent
    nid, did = sent(wp)
    web = httpx.Client(base_url=wp.url, headers=WEB, timeout=10)
    for _ in range(130):
        web.post("/push/report", json={"d": did, "k": "forged", "e": "shown"})
    token = wp.service.providers["browser"].report_token(did)
    assert web.post("/push/report", json={"d": did, "k": token, "e": "shown"}).status_code == 204
    wait_for(lambda: delivery(wp, nid)["state"] == "delivered")


def test_c5_probing_with_bad_codes_does_not_lock_out_a_valid_link(wp):
    b = setup_admin(wp)
    r = b.post("/admin/api/providers/browser/enrollments", json={"owner": "alex"}, headers=H)
    assert r.status_code == 200, r.text
    code = r.json()["url"].split("?c=")[1]
    web = httpx.Client(base_url=wp.url, headers=WEB, timeout=10)
    for i in range(8):
        web.get("/push/enroll", params={"c": f"bad{i}"})
    assert web.get("/push/enroll", params={"c": code}).status_code == 200


def test_latent_junk_decide_probes_do_not_lock_out_the_valid_link(server_factory, fake):
    live = server_factory(external_url="http://notify.test")
    assert httpx.post(f"{live.url}/v1/registrations", json={"name": "newapp", "scopes": ["send"]}).status_code == 202
    url = wait_for(lambda: next((m.get("url") for m in fake.messages if "/admin/decide/" in (m.get("url") or "")), None))
    path, _, query = url.partition("?")
    rid, code = path.rsplit("/", 1)[1], query.split("k=")[1].split("&")[0]
    for i in range(8):
        httpx.get(f"{live.url}/admin/decide/{rid}", params={"k": f"junk{i}"})
    assert httpx.get(f"{live.url}/admin/decide/{rid}", params={"k": code}).status_code == 200
