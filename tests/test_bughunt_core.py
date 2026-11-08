"""Bug hunt (core area) at bb0294f: each test asserts the CORRECT behaviour and fails on the code as it is."""

from __future__ import annotations

import re
import time

import httpx

from notify import attachments
from tests.conftest import fake_resolve, wait_for


def submit(live, who="agent", **body):
    body.setdefault("targets", ["user:alex"])
    body.setdefault("message", "hello")
    return live.client(who).post("/v1/notifications", json=body)


# ------------------------------------------------------------------ BH-001 decision code in the attempt log

def test_bh001_attempt_log_never_shows_decision_codes(server_factory):
    """C.2 masks `url`/`actions` in listings, but Pushover's request summary keeps the link (with k=<code>)
    in attempts.request_json, which the detail view returns to any admin bearer token."""
    live = server_factory(external_url="http://notify.test")
    assert httpx.post(f"{live.url}/v1/registrations", json={"name": "newapp", "scopes": ["send"]}).status_code == 202
    n = wait_for(lambda: live.service.db.one(
        "SELECT n.id FROM notifications n JOIN deliveries d ON d.notification_id=n.id"
        " JOIN attempts a ON a.delivery_id=d.id WHERE n.client_id='hnl-notify'"))
    text = live.client("admin").get(f"/v1/notifications/{n['id']}").text
    assert "/admin/decide/" in text
    m = re.search(r"[?&]k=(?!hidden)", text)
    assert not m, f"decision code readable by a bearer admin: ...{text[max(0, m.start() - 160):m.end() + 20]}"


# ------------------------------------------------------------------ BH-002 webhook resolver poison

def test_bh002_unresolvable_hostname_does_not_block_every_webhook(live, fake):
    """webhook_address_ok only catches AttachmentError/OSError; getaddrinfo raises UnicodeError (a ValueError)
    for a label over 63 characters. The exception escapes deliver(), the row never gets a result and is claimed
    again first on every pass: 20 such rows stop webhooks for every client."""
    from notify.attachments import default_resolve

    async def resolve(host: str) -> list[str]:
        if host.startswith("a" * 64):
            return await default_resolve(host)  # raises UnicodeError before any network access
        return await fake_resolve(host)
    live.service.resolve = resolve
    bad = "http://" + "a" * 64 + ".example/hook"
    assert live.client("agent").post("/v1/subscriptions", json={"url": bad}).status_code == 201
    for i in range(8):
        assert submit(live, message=f"m{i}").status_code == 202
    wait_for(lambda: (live.service.db.scalar("SELECT COUNT(*) FROM webhook_deliveries") or 0) >= 20)
    assert live.client("other").post("/v1/subscriptions", json={"url": "http://hooks.test/other"}).status_code == 201
    assert submit(live, who="other", message="for other").status_code == 202
    # the poison rows get a result (failed attempt) and the other client's webhook is delivered
    wait_for(lambda: fake.webhooks, timeout=12)


# ------------------------------------------------------------------ BH-003 quiet subscription, short outage

def test_bh003_short_outage_of_a_quiet_subscription_does_not_disable_it(server_factory, fake):
    """A.6's fix measures 'down for a day' from the last success, not from the start of the failure streak:
    a subscription that last fired successfully days ago (quiet tag/event filter) is disabled for good by a
    few minutes of outage."""
    live = server_factory(webhook_disable_after_failures=2)
    sid = live.client().post("/v1/subscriptions", json={"url": "http://hooks.test/h"}).json()["id"]
    with live.service.db.tx() as c:  # worked fine three days ago, nothing to report since
        c.execute("UPDATE subscriptions SET created_at='2026-01-01T00:00:00.000000Z',"
                  " last_success_at=? WHERE id=?",
                  (time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(time.time() - 3 * 86400)), sid))
    fake.webhook_status = 500  # the receiver restarts: a short outage starts now
    assert submit(live).status_code == 202
    wait_for(lambda: (live.service.db.scalar("SELECT consecutive_failures FROM subscriptions WHERE id=?",
                                             (sid,)) or 0) >= 2)
    time.sleep(0.3)
    sub = live.service.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))
    assert sub["active"] == 1, f"disabled after a short outage: {sub['last_error']}"
