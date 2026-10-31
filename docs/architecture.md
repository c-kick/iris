# hnl-notify — architecture

Unified notification service for the LAN. Senders state *intent* (what, how
urgent, for whom). hnl-notify owns routing, delivery, persistence, retries,
status tracking and provider quirks. Pushover (phone push) is the first
delivery path, email (SMTP, with file attachments) the second; nothing in the
public REST or MCP interface is provider-specific.

## Shape

One Python process (FastAPI + uvicorn, single worker) in one container with
one SQLite database (WAL, `synchronous=FULL`). No broker, no Redis: SQLite *is*
the durable queue, and all background work (dispatch, receipt polling, webhook
delivery, expiry sweeps, the reply mailbox poll) runs as asyncio tasks in the same
process; reply claims wake through an in-process event bus, so the service must
stay a single process. Volume is household-scale (tens to hundreds of notifications a day); one process and one
file keep the moving parts proportionate. Stack: Python 3.12, FastAPI, httpx,
pydantic, plus the official MCP Python SDK.

```
 senders (HA, Node-RED, n8n, scripts, agents)
   │  REST /v1/*             MCP /mcp  (same auth, same core)
   ▼
 ┌─────────────────────────── hnl-notify ───────────────────────────┐
 │ auth ─► intake ─► router ─► deliveries (SQLite, 1 per device)    │
 │                                  │                               │
 │   dispatcher ◄───────────────────┘   (lease, retry/backoff)      │
 │      │ provider adapter (pushover | email; later ntfy/HA/webpush)│
 │      ▼                                                           │
 │   receipt poller / inbound callbacks ─► state transitions        │
 │                                      │                           │
 │   events table (append-only cursor) ─┼─► SSE stream              │
 │                                      └─► webhook outbox ─► POST  │
 └──────────────────────────────────────────────────────────────────┘
```

## Logical model — intent is separate from routing

| Concept | Meaning | Example |
|---|---|---|
| **provider** | A delivery backend: adapter + declared capabilities + a *kind* (`push` or `email`) | `pushover`, `mail` |
| **device** (endpoint) | One concrete address on one provider | `alex-phone` = Pushover user key + device `iphone`; `alex-email` = `alex@example.org` |
| **user** | A person; owns devices | `alex` |
| **group** | A set of users/devices | `household` |
| **channel** | A topic with subscribed targets and an optional severity floor | `infra` |

Senders address `user:alex`, `device:alex-phone`, `group:household`,
`channel:infra` (a bare name is accepted when unambiguous). The router expands
targets to a de-duplicated set of devices, applies channel/device
`min_severity`, and creates one **delivery** per device. Provider identifiers
(Pushover user keys, device names, app tokens) exist only in configuration and
inside the adapter.

### Kinds and opt-in

Every provider has a *kind*: `push` (Pushover) or `email`. A request may carry
`kinds: ["push"]`, `["email"]` or both (`via` keeps its existing meaning: the
expansion path). A device is *opt-in* when `opt_in` is true; unset means the
kind default (email: yes, push: no). Rules: a channel `min_severity` floor cuts
the whole channel before any device is examined; kind and opt-in filtering
happens before the per-device disabled and `min_severity` checks; the
suppression reason reflects the first rule that applied:

- **No `kinds`**: opt-in devices are skipped, except a device named as a
  top-level target (`device:alex-email`). Devices reached through a user, group
  or channel obey `opt_in`. Skipped opt-in devices are not listed in
  `suppressed`. A config without email devices therefore routes exactly as
  before email existed.
- **`kinds` given**: a device is included iff its kind is requested (opt-in is
  ignored). A top-level device of another kind is suppressed ("device kind push
  not requested"); a requested kind that a top-level target has no device for is
  suppressed with "no email device" (unless a channel floor already cut that
  target). `routing_json` records the `kinds`.

Configuration is managed in the **admin UI** (`/admin`) and stored in the
same SQLite database (`settings` table). Fields each provider adapter declares
secret (Pushover app token, user keys) plus the callback secret are stored as
AES-256-GCM ciphertext; the 32-byte key is generated on first start in a
separate 0600 file (`/secrets/master.key`, its own volume). The admin API never
returns secrets, only "set, ends in …abcd"; a blank secret field on edit keeps
the stored value. API client tokens are shown once and stored as SHA-256
hashes. Every edit is validated as a whole (references between devices,
people, groups and channels), persisted, and applied live: routing, providers
and token checks switch over without a restart; queued deliveries keep their
device names. A YAML file (`NOTIFY_CONFIG`, `${ENV}` references allowed) is an
optional one-time import into an empty database.

Admin authentication: first start logs a one-time setup code; entering it
sets the admin password (scrypt). Sessions are random ids stored hashed,
HttpOnly + SameSite=Strict cookies, 14 days. Browser mutations additionally
require an `X-Notify-Admin` header and a same-origin `Origin`, login/setup are
throttled per address, and `/admin` is served with a strict CSP
(`script-src 'self'`, no framing). A bearer token with `admin` scope can call
the admin API too. `python -m notify reset-admin` clears the password, signs
everyone out and prints a new setup code.

## Data model (SQLite)

- `notifications` — the intent: id, client, idempotency key, source, title,
  message, severity, url/url_title, actions, data (opaque JSON echoed back),
  tags, targets, require_ack, expires_at, derived status, routing notes.
- `deliveries` — one per (notification, device): state, attempt counters,
  `next_attempt_at`, lease, provider message id, provider receipt,
  `tracking` (`none`/`receipt`/`report`), **`observable_states`**,
  **`capability_gaps`**, per-state timestamps (dispatched / delivered /
  acknowledged / interacted / finalized), acknowledged_by, last error.
- `attempts` — every provider call: start/finish, outcome
  (`success`/`retryable`/`permanent`), HTTP status, redacted request summary,
  truncated raw provider response, error text.
- `replies` — one per accepted reply: id (`rpl_…`), conversation, notification,
  delivery, Message-ID, from, subject, received_at, `text`, `stripped` /
  `truncated` flags, dropped attachment names, `state`
  (`waiting` / `claimed` / `unclaimed`), `claimed_at`, `claimed_by` (client),
  `waiter_id`, `autoreply_notification_id`.
- `reply_attachments` — bytes of reply attachments (BLOB, filename, type, size,
  sha256, `purged_at`), removed by the attachment retention sweep.
- `reply_quarantine` — rejected inbound mail: received_at, from, subject,
  Message-ID, reason, notification id when the thread matched. No content.
- `conversations` — one per reply conversation (`id` = root notification id): `handle_sha256`
  (SHA-256 of the reply handle's secret; the handle itself is never stored), `handle_notification_id`,
  `legacy` (conversations from before 0.7.0 keep the token rule), `created_at`. A row is deleted when no notification
  of the conversation is left. `replies.late_ok` marks unclaimed replies that may still be collected.
- New columns: `notifications.reply_wanted`, `in_reply_to`, `conversation_id`
  (own id unless a follow-up), `listener_seen_at`; `deliveries.recipient_address`
  (the address at the moment the delivery was claimed for sending).
- Scheduling columns on `notifications` (0.8.0): `send_at` (UTC, null for immediate ones), `max_late_seconds`,
  `deliver_by` (`send_at + max_late_seconds`, recomputed on edit), `ttl_seconds` and `explicit_expires_at` (as
  submitted, so a TTL can move with the send time while `expires_at` keeps the effective expiry), `released_at`
  (when routing ran; set on success, miss and failure alike) and `dismissed_at` (dismissed from Needs attention),
  plus an index on `(status, send_at)`. `status = 'scheduled'` is stored explicitly.
- `events` — append-only log; its integer id is the global cursor for history,
  SSE resume (`Last-Event-ID`) and webhook payloads. `mailbox.cleanup` and `mailbox.settings` events belong to no
  notification (no webhook fan-out) and are pruned by the retention sweep after `retention_days`.
- `subscriptions` + `webhook_deliveries` — webhook subscriptions and their
  durable outbox with its own retry state.

## Lifecycle

Delivery states:

```
queued ──► sending ──► dispatched ──► delivered ──► acknowledged
  │  ▲        │            │                         (interacted)
  │  └─retry──┘            └──► expired  (receipt expired without ack)
  ├──► expired   (TTL passed before dispatch)
  ├──► failed    (permanent error / retries exhausted)
  └──► cancelled (before dispatch, or provider-side cancel of a receipt)
```

- `dispatched` means **the provider accepted the request** — nothing more.
- `delivered`, `acknowledged`, `interacted` are only ever set from
  provider-originated evidence (receipt poll or callback). Every delivery
  carries its `observable_states`, so a reader can tell "not delivered yet"
  from "this path can never report delivery".
- Notification `status` is derived from its deliveries: `pending`,
  `dispatched`, `delivered`, `acknowledged`, `partial`, `failed`, `expired`,
  `cancelled`, or `suppressed` (routing produced no deliveries, e.g. below a
  channel's severity floor).

Durability: a submission is committed before `202` is returned. The dispatcher
claims deliveries with a lease (`sending` + `lease_until`); at start-up, expired
leases return to `queued`. Pushover has no idempotency key, so a crash in the
milliseconds between Pushover accepting and our commit can cause **one
duplicate**: the service is at-least-once, never silently at-most-once.

Retry: network errors, 5xx and 429 are retried with exponential backoff and
jitter (10 s × 3ⁿ, capped at 15 min, never below Pushover's 5 s, 8 attempts by
default, never past `expires_at`). 4xx is permanent (Pushover: "repeating your
same request will not work"). Manual retry re-queues `failed` deliveries.

### Scheduled notifications

A submit with `send_at` or `delay` (and a send time more than 5 s ahead) creates a notification in status
`scheduled` with **no deliveries**; routing and planning are deferred.

```
scheduled ──► released ──► (queued → ... normal delivery states; status derived from the deliveries)
   │              ├──► suppressed / failed   (nothing to send to, routing error, sender gone or no longer
   │              │                           allowed to send, release error)
   │              └──► expired               (missed: now > deliver_by)
   └──► cancelled
```

- **Release door.** `Scheduler.release(nid, force=False)` in `notify/schedule.py` is the only way out of
  `scheduled`: the scheduled release, *send now* and the retry of a missed or failed release all use it. It runs in
  one `db.tx()` (no intermediate `releasing` status): re-read the row and continue only while `scheduled`; unless
  `force`, a release after `deliver_by` marks the notification `expired` (`notification.missed`); a sender that no
  longer exists, or no longer has the `send` (or `admin`) scope, makes it `failed` (authority is re-checked at
  send time); otherwise routing is resolved **now**, deliveries are planned and inserted, and
  `released_at` is set. A routing error is `failed`; zero deliveries is `suppressed` (as today) but also counts as a
  release problem, as does a `reply` notification that reached no email device. `notification.created` (payload
  `scheduled: true`) is emitted here, not at submit.
- **Per-row isolation.** `release_due()` runs at the start of every dispatcher pass and releases each scheduled row
  with `send_at <= now` inside its own `try/except`: an unexpected error marks that one notification `failed`
  ("release error: ...") and the loop goes on to the next row and to normal dispatch. A bad row cannot stop deliveries.
  The loop wakes at least once a second, which is the precision; edits and send now call `wake`. The first pass after a
  start releases everything overdue, subject to `deliver_by`.
- **`deliver_by` bounds dispatch too.** A delivery of a scheduled notification not dispatched by `deliver_by` is
  expired by the `expire_due` sweep and retry backoff never schedules past it. It is never passed to a provider:
  `expires_at` goes to Pushover as TTL, and using `deliver_by` there would make a delivered message vanish later.
- **Effective time.** Wherever a timestamp means "when it went out" the code uses
  `COALESCE(released_at, send_at, created_at)`: retention (`retention_sweep` never deletes a `scheduled` row),
  the attachment purge, and the provider timestamp (Pushover `timestamp`, email `Date`). `created_at` stays "submitted".
  Because `released_at` is also set on a miss, a missed notification survives `retention_days` from the miss, with its
  reply conversation and webhook subscription.
- **Attachments.** For a scheduled request URL attachments are always fetched and all bytes always stored (a preview
  that reaches nobody now may reach a device at release). The purge skips `scheduled` rows, so the bytes are kept until
  the send time plus `attachment_retention_days`; a retry soon after a miss still has its files.
- **Edit and cancel.** `PATCH` (`Scheduler.update`) merges the patch, validates the whole result as a submit would and
  writes it inside one `db.tx()`; a row that is no longer `scheduled` yields 409 and no change. Cancel is a single
  `UPDATE ... WHERE status='scheduled' AND (admin OR client_id=caller)` in the service; if it changes no row the caller
  needs `manage` and the delivery cancel runs. Hence the `send` scope suffices for the caller's own scheduled
  notifications. Time zone: `ZoneInfo(TZ)` when `TZ` is set and valid, else UTC (needs the `tzdata` package).
- **Caps and limits.** Per client, 100 scheduled and 50 MiB of their attachment bytes (admin and internal clients
  exempt); at most 366 days ahead; `max_late_seconds` 60 to 86 400, default 3 600.
- **`release_problems`.** `attention` has an additive notification-level bucket: missed notifications, release
  failures, releases that reached no device or no reply-capable device, deliveries expired by `deliver_by`, and rows still
  `scheduled` more than 60 s past `send_at`. Dismiss sets `notifications.dismissed_at`; retry calls
  `release(nid, force=True)` (lateness ignored; an already past effective `expires_at` is a 409).
  `/health/ready` has `checks.scheduler` (not ready while a row is overdue); `/metrics` adds
  `notify_scheduled_next_seconds` and `notify_scheduled_overdue`.

## Severity and capabilities

Severity: `min`, `low`, `normal`, `high`, `critical`.

Each provider declares a capability document; each device may narrow it.
`GET /v1/targets` and MCP `list_targets` expose the effective capabilities.
Requests a path cannot honour are **not silently dropped or faked**: they are
recorded as `capability_gaps` on the delivery and returned in the submit
response.

Capability keys: `accept_confirmation`, `delivery_receipt`, `acknowledgement`,
`interactions`, `actions` (max buttons), `links` (max URLs), `cancel_after_dispatch`,
`ttl`, `severities`, `max_title_length`, `max_message_length`. Receipt-type
capabilities state the condition under which they apply (e.g.
`{"supported": true, "when": "severity=critical"}`).

## What Pushover can and cannot report (pushover.net/api, checked 2026-09-27)

| Question | Pushover | Modelled as |
|---|---|---|
| Request accepted? | HTTP 200 `{status:1, request}` | `dispatched` + provider `request` id |
| Delivered to device? | **Priority 2 (emergency) only**: receipt `last_delivered_at`. No receipts for priorities −2…1 | `delivered` observable only at severity `critical` |
| Read/opened? | Never | not modelled |
| Acknowledged? | Priority 2 only: receipt `acknowledged(_at/_by/_by_device)`, or a callback POST to a **publicly reachable** URL | `acknowledged`; receipt polling by default (Pushover: ≤ once per 5 s; we poll every 30 s), callback only if `public_base_url` is set |
| Emergency expired without ack? | Receipt `expired=1` | delivery `expired` (keeps `delivered_at`) |
| Action buttons / interaction reports | None. One supplementary URL (`url`, `url_title`) opens on tap; HTML `<a>` links in the message text are tappable in the Pushover app (stripped from the lock-screen banner); no taps are reported | `links: 1`, `actions: 0` + `inline_links`; the first URL action is the tap link, further URL actions become links in the message text (`html=1`, text escaped), actions without URL → capability gap; `interactions: false` |
| Recall after sending | Only the repeating emergency alert (`receipts/{r}/cancel.json`) | `cancel_after_dispatch: severity=critical` |
| TTL | `ttl` seconds, auto-deletes from device; ignored for priority 2 | local expiry always; `ttl` forwarded when not critical |
| Errors | 4xx + `errors[]` = invalid, don't retry; 429 = monthly quota exhausted; 5xx = retry after ≥ 5 s | `permanent` / `retryable` attempt outcome, raw body stored |
| Limits | message 1024 chars, title 250, url 512, url_title 100; 10 000 msgs/month; ≤ 2 concurrent connections | truncation recorded as gap; adapter concurrency 2; quota headers surfaced in `/v1/providers` and `/metrics` |
| Unknown device name | `messages.json` still answers `status:1` and sends to **every active device** on the account (checked 2026-10-30); only `users/validate.json` with `device` says "device name is not valid for user". Validation does not count as a message | before a push to a named device, `users/validate.json` when the last good check is older than `device_check_seconds` (default 900, 0 = every push); a rejected name fails the delivery (`permanent`, nothing sent), shows as a device problem (`ready: false`) and alerts the announce targets once until a check passes again (`meta` flag `device_invalid:<device>`, events `device.invalid` / `device.valid`). All devices are also checked at start-up and after every configuration change. No answer (network, 5xx, 429) never holds a push |

Severity → Pushover priority: `min`→−2, `low`→−1, `normal`→0, `high`→1,
`critical`→2 (retry 60 s, expire = min(TTL, 3 h)). `require_ack` below
`critical` is **not** silently escalated to an emergency alarm; it becomes the
gap "acknowledgement on pushover requires severity=critical".

## What browser push can and cannot report (RFC 8030/8291/8292)

| Question | Browser push | Modelled as |
|---|---|---|
| Request accepted? | The vendor push service answers 201/202 | `dispatched`; tracking `report` (not finalized at dispatch) |
| Shown? | The service worker POSTs `shown` to `/push/report` after `showNotification`; works only while the browser runs | `delivered`; authenticated by a per-delivery HMAC token carried in the payload |
| Clicked? | The service worker POSTs `clicked` on `notificationclick` (implies shown) | `acknowledged` (`acknowledged_by` = the device), at every severity |
| Report before the dispatch commit? | Possible: the browser can beat the worker's database write | stored on the row while `sending`, replayed when `dispatched` commits |
| Never shown? | Push service drops the message after its TTL | sweep: `dispatched` past its horizon becomes `expired` ("not shown within its lifetime") |
| Shown but not clicked? | n/a | high/critical/`require_ack`: `expired` ("not acknowledged within its lifetime", keeps `delivered_at`) and listed under awaiting acknowledgement; ordinary: finalized `delivered` |
| Dead subscription? | 404/410 from the push service | `permanent`; the `device_invalid:<device>` meta flag is set (persisted, so `ready: false` survives provider rebuilds and restarts) with one alert, until a later send succeeds; re-enroll the browser |
| TTL | `TTL` header = remaining expiry, else `default_ttl_seconds` (86400) | horizon of the sweep above |
| Urgency | `Urgency` header: min `very-low`, low `low`, normal `normal`, high/critical `high` | no repeating alarm for critical |
| Images / attachments | Not carried | capability gap `attachments: not supported on browser push` |
| Actions | Not carried; one tap link (`url`, else the first action URL) opens on click | `actions: 0` |
| Browser not running | Chrome/Edge/Firefox receive nothing until it runs | message waits at the push service until TTL |
| Errors | 429 retry (honours `Retry-After`), 5xx/network retryable, 413 and other 4xx permanent | attempt outcomes as for other providers |
| Payload | Encrypted aes128gcm record ≤ 4096 bytes; the message is truncated with `…` to fit | recorded in the request summary as byte size |
| Security | Subscription endpoints must be `https` on a fixed vendor allowlist; `/push/*` answers only on `server.external_url` | `push_endpoint_ok`, host guard |

## What email can and cannot report

| Question | Email (SMTP) | Modelled as |
|---|---|---|
| Request accepted? | The SMTP server accepted the message (250) | `dispatched`; `provider_message_id` = Message-ID |
| Delivered / read / acknowledged? | Never observable | not modelled; observable states end at `dispatched` |
| Reply from the recipient? | Only when a reply mailbox is configured and the request sets `reply` | inbound IMAP poll; delivery → `interacted` when an agent collects the first reply |
| Recall after sending | Impossible | no `cancel_after_dispatch`; `ttl` applies only before sending |
| Action buttons / interactions | None; links only | `actions: 0` + `inline_links`; only http(s) links become clickable, anything else is shown as text |
| Errors | 5xx reply = rejected for good; 4xx, connection errors, TLS errors, timeouts = try again | `permanent` / `retryable` attempt outcome |
| Wrong credentials | Login rejected | retried after 15 min, text points at the admin UI |
| Message too large | Server advertises `SIZE` | checked before login; `permanent` if the encoded message is larger |
| Slow or stuck server | | hard session deadline (`timeout_seconds`, default 60, 10-300) enforced by a watchdog that closes the socket; the delivery lease is extended to timeout + 30 s so a running session is never reclaimed |

The `Message-ID` is derived from the delivery id, so a resend after a crash
(at-least-once) carries the same id and mail systems can collapse it. Subject
is the prefix plus title, or the first message line (120 chars). `high` and
`critical` set `Importance: high` / `X-Priority: 1`; `min` and `low` set
`Importance: low`. Header values are stripped of control characters; the adapter
never raises on sender input. One send at a time per email provider (`max_concurrency` 1);
`smtplib` debug output stays off, the password is stored encrypted.

## Replies

Optional, email only. An agent sends with `reply: true`; the email carries
`Reply-To: <reply mailbox>` and a footer saying a reply reaches the agent. The
person presses Reply, hnl-notify reads the mailbox, checks the mail and hands
the new text (plus attachments) to the agent that is waiting for it. Paths that
cannot carry replies (Pushover, email without a reply mailbox) record the
capability gap `replies` and still send. Notifications without `reply` /
`in_reply_to` keep the 0.5.1 response shape and headers. The capability key is
`replies` (`{"supported", "note"}`); email declares it when the reply mailbox
and `authserv_id` are configured.

**Access.** One gate (`Replies._access`) serves claim, listing, attachment download and follow-up submission:
the sending token's visibility, then the conversation row (a reply conversation without a row is refused), then the
reply handle (`hmac.compare_digest` against the stored hash; legacy rows need the sending token instead). hnl-notify's
own notifications (autoreply, size alert) skip the handle through a private flag on the request. Bearer admin tokens
get no bypass and see no reply content in `/admin/api`. Late replies: a swept (`unclaimed`) reply of a non-legacy
conversation has `late_ok=1` and is claimed after the waiting ones.

**Mailbox cleanup.** `notify/mailbox_cleanup.py` is its own IMAP entry point (never creates folders, EXAMINE for
previews, SELECT only the target folder, UIDPLUS decided from the post-login CAPABILITY) and `notify/cleanup_service.py`
runs it: per-step locking on `NotifyService.mailbox_lock(provider)`, a daily worker, `mailbox.cleanup` /
`mailbox.settings` events without a notification, and an internal size alert. A manual run (`POST /admin/api/mailbox/{provider}/cleanup`, signed-in session only) is bounded to about 60 s and reports `unfinished` folders; it answers 409 "mailbox busy, try again" when the lock is held too long.

**Inbound.** The reply mailbox is an IMAP account over implicit TLS, polled every
30 s by the worker (no IDLE, no inbound SMTP, no MX change). Connection errors
back off (10 s × 3ⁿ, cap 15 min); the admin UI shows the last success and error.
Each session has a hard deadline. Mail is moved with `UID MOVE` (fallback
`COPY` + `\Deleted` + `EXPUNGE`) to `Processed` or `Quarantine`. The database
commit precedes the move, so a crash in between ends as a duplicate on the next
poll, never as a second reply.

**Checks, in order; the first failing one decides:**

1. **Size and headers.** Headers are fetched bounded (first 64 KiB). A header
   block of 64 KiB or more, a size over 15 MiB (`RFC822.SIZE`) or a missing size
   → `too_large`, without fetching the body. Then the header block is checked byte
   for byte, independent of Python's lenient parser (which silently ends the headers
   at the first line it does not recognise and treats a bare CR as a line break):
   CRLF line endings only, no control characters, every line a `Name:` header or a
   continuation, exactly one `From` (also counting `From :`) → else
   `malformed_headers`. This closes a replay attack where a validly signed mail
   from another account of the same provider gets an extra From in front of a line
   Python stops at.
2. **Automatic mail** → `automatic`: `Auto-Submitted` other than `no`,
   `Precedence: bulk|junk|list`, empty or `MAILER-DAEMON` / `postmaster` return
   path or From, `multipart/report`.
3. **Thread.** Our Message-ID is `<{delivery_id}@{from domain}>`; the poller looks at
   `In-Reply-To`, then `References` newest first, for a local part that is a
   delivery id. None → `no_thread`. The notification was not sent with `reply`
   (including every mail sent before 0.6.0) → `not_reply_enabled`.
4. **Sender.** The bare From address, lower-cased, must equal the delivery's stored
   `recipient_address`; a message with more than one From header never matches
   → `sender_mismatch`. The From header is read strictly from its raw form: only
   `addr@domain` or `Name <addr@domain>` (dot-atom address, no `@` in the display
   name, no address lists, comments, source routes or encoded words in the address).
   Anything else yields no sender, so a lenient parser can never read a different
   address than the receiving server and DKIM alignment saw (CVE-2023-27043 class).
5. **DKIM** → `dkim_fail`. Only the **first** `Authentication-Results` header
   counts, because the receiving server prepends its own and anything below may
   have been written by the sender (checked against the deployment's mail server:
   forged headers claiming its authserv-id are not removed). Its authserv-id must
   equal the admin-entered `authserv_id`. RFC 8601 comments and quoted strings are
   stripped before parsing, so `;` and tokens inside them cannot forge a verdict.
   `dkim=pass` must be the method token of a result, with a standalone `header.d`
   property (containing a dot) equal to or a parent of the From domain, **and** the
   same header must show `dmarc=pass` with `header.from` equal to the From domain.
   DMARC is the server's own reading of the From header (it fails on multiple From
   headers), which binds the signature to the address compared in step 4. A mail
   server that does not run DMARC therefore accepts no replies.
6. **Duplicate** (reply Message-ID, or a hash of the headers, already stored) →
   moved to Processed.
7. **Accept.** The full message is fetched, parsed and stored; then the **full**
   header block is judged again and must give the same verdict, delivery and sender,
   else `header_mismatch` (the first verdict came from a bounded fetch). A message
   that cannot be parsed → `unreadable`.

Quarantined mail is recorded (time, from, subject, Message-ID, reason) and the
event `reply.quarantined` is emitted; its content is never stored and never
shown to agents.

**Accepted residual risk.** If the mail server's milter ever passed a message
without adding its header, a forged header would become the first one. Forging a
reply would then still need the Message-ID of a live reply-enabled delivery (40
random bits, present only in mail to the recipient) and the recipient's exact
address. In-process DKIM verification is the fix if that is no longer acceptable.

**Content.** Plain text part, or a text conversion of HTML. Quoted history
(`On … wrote:`, Dutch `Op … schreef`, Outlook separators, `>` blocks) and the
signature after `-- ` are cut; if nothing is left the unstripped text is kept
(`stripped: false`). At most 8000 characters (`truncated: true`). Attachments:
up to 10 files and 10 MiB; the rest are listed as dropped.

**Conversations.** Notifications chained by `in_reply_to` (same client; admin
clients, including the system client, may reference any; unknown or foreign id → 422) share a `conversation_id`,
the root notification id. The follow-up's email carries `In-Reply-To` and
`References` derived from delivery ids. Listening, claiming and the grace period
apply per conversation, so a reply to an older mail of a thread reaches the
current waiter.

**Claim.** `POST /v1/notifications/{id}/replies/claim?waiter=W&wait=N` (`read`
scope, the notification's client or `admin`; N 0-55 s, default 50; W an opaque
caller-chosen id) refreshes the conversation's `listener_seen_at`, then returns
the oldest `waiting` reply marked `claimed` by W. A retry with the same W after a
lost response gets the same reply. With no reply it waits up to N s, then 204.
The wait wakes through the in-process event bus (every commit signals it), which
relies on the single-process design: running more than one worker would need a
shared signal. The first claimed reply moves the email delivery from `dispatched`
(or `delivered`) to `interacted` with `interaction_json = {"reply_id"}`; later
replies are stored and claimable without a state change. Email lists `interacted`
in `observable_states` only for notifications sent with `reply` on a path that
declares `replies`.

**Who sees reply content.** Agents (non-admin clients) receive reply text and
attachments only by claiming. `GET /v1/notifications/{id}/replies` and MCP
`get_replies` list a conversation's replies with their state, but text and
attachments are shown only for `claimed` replies; the attachment download also
requires a claimed reply (admin tokens see everything). Unclaimed content is never
handed to an agent.

**Listener, grace, autoreply.** A conversation is *listened to* while its last claim
poll is at most 2 minutes old. A `waiting` reply older than 10 minutes in a
conversation that is not listened to becomes `unclaimed` (checked on every poll;
never while a listener is present) and gets an autoreply: an ordinary internal
notification from the system client to that device (`kinds: ["email"]`,
`in_reply_to` the replied notification, `Auto-Submitted: auto-replied`, title
`Re: <title>`) saying the agent is no longer listening and the reply was not
delivered. It is durable and retried like any notification. At most one autoreply
per conversation per hour; further unclaimed replies in that hour are only
recorded. Autoreplies are never themselves answered (automatic mail is
quarantined).

**Attachment download.** `GET /v1/replies/{id}/attachments/{idx}` sends
`Content-Disposition: attachment` with a percent-encoded filename and
`X-Content-Type-Options: nosniff`. Content types must be ASCII `type/subtype`
tokens of at most 100 characters; anything else, and `text/html` and
`image/svg+xml`, is served as `application/octet-stream`.

**Events.** `reply.received`, `reply.claimed`, `reply.unclaimed`,
`reply.quarantined` (SSE and webhooks).

**`scripts/notify-wait`** (stdlib-only) is the agent side: it long-polls the claim
endpoint with one waiter id per run, prints the reply and saves attachments.

- Base URL and token: `--url` / `NOTIFY_URL`, `--token-file` / `NOTIFY_TOKEN`, else the
  `hnl-notify` MCP entry in `~/.claude.json`. The token is never printed.
- Attachment URLs from the service must be relative paths on the service; the
  token is only ever sent to the configured base URL and redirects are not followed.
- Filenames are reduced to a base name, control characters, separators and a
  leading dot removed, capped at 200 bytes, collisions numbered; files are created
  exclusively (`xb`), never overwritten.
- Control characters in the printed output are stripped (terminal escapes).
- `--timeout` defaults to 60 minutes; a bare number means minutes. If saving attachments fails the reply is still printed (stderr note, exit 0).
- Exit codes: 0 reply printed, 1 error (auth, 404/422, bad config, unexpected
  response, download or save failure), 2 no reply before `--timeout`.
- Network errors and 5xx are retried with backoff and the same waiter id.

## Attachments

`attachments` on a submit (REST, MCP, not `/v1/ingest/arr`): up to **10**
files, **10 MiB** decoded in total. Each item is either
`{"filename", "content_type"?, "data_base64"}` (at most 14 200 000 base64
characters) or `{"url", "filename"?}`. Over MCP, inline data is limited to
256 KiB per notification; agents should send URLs. Filenames are reduced to a
safe basename; content types are validated, guessed from the name, or fall back
to `application/octet-stream`. Violations are a 422 naming the attachment.

- **Email** sends all attachments. **Pushover** sends the first `image/*` up to
  5 MiB (`attachment_base64`); the others are recorded as `capability_gaps`.
- **Idempotent replays** are answered before any attachment check or fetch.
  Fetching URLs can take a while, so a submit may be refused when its own TTL
  ran out in the meantime. Intake of attachments is serialised (one at a time).
- **Storage**: bytes live as BLOBs in the `attachments` table next to the
  notification, with filename, type, size, sha256, source and (for URLs) the
  host. A 10 MiB insert briefly holds the SQLite write lock, and the database
  file does **not shrink** after purging (no auto_vacuum); freed pages are
  reused. The retention sweep deletes the bytes after
  `server.attachment_retention_days` (default 7, 1-90; Settings) once no delivery
  is queued or sending; the metadata stays (`purged: true`). A retry after the
  purge sends without them and records a gap. There is no download endpoint for
  outgoing attachments (reply attachments have one, see Replies).
  Plans with no deliveries (all suppressed) store no bytes.
- **URL fetching** is off until an admin lists hosts in
  `server.attachment_url_hosts` (Settings, "Hosts allowed for attachment URLs").
  Host and `host:port` entries match host names; CIDR and bare-IP entries match
  only URLs that use an IP literal (a host name is never matched against a
  CIDR). Every hop, including each of at most 3 redirects, is checked again.
  The resolved and the connected peer address are refused when they are in
  `127.0.0.0/8`, `::1`, `0.0.0.0/8`, `::`, `169.254.0.0/16` or `fe80::/10`,
  including IPv4-mapped, IPv4-compatible, 6to4 and NAT64 forms. The fetcher
  builds its own client (no environment proxies, no automatic redirects), sends
  `Accept-Encoding: identity` and has one 15 s budget for all URLs of a request.
  The URL may carry a token, so it is never stored or logged; only the host is.
  `dry_run` validates URLs against the allowlist but does not fetch them.

## Request size

A pure-ASGI limit rejects with **413** when a body exceeds 15 MiB on
`POST /v1/notifications` and 1 MiB on every other route (MCP, ingest, admin),
by `Content-Length` or by counting streamed bytes. Container memory is capped at
320 MiB (`mem_limit` and `memswap_limit`); a 10 MiB request needs several times
that in Python while it is decoded, which is why attachment intake and email
sending are one at a time. Requests over 1 MiB must carry a valid credential
(checked before the body is read), at most two such bodies are read at once and
each must arrive within 60 s; Pushover loads only the image it sends, and the
image sets `MALLOC_MMAP_THRESHOLD_` so freed buffers return to the OS.
`scripts/memprobe.py` measures the peak.

## REST API (bearer tokens; scopes `send`, `read`, `manage`, `admin`)

| Method & path | Purpose |
|---|---|
| `POST /v1/notifications` | Submit (optional `kinds`, `attachments`, `reply`, `in_reply_to`, `send_at` / `delay`, `max_late_seconds`). 202; 200 on idempotent replay; `dry_run: true` returns the routing plan only; 413 above 15 MiB |
| `GET /v1/notifications` | History; filter by status/source/tag/since. `status=scheduled` lists all of the caller's scheduled ones by `send_at`, no cursor |
| `PATCH /v1/notifications/{id}` | Reschedule, edit or `send_now` while `scheduled` (`send`; own or `admin`); 409 once released |
| `GET /v1/notifications/{id}` | Full status: deliveries, attempts, events, attachment metadata |
| `POST /v1/notifications/{id}/cancel`, `/retry` | Where meaningful; 409 with reason otherwise. Cancel needs only `send` for one's own `scheduled` notification, else `manage` |
| `POST /v1/notifications/{id}/replies/claim?waiter=…&wait=…` | Long-poll for the oldest waiting reply of the conversation: 200 with the reply, 204 when none came in time (`read`; own notification or `admin`) |
| `GET /v1/notifications/{id}/replies` | Replies of the conversation with state; subject, text and attachments only for claimed replies (all for `admin`) |
| `GET /v1/replies/{id}/attachments/{idx}` | Download an attachment of a claimed reply |
| `GET /v1/targets` | Users, devices (with `kind`, `opt_in`), groups, channels + effective capabilities |
| `GET /v1/providers` | Provider capabilities, health, quota |
| `GET /v1/events`, `GET /v1/events/stream` | Event history by cursor; Server-Sent Events |
| `POST/GET/DELETE /v1/subscriptions` | Webhook subscriptions |
| `POST\|PUT /v1/ingest/arr?targets=…` | Radarr/Sonarr/Lidarr/Prowlarr webhook adapter (`send`); translated in `notify/arr.py` |
| `POST /v1/callbacks/{provider}/{secret}` | Inbound provider callbacks |
| `GET /health/live`, `/health/ready`, `/metrics`, `/v1/whoami` | Operations |

Clients see and manage only their own notifications unless they hold `admin`.
A token may also arrive as the HTTP Basic password (username ignored, or the
username when the password is empty), for senders that cannot set a bearer
header.

## Self-registration

`POST /v1/registrations` (unauthenticated, LAN) creates a pending request and
returns a token immediately; only its SHA-256 is stored, in `registrations`,
and the authenticator ignores it. Approval (admin UI or admin API) copies that
hash into the API-client configuration with the granted scopes, so the app's
token starts working without reconfiguration; denial or 7-day expiry leaves it
inert forever. Admin scope can never be requested or granted this way. New
requests alert the configured targets (default: every person) through
hnl-notify itself. The alert's link opens `/admin/decide/{id}?k=<code>`: a
login-free page for that one request with Approve/Deny buttons (POST; GET
never decides). `<code>` is random, stored only as a hash, cleared by any
decision (phone or admin UI), expires with the request, and wrong codes are
throttled per address. The trade-off: the link transits Pushover, and whoever
holds it can decide that single request. Optional auto-approval by
source subnet, limited to a configured scope set. Abuse limits: 5 stored
requests per source address per hour, 20 pending in total.

## Events and subscriptions

Event types: `notification.created`, `notification.status_changed`,
`delivery.queued`, `delivery.attempt_failed`, `delivery.dispatched`,
`delivery.delivered`, `delivery.acknowledged`, `delivery.interacted`,
`delivery.failed`, `delivery.expired`, `delivery.cancelled`, `reply.received`,
`reply.claimed`, `reply.unclaimed`, `reply.quarantined`, `notification.scheduled`, `notification.updated`,
`notification.missed`. A scheduled notification emits `notification.scheduled` at submit and `notification.created`
at release; `*` and `notification.*` subscribers receive the new types.

- **SSE** `GET /v1/events/stream`: filterable, resumable with `Last-Event-ID`.
- **Webhooks**: per-client subscriptions (event types plus optional
  notification/source/tag filters), or a per-notification `webhook` shortcut
  in the submit body. Sent from a durable outbox with retries, signed with
  `X-Notify-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>`.

## MCP

Streamable HTTP at `/mcp` in the same process, same bearer tokens and scopes.
`send_notification` and `preview_routing` accept `kinds`; `send_notification`
also takes `attachments` (URLs recommended, inline data up to 256 KiB), `reply`,
`in_reply_to`, and the scheduling fields `send_at`, `delay` and `max_late_seconds`
(`update_scheduled_notification` edits, reschedules or sends such a notification now). Tools:
`send_notification`, `preview_routing`, `get_notification`,
`list_notifications`, `list_attention` (failed / pending / stuck / awaiting ack / `release_problems`; failed and expired items
an admin dismissed in Admin → Activity are left out, `deliveries.dismissed_at`, cleared by a retry),
`list_targets`, `retry_notification`, `cancel_notification`, `update_scheduled_notification` (`clear=[...]` removes
optional fields), `get_replies`
(conversation replies with state; text only for claimed ones). Resources:
`notify://targets`, `notify://notifications/{id}`. Everything is phrased in
notification semantics — targets, severity, states — never Pushover keys or
priorities.

## Observability

JSON logs to stdout (one object per line, carrying notification/delivery ids);
`/health/live` (process up), `/health/ready` (DB writable, dispatcher heartbeat
fresh, config loaded, scheduler not overdue); `/metrics` in Prometheus text format (deliveries by
state, queue depth, oldest queued age, webhook backlog, provider quota, next scheduled release and overdue count).
