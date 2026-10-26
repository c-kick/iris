"""Self-registration: request → pending token → admin approves/denies (or auto-approve)."""

from __future__ import annotations

import httpx

from tests.conftest import TOKENS, wait_for


def admin(live) -> httpx.Client:
    return live.client("admin")


def register(live, **body):
    body.setdefault("name", "node-red")
    return httpx.post(f"{live.url}/v1/registrations", json=body, headers={"User-Agent": "node-red/4.0"})


def as_token(live, token) -> httpx.Client:
    return httpx.Client(base_url=live.url, headers={"Authorization": f"Bearer {token}"}, timeout=10)


def test_request_approve_then_token_works_without_reconfiguration(live, fake):
    r = register(live, description="Flow alerts", scopes=["send", "read"])
    assert r.status_code == 202, r.text
    reg = r.json()
    assert reg["status"] == "pending" and reg["token"].startswith("ntk_")
    app = as_token(live, reg["token"])
    # inert until approved
    assert app.post("/v1/notifications", json={"targets": ["alex"], "message": "x"}).status_code == 401
    assert app.get("/v1/registrations/me").json()["status"] == "pending"
    # the admin was alerted on their phone, with a link to the review page
    msg = wait_for(lambda: next((m for m in fake.messages if m.get("title") == "Access request: node-red"), None))
    assert "wants to send notifications and read its own history" in msg["message"]
    assert "“Flow alerts”" in msg["message"] and f"/admin/decide/{reg['id']}?k=" in msg["url"]
    # listed for the admin
    pending = admin(live).get("/admin/api/registrations").json()["pending"]
    assert pending[0]["name"] == "node-red" and pending[0]["user_agent"] == "node-red/4.0"
    # approve with fewer permissions than requested
    d = admin(live).post(f"/admin/api/registrations/{reg['id']}/approve", json={"scopes": ["send"]})
    assert d.status_code == 200, d.text
    assert d.json()["registration"]["status"] == "approved"
    assert d.json()["state"]["config"]["clients"]["node-red"]["scopes"] == ["send"]
    # same token now works, with the granted scopes only
    n = app.post("/v1/notifications", json={"targets": ["alex"], "message": "hello from node-red"})
    assert n.status_code == 202 and n.json()["client"] == "node-red"
    assert app.get("/v1/notifications").status_code == 403  # read was not granted
    me = app.get("/v1/registrations/me").json()
    assert me["status"] == "approved" and me["granted_scopes"] == ["send"] and "source_ip" not in me
    # deciding twice is refused
    again = admin(live).post(f"/admin/api/registrations/{reg['id']}/deny")
    assert again.status_code == 409


def test_deny_and_revoked_status(live):
    reg = register(live).json()
    admin(live).post(f"/admin/api/registrations/{reg['id']}/deny")
    app = as_token(live, reg["token"])
    assert app.get("/v1/registrations/me").json()["status"] == "denied"
    assert app.get("/v1/whoami").status_code == 401
    # a new request under the same name is allowed after a denial
    reg2 = register(live).json()
    admin(live).post(f"/admin/api/registrations/{reg2['id']}/approve")
    assert as_token(live, reg2["token"]).get("/v1/whoami").status_code == 200
    # revoking the client under API access shows up as 'revoked'
    admin(live).delete("/admin/api/clients/node-red")
    assert as_token(live, reg2["token"]).get("/v1/registrations/me").json()["status"] == "revoked"


def test_validation_and_abuse_limits(live):
    assert register(live, scopes=["admin"]).status_code == 422
    assert register(live, name="Bad Name!").status_code == 422
    assert register(live, name="sender").status_code == 409  # existing API client
    assert register(live, name="dup").status_code == 202
    assert register(live, name="dup").status_code == 409  # already waiting
    codes = [register(live, name=f"app{i}").status_code for i in range(5)]
    assert codes[:4] == [202] * 4 and codes[4] == 429  # 5 stored requests per address per hour
    assert httpx.get(f"{live.url}/v1/registrations/me").status_code == 401
    assert httpx.get(f"{live.url}/v1/registrations/me",
                     headers={"Authorization": "Bearer ntk_unknown"}).status_code == 404
    # admin can't be granted through a request either
    reg_id = admin(live).get("/admin/api/registrations").json()["pending"][0]["id"]
    r = admin(live).post(f"/admin/api/registrations/{reg_id}/approve", json={"scopes": ["admin"]})
    assert r.status_code == 422


def test_pending_requests_expire(live):
    reg = register(live).json()
    with live.service.db.tx() as c:
        c.execute("UPDATE registrations SET created_at='2000-01-01T00:00:00.000000Z'")
    assert as_token(live, reg["token"]).get("/v1/registrations/me").json()["status"] == "expired"
    r = admin(live).post(f"/admin/api/registrations/{reg['id']}/approve")
    assert r.status_code == 409 and "expired" in r.text


def test_auto_approve_from_subnet(live, fake):
    s = admin(live).put("/admin/api/registration", json={
        "enabled": True, "notify_targets": ["user:alex"],
        "auto_approve_subnets": ["127.0.0.0/8"], "auto_approve_scopes": ["send"]})
    assert s.status_code == 200, s.text
    # within the auto-approvable permissions: works immediately, admin informed
    reg = register(live, name="script", scopes=["send"]).json()
    assert reg["status"] == "approved"
    assert as_token(live, reg["token"]).post("/v1/notifications",
                                             json={"targets": ["alex"], "message": "x"}).status_code == 202
    wait_for(lambda: any(m.get("title") == "Access granted: script" for m in fake.messages))
    # asking for more than that still needs a human
    assert register(live, name="greedy", scopes=["send", "manage"]).json()["status"] == "pending"
    bad = admin(live).put("/admin/api/registration", json={"auto_approve_subnets": ["not-a-net"]})
    assert bad.status_code == 422 and "not a subnet" in bad.text


def test_registration_can_be_turned_off(live):
    admin(live).put("/admin/api/registration", json={"enabled": False})
    r = register(live)
    assert r.status_code == 403 and "turned off" in r.text


def test_admin_state_counts_pending(live):
    register(live)
    st = admin(live).get("/admin/api/state").json()
    assert st["pending_registrations"] == 1
    assert st["config"]["registration"]["enabled"] is True
    assert TOKENS  # fixture tokens unchanged


def test_announce_targets():
    from notify.registrations import announce_targets
    from tests.conftest import make_config
    cfg = make_config()
    assert announce_targets(cfg) == [f"user:{u}" for u in cfg.users]
    cfg.registration.notify_targets = ["user:alex"]
    assert announce_targets(cfg) == ["user:alex"]
