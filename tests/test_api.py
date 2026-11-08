from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from tests.conftest import USER_KEY, wait_for


def get(live, nid, who="agent"):
    return live.client(who).get(f"/v1/notifications/{nid}").json()


def submit(live, who="agent", **body):
    body.setdefault("targets", ["user:alex"])
    body.setdefault("message", "hello")
    return live.client(who).post("/v1/notifications", json=body)


# ------------------------------------------------------------------ happy path

def test_normal_notification_is_dispatched_but_never_claims_delivery(live, fake):
    r = submit(live, title="Washer", message="Done", severity="normal", url="http://ha.local/x", tags=["laundry"],
               data={"automation": "washer"}, ttl_seconds=600)
    assert r.status_code == 202, r.text
    n = r.json()
    assert n["status"] == "pending"
    assert n["data"] == {"automation": "washer"}
    n = wait_for(lambda: (x := get(live, n["id"]))["status"] == "dispatched" and x)
    d = n["deliveries"][0]
    assert d["device"] == "alex-phone" and d["state"] == "dispatched" and d["final"] is True
    assert d["tracking"] == "none"
    assert "delivered" not in d["observable_states"] and "acknowledged" not in d["observable_states"]
    assert d["provider_ref"]["message_id"] == "req-1"
    assert d["attempt_log"][0]["outcome"] == "success"
    assert "token" not in d["attempt_log"][0]["request"]  # secrets redacted
    form = fake.messages[0]
    assert form["user"] == USER_KEY and form["device"] == "iphone" and form["priority"] == "0"
    assert form["title"] == "Washer" and form["url"] == "http://ha.local/x"
    assert 590 <= int(form["ttl"]) <= 600 and "retry" not in form
    types = [e["type"] for e in n["events"]]
    assert types[:2] == ["notification.created", "delivery.queued"]
    assert "delivery.dispatched" in types and "delivery.delivered" not in types


def test_severity_mapping_and_group_dedup(live, fake):
    # household = user:alex + device:alex-phone -> one device, one delivery
    r = submit(live, targets=["group:household", "user:alex"], severity="low")
    n = r.json()
    assert len(n["deliveries"]) == 1
    wait_for(lambda: fake.messages)
    assert fake.messages[0]["priority"] == "-1"


def test_idempotency_key_returns_same_notification(live, fake):
    a = submit(live, idempotency_key="k1")
    b = live.client().post("/v1/notifications", json={"targets": ["alex"], "message": "hello"},
                           headers={"Idempotency-Key": "k1"})
    assert a.status_code == 202 and b.status_code == 200
    assert a.json()["id"] == b.json()["id"] and b.headers["Idempotent-Replay"] == "true"
    wait_for(lambda: get(live, a.json()["id"])["status"] == "dispatched")
    assert len(fake.messages) == 1


def test_bad_targets_rejected(live):
    assert submit(live, targets=["user:nobody"]).status_code == 422
    assert submit(live, targets=["pager:alex"]).status_code == 422
    assert submit(live, targets=[]).status_code == 422


def test_channel_severity_floor_suppresses(live, fake):
    n = submit(live, targets=["channel:infra"], severity="normal").json()
    assert n["status"] == "suppressed" and n["deliveries"] == []
    assert "below channel min_severity high" in n["suppressed"][0]["reason"]
    n2 = submit(live, targets=["channel:infra"], severity="high").json()
    assert len(n2["deliveries"]) == 1


def test_disabled_device_suppressed(live):
    n = submit(live, targets=["device:spare"]).json()
    assert n["status"] == "suppressed" and n["suppressed"][0]["reason"] == "device disabled"


def test_dry_run_reports_capabilities_without_sending(live, fake):
    r = submit(live, severity="normal", require_ack=True,
               actions=[{"id": "open", "label": "Open", "url": "http://x"}, {"id": "snooze", "label": "Snooze"}],
               dry_run=True)
    assert r.status_code == 200
    plan = r.json()
    d = plan["deliveries"][0]
    assert any(g.startswith("acknowledgement") for g in d["capability_gaps"])
    assert any("action 'open' sent as the tap link" in g for g in d["capability_gaps"])
    assert fake.messages == []
    assert get(live, "ntf_x").get("detail")  # nothing persisted


# ------------------------------------------------------------------ auth

def test_auth_and_scopes(live):
    assert httpx.post(f"{live.url}/v1/notifications", json={"targets": ["alex"], "message": "x"}).status_code == 401
    assert live.client("reader").post("/v1/notifications", json={"targets": ["alex"], "message": "x"}).status_code == 403
    nid = submit(live, who="sender").json()["id"]
    assert live.client("sender").get(f"/v1/notifications/{nid}").status_code == 403  # send-only
    assert live.client("other").get(f"/v1/notifications/{nid}").status_code == 404   # not theirs
    assert live.client("admin").get(f"/v1/notifications/{nid}").status_code == 200
    assert live.client("other").post(f"/v1/notifications/{nid}/cancel").status_code == 404
    assert live.client("agent").get("/v1/whoami").json()["scopes"] == ["manage", "read", "send"]


# ------------------------------------------------------------------ receipts (critical)

def test_critical_tracks_delivery_and_acknowledgement(live, fake):
    n = submit(live, severity="critical", message="Water leak", ttl_seconds=900).json()
    n = wait_for(lambda: (x := get(live, n["id"]))["status"] == "dispatched" and x)
    d = n["deliveries"][0]
    assert d["tracking"] == "receipt" and {"delivered", "acknowledged"} <= set(d["observable_states"])
    form = fake.messages[0]
    assert form["priority"] == "2" and form["retry"] == "60" and 60 <= int(form["expire"]) <= 900
    assert form["tags"] == n["id"] and "ttl" not in form
    receipt = d["provider_ref"]["receipt"]
    fake.receipts[receipt]["last_delivered_at"] = 1790000000
    live.fast_forward()
    wait_for(lambda: get(live, n["id"])["status"] == "delivered")
    fake.receipts[receipt].update(acknowledged=1, acknowledged_at=1790000100, acknowledged_by=USER_KEY,
                                  acknowledged_by_device="iphone")
    live.fast_forward()
    n = wait_for(lambda: (x := get(live, n["id"]))["status"] == "acknowledged" and x)
    d = n["deliveries"][0]
    assert d["acknowledged_by"] == "iphone" and d["timestamps"]["delivered"] and d["final"]
    types = [e["type"] for e in n["events"]]
    assert types.index("delivery.delivered") < types.index("delivery.acknowledged")


def test_critical_expired_without_ack(live, fake):
    n = submit(live, severity="critical").json()
    d = wait_for(lambda: (x := get(live, n["id"]))["deliveries"][0]["provider_ref"]["receipt"] and x)["deliveries"][0]
    fake.receipts[d["provider_ref"]["receipt"]].update(expired=1, last_delivered_at=1790000000)
    live.fast_forward()
    n = wait_for(lambda: (x := get(live, n["id"]))["status"] == "expired" and x)
    assert n["deliveries"][0]["timestamps"]["delivered"]  # delivered, but never acknowledged


def test_cancel_critical_stops_provider_alert(live, fake):
    n = submit(live, severity="critical").json()
    wait_for(lambda: get(live, n["id"])["status"] == "dispatched")
    r = live.client().post(f"/v1/notifications/{n['id']}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["notification"]["status"] == "cancelled"
    assert len(fake.cancelled) == 1


def test_callback_acknowledgement(server_factory, fake):
    live = server_factory(public_base_url="https://notify.example", callback_secret="s" * 32)
    n = submit(live, severity="critical").json()
    d = wait_for(lambda: (x := get(live, n["id"]))["deliveries"][0]["provider_ref"]["receipt"] and x)["deliveries"][0]
    assert fake.messages[0]["callback"] == "https://notify.example/v1/callbacks/pushover/" + "s" * 32
    bad = httpx.post(f"{live.url}/v1/callbacks/pushover/wrong", data={"receipt": "x"})
    assert bad.status_code == 404
    ok = httpx.post(f"{live.url}/v1/callbacks/pushover/{'s' * 32}",
                    data={"receipt": d["provider_ref"]["receipt"], "acknowledged": "1",
                          "acknowledged_at": "1790000100", "acknowledged_by": USER_KEY,
                          "acknowledged_by_device": "iphone"})
    assert ok.status_code == 204
    assert get(live, n["id"])["status"] == "acknowledged"


# ------------------------------------------------------------------ failures & retry

def test_transient_error_is_retried(live, fake):
    fake.responses.append((500, {"status": 0, "errors": ["internal"]}))
    n = submit(live).json()
    wait_for(lambda: len(fake.messages) == 1)
    d = wait_for(lambda: (x := get(live, n["id"])["deliveries"][0])["attempts"] == 1 and x["state"] == "queued" and x)
    assert d["last_error"] == "outcome unknown; may be duplicated: internal" and d["next_attempt_at"]  # a 5xx may follow acceptance
    live.fast_forward()
    n = wait_for(lambda: (x := get(live, n["id"]))["status"] == "dispatched" and x)
    log = n["deliveries"][0]["attempt_log"]
    assert [a["outcome"] for a in log] == ["retryable", "success"]
    assert log[0]["http_status"] == 500 and "internal" in log[0]["response"]
    assert "delivery.attempt_failed" in [e["type"] for e in n["events"]]


def test_permanent_error_fails_and_manual_retry(live, fake):
    fake.responses.append((400, {"status": 0, "errors": ["user identifier is invalid"], "user": "invalid"}))
    n = submit(live).json()
    n = wait_for(lambda: (x := get(live, n["id"]))["status"] == "failed" and x)
    d = n["deliveries"][0]
    assert d["last_error"] == "user identifier is invalid" and d["attempts"] == 1
    assert live.client().get("/v1/attention").json()["failed"][0]["notification_id"] == n["id"]
    r = live.client().post(f"/v1/notifications/{n['id']}/retry")
    assert r.status_code == 200 and r.json()["results"][0]["result"] == "requeued"
    wait_for(lambda: get(live, n["id"])["status"] == "dispatched")
    assert live.client().post(f"/v1/notifications/{n['id']}/retry").status_code == 409


def test_ttl_expires_while_provider_down(live, fake):
    fake.responses.extend([(503, {"status": 0})] * 3)
    n = submit(live, ttl_seconds=6).json()
    n = wait_for(lambda: (x := get(live, n["id"]))["status"] == "expired" and x, timeout=15)
    assert n["deliveries"][0]["state"] == "expired"
    assert n["deliveries"][0]["timestamps"]["dispatched"] is None


def test_cancel_before_dispatch_and_not_after(live, fake):
    fake.responses.extend([(503, {"status": 0})])
    n = submit(live).json()
    wait_for(lambda: get(live, n["id"])["deliveries"][0]["attempts"] == 1
             and get(live, n["id"])["deliveries"][0]["state"] == "queued")
    r = live.client().post(f"/v1/notifications/{n['id']}/cancel")
    assert r.status_code == 200 and r.json()["notification"]["status"] == "cancelled"
    n2 = submit(live).json()
    wait_for(lambda: get(live, n2["id"])["status"] == "dispatched")
    r = live.client().post(f"/v1/notifications/{n2['id']}/cancel")
    assert r.status_code == 409 and "cannot recall" in r.text


def test_quota_exhausted_is_retryable(live, fake):
    fake.responses.append((429, {"status": 0, "errors": ["message limit reached"]}))
    n = submit(live).json()
    d = wait_for(lambda: (x := get(live, n["id"])["deliveries"][0])["attempts"] == 1 and x["state"] == "queued" and x)
    assert d["last_error"].startswith("quota exhausted")


# ------------------------------------------------------------------ restart durability

def test_interrupted_send_is_recovered_on_restart(tmp_path, fake):
    import httpx as _h

    from notify.app import create_app
    from notify.db import Database
    from tests.conftest import LiveServer, make_config

    db_path = str(tmp_path / "restart.sqlite3")
    app = create_app(make_config(), db_path=db_path, key_path=str(tmp_path / "k"), transport=_h.MockTransport(fake.handler),
                     start_workers=False)
    srv = LiveServer.__new__(LiveServer)  # start without workers: nothing dispatches
    LiveServer.__init__(srv, app, fake)
    srv.thread.start()
    wait_for(lambda: srv.server.started)
    nid = srv.client().post("/v1/notifications", json={"targets": ["alex"], "message": "survive"}).json()["id"]
    srv.server.should_exit = True
    srv.thread.join(timeout=20)
    # simulate a crash mid-send: delivery claimed with a lease
    db = Database(db_path)
    with db.tx() as c:
        c.execute("UPDATE deliveries SET state='sending', attempts=1, lease_until='2999-01-01T00:00:00.000000Z'")
    db.close()
    app2 = create_app(make_config(), db_path=db_path, key_path=str(tmp_path / "k"),
                      transport=_h.MockTransport(fake.handler))
    srv2 = LiveServer(app2, fake)
    srv2.start()
    try:
        n = wait_for(lambda: (x := get(srv2, nid))["status"] == "dispatched" and x)
        assert "lease expired or restart" in json.dumps(n["events"])
        assert len(fake.messages) == 1
    finally:
        srv2.stop()


# ------------------------------------------------------------------ events: webhooks & SSE

def test_per_notification_webhook_is_signed(live, fake):
    r = submit(live, webhook={"url": "http://hooks.test/notify", "events": ["delivery.*"]})
    body = r.json()
    secret = body["webhook_secret"]
    wait_for(lambda: len(fake.webhooks) >= 2)
    req = fake.webhooks[0]
    ts, mac = dict(p.split("=", 1) for p in req.headers["X-Notify-Signature"].split(",")).values()
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + req.content, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(mac, expected)
    types = [json.loads(w.content)["type"] for w in fake.webhooks]
    assert types[:2] == ["delivery.queued", "delivery.dispatched"]
    payload = json.loads(fake.webhooks[-1].content)
    assert payload["notification"]["id"] == body["id"]


def test_subscription_scoping_and_retry(live, fake):
    c = live.client()
    sub = c.post("/v1/subscriptions", json={"url": "http://hooks.test/all", "events": ["notification.status_changed"],
                                            "tag": "watched"}).json()
    assert sub["secret"]
    fake.webhook_status = 500
    submit(live, who="other", tags=["watched"])  # other client's notification: not visible to agent's subscription
    n = submit(live, tags=["watched"]).json()
    wait_for(lambda: len(fake.webhooks) >= 1)
    assert all(json.loads(w.content)["notification_id"] == n["id"] for w in fake.webhooks)
    s = wait_for(lambda: (x := c.get("/v1/subscriptions").json()["items"][0])["consecutive_failures"] >= 1 and x)
    assert s["last_error"] == "HTTP 500"
    fake.webhook_status = 200
    live.fast_forward()
    wait_for(lambda: c.get("/v1/subscriptions").json()["items"][0]["consecutive_failures"] == 0)
    assert c.delete(f"/v1/subscriptions/{sub['id']}").status_code == 204


def test_event_history_and_sse(live):
    n = submit(live).json()
    wait_for(lambda: get(live, n["id"])["status"] == "dispatched")
    page = live.client().get("/v1/events", params={"after": 0, "notification_id": n["id"]}).json()
    assert page["items"][0]["type"] == "notification.created"
    got = []
    with live.client().stream("GET", "/v1/events/stream", params={"after": 0, "types": "delivery.*"}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("event: "):
                got.append(line[7:])
            if len(got) >= 2:
                break
    assert got == ["delivery.queued", "delivery.dispatched"]


# ------------------------------------------------------------------ ops

def test_health_targets_providers_metrics(live):
    assert httpx.get(f"{live.url}/health/live").json()["status"] == "ok"
    ready = httpx.get(f"{live.url}/health/ready")
    assert ready.status_code == 200, ready.text
    t = live.client("reader").get("/v1/targets").json()
    dev = next(d for d in t["devices"] if d["target"] == "device:alex-phone")
    assert dev["ready"] and dev["capabilities"]["actions"]["max"] == 0
    assert dev["capabilities"]["delivery_receipt"]["when"] == "severity=critical"
    assert USER_KEY not in json.dumps(t)  # provider identifiers stay private
    submit(live)
    wait_for(lambda: live.client().get("/v1/providers").json()["items"][0].get("quota"))
    m = httpx.get(f"{live.url}/metrics").text
    assert 'notify_provider_ready{provider="pushover"} 1' in m and "notify_deliveries" in m


def test_not_ready_without_credentials(tmp_path, fake):
    from notify.app import create_app
    from tests.conftest import LiveServer, make_config

    cfg = make_config()
    cfg.providers["pushover"].app_token = ""  # type: ignore[attr-defined]
    app = create_app(cfg, db_path=str(tmp_path / "x.sqlite3"), key_path=str(tmp_path / "k"),
                     transport=httpx.MockTransport(fake.handler))
    srv = LiveServer(app, fake)
    srv.start()
    try:
        r = httpx.get(f"{srv.url}/health/ready")
        assert r.status_code == 503 and "app_token" in r.text
        n = srv.client().post("/v1/notifications", json={"targets": ["alex"], "message": "x"}).json()
        assert any("provider not ready" in g for g in n["deliveries"][0]["capability_gaps"])
        d = wait_for(lambda: (x := get(srv, n["id"])["deliveries"][0])["attempts"] >= 1 and x["state"] == "queued" and x)
        assert "app_token is not set" in d["last_error"] and fake.messages == []
    finally:
        srv.stop()
