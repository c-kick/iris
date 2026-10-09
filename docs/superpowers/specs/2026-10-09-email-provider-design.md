# Email delivery path and attachments — design

Date: 2026-10-09 · Status: revision 2 (after staff review + ambassador ruling) ·
Target version: 0.5.0

## Intent

Add email as a second delivery path next to Pushover. Senders (apps, HA,
agents) choose push, email, or both per notification. Email carries file
attachments, which makes richer notifications possible (reports, snapshots,
logs). The SMTP server is configured entirely in the admin UI.

Success criteria:

1. An admin can add an SMTP server (host, port, security, credentials, From)
   in `/admin`, test the connection, and add email-address devices for people.
2. Existing senders that do not mention email behave exactly as before
   (no surprise emails, unchanged response shapes).
3. A sender can say `kinds: ["email"]` or `kinds: ["push", "email"]` and attach
   files inline (base64) or by URL (from admin-allowed hosts).
4. Pushover deliveries show the first image attachment; everything a path
   cannot carry is reported as a `capability_gap`, never silently dropped.
5. Public REST/MCP interfaces stay provider-agnostic.
6. The service stays within its container budget and never sends one
   delivery twice concurrently.

## Decisions

| Question | Decision |
|---|---|
| Default path when `kinds` is omitted | Devices that are effectively `opt_in` are skipped; email devices default to `opt_in`, push devices do not. |
| Request field name | `kinds` — `via` already means "expansion path" in routing output. |
| Attachment input | Inline base64, or URL fetched once at intake from **admin-allowlisted hosts** only. |
| Attachments on Pushover | First `image/*` ≤ 5 MiB is sent as the Pushover attachment; the rest become gaps. |
| Attachment storage | SQLite BLOBs (keeps "one DB file + key" backup); purged after N days. Files on disk considered and rejected. |
| Memory budget | Intake serialised (1 at a time), email sends 1 at a time, container `mem_limit` 192m → 320m. |

## 1. Delivery kinds and routing

- `Provider.kind: ClassVar[str]` — `PushoverProvider.kind = "push"`,
  `EmailProvider.kind = "email"`. `KINDS = ("push", "email")` in `models.py`.
- `DeviceConfig.opt_in: bool | None = None`. `None` = kind default
  (`email` → True, `push` → False). Helper on `NotifyConfig`:
  `device_kind(name) -> str` = `REGISTRY[providers[dev.provider].type].kind`,
  and `device_opt_in(name) -> bool`. The router uses these; no callables, no
  live provider instances (routing stays config-only and reconfigures with
  the config). To avoid an import cycle, `config.py` imports `REGISTRY`
  lazily inside the helper.
- `NotificationIn.kinds: list[Literal["push","email"]] | None = None`
  (1–2 items, de-duplicated; empty list → 422).
- `Router.resolve(targets, severity, kinds)` rules, applied after the existing
  rules (disabled device, device `min_severity`, channel floor):
  - `kinds is None`: include a device iff it is not opt-in, **except** a
    device named as a *top-level* target (`device:alex-email` in the request's
    `targets`) is always eligible. Devices reached through user/group/channel
    expansion obey `opt_in`. Opt-in devices skipped this way are not listed in
    `suppressed`.
  - `kinds` given: include a device iff its kind ∈ `kinds` (opt-in ignored).
    A *top-level* device target of a non-requested kind → suppressed
    `{"target", "via", "reason": "device kind push not requested"}`.
  - With `kinds` given, for each top-level target and each requested kind
    that the target's expansion yields no device for (before severity
    floors), one suppressed entry `{"target": <top-level>, "via":
    <top-level>, "reason": "no email device"}`.
  - `suppressed[].via` and `routing_json["via"]` keep their existing meaning
    (expansion path / device → targets). `routing_json` gains `"kinds"`.
- Regression guarantee: requests without `kinds`, against configs without
  email devices, produce byte-identical routing output to 0.4.0.

## 2. Email provider (`notify/providers/email.py`)

`type = "email"`, `kind = "email"`, `label = "Email (SMTP)"`,
`max_concurrency = 1`, `secret_settings = frozenset({"password"})`.

Settings (`settings_schema`):

| key | kind | default | notes |
|---|---|---|---|
| `host` | text | — | required |
| `port` | int | 587 | |
| `security` | select | `starttls` | options `starttls` / `tls` (implicit) / `none` |
| `username` | text | "" | empty = no AUTH |
| `password` | secret | "" | encrypted at rest |
| `from_address` | text | — | required, validated |
| `from_name` | text | `hnl-notify` | |
| `subject_prefix` | text | "" | e.g. `[home]` |
| `timeout_seconds` | int | 60 | **total** SMTP session deadline, 10–300 |
| `verify_tls` | check | true | off allows self-signed relays |

`config_problems()`: missing host/from_address; invalid from_address;
`security == "none"` with a username ("would send the password unencrypted;
use starttls or tls"); unknown settings; select value outside options.

Device address (`address_schema`): `to` — single address, validated
(`email.utils.parseaddr` + `local@domain` with a dot in the domain, no CR/LF).

### Message construction (`EmailMessage`, `email.policy.SMTP`)

- Every header-bound value (title, subject_prefix, from_name, filenames) is
  passed through `_header_safe()` first: control characters (incl. CR/LF)
  replaced by spaces, collapsed, trimmed. The adapter must never raise on
  sender input (an exception would burn retries as "adapter error").
- `Subject`: prefix + title, or the first line of the message (≤ 120 chars).
- `From` `formataddr((from_name, from_address))`, `To`, `Date`
  (created_at), `Message-ID: <{delivery_id}@{from_domain}>` (stable across
  retries), `X-Notify-Notification: {notification_id}`.
- Severity `high`/`critical` → `Importance: high`, `X-Priority: 1`;
  `min`/`low` → `Importance: low`.
- Body: `text/plain` (message; then `url_title: url`; then each URL action
  `label: url`) + `text/html` alternative: message escaped with `<br>`, links
  as simple buttons. Only `http`/`https` URLs become `<a href>`
  (`html.escape(quote=True)`); anything else is rendered as escaped text. No
  remote images, no templates.
- Attachments: each with its stored filename/content type
  (`maintype/subtype` split from the validated type).

### Sending

`asyncio.to_thread(self._send_sync, ...)` using `smtplib.SMTP` /
`SMTP_SSL` (`debuglevel` 0 always). A monotonic deadline of
`timeout_seconds` covers the whole session: before each phase (connect,
EHLO, STARTTLS, AUTH, MAIL/RCPT/DATA) the socket timeout is set to the
remaining time; past the deadline → retryable "SMTP session exceeded N s".
If the server advertises `SIZE` and the encoded message exceeds it →
permanent "message is X MB, server accepts at most Y MB".

`Provider.send_deadline_seconds: float | None` (new, default `None`).
`claim_due` uses lease `max(server.lease_seconds, send_deadline + 30)`, so a
slow SMTP session can never be reclaimed while still running. Pushover keeps
the server lease. A restart mid-send cannot cancel the thread; the delivery
may be sent twice — at-least-once, as documented; the stable Message-ID
lets mail systems collapse it.

Outcomes:

| Situation | Outcome |
|---|---|
| accepted | `success`, `provider_message_id` = Message-ID |
| `SMTPResponseException` family (RecipientsRefused, SenderRefused, DataError, generic) 5xx | `permanent` with the server reply |
| same, 4xx | `retryable` |
| `SMTPAuthenticationError` | `retryable`, `retry_after=900`, "login rejected; check username/password in the admin UI" |
| STARTTLS required but not offered (`SMTPNotSupportedError`) | `retryable`, config-error text |
| `SMTPConnectError`, `SMTPHeloError`, `SMTPServerDisconnected`, `ssl.SSLError`, `OSError`, timeout, deadline | `retryable` |
| SIZE exceeded | `permanent` |
| `config_problems()` / `device_problems()` | `retryable` (as Pushover) |

`request_summary`: host, port, security, recipient domain (`…@example.org`),
subject length, attachment count and bytes, encoded size.

Capabilities: `accept_confirmation: true`; delivery/read receipt,
acknowledgement, interactions, cancel_after_dispatch unsupported;
`actions: {max: 0, inline_links: true}`; `links: {max: 10}`;
`ttl: {supported: false, note: "email cannot be recalled; expiry applies only
before sending"}`; `attachments: {max_count: 10, max_bytes: 10485760,
types: ["*/*"]}`; `max_message_length: null`.

`plan()`: observable `BASE_OBSERVABLE`; gaps for `require_ack` and actions
without URL; actions with a non-http(s) URL ("shown as text").

### Test connection

New `Provider.test_connection() -> dict` (default `{"supported": False}`).
Email: connect, TLS, AUTH, `NOOP`, `QUIT` within the deadline; returns
`{"supported": true, "ok": bool, "error"?, "size_limit"?: int,
"tls": "starttls|tls|none"}`. Endpoint `POST /admin/api/providers/{name}/test`
(admin). Real test mail = existing admin test-send to a device.

## 3. Attachments

### Request schema

```json
"attachments": [
  {"filename": "report.pdf", "content_type": "application/pdf", "data_base64": "..."},
  {"url": "http://ha.lan:8123/api/camera_proxy/camera.door?token=...", "filename": "door.jpg"}
]
```

`AttachmentIn` (`extra="forbid"`): exactly one of `data_base64` (max
14 200 000 chars) / `url` (http/https, ≤ 2048); `filename` required for
base64, optional for URL (basename of the final URL path, else
`attachment-N`); `content_type` optional. Filenames: basename only, control
chars stripped, ≤ 200 chars, non-empty. Content types (sender-given or from
the response header, parameters dropped) are lowercased and must match
`^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$`, else `mimetypes.guess_type`, else
`application/octet-stream`.

Limits (constants in `notify/attachments.py`): 10 attachments, 10 MiB
decoded total. MCP inline `data_base64` limited to 256 KiB per notification
(agents should use URLs).

### Request size

A pure-ASGI middleware (`notify/bodylimit.py`) rejects with 413 when
`Content-Length` exceeds the route's cap, and counts streamed bytes for
chunked bodies: 15 MiB for `POST /v1/notifications`, 1 MiB for everything
else (including `/mcp`, `/v1/ingest/arr`, admin). No such limit exists today.

### Submit flow (one entry point)

`NotifyService.submit` is split:

1. `plan_submit(client, req) -> Replay | SubmitPlan` (sync): idempotent
   replay, expiry, routing, per-device plans/gaps — everything the current
   `submit` does before the transaction. Attachment *metadata* known so far
   (inline: name/type/size; URL: unknown) is fed into `provider.plan`.
2. If the plan has ≥ 1 delivery and attachments are present and not dry-run:
   `await attachments.prepare(req.attachments, ...)` (inline decode + URL
   fetch), under a global `asyncio.Semaphore(1)`. Then re-run the per-device
   `plan()` with the real metadata so gaps are accurate.
3. `commit(plan, attachments)` (sync): the existing transaction plus
   attachment rows. A plan with 0 deliveries stores no attachment bytes
   (metadata only).

`async submit_async(client, req)` wraps 1–3 and is the only entry point for
REST, MCP and `/v1/ingest/arr`. A concurrent duplicate idempotency key still
resolves via the existing `IntegrityError` path (it only wastes one fetch).

Dry run: inline attachments are decoded and checked; URL attachments are
validated against the allowlist but **not fetched**; Pushover gap for them:
"attachment type and size unknown until sent; pushover carries one image
≤ 5 MB".

### URL fetching

- `server.attachment_url_hosts: list[str]` — admin allowlist (host names,
  `host:port`, or CIDRs), edited under Settings. **Empty (default) = URL
  attachments disabled** (422 "URL attachments are not enabled; an admin can
  allow hosts under Settings"). Rationale: self-registered apps can be
  auto-approved with `send`, so fetching is an admin opt-in.
- Each hop (initial + up to 3 redirects, followed manually) must match the
  allowlist; resolved addresses in `127.0.0.0/8`, `::1`, `0.0.0.0/8`, `::`,
  `169.254.0.0/16`, `fe80::/10` (and IPv4-mapped forms) are always refused.
  No IP pinning (the admin has named the hosts; DNS rebinding is out of scope).
- Dedicated `httpx.AsyncClient(trust_env=False, follow_redirects=False)`.
- One 15 s **total** budget for all URL fetches of a request; streamed with
  the running total checked against the remaining byte budget.
- Errors → 422 naming the attachment index, with the upstream HTTP status
  code at most (no body). The URL is never stored or logged (it may carry
  tokens); only `source='url'` and the host are kept.
- Name resolution goes through an injectable `resolve(host) -> list[ip]`
  seam so tests can exercise the deny ranges.

### Storage

```sql
CREATE TABLE IF NOT EXISTS attachments (
  notification_id TEXT NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  filename TEXT NOT NULL,
  content_type TEXT NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  source TEXT NOT NULL,          -- 'inline' | 'url'
  source_host TEXT,
  data BLOB,                     -- NULL once purged (or never stored)
  purged_at TEXT,
  PRIMARY KEY (notification_id, idx)
);
```

Created via `SCHEMA` (`CREATE TABLE IF NOT EXISTS`); `PRAGMA
journal_size_limit = 67108864` added at open. Trade-off documented: a 10 MiB
insert briefly holds the DB lock; the DB file does not shrink after purge
(no auto_vacuum).

`OutboundMessage.attachments: list[AttachmentData]` (filename,
content_type, size, data | None). `message_for(..., with_data=True)` loads
bytes for sends; plans use metadata only.

Purge: the retention sweep sets `data=NULL, purged_at=now` for attachments
older than `server.attachment_retention_days` (default 7, 1–90, Settings)
whose notification has no delivery in `queued`/`sending`. Rows are removed
with the notification via cascade. `core.retry()` appends the gap
"attachments purged after N days: k not sent" to the requeued delivery's
`capability_gaps` in the same transition when any attachment is purged.

API output: `GET /v1/notifications/{id}` adds
`attachments: [{filename, content_type, size, sha256, source, purged}]`.
No download endpoint.

### Pushover

- `capabilities()` adds `attachments: {max_count: 1, max_bytes: 5242880,
  types: ["image/*"]}`.
- `plan()`: first `image/*` ≤ 5 MiB is carried; gap for the rest
  ("pushover carries one image ≤ 5 MB; N attachments not sent").
- `send`: `attachment_base64` + `attachment_type` form fields
  (urlencoded). Documented by Pushover as the non-multipart alternative;
  urlencoded use is **verified with one live call** during implementation —
  if it fails, switch to multipart `attachment`. `_redact` replaces the data
  with its byte count.

## 4. Surfaces

- REST `NotificationIn`: `kinds`, `attachments`. `GET /v1/targets`: each
  device gets `kind` and effective `opt_in`. `GET /v1/providers`: `kind`.
- MCP `send_notification`: `kinds`, `attachments` (URL form recommended;
  inline ≤ 256 KiB). `preview_routing`: `kinds`. `list_targets` reflects
  kind/opt_in.
- `/v1/ingest/arr`: `kinds` query parameter (comma-separated). No attachments.
- Admin API: `DeviceIn.opt_in: bool | None`; `ServerIn.attachment_retention_days`,
  `ServerIn.attachment_url_hosts`; `put_provider` coerces `check` to bool,
  `int` to int, and validates `select` against `options`;
  `POST /api/providers/{name}/test`. `put_device` persists `opt_in`.
- Admin UI (`app.js`) — explicit work, nothing appears automatically today:
  - The Pushover-only "delivery" page becomes a **Providers** page listing
    every configured provider, with "Add provider" per `REGISTRY` type and
    edit/delete per instance; the Pushover form keeps its current behaviour.
  - Field mappers pass `options` (select) and booleans (check) from schemas.
  - "Test connection" button for providers that support it.
  - Device form: provider picker (configured providers), address fields per
    provider schema, provider-neutral "check" button/text, `opt_in`
    checkbox ("Only when an app asks for this kind explicitly"), default
    shown per kind.
  - Nav label "Pushover" → "Providers"; dashboard pipeline lists every provider.
  - Dashboard quick-send: "Kinds" select (default / push / email / both).
  - Settings: "Keep attachments for (days)", "Hosts allowed for attachment
    URLs".
  - Notification detail: attachment list (name, type, size, purged).
- Internal notifications (registration alerts): unchanged (no `kinds`).

## 5. Error handling summary

- Bad `kinds`, bad base64, limits, disallowed or failing URL → 422 before
  anything is stored; oversized body → 413.
- SMTP misconfiguration (incl. missing STARTTLS, auth) → delivery stays
  queued and retries; visible in attention list and provider health. Note:
  `/health/ready` reports 503 while any provider has problems (existing
  behaviour, also for a half-configured email provider).
- SMTP permanent rejection / SIZE exceeded → delivery `failed` with reason.
- Crash or restart mid-send → possible duplicate; stable Message-ID.

## 6. Deployment

- `compose.yaml`: `mem_limit`/`memswap_limit` 192m → 320m (peak per
  10 MiB request ≈ 50 MB in Python; intake and email sending are each
  serialised to one at a time).
- Version 0.5.0; DB backup before deploy as in previous releases.

## 7. Testing

- `tests/test_email.py`: construction (headers, both parts, attachments,
  severity headers, only http(s) hrefs), `_header_safe` property tests (the
  adapter never raises on CR/LF, control chars, non-ASCII in title,
  filename, content type), outcome mapping per exception (stub
  `smtplib.SMTP`), deadline exceeded, STARTTLS missing, SIZE exceeded,
  `test_connection`, config problems incl. cleartext auth.
- Integration: in-process `aiosmtpd` sink (dev dependency) through the
  uvicorn harness: one end-to-end send with an attachment.
- Lease: SMTP stub sleeping past `server.lease_seconds` (but within the
  provider deadline) → exactly one send.
- Routing: `kinds`/`opt_in` matrix, top-level device bypass, group member
  not bypassing, "no email device" and "kind not requested" entries,
  regression: 0.4.0-shaped requests give identical `routing_json`.
- Attachments: base64 (strict, whitespace stripped), count/size limits,
  content-type validation, URL fetch via `MockTransport` with injectable
  resolver (allowlist off → 422, host not allowed, redirect to disallowed
  host, > 3 redirects, oversize stream, total-time budget, loopback and
  `::ffff:127.0.0.1` refused), idempotent replay / zero deliveries / dry run
  do not fetch, purge + retry gap.
- Body limit: Content-Length over cap, chunked over cap, `/mcp` 1 MiB.
- Pushover: image in form, non-image gap, > 5 MiB gap, redaction,
  capabilities.
- Admin: email provider round-trip (password kept on blank edit, select/check
  coercion and validation), device `opt_in` round-trip, server settings
  round-trip, test-connection endpoint.
- MCP: `send_notification` with `kinds` + URL attachment; inline > 256 KiB
  rejected.

## 8. Documentation

- `docs/architecture.md`: kinds and routing rule; "What email can and cannot
  report" table; attachments (limits, storage trade-off, purge, URL
  allowlist); body limits; diagram line.
- `README.md`, `config/notify.example.yaml`: email provider/device, `kinds`
  and attachment examples.
- `~/documentation/projects.md` hnl-notify row: 0.5.0, email path, new
  memory limit.

## Out of scope

Multiple recipients per email device, inbound mail/replies, editable
templates, multipart request upload, attachment download endpoint, IP
pinning for URL fetches, files-on-disk attachment storage.
