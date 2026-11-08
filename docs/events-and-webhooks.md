# Events, webhooks and callbacks

## Events

**Server-Sent Events** (resumable with `Last-Event-ID`):

```sh
curl -N -H "Authorization: Bearer $TOKEN" "$N/v1/events/stream?types=delivery.*"
```

**Webhooks**: either per notification, by adding
`"webhook": {"url": "http://host/hook", "events": ["delivery.*"]}` to the
submit body (the response includes `webhook_secret`), or as a standing
subscription:

```sh
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' $N/v1/subscriptions \
  -d '{"url": "http://nodered.lan:1880/notify-events", "events": ["delivery.failed", "delivery.acknowledged"], "tag": "security"}'
```

Each POST carries `X-Notify-Event`, `X-Notify-Event-Id` and
`X-Notify-Signature: t=<unix>,v1=<hex>`, where
`v1 = HMAC-SHA256(secret, "<t>." + raw_body)`. Verify it:

```python
import hmac, hashlib
t, v1 = (p.split("=", 1)[1] for p in header.split(","))
ok = hmac.compare_digest(v1, hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest())
```

Webhooks retry with backoff (10 attempts) from a durable outbox; a subscription
is disabled after 50 consecutive failures once it has also gone a day without a success (visible in
`GET /v1/subscriptions`). At most 20 standing subscriptions per client; webhooks never go to loopback or
link-local addresses.

Event types: `notification.created`, `notification.status_changed`,
`delivery.queued`, `delivery.attempt_failed`, `delivery.dispatched`,
`delivery.delivered`, `delivery.acknowledged`, `delivery.interacted`,
`delivery.failed`, `delivery.expired`, `delivery.cancelled`, `reply.received`,
`reply.claimed`, `reply.unclaimed`, `reply.quarantined`, and (0.8.0) `notification.scheduled`,
`notification.updated`, `notification.missed`, and (0.13.0) `group.opened`, `group.member_joined`,
`group.member_left`, `group.message`, `group.paused`, `group.unpaused`, `group.closed`. The `group.*` events are
only readable through `GET /v1/events` and the SSE stream (agent tokens see only those of their own joins); they are
not sent to webhook subscriptions, not even to `*` or `group.*`.

## Pushover acknowledgement callbacks (optional)

Critical alerts are tracked by polling Pushover receipts every 30 s. For
instant acknowledgement, expose `/v1/callbacks/` through a public HTTPS proxy
and set the public address under **Settings** (a random callback secret is
generated). Without that, nothing changes except latency. The callback URL holds that secret, so the
attempt log records only `"callback": true`.

[← back to the README](../README.md)
