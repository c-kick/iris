"""Browser surface: enrollment link, host guard, service worker, report endpoint."""

from __future__ import annotations

import threading
from typing import Any

import httpx
import pytest

from notify.app import create_app
from notify.config import NotifyConfig
from tests.conftest import LiveServer, make_config, wait_for
from tests.test_attachments_api import submit
from tests.test_webpush_lifecycle import delivery

EXTERNAL = "https://notify.example.org:8931"
HEADERS = {"Host": "notify.example.org:8931", "X-Forwarded-Proto": "https"}
UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"  # ggignore: RFC 8291 appendix A test vector, not a secret
SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/xyz", "keys": {"p256dh": UA_PUBLIC, "auth": AUTH}}
EDGE = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36 Edg/120.0"


def config(external: str | None = EXTERNAL) -> NotifyConfig:
    d = make_config().model_dump(mode="json", exclude_none=True)
    if external:
        d["server"]["external_url"] = external
    d["providers"]["browser"] = {"type": "webpush", "contact": "mailto:alex@example.org"}
    return NotifyConfig.model_validate(d)


def build(tmp_path, fake, cfg) -> LiveServer:
    app = create_app(cfg, db_path=str(tmp_path / "e.sqlite3"), key_path=str(tmp_path / "master.key"),
                     transport=httpx.MockTransport(fake.handler))
    srv = LiveServer(app, fake)
    srv.start()
    return srv


@pytest.fixture
def wp(tmp_path, fake):
    srv = build(tmp_path, fake, config())
    save_provider(srv)
    yield srv
    srv.stop()


def save_provider(srv):
    r = srv.client("admin").put("/admin/api/providers/browser",
                                json={"type": "webpush", "settings": {"contact": "mailto:alex@example.org"}})
    assert r.status_code == 200, r.text
    return r


def web(srv) -> httpx.Client:
    return httpx.Client(base_url=srv.url, headers=HEADERS, timeout=10)


def link(srv, owner="alex") -> str:
    r = srv.client("admin").post("/admin/api/providers/browser/enrollments", json={"owner": owner})
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.startswith(EXTERNAL + "/push/enroll?c=")
    return url.split("?c=")[1]


def test_enroll_flow_creates_device(wp):
    code = link(wp)
    page = web(wp).get("/push/enroll", params={"c": code})
    assert page.status_code == 200 and "script-src 'self'" in page.headers["content-security-policy"]
    pub = wp.service.config.providers["browser"].model_dump()["vapid_public_key"]
    assert pub in page.text
    r = web(wp).post("/push/enroll", json={"code": code, "subscription": SUB, "user_agent": EDGE})
    assert r.status_code == 201 and r.json() == {"device": "alex-browser"}
    cfg = wp.service.config
    dev = cfg.devices["alex-browser"]
    assert dev.owner == "alex" and dev.description == "Edge on Windows" and dev.provider == "browser"
    assert "alex-browser" in cfg.users["alex"].devices
    assert dev.address["endpoint"] == SUB["endpoint"]
    code2 = link(wp)
    r = web(wp).post("/push/enroll", json={"code": code2, "subscription": SUB, "user_agent": "x"})
    assert r.json() == {"device": "alex-browser-2"}


def test_enroll_code_single_use(wp):
    code = link(wp)
    results: list[int] = []

    def go():
        results.append(web(wp).post("/push/enroll", json={"code": code, "subscription": SUB}).status_code)
    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [201, 410]
    assert [d for d in wp.service.config.devices if d.endswith("browser") or "browser-" in d] == ["alex-browser"]


def test_enroll_rejects_non_push_endpoint(wp):
    code = link(wp)
    bad = {**SUB, "endpoint": "https://192.168.1.10/x"}
    assert web(wp).post("/push/enroll", json={"code": code, "subscription": bad}).status_code == 400
    assert web(wp).post("/push/enroll", json={"code": code, "subscription": {**SUB, "keys": {"p256dh": "AA", "auth": AUTH}}}
                        ).status_code == 400
    assert web(wp).post("/push/enroll", json={"code": code, "subscription": SUB}).status_code == 201  # not burnt


def test_unknown_code_is_gone(wp):
    assert web(wp).post("/push/enroll", json={"code": "nope", "subscription": SUB}).status_code == 410
    assert web(wp).get("/push/enroll", params={"c": "nope"}).status_code == 410


def test_enroll_link_needs_https_external_url(tmp_path, fake):
    srv = build(tmp_path, fake, config(external=None))
    try:
        save_provider(srv)
        r = srv.client("admin").post("/admin/api/providers/browser/enrollments", json={"owner": "alex"})
        assert r.status_code == 409
    finally:
        srv.stop()


def test_host_guard(wp):
    plain = httpx.Client(base_url=wp.url)
    assert plain.get("/push/sw.js").status_code == 404
    assert plain.get("/push/sw.js", headers={"X-Forwarded-Proto": "https"}).status_code == 404  # wrong host
    assert web(wp).get("/push/sw.js", headers={"Host": "notify.example.org:9999"}).status_code == 404
    r = web(wp).get("/push/sw.js")
    assert r.status_code == 200 and r.headers["service-worker-allowed"] == "/push/"
    assert r.headers["cache-control"] == "no-cache"
    assert web(wp).get("/push/enroll.js").status_code == 200


def enrolled_send(wp, **body) -> tuple[str, str]:
    code = link(wp)
    assert web(wp).post("/push/enroll", json={"code": code, "subscription": SUB}).status_code == 201
    r = submit(wp, targets=["device:alex-browser"], **body)
    nid = r.json()["id"]
    wait_for(lambda: delivery(wp, nid)["state"] == "dispatched")
    return nid, delivery(wp, nid)["id"]


def post_report(wp, did, token, kind="shown") -> int:
    return web(wp).post("/push/report", json={"d": did, "k": token, "e": kind}).status_code


def test_report_applies(wp):
    nid, did = enrolled_send(wp)
    token = wp.service.providers["browser"].report_token(did)
    assert post_report(wp, did, token) == 204
    assert delivery(wp, nid)["state"] == "delivered"
    assert post_report(wp, did, token, "clicked") == 204
    assert delivery(wp, nid)["state"] == "acknowledged"


def test_report_forged_or_late_is_ignored(wp):
    nid, did = enrolled_send(wp)
    good = wp.service.providers["browser"].report_token(did)
    assert post_report(wp, did, "x" * 22) == 204
    assert post_report(wp, "dlv_unknown", good) == 204
    assert delivery(wp, nid)["state"] == "dispatched"
    post_report(wp, did, good, "clicked")
    assert post_report(wp, did, good, "shown") == 204
    assert delivery(wp, nid)["state"] == "acknowledged"
    assert web(wp).post("/push/report", content=b"not json").status_code == 204
    assert web(wp).post("/push/report", json={"d": did, "k": good, "e": "bogus"}).status_code == 204


def test_vapid_generated_once(wp):
    first = wp.service.config.providers["browser"].model_dump()
    assert first["vapid_private_key"] and first["vapid_public_key"]
    r = wp.client("admin").put("/admin/api/providers/browser", json={
        "type": "webpush", "settings": {"contact": "mailto:b@example.org", "vapid_public_key": "attacker"}})
    assert r.status_code == 200
    second = wp.service.config.providers["browser"].model_dump()
    assert second["vapid_public_key"] == first["vapid_public_key"]
    assert second["vapid_private_key"] == first["vapid_private_key"] and second["contact"] == "mailto:b@example.org"
    state = wp.client("admin").get("/admin/api/state").text
    assert first["vapid_private_key"] not in state and first["vapid_public_key"] in state
    shown = wp.client("admin").get("/admin/api/state").json()["config"]["providers"]["browser"]
    assert shown["vapid_private_key"] == {"set": True, "hint": "…" + first["vapid_private_key"][-4:]}
    assert shown["vapid_public_key"] == first["vapid_public_key"]


def test_enrolled_keys_are_masked_in_admin_listing(wp):
    code = link(wp)
    assert web(wp).post("/push/enroll", json={"code": code, "subscription": SUB}).status_code == 201
    text = wp.client("admin").get("/admin/api/state").text
    assert UA_PUBLIC not in text and AUTH not in text
    address = wp.client("admin").get("/admin/api/state").json()["config"]["devices"]["alex-browser"]["address"]
    assert address["p256dh"] == {"set": True, "hint": "…" + UA_PUBLIC[-4:]}
    assert address["auth"] == {"set": True, "hint": "…" + AUTH[-4:]}
    assert address["endpoint"] == SUB["endpoint"]


def test_host_guard_miss_is_logged(wp, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="notify.webpush"):
        httpx.get(wp.url + "/push/sw.js")
    assert any("expected" in r.getMessage() and "notify.example.org" in r.getMessage() for r in caplog.records)


def test_host_guard_warnings_are_bounded(wp, caplog, monkeypatch):
    import logging
    from notify import webpush_routes
    monkeypatch.setattr(webpush_routes, "_warned", set())
    with caplog.at_level(logging.WARNING, logger="notify.webpush"):
        for i in range(webpush_routes._WARN_MAX + 20):
            httpx.get(wp.url + "/push/sw.js", headers={"Host": f"h{i}-" + "x" * 500})
    assert len(webpush_routes._warned) == webpush_routes._WARN_MAX
    misses = [r for r in caplog.records if "push request refused" in r.getMessage()]
    assert len(misses) == webpush_routes._WARN_MAX
    assert all(len(r.getMessage()) < 600 for r in misses)


def test_key_is_html_escaped_in_page(wp):
    code = link(wp)
    wp.service.config.providers["browser"].__pydantic_extra__["vapid_public_key"] = '"><script>x</script>'
    page = web(wp).get("/push/enroll", params={"c": code})
    assert "<script>x" not in page.text and "&lt;script&gt;x" in page.text


def flag(srv, device):
    with srv.service.db.tx() as c:
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, 'gone')", (f"device_invalid:{device}",))


def flagged(srv, device):
    return srv.service.db.one("SELECT 1 FROM meta WHERE key=?", (f"device_invalid:{device}",)) is not None


def test_reenrolled_device_does_not_inherit_old_flag(wp):
    code = link(wp)
    assert web(wp).post("/push/enroll", json={"code": code, "subscription": SUB}).status_code == 201
    flag(wp, "alex-browser")
    assert wp.client("admin").delete("/admin/api/devices/alex-browser").status_code == 200
    assert not flagged(wp, "alex-browser")
    flag(wp, "alex-browser")  # a flag left behind some other way is cleared by the new enrollment
    code = link(wp)
    assert web(wp).post("/push/enroll", json={"code": code, "subscription": SUB}).json() == {"device": "alex-browser"}
    assert not flagged(wp, "alex-browser")
    dev = next(d for d in wp.service.targets()["devices"] if d["target"] == "device:alex-browser")
    assert dev["ready"] is True


def test_deleting_a_flagged_pushover_device_leaves_no_flag(wp):
    flag(wp, "alex-phone")
    assert wp.client("admin").delete("/admin/api/devices/alex-phone").status_code == 200
    assert not flagged(wp, "alex-phone")
