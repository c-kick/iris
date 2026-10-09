# hnl-notify

A self-hosted notification gateway for a home or small-office network.
Trusted services (Home Assistant, Node-RED, n8n, scripts, AI agents) submit
**notification intent** — message, severity, logical target — over REST or
MCP. hnl-notify routes it to devices, delivers through provider adapters,
retries, persists everything and reports status honestly: it only claims what
the provider can actually observe.

Delivery paths are **Pushover** (phone push) and **email** (SMTP, with file
attachments). Senders never see Pushover keys, priorities or SMTP settings;
adding ntfy, Home Assistant or Web Push later is a new adapter in
`notify/providers/`, not an API change.

Design, data model, lifecycle and the full Pushover capability analysis:
[docs/architecture.md](docs/architecture.md).

- Stack: Python 3.12, FastAPI, SQLite (WAL) as durable queue, official MCP SDK
- One container, one process, port **8930**; ~50 MiB resident idle (container limit 320 MiB, because attachments are held in memory while they are handled)
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
Resource limits (320 MiB, no extra swap, 0.5 CPU, 64 PIDs) are set in
`compose.yaml`.

Upgrading to 0.5.0 migrates the database in place. Rolling back to 0.4.0
requires restoring the pre-0.5.0 database backup (take one before upgrading).

Open **http://notify.lan:8930/admin**, enter the setup code and choose an
admin password. Then, in the admin UI:

1. **Providers**: for push, paste the API token of an application created at
   [pushover.net/apps/build](https://pushover.net/apps/build). For email, add
   an *Email (SMTP)* provider: host (e.g. `smtp.example.org`), port, security
   (`starttls`, `tls` or `none`), username and password if the server wants
   a login, and the sender address; then press *Test connection*. Give the
   session as much time as a slow relay needs (`timeout_seconds`, default 60).
2. **People & groups → Add person**: add yourself (e.g. `alex`).
3. **Devices → Add device**: name it (e.g. `alex-phone`), let it belong to
   you, paste your Pushover user key, press *Find devices* and pick the phone
   (leave it empty to reach all your Pushover devices). For email, add a
   second device (e.g. `alex-email`), pick the email provider and enter the
   address. By default an email device is used only when a sender asks for it
   (`kinds`, below) or names it directly; untick *Only when an app asks for
   this kind explicitly* to include it in normal sends too.
4. **API access → Create token**: one per sender (Home Assistant, Node-RED,
   agents…), or let senders request access themselves (see below). A token is
   shown once, with ready-to-paste snippets.
5. **Overview → Send test notification**.

No `.env` and no config file are needed: everything lives in
`data/notify.sqlite3`, with secrets (Pushover token, SMTP password, user keys) encrypted
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

## Email and attachments

Without `kinds` a notification goes to your push devices exactly as before.
Add `kinds` to choose the path: `["email"]`, `["push"]` or `["push", "email"]`.
A person without a device of a requested kind shows up under `suppressed` in
the response. Naming an email device (`device:alex-email`) as a target also
works without `kinds`.

```sh
# email only, with an inline attachment (base64, up to 10 MiB in total)
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' $N/v1/notifications -d '{
  "targets": ["user:alex"], "kinds": ["email"],
  "title": "Monthly report", "message": "The report is attached",
  "attachments": [{"filename": "report.pdf", "content_type": "application/pdf",
                   "data_base64": "'"$(base64 -w0 report.pdf)"'"}]
}'

# push and email, attachment fetched from a URL
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' $N/v1/notifications -d '{
  "targets": ["user:alex"], "kinds": ["push", "email"],
  "title": "Front door", "message": "Motion at the front door",
  "attachments": [{"url": "http://homeassistant.lan:8123/api/camera_proxy/camera.door?token=...",
                   "filename": "door.jpg"}]
}'
```

- At most 10 attachments, 10 MiB decoded in total. Over MCP, inline data is
  limited to 256 KiB; use URLs. Request bodies above 15 MiB (1 MiB on every
  other route) get 413.
- URL attachments are **disabled until an admin allows hosts**: *Settings →
  Hosts allowed for attachment URLs* (`homeassistant.lan:8123`, a host name,
  or an IP/CIDR for URLs that use an IP address). Redirects are re-checked,
  loopback and link-local addresses are always refused, and the URL (which
  may hold a token) is never stored or logged.
- Pushover shows the first image up to 5 MiB; other attachments are listed in
  the delivery's `capability_gaps`. Email carries all of them.
- Email never reports more than "the mail server accepted it" (`dispatched`);
  there is no delivery, read or acknowledgement state and no recall.
- Attachment bytes are kept in the database for 7 days (*Settings → Keep
  attachments for*), then deleted. The database file does not shrink when that
  happens, so size the `data/` volume for your peak.

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
message text with Pushover and email (only http(s) links are clickable); taps
are not reported back. OpenAPI docs:
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

### Radarr / Sonarr / Lidarr / Prowlarr

The *arr apps cannot send our JSON, so hnl-notify accepts their own webhook
format at `/v1/ingest/arr` and writes the notification itself. Give the app its
own client with the `send` scope, then in the app go to *Settings → Connect →
+ → Webhook*:

- **URL**: `http://notify.lan:8930/v1/ingest/arr?targets=user:alex`
  (repeat `targets=` or comma-separate for more)
- **Method**: POST
- **Username**: anything (e.g. `radarr`) · **Password**: the `ntk_...` token

Tick the triggers you want and press *Test*: you get a "Radarr: Test" push.

| Event | Severity | Message |
|---|---|---|
| Grab | low | title (year), quality · group · size, indexer |
| Download / Upgrade | normal | title (year), quality · group · size |
| Import needs attention (manual interaction) | high | title plus Radarr's reasons |
| Health (error / warning / other) | high / normal / low | the health message, wiki link |
| Added, deleted, file deleted, health restored, update, test | low | |
| Rename / Retag | min | |

Movies link to IMDb (or TMDB), series to IMDb (or TheTVDB). Grab, Download
and manual-interaction events are keyed on the download id, so a re-sent
webhook does not notify twice. Sonarr names episodes (`S02E03 · Title`),
Lidarr names artist and album; unknown events still arrive, titled with the
event name.

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

`send_notification` and `preview_routing` take `kinds`; `send_notification`
also takes `attachments` (URLs recommended, inline data up to 256 KiB). Tools:
`send_notification`, `preview_routing`, `get_notification`,
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
- Backup: `sqlite3 data/notify.sqlite3 ".backup data/notify-$(date +%F).sqlite3"` **plus** `secrets/master.key` (without it the stored Pushover and SMTP secrets cannot be decrypted). Backups include attachment bytes that have not yet been purged. History is kept 90 days (Settings).
- Delivery guarantee: at-least-once. If the container dies in the few ms after
  Pushover accepts a message but before the result is committed, that message
  is sent again after restart (logged as "send interrupted").

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -p no:warnings -o addopts=""   # addopts has -q, which hides the summary line
# on a shared host, cap it: systemd-run --user --scope -p MemoryMax=768M -p MemorySwapMax=0 -- .venv/bin/python -m pytest -o addopts=""
```

UI work without touching real Pushover: `.venv/bin/python scripts/dev_server.py
--dir /tmp/notify-dev --demo --password devpassword123` serves the app on
`127.0.0.1:18931` with a fake Pushover and demo data.

`aiosmtpd` (a dev dependency) provides a real local SMTP server for the email
tests. Tests start a real uvicorn server with a fake Pushover API (httpx
MockTransport) and exercise REST, SSE, webhooks, restart recovery and MCP via
the official SDK client. Adding a provider: subclass `notify.providers.base.Provider`
(capabilities, `plan`, `send`, optional receipts/cancel/callbacks) and
register it in `notify/providers/__init__.py`.
