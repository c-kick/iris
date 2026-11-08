# REST API, integrations, self-registration and MCP

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
curl -s -H "Authorization: Bearer $TOKEN" $N/v1/attention                   # failed / stuck / awaiting ack (dismissible in Admin → Activity)
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
also takes `attachments` (URLs recommended, inline data up to 256 KiB),
`reply`, `in_reply_to` and `reply_handle` (the send result carries `reply_handle` once), plus `send_at`,
`delay` and `max_late_seconds` (see [scheduling](scheduling.md)). Tools:
`send_notification`, `preview_routing`, `get_notification`,
`list_notifications`, `list_attention`, `list_targets`, `retry_notification`,
`cancel_notification`, `update_scheduled_notification`, `get_replies` (takes `reply_handle`), and for group chats
(scope `chat`) `group_post`, `group_read`, `group_history` and `group_leave`. Resources: `notify://targets`,
`notify://notifications/{id}`. The server instructions tell agents not to
report `dispatched` as `delivered` and to reserve `critical` for emergencies.

[← back to the README](../README.md)
