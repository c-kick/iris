"""Management UI backend: first-run setup, session security, live configuration."""

from __future__ import annotations

import os
import stat

import httpx
import pytest

from notify import __version__
from notify.app import create_app
from tests.conftest import APP_TOKEN, USER_KEY, LiveServer, wait_for

H = {"X-Notify-Admin": "1"}


@pytest.fixture
def fresh(tmp_path, fake, monkeypatch):
    """A server with an empty database and no seed config, like a first deployment."""
    monkeypatch.setenv("NOTIFY_CONFIG", str(tmp_path / "absent.yaml"))
    servers = []

    def make():
        app = create_app(db_path=str(tmp_path / "n.sqlite3"), key_path=str(tmp_path / "secrets" / "master.key"),
                         transport=httpx.MockTransport(fake.handler))
        srv = LiveServer(app, fake)
        srv.start()
        servers.append(srv)
        return srv

    yield make
    for s in servers:
        s.stop()


def browser(srv) -> httpx.Client:
    return httpx.Client(base_url=srv.url, timeout=10)


def setup_admin(srv, password="correct horse battery") -> httpx.Client:
    b = browser(srv)
    code = srv.app.state.store.new_setup_code()
    r = b.post("/admin/api/setup", json={"code": code, "password": password}, headers=H)
    assert r.status_code == 200, r.text
    return b


def test_first_run_setup_and_login(fresh):
    srv = fresh()
    b = browser(srv)
    assert b.get("/admin/api/session").json() == {"setup_required": True, "signed_in": False, "version": __version__}
    ready = b.get("/health/ready").json()
    assert "setup required" in ready["checks"]["admin"] and ready["status"] == "not_ready"
    assert b.get("/admin/api/state").status_code == 401
    assert b.post("/admin/api/setup", json={"code": "nope", "password": "long enough pw"}, headers=H).status_code == 403
    code = srv.app.state.store.new_setup_code()
    assert b.post("/admin/api/setup", json={"code": code, "password": "short"}, headers=H).status_code == 422
    r = b.post("/admin/api/setup", json={"code": code, "password": "correct horse battery"}, headers=H)
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    assert b.get("/admin/api/state").status_code == 200
    # the code is single-use
    assert b.post("/admin/api/setup", json={"code": code, "password": "another password"}, headers=H).status_code == 409
    b.post("/admin/api/logout", headers=H)
    assert b.get("/admin/api/state").status_code == 401
    assert b.post("/admin/api/login", json={"password": "wrong"}, headers=H).status_code == 403
    assert b.post("/admin/api/login", json={"password": "correct horse battery"}, headers=H).status_code == 200
    assert b.get("/admin/api/state").status_code == 200


def test_browser_mutations_need_csrf_header_and_same_origin(fresh):
    srv = fresh()
    b = setup_admin(srv)
    body = {"type": "pushover", "settings": {"app_token": APP_TOKEN}}
    assert b.put("/admin/api/providers/pushover", json=body).status_code == 403
    assert b.put("/admin/api/providers/pushover", json=body,
                 headers={**H, "Origin": "http://evil.example"}).status_code == 403
    assert b.put("/admin/api/providers/pushover", json=body,
                 headers={**H, "Origin": srv.url}).status_code == 200


def test_login_throttled(fresh):
    srv = fresh()
    b = setup_admin(srv)
    b.post("/admin/api/logout", headers=H)
    codes = [b.post("/admin/api/login", json={"password": "x"}, headers=H).status_code for _ in range(6)]
    assert codes[:5] == [403] * 5 and codes[5] == 429


def test_configure_everything_from_the_ui_then_send(fresh, fake, tmp_path):
    srv = fresh()
    b = setup_admin(srv)
    st = b.put("/admin/api/providers/pushover", json={"type": "pushover", "settings": {
        "app_token": APP_TOKEN, "emergency_retry_seconds": "90"}}, headers=H).json()
    assert st["config"]["providers"]["pushover"]["app_token"] == {"set": True, "hint": "…aaaa"}
    assert st["config"]["providers"]["pushover"]["emergency_retry_seconds"] == 90
    # device lookup through Pushover's validate endpoint
    chk = b.post("/admin/api/providers/pushover/check", json={"address": {"user_key": USER_KEY}}, headers=H).json()
    assert chk == {"supported": True, "valid": True, "devices": ["iphone", "ipad"], "is_group": False}
    bad = b.post("/admin/api/providers/pushover/check", json={"address": {"user_key": "b" * 30}}, headers=H).json()
    assert bad["valid"] is False and "invalid" in bad["error"]
    b.put("/admin/api/users/alex", json={"display_name": "Alex"}, headers=H)
    st = b.put("/admin/api/devices/alex-phone", json={
        "provider": "pushover", "owner": "alex", "description": "Alex's phone",
        "address": {"user_key": USER_KEY, "device": "iphone"}}, headers=H).json()
    assert st["config"]["users"]["alex"]["devices"] == ["alex-phone"]
    assert st["devices"]["alex-phone"]["ready"] is True
    b.put("/admin/api/groups/household", json={"members": ["user:alex"]}, headers=H)
    b.put("/admin/api/channels/security", json={"targets": ["group:household"], "min_severity": "high"}, headers=H)
    created = b.post("/admin/api/clients/home-assistant", json={"scopes": ["send", "read"]}, headers=H).json()
    token = created["token"]
    assert "token_sha256" not in created["config"]["clients"]["home-assistant"]
    assert httpx.get(f"{srv.url}/health/ready").status_code == 200

    # the new token works immediately (no restart) for REST
    api = httpx.Client(base_url=srv.url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    n = api.post("/v1/notifications", json={"targets": ["channel:security"], "message": "Door open",
                                            "severity": "high"}).json()
    wait_for(lambda: api.get(f"/v1/notifications/{n['id']}").json()["status"] == "dispatched")
    assert fake.messages[-1]["user"] == USER_KEY and fake.messages[-1]["device"] == "iphone"

    # nothing secret ever leaves through the admin API or sits in the database in plaintext
    state_text = b.get("/admin/api/state").text
    assert USER_KEY not in state_text and APP_TOKEN not in state_text and token not in state_text
    raw = srv.service.db.scalar("SELECT value FROM settings WHERE key='config'")
    assert USER_KEY not in raw and APP_TOKEN not in raw and "$enc" in raw
    key = tmp_path / "secrets" / "master.key"
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600

    # a blank secret on edit keeps the stored value
    b.put("/admin/api/devices/alex-phone", json={"provider": "pushover", "owner": "alex",
                                                   "description": "iPhone 16", "address": {"device": "iphone"}},
          headers=H)
    assert srv.app.state.config.devices["alex-phone"].address["user_key"] == USER_KEY

    # replacing a token cuts off the old one at once
    new = b.post("/admin/api/clients/home-assistant/rotate", headers=H).json()["token"]
    assert api.get("/v1/whoami").status_code == 401
    assert httpx.get(f"{srv.url}/v1/whoami", headers={"Authorization": f"Bearer {new}"}).status_code == 200

    # test send + activity
    t = b.post("/admin/api/test", json={"targets": ["user:alex"]}, headers=H).json()
    assert t["source"] == "admin-ui"
    wait_for(lambda: any(x["id"] == t["id"] for x in b.get("/admin/api/activity").json()["notifications"]))

    # removing a device cleans up references everywhere
    st = b.delete("/admin/api/devices/alex-phone", headers=H).json()
    assert st["config"]["users"]["alex"]["devices"] == []
    # a provider still used by a device cannot be removed
    b.put("/admin/api/devices/spare", json={"provider": "pushover", "address": {"user_key": USER_KEY}}, headers=H)
    r = b.delete("/admin/api/providers/pushover", headers=H)
    assert r.status_code == 422 and "spare" in r.json()["detail"]


def test_configuration_survives_restart_without_yaml(fresh, fake):
    srv = fresh()
    b = setup_admin(srv)
    b.put("/admin/api/providers/pushover", json={"type": "pushover", "settings": {"app_token": APP_TOKEN}}, headers=H)
    b.put("/admin/api/devices/phone", json={"provider": "pushover", "address": {"user_key": USER_KEY}}, headers=H)
    srv.stop()
    srv2 = fresh()
    assert srv2.app.state.config.devices["phone"].address["user_key"] == USER_KEY
    assert srv2.app.state.config.providers["pushover"].app_token == APP_TOKEN  # type: ignore[attr-defined]
    assert browser(srv2).get("/admin/api/session").json()["setup_required"] is False


def test_bearer_admin_token_and_scope_check(fresh):
    srv = fresh()
    b = setup_admin(srv)
    admin_tok = b.post("/admin/api/clients/ops", json={"scopes": ["admin"]}, headers=H).json()["token"]
    send_tok = b.post("/admin/api/clients/sender", json={"scopes": ["send"]}, headers=H).json()["token"]
    ok = httpx.get(f"{srv.url}/admin/api/state", headers={"Authorization": f"Bearer {admin_tok}"})
    assert ok.status_code == 200
    no = httpx.get(f"{srv.url}/admin/api/state", headers={"Authorization": f"Bearer {send_tok}"})
    assert no.status_code == 403


def test_reset_admin_and_page_headers(fresh):
    srv = fresh()
    b = setup_admin(srv)
    code = srv.app.state.store.reset_admin()
    assert b.get("/admin/api/state").status_code == 401  # sessions revoked
    assert b.get("/admin/api/session").json()["setup_required"] is True
    assert b.post("/admin/api/setup", json={"code": code, "password": "a new password"}, headers=H).status_code == 200
    page = httpx.get(f"{srv.url}/admin")
    assert page.status_code == 200 and "app.js" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert httpx.get(f"{srv.url}/admin/static/app.js").status_code == 200


def _email_provider(b, **over):
    settings = {"host": "smtp.example.org", "port": "2525", "security": "starttls", "password": "hunter2-secret-pw",
                "from_address": "n@example.org", "verify_tls": False, **over}
    return b.put("/admin/api/providers/mail", json={"type": "email", "settings": settings}, headers=H)


def test_email_provider_settings(fresh):
    b = setup_admin(fresh())
    st = _email_provider(b).json()
    mail = st["config"]["providers"]["mail"]
    assert mail["password"]["set"] is True and "hunter2-secret-pw" not in b.get("/admin/api/state").text
    assert mail["port"] == 2525 and mail["verify_tls"] is False
    assert st["providers"]["mail"]["kind"] == "email" and st["provider_types"]["email"]["kind"] == "email"
    st = _email_provider(b, password="").json()  # blank secret keeps the stored one
    assert st["config"]["providers"]["mail"]["password"]["set"] is True
    r = _email_provider(b, security="bogus")
    assert r.status_code == 422 and "Encryption" in r.json()["detail"]


def test_email_device_opt_in_and_server_attachment_settings(fresh):
    b = setup_admin(fresh())
    _email_provider(b)
    body = {"provider": "mail", "address": {"to": "alex@example.org"}}
    st = b.put("/admin/api/devices/alex-email", json={**body, "opt_in": False}, headers=H).json()
    assert st["devices"]["alex-email"]["opt_in"] is False and st["devices"]["alex-email"]["kind"] == "email"
    st = b.put("/admin/api/devices/alex-email", json=body, headers=H).json()
    assert st["devices"]["alex-email"]["opt_in"] is True
    st = b.put("/admin/api/server", json={"attachment_url_hosts": ["ha.lan:8123"], "attachment_retention_days": 3},
               headers=H).json()
    assert st["config"]["server"]["attachment_url_hosts"] == ["ha.lan:8123"]
    assert st["config"]["server"]["attachment_retention_days"] == 3
    assert b.put("/admin/api/server", json={"attachment_retention_days": 0}, headers=H).status_code == 422


def test_provider_test_connection(fresh, monkeypatch):
    from notify.providers.email import EmailProvider
    monkeypatch.setattr(EmailProvider, "_test_sync", lambda self: {"supported": True, "ok": True, "tls": "starttls",
                                                                  "size_limit": None})
    b = setup_admin(fresh())
    _email_provider(b)
    b.put("/admin/api/providers/pushover", json={"type": "pushover", "settings": {"app_token": APP_TOKEN}}, headers=H)
    r = b.post("/admin/api/providers/mail/test", headers=H)
    assert r.status_code == 200 and r.json() == {"supported": True, "ok": True, "tls": "starttls", "size_limit": None}
    assert b.post("/admin/api/providers/pushover/test", headers=H).json() == {"supported": False}
    assert b.post("/admin/api/providers/nope/test", headers=H).status_code == 404


def test_provider_setting_validation_and_test_endpoint_needs_admin(fresh):
    srv = fresh()
    b = setup_admin(srv)
    r = _email_provider(b, verify_tls="false")
    assert r.status_code == 422 and "must be true or false" in r.json()["detail"]
    assert _email_provider(b, port="abc").status_code == 422
    _email_provider(b)
    tok = b.post("/admin/api/clients/sender", json={"scopes": ["send"]}, headers=H).json()["token"]
    r = httpx.post(f"{srv.url}/admin/api/providers/mail/test", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403
    assert httpx.post(f"{srv.url}/admin/api/providers/mail/test").status_code == 401


def test_test_send_passes_kinds(fresh):
    b = setup_admin(fresh())
    _email_provider(b)
    b.put("/admin/api/devices/alex-email", json={"provider": "mail", "address": {"to": "alex@example.org"}}, headers=H)
    b.put("/admin/api/users/alex", json={"devices": ["alex-email"]}, headers=H)
    t = b.post("/admin/api/test", json={"targets": ["user:alex"], "kinds": ["email"]}, headers=H).json()
    assert [d["device"] for d in t["deliveries"]] == ["alex-email"]
    t = b.post("/admin/api/test", json={"targets": ["user:alex"]}, headers=H).json()
    assert t["deliveries"] == []
    assert b.post("/admin/api/test", json={"targets": ["user:alex"], "kinds": ["sms"]}, headers=H).status_code == 422


def test_provider_type_change_blocked_while_devices_use_it(fresh):
    b = setup_admin(fresh())
    _email_provider(b)
    b.put("/admin/api/devices/alex-email", json={"provider": "mail", "address": {"to": "alex@example.org"}}, headers=H)
    r = b.put("/admin/api/providers/mail", json={"type": "pushover", "settings": {"app_token": APP_TOKEN}}, headers=H)
    assert r.status_code == 422 and "alex-email" in r.json()["detail"]
    assert _email_provider(b).status_code == 200  # same type is fine


def test_validation_errors_do_not_echo_large_input(live):
    with live.client() as c:
        r = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "m",
                                              "attachments": [{"data_base64": "A" * (1 << 20)}]})
        assert r.status_code == 422 and len(r.content) < 10240, len(r.content)
        d = r.json()["detail"]
        assert d and all({"loc", "msg", "type"} <= set(e) for e in d)


def _session_client(srv) -> httpx.Client:
    """A browser with a valid signed-in admin cookie."""
    sid = srv.app.state.store.new_session()
    return httpx.Client(base_url=srv.url, cookies={"notify_admin": sid}, timeout=10)


def _fill_replies(srv) -> None:
    from tests.mailfactory import reply_raw
    from tests.test_replies_core import deliver, send
    _, did = send(srv)
    deliver(srv, reply_raw(did, attachment=b"\x89PNG\r\n\x1a\n" + b"0" * 32))
    deliver(srv, reply_raw(did, message_id="<m@x>", sender="mallory@example.org"))


def test_admin_replies_view(email_live):
    _fill_replies(email_live)
    with _session_client(email_live) as a:
        r = a.get("/admin/api/replies")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replies"][0]["text"] == "Go with B."
    assert body["quarantine"][0]["reason"] == "sender_mismatch"
    with email_live.client("agent") as c:
        assert c.get("/admin/api/replies").status_code in (401, 403)


def test_bearer_admin_sees_no_reply_content(email_live):
    _fill_replies(email_live)
    with email_live.client("admin") as a:
        r = a.get("/admin/api/replies")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replies"] and body["quarantine"]
    for item in body["replies"]:
        assert item["text"] is None and item["subject"] is None
        assert item["attachments"] == [] and item["dropped_attachments"] == []
        assert item["state"] and item["received_at"]
    for q in body["quarantine"]:
        assert q["from_address"] is None and q["subject"] is None and q["message_id"] is None
        assert q["reason"] and q["received_at"]
    with _session_client(email_live) as a:
        full = a.get("/admin/api/replies").json()
    assert full["replies"][0]["text"] == "Go with B." and full["replies"][0]["attachments"]
    assert full["quarantine"][0]["from_address"]


def test_require_session():
    from fastapi import HTTPException
    from notify.admin import UI_CLIENT, require_session
    from notify.core import SYSTEM_CLIENT
    assert UI_CLIENT.via_session is True and SYSTEM_CLIENT.via_session is False
    require_session(UI_CLIENT)
    with pytest.raises(HTTPException) as e:
        require_session(SYSTEM_CLIENT)
    assert e.value.status_code == 403
