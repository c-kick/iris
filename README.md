# hnl-notify

A self-hosted notification gateway for a home or small-office network.
Trusted services (Home Assistant, Node-RED, n8n, scripts, AI agents) submit
**notification intent** — message, severity, logical target — over REST or
MCP. hnl-notify routes it to devices, delivers through provider adapters,
retries, persists everything and reports status honestly: it only claims what
the provider can actually observe.

The first provider is **Pushover** (phone push notifications). Senders never
see Pushover keys or priorities; adding ntfy, Home Assistant, email or Web
Push later is a new adapter in `notify/providers/`, not an API change.

Design, data model, lifecycle and the full Pushover capability analysis:
[docs/architecture.md](docs/architecture.md).

- Stack: Python 3.12, FastAPI, SQLite (WAL) as durable queue, official MCP SDK
- One container, one process, port **8930**; ~50 MiB resident idle
- Intended for a trusted LAN/VPN: plain HTTP, bearer tokens. Put it behind a
  TLS reverse proxy if it has to leave the local network.

Examples below assume the service runs at `http://notify.lan:8930`; replace
that with your host.

## Deploy

```sh
git clone <this repository> hnl-notify && cd hnl-notify
mkdir -p data secrets && chmod 700 data secrets
docker compose up -d --build
docker logs hnl-notify 2>&1 | grep "setup code"
```

The container runs as UID/GID 1026 by default (`user:` in `compose.yaml`);
make sure that account can write `data/` and `secrets/`, or change it.
Resource limits (192 MiB, no extra swap, 0.5 CPU, 64 PIDs) are set in
`compose.yaml`.

Open **http://notify.lan:8930/admin**, enter the setup code and choose an
admin password. Then, in the admin UI:

1. **Pushover**: paste the API token of an application created at
   [pushover.net/apps/build](https://pushover.net/apps/build).
2. **People & groups → Add person**: add yourself (e.g. `alex`).
3. **Devices → Add device**: name it (e.g. `alex-phone`), let it belong to
   you, paste your Pushover user key, press *Find devices* and pick the phone
   (leave it empty to reach all your Pushover devices).
4. **API access → Create token**: one per sender (Home Assistant, Node-RED,
   agents…), or let senders request access themselves (see below). A token is
   shown once, with ready-to-paste snippets.
5. **Overview → Send test notification**.

No `.env` and no config file are needed: everything lives in
`data/notify.sqlite3`, with secrets (Pushover token and user keys) encrypted
using the 32-byte key that the container generates in `secrets/master.key`
(0600). **Back up both**; the database is useless without the key. Changes
apply immediately, no restart.

Links in notifications that hnl-notify sends you (access requests) need the
address under which you reach the service. Set it under **Settings → Address
of this service**, or as `NOTIFY_EXTERNAL_URL` in a local, git-ignored
`compose.override.yaml`:

```yaml
services:
  notify:
    environment:
      NOTIFY_EXTERNAL_URL: http://notify.lan:8930
```

Forgot the admin password: `docker exec hnl-notify python -m notify reset-admin`
prints a new setup code (and signs out every browser).

`GET /health/ready` returns 503 with the exact reason (no device ready,
provider token missing) until delivery can work. Notifications accepted
meanwhile stay queued and retry. Post-deploy check that sends nothing:
`scripts/smoke.sh http://127.0.0.1:8930 FILE_WITH_A_TOKEN [user:NAME]`.

Infrastructure-as-code is still possible: mount `./config:/config:ro` with a
`config/notify.yaml` (see `config/notify.example.yaml`, `${ENV}` references
allowed). It is imported **once**, into an empty database only.

## Concepts

| Target | Example | Meaning |
|---|---|---|
| `user:NAME` | `user:alex` | all of a person's devices |
| `device:NAME` | `device:alex-phone` | one concrete endpoint |
| `group:NAME` | `group:household` | several users/devices |
| `channel:NAME` | `channel:infra` | a topic with subscribers and an optional severity floor |

Bare names (`alex`) work when unambiguous. Severities: `min` (silent),
`low` (no sound), `normal`, `high` (bypasses quiet hours), `critical`
(emergency: repeats on the phone until acknowledged).

Delivery states: `queued → sending → dispatched → delivered → acknowledged`,
or `failed` / `expired` / `cancelled`. Each delivery lists its
`observable_states`: **with Pushover, `delivered` and `acknowledged` exist only
for `critical`**. A normal message ends at `dispatched` (Pushover accepted it)
because Pushover never reports whether it reached the phone.
`capability_gaps` lists anything you asked for that the device could not do
(e.g. `require_ack` below critical, action buttons).

## REST examples

```sh
TOKEN=ntk_...; N=http://notify.lan:8930

# send
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' $N/v1/notifications -d '{
  "targets": ["user:alex"],
  "title": "Washing machine",
  "message": "The laundry is done",
  "severity": "normal",
  "url": "http://homeassistant.lan:8123/", "url_title": "Open Home Assistant",
  "tags": ["laundry"], "ttl_seconds": 3600,
  "data": {"automation": "laundry_done"},
  "idempotency_key": "laundry-2026-09-27T14:00"
}'
# -> 202 {"id": "ntf_...", "status": "pending", "deliveries": [...]}

curl -s -H "Authorization: Bearer $TOKEN" $N/v1/notifications/ntf_...     # status + attempts + events
curl -s -H "Authorization: Bearer $TOKEN" "$N/v1/notifications?status=failed,expired&limit=20"
curl -s -H "Authorization: Bearer $TOKEN" $N/v1/attention                   # failed / stuck / awaiting ack
curl -s -H "Authorization: Bearer $TOKEN" $N/v1/targets                     # targets + capabilities
curl -s -X POST -H "Authorization: Bearer $TOKEN" $N/v1/notifications/ntf_.../retry
curl -s -X POST -H "Authorization: Bearer $TOKEN" $N/v1/notifications/ntf_.../cancel
```

Add `"dry_run": true` to see routing, observable states and capability gaps
without sending. Repeating a request with the same `idempotency_key` (or
`Idempotency-Key` header) returns the original notification (HTTP 200).
Optional `actions` (`[{"id", "label", "url"}]`) become extra links in the
message text with Pushover; taps are not reported back. OpenAPI docs:
`http://notify.lan:8930/docs`.

Scopes: `send` (submit), `read` (own history, targets, events, subscriptions),
`manage` (retry/cancel own), `admin` (everything, all clients).

### Home Assistant

```yaml
# configuration.yaml
rest_command:
  notify:
    url: http://notify.lan:8930/v1/notifications
    method: POST
    headers:
      Authorization: !secret hnl_notify_token   # "Bearer ntk_..."
    content_type: application/json
    payload: >
      {"targets": {{ (targets | default(['user:alex'])) | tojson }},
       "title": {{ title | default('Home Assistant') | tojson }},
       "message": {{ message | tojson }},
       "severity": {{ severity | default('normal') | tojson }},
       "source": "home-assistant"}
```

```yaml
action: rest_command.notify
data: {message: "The front door is open", severity: high, targets: ["channel:security"]}
```

### Node-RED / n8n

HTTP request node: `POST http://notify.lan:8930/v1/notifications`, header
`Authorization: Bearer ntk_...`, JSON body as above.

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
is disabled after 50 consecutive failures (visible in `GET /v1/subscriptions`).

Event types: `notification.created`, `notification.status_changed`,
`delivery.queued`, `delivery.attempt_failed`, `delivery.dispatched`,
`delivery.delivered`, `delivery.acknowledged`, `delivery.interacted`,
`delivery.failed`, `delivery.expired`, `delivery.cancelled`.

## Letting apps ask for access (self-registration)

Instead of creating every token in the admin UI, an app can ask for one:

```sh
curl -s -X POST http://notify.lan:8930/v1/registrations -H 'Content-Type: application/json' \
  -d '{"name": "node-red", "description": "Flow alerts: washer, boiler, doors", "scopes": ["send", "read"]}'
# -> 202 {"id": "reg_…", "status": "pending", "token": "ntk_…", "status_url": "/v1/registrations/me", …}
```

- The app stores the token straight away. It does nothing until you approve.
- You get a notification ("Access request: node-red"). Tapping it (or the
  **Approve** / **Deny** links in the message text in the Pushover app) opens
  a small page for that one request with Approve and Deny buttons; no login.
  The link carries a single-use code for that request only (only its hash is
  stored), stops working once anything decides the request, and opening it
  never decides anything by itself. To grant fewer permissions, use
  **Admin → Access requests** instead. After approval the same token works;
  the app needs no change. It can check with `GET /v1/registrations/me`.
- Self-requestable permissions: `send`, `read`, `manage`. `admin` never.
- Limits: 5 requests per address per hour, at most 20 waiting, unanswered
  requests expire after 7 days. A name that already exists is refused (409).
- Settings → Access requests: turn it off, choose who gets alerted, or
  auto-approve requests from given subnets that stay within chosen permissions
  (off by default). Revoking later is done under API access as usual.
- Source addresses are what the container sees: LAN clients keep their own
  address; apps on the Docker host itself may appear as the Docker bridge
  gateway (172.x.0.1), so choose auto-approve subnets with that in mind.

An agent with a shell can onboard itself: request access, wait for your
approval, then `claude mcp add … --header "Authorization: Bearer <token>"`.

## MCP

Streamable HTTP at `http://notify.lan:8930/mcp`, same bearer tokens and scopes.

```sh
claude mcp add --transport http hnl-notify http://notify.lan:8930/mcp \
  --header "Authorization: Bearer ntk_..."
```

Tools: `send_notification`, `preview_routing`, `get_notification`,
`list_notifications`, `list_attention`, `list_targets`, `retry_notification`,
`cancel_notification`. Resources: `notify://targets`,
`notify://notifications/{id}`. The server instructions tell agents not to
report `dispatched` as `delivered` and to reserve `critical` for emergencies.

## Pushover acknowledgement callbacks (optional)

Critical alerts are tracked by polling Pushover receipts every 30 s. For
instant acknowledgement, expose `/v1/callbacks/` through a public HTTPS proxy
and set the public address under **Settings** (a random callback secret is
generated). Without that, nothing changes except latency.

## Operations

- Logs: `docker logs hnl-notify` (one JSON object per line; every state change is an `event` line with ids).
- Health: `/health/live`, `/health/ready`. Metrics (Prometheus text): `/metrics`.
- Admin UI: `/admin` (session login, CSRF header, strict CSP). Admin API calls also accept a bearer token with the `admin` scope.
- Backup: `sqlite3 data/notify.sqlite3 ".backup data/notify-$(date +%F).sqlite3"` **plus** `secrets/master.key` (without it the stored Pushover secrets cannot be decrypted). History is kept 90 days (Settings).
- Delivery guarantee: at-least-once. If the container dies in the few ms after
  Pushover accepts a message but before the result is committed, that message
  is sent again after restart (logged as "send interrupted").

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
# on a shared host, cap it: systemd-run --user --scope -p MemoryMax=768M -p MemorySwapMax=0 -- .venv/bin/python -m pytest
```

UI work without touching real Pushover: `.venv/bin/python scripts/dev_server.py
--dir /tmp/notify-dev --demo --password devpassword123` serves the app on
`127.0.0.1:18931` with a fake Pushover and demo data.

Tests start a real uvicorn server with a fake Pushover API (httpx
MockTransport) and exercise REST, SSE, webhooks, restart recovery and MCP via
the official SDK client. Adding a provider: subclass `notify.providers.base.Provider`
(capabilities, `plan`, `send`, optional receipts/cancel/callbacks) and
register it in `notify/providers/__init__.py`.
