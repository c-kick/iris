"""Phone decision links for access requests, and Pushover inline action links."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx

from notify.models import Severity
from notify.providers.base import OutboundMessage
from notify.providers.pushover import PushoverProvider
from tests.conftest import wait_for


def register(live, name="node-red"):
    r = httpx.post(f"{live.url}/v1/registrations", json={"name": name, "description": "Flow <alerts> & more",
                                                          "scopes": ["send", "read"]})
    assert r.status_code == 202, r.text
    return r.json()


def alert_for(fake, name):
    return wait_for(lambda: next((m for m in fake.messages if m.get("title") == f"Access request: {name}"), None))


def decide_link(live, msg):
    """Tap link from the alert, pointed at the test server."""
    parts = urlsplit(msg["url"])
    return f"{live.url}{parts.path}", parse_qs(parts.query)["k"][0]


def test_alert_carries_tap_link_and_inline_approve_deny(live, fake):
    reg = register(live)
    msg = alert_for(fake, "node-red")
    assert msg["url_title"] == "Approve or deny" and f"/admin/decide/{reg['id']}?k=" in msg["url"]
    assert msg["html"] == "1"
    body = msg["message"]
    assert "Flow &lt;alerts&gt; &amp; more" in body  # sender text is escaped, not interpreted
    assert '&amp;action=approve">Approve</a>' in body and '&amp;action=deny">Deny</a>' in body


def test_approve_from_phone_link(live, fake):
    reg = register(live)
    page, k = decide_link(live, alert_for(fake, "node-red"))
    # opening the page decides nothing (link previews / prefetch are harmless)
    g = httpx.get(page, params={"k": k, "action": "approve"})
    assert g.status_code == 200 and "node-red wants access" in g.text and "Flow &lt;alerts&gt;" in g.text
    assert g.headers["cache-control"] == "no-store" and g.headers["referrer-policy"] == "no-referrer"
    token_client = httpx.Client(base_url=live.url, headers={"Authorization": f"Bearer {reg['token']}"})
    assert token_client.get("/v1/registrations/me").json()["status"] == "pending"
    p = httpx.post(page, data={"k": k, "decision": "approve"})
    assert p.status_code == 200 and "node-red approved" in p.text
    assert token_client.get("/v1/whoami").status_code == 200
    me = token_client.get("/v1/registrations/me").json()
    assert me["status"] == "approved" and me["decided_by"] == "phone link"
    # single use
    assert httpx.get(page, params={"k": k}).status_code == 410
    assert httpx.post(page, data={"k": k, "decision": "deny"}).status_code == 410
    assert token_client.get("/v1/whoami").status_code == 200


def test_deny_from_phone_link_and_wrong_codes(live, fake):
    reg = register(live)
    page, k = decide_link(live, alert_for(fake, "node-red"))
    assert httpx.get(page, params={"k": "wrong"}).status_code == 410
    assert httpx.get(page).status_code == 410
    g = httpx.get(page, params={"k": k, "action": "deny"})
    assert g.text.index('value="deny"') < g.text.index('value="approve"')  # the tapped choice comes first
    assert httpx.post(page, data={"k": k}).status_code == 400  # no button pressed
    p = httpx.post(page, data={"k": k, "decision": "deny"})
    assert p.status_code == 200 and "node-red denied" in p.text
    me = httpx.get(f"{live.url}/v1/registrations/me", headers={"Authorization": f"Bearer {reg['token']}"}).json()
    assert me["status"] == "denied"


def test_admin_decision_invalidates_phone_link(live, fake):
    reg = register(live)
    page, k = decide_link(live, alert_for(fake, "node-red"))
    live.client("admin").post(f"/admin/api/registrations/{reg['id']}/approve")
    assert httpx.get(page, params={"k": k}).status_code == 410


def test_wrong_codes_are_throttled(live, fake):
    reg = register(live)
    page = f"{live.url}/admin/decide/{reg['id']}"
    codes = [httpx.get(page, params={"k": f"guess{i}"}).status_code for i in range(6)]
    assert codes[:5] == [410] * 5 and codes[5] == 429


def test_auto_approved_alert_has_no_decision_links(live, fake):
    live.client("admin").put("/admin/api/registration", json={"auto_approve_subnets": ["127.0.0.0/8"],
                                                              "auto_approve_scopes": ["send", "read"]})
    register(live, "script")
    msg = wait_for(lambda: next((m for m in fake.messages if m.get("title") == "Access granted: script"), None))
    assert msg["url"].endswith("/admin#requests") and "html" not in msg


def _msg(message: str, actions: list[dict], url: str | None = None) -> OutboundMessage:
    return OutboundMessage("ntf_1", "dlv_1", "T", message, Severity.normal, url, None, actions, [], False,
                           datetime.now(UTC), None)


def test_pushover_inline_links_and_escaping():
    p = PushoverProvider("pushover", {"app_token": "a" * 30}, None)  # type: ignore[arg-type]
    now = datetime.now(UTC)
    addr = {"user_key": "u" * 30}
    # no extra actions: plain text, untouched
    plain = p.build_form(_msg("a < b", []), addr, now)
    assert plain["message"] == "a < b" and "html" not in plain
    # first URL action becomes the tap link, the rest inline
    acts = [{"id": "open", "label": "Open", "url": "http://x/open"},
            {"id": "snooze", "label": "Snooze <1h>", "url": "http://x/s?a=1&b=2"},
            {"id": "noop", "label": "No URL"}]
    f = p.build_form(_msg("x & y", acts), addr, now)
    assert f["url"] == "http://x/open" and f["html"] == "1"
    assert f["message"] == 'x &amp; y\n\n<a href="http://x/s?a=1&amp;b=2">Snooze &lt;1h&gt;</a>'
    gaps = p.plan(_msg("x", acts), addr).capability_gaps[0]
    assert "'open' sent as the tap link" in gaps and "'snooze' shown as a link" in gaps and "'noop' dropped" in gaps
    # long messages are truncated before the link line, never through an entity
    f = p.build_form(_msg("&" * 2000, acts), addr, now)
    assert len(f["message"]) <= 1024 and f["message"].endswith("</a>")
    assert f["message"].split("\n\n")[0].endswith("&amp;")
