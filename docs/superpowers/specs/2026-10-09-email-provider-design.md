# Email delivery path and attachments — design

Date: 2026-10-09 · Status: draft for review · Target version: 0.5.0

## Intent

Add email as a second delivery path next to Pushover. Senders (apps, HA,
agents) choose push, email, or both per notification. Email carries file
attachments, which makes richer notifications possible (reports, snapshots,
logs). The SMTP server is configured entirely in the admin UI.

Success criteria:

1. An admin can add an SMTP server (host, port, security, credentials, From)
   in `/admin`, test the connection, and add email-address devices for people.
2. Existing senders that do not mention email behave exactly as before
   (no surprise emails).
3. A sender can say `via: ["email"]` or `via: ["push", "email"]` and attach
   files inline (base64) or by URL.
4. Pushover deliveries show the first image attachment; everything that a
   path cannot carry is reported as a `capability_gap`, never silently dropped.
5. Public REST/MCP interfaces stay provider-agnostic (no "pushover"/"smtp"
   words in what a sender must send).

## Decisions taken in brainstorming

| Question | Decision |
|---|---|
| Default path when `via` is omitted | Devices flagged `opt_in` are skipped; email devices default to `opt_in`, push devices do not. Existing behaviour is preserved. |
| How senders supply attachments | Inline base64 **or** a URL that hnl-notify fetches once at intake. |
| Attachments on Pushover | First `image/*` attachment ≤ 5 MB is sent as the Pushover attachment; others become a capability gap. |

## 1. Delivery kinds and routing

- `Provider.kind: str` — new class attribute. `PushoverProvider.kind = "push"`,
  `EmailProvider.kind = "email"`. Kinds are the only provider-level concept
  exposed to senders. Valid kinds: `push`, `email` (`KINDS` constant in
  `models.py`; extended when a new kind of adapter is added).
- `DeviceConfig.opt_in: bool | None = None`. `None` means "use the provider
  kind default": `True` for `email`, `False` for `push`. The effective value is
  computed by a helper `effective_opt_in(device, provider_kind)`.
- `NotificationIn.via: list[Literal["push", "email"]] | None = None`
  (max 2, de-duplicated; empty list rejected with 422).
- `Router.resolve(targets, severity, via)`:
  - `via is None`: a device is included iff it is not effectively opt-in.
    Skipped opt-in devices are **not** listed in `suppressed` (it is the
    normal case and would clutter every response).
  - `via` given: a device is included iff its provider kind is in `via`
    (opt-in or not). Devices of other kinds are silently not selected.
  - For each target expanded with `via` given, if the target yields no
    device of a requested kind, add a `suppressed` entry
    `{"target", "via", "reason": "no email device"}` (one per missing kind).
    Existing rules (disabled device, `min_severity`, channel floor) apply
    unchanged and before the kind filter.
  - Device targets addressed directly (`device:alex-email`) are always
    eligible regardless of `opt_in` when `via` is omitted — explicitly naming
    a device is an opt-in. With `via` given, the kind filter still applies.
- The router needs the provider kind per device: `Router` receives a
  `kind_of: Callable[[str], str]` (provider name → kind) from the service,
  built from the live provider instances, so routing stays config-only and
  testable.
- `routing_json` stores `via` so history shows what was asked.

## 2. Email provider (`notify/providers/email.py`)

`type = "email"`, `kind = "email"`, `label = "Email (SMTP)"`,
`max_concurrency = 2`.

Settings (`settings_schema`, rendered by the existing generic admin form):

| key | kind | default | notes |
|---|---|---|---|
| `host` | text | — | required |
| `port` | int | 587 | |
| `security` | select | `starttls` | `starttls` / `tls` (implicit, 465) / `none` |
| `username` | text | empty | empty = no AUTH |
| `password` | secret | empty | AES-GCM at rest like other secrets |
| `from_address` | text | — | required, validated as an address |
| `from_name` | text | `hnl-notify` | |
| `subject_prefix` | text | empty | e.g. `[home]` |
| `timeout_seconds` | int | 20 | per SMTP session |
| `verify_tls` | check | true | allow self-signed relays when off |

The generic admin form must learn `select` options from the schema
(`options: [[value, label], ...]`) and `check` defaults; the field kinds
already exist in `app.js`.

Device address (`address_schema`): `to` (text, single address, validated
with `email.utils.parseaddr` + a simple `local@domain` check). Not secret.

Message construction (`email.message.EmailMessage`):

- `Subject`: `subject_prefix` + title, or the first line of the message
  truncated to 120 chars when there is no title. Header-injection safe via
  `EmailMessage` (no raw concatenation; CR/LF stripped).
- `From`: `formataddr((from_name, from_address))`; `To`: device address.
- `Date`: notification `created_at`; `Message-ID`:
  `<{delivery_id}@{from_domain}>` (stable across retries so a duplicate after
  a crash is recognisable); `X-Notify-Notification: {notification_id}`.
- Severity `high`/`critical`: `Importance: high`, `X-Priority: 1`.
  `min`/`low`: `Importance: low`.
- Body: `text/plain` (message, then `url_title: url`, then each URL action as
  `label: url`) plus `text/html` alternative (escaped message with line
  breaks, links as buttons). No remote images, no templates.
- Attachments: every stored attachment, `Content-Disposition: attachment`
  with the original filename and content type.

Sending: stdlib `smtplib` (`SMTP` or `SMTP_SSL`) inside `asyncio.to_thread`;
STARTTLS when `security=starttls` (fail permanently with a clear message if
the server does not offer it — no silent downgrade). One SMTP session per
delivery (volume is household-scale).

Outcomes:

| Situation | Outcome |
|---|---|
| `sendmail` accepts the recipient | `success`, `provider_message_id` = Message-ID |
| `SMTPRecipientsRefused`/`SMTPSenderRefused`/`SMTPDataError` with 5xx | `permanent` |
| same with 4xx | `retryable` |
| `SMTPAuthenticationError` | `retryable` (config can be fixed live; mirrors Pushover's missing-token handling) with error text saying so |
| connect/timeout/TLS/`OSError` | `retryable` |
| `config_problems()` non-empty | `retryable` (as Pushover) |

`request_summary`: host, port, security, recipient domain only
(`…@example.org`), subject length, attachment count and total bytes.

Capabilities: `accept_confirmation: true`; `delivery_receipt`,
`read_receipt`, `acknowledgement`, `interactions`, `cancel_after_dispatch`:
unsupported; `actions: {max: 0, inline_links: true}`; `links: {max: 10}`;
`ttl: {supported: false, note: "email cannot be recalled; expiry applies only
before sending"}`; `attachments: {max_count: 10, max_bytes: 10 MiB}`;
`max_message_length: null`.

`plan()`: observable states = `BASE_OBSERVABLE`; gaps for `require_ack`
("email cannot report acknowledgement; not tracked") and actions without URL.

`validate_address()` / provider check: the admin "Test" calls a new
`Provider.test_connection()` (default: `{"supported": False}`); for email it
connects, negotiates TLS, logs in, issues `NOOP`, and quits — no mail sent.
Exposed as `POST /admin/api/providers/{name}/test`. Sending a real test mail
uses the existing admin test-send to a device.

## 3. Attachments

### Request schema

```json
"attachments": [
  {"filename": "report.pdf", "content_type": "application/pdf", "data_base64": "..."},
  {"url": "http://ha.lan:8123/api/camera_proxy/camera.door?token=...", "filename": "door.jpg"}
]
```

`AttachmentIn`: exactly one of `data_base64` / `url`; `filename` required for
base64, optional for URL (derived from the final URL path, else
`attachment-N`); `content_type` optional (from response header, else
`mimetypes.guess_type`, else `application/octet-stream`). Filenames are
sanitised (basename only, control chars stripped, ≤ 200 chars).

Limits (constants in `notify/attachments.py`; not configurable in v1):
max 10 attachments, max 10 MiB decoded total. The JSON body limit on
`POST /v1/notifications` must allow ~14 MiB for base64 overhead.

### Intake (`notify/attachments.py`)

`async def prepare(items, http) -> list[Attachment]` runs in the API/MCP layer
**before** `NotifyService.submit` (which stays synchronous) and **after** the
idempotency short-circuit — `submit` gains an `attachments` argument and the
API calls a cheap `service.idempotent_replay(client, key)` first so replays
never refetch.

- base64: strict decode (`validate=True`); error → 422 naming the index.
- URL: `http`/`https` only; `GET` with 15 s total timeout, up to 3 redirects,
  streamed with the running total checked against the remaining budget
  (abort → 422 "attachment N exceeds size limit"). Non-2xx → 422 with status.
  Only the sender's URL credentials (query string) are used; no hnl-notify
  credentials are added.
- **Trust decision:** private/LAN addresses are allowed (fetching from HA on
  the LAN is the main use case). Senders already hold a `send` token issued
  by the admin; the fetch result is only ever sent to configured devices,
  never returned to the sender. Documented in `architecture.md`. Loopback and
  link-local (`127.0.0.0/8`, `::1`, `169.254.0.0/16`) are rejected, since
  they would reach the service's own container rather than a LAN host.
- `dry_run` validates attachments (decode, count, size) but does not fetch
  URLs; the plan lists them as `{"url": ..., "fetched": false}`.

### Storage

New table:

```sql
CREATE TABLE IF NOT EXISTS attachments (
  notification_id TEXT NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  filename TEXT NOT NULL,
  content_type TEXT NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  source TEXT NOT NULL,          -- 'inline' | 'url'
  data BLOB,                     -- NULL once purged
  purged_at TEXT,
  PRIMARY KEY (notification_id, idx)
);
```

Inserted in the same transaction as the notification. `OutboundMessage`
gains `attachments: list[Attachment]` (filename, content_type, data) loaded
by `message_for` only when the provider's `plan` needs them — simplest
correct form: `message_for` loads them for real sends; for planning, a
metadata-only list (data `None`) is enough.

Purge: the existing retention sweep also sets `data=NULL, purged_at=now`
for attachments older than `server.attachment_retention_days`
(new `ServerConfig` field, default 7, 1–90, editable on the Settings page)
**and** whose notification has no delivery in `queued`/`sending`. Full row
removal follows the notification's own retention via `ON DELETE CASCADE`.
A delivery sent after purge (manual retry) sends without the missing files
and appends the gap "attachments purged after N days: k not sent".

API output: `GET /v1/notifications/{id}` includes
`attachments: [{filename, content_type, size, sha256, source, purged}]`.
No download endpoint in v1.

### Pushover

`PushoverProvider.plan`: if attachments exist, pick the first `image/*` with
size ≤ 5 MiB (Pushover limit, checked 2026-10-09 against pushover.net/api:
`attachment` multipart or `attachment_base64` + `attachment_type`). Gap for
the rest: "pushover carries one image ≤ 5 MB; N attachments not sent".
`send` uses `attachment_base64` + `attachment_type` form fields so the request
stays `application/x-www-form-urlencoded` and the fake API keeps working.
`_redact` replaces the base64 with its byte count.

## 4. Surfaces

- REST `NotificationIn`: `via`, `attachments`. `GET /v1/targets`: each device
  gets `kind` and `opt_in` (effective). `GET /v1/providers`: `kind`.
- MCP `send_notification`: `via`, `attachments` (same schema; docstring
  points agents at the URL form). `preview_routing`: `via`.
  `list_targets` reflects kind/opt_in.
- `/v1/ingest/arr`: accepts `via` query parameter (comma-separated), passed
  through. No attachments.
- Admin UI:
  - provider form: email type appears automatically via `REGISTRY`;
    `select`/`check` schema kinds wired; "Test connection" button.
  - device form: `opt_in` checkbox labelled "Only when an app asks for
    email/push explicitly", default shown per kind.
  - dashboard quick-send: "Via" select (default / push / email / both).
  - Settings: "Keep attachments for (days)".
  - Notification detail: attachment list (name, type, size, purged).
- Self-registration alerts and other internal notifications: unchanged
  (no `via`, so push only).

## 5. Error handling summary

- Bad `via` value, bad base64, too many/too large attachments, URL fetch
  failure → 422 before anything is stored.
- SMTP misconfiguration → delivery stays queued and retries (like Pushover),
  visible in attention list and `/health/ready` provider problems.
- SMTP permanent rejection → delivery `failed` with the server's reply text.
- Crash between SMTP accept and commit → possible duplicate; Message-ID is
  stable, so mail clients/servers that dedupe on it will collapse it.

## 6. Testing

- `tests/test_email.py`: message construction (headers, both parts,
  attachments, header-injection in title, severity headers), outcome mapping
  per SMTP exception (stub `smtplib.SMTP` via monkeypatch), STARTTLS-required
  failure, `test_connection`, config problems.
- Integration: a real in-process SMTP sink (`aiosmtpd`, dev dependency only)
  through the uvicorn harness for one end-to-end send with an attachment.
- `tests/test_routing.py` (new) or extend `test_api.py`: `via`/`opt_in`
  matrix, direct device addressing, suppressed "no email device" entries.
- Attachments: base64 errors, count/size limits, URL fetch via the existing
  `httpx.MockTransport` (2xx, 404, oversize stream, redirect chain > 3,
  loopback rejected), idempotent replay does not refetch, dry run does not
  fetch, purge + retry gap.
- Pushover: image attachment in form, non-image gap, > 5 MB gap, redaction.
- Admin: email provider round-trip with password secret kept on blank edit,
  device `opt_in` round-trip, test-connection endpoint.
- MCP: `send_notification` with `via` + attachment.

## 7. Documentation

- `docs/architecture.md`: kinds and `via`/`opt_in` routing rule; "What email
  can and cannot report" table; attachments (limits, storage, purge, URL
  trust decision); update the diagram line `pushover | later ntfy/HA/email`.
- `README.md` and `config/notify.example.yaml`: email provider and device
  example, `via` and attachment examples.
- `~/documentation/` project page for hnl-notify: note the email path.
- Version bump to 0.5.0.

## Out of scope

Multiple recipients per email device (use several devices or a group),
inbound mail / replies, editable templates, multipart request upload,
attachment download endpoint, per-provider attachment limits beyond the two
documented.
