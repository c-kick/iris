# Scheduled notifications — design

Date: 2026-10-27 · Status: revision 2 (after builder check, hostile review and ambassador ruling) ·
Target version: 0.8.0 (on top of 0.7.0)

Revision 2 folds in hostile-review findings HR-1…HR-21 as ruled by the project ambassador
(rejected: moving "stuck scheduled" out of the UI into health only, part of HR-19; see the end).

## Intent

Today a notification goes out the moment it is submitted. Senders (agents especially) that want
to reach the owner *later* ("remind me at 07:30", "ask at 09:00 whether to merge", "ping me in
2 hours if …") have to keep a process alive, or not do it at all. hnl-notify becomes the place
that holds a notification until its time and then sends it, durably, through the normal
pipeline. Agents and other senders must be told the feature exists, and the owner needs a page
in the admin UI to see what is coming and change or cancel it.

## Goals

1. Any sender can schedule a one-off notification through REST and MCP, by an absolute time or a
   relative delay.
2. Everything a normal notification can do works when scheduled: targets, severity, kinds,
   attachments, links/actions, TTL, require_ack, webhooks, idempotency keys, **email replies**
   (`reply=true`, the reply handle is returned at scheduling time) and follow-ups.
3. Routing (which devices receive it) is worked out **when it fires**, so config changes made in
   between (a new phone, a disabled device) apply.
4. A scheduled notification that cannot leave hnl-notify on time (hnl-notify down, provider or
   network down) still goes out late, but only within a limit the sender can set (default 1 h);
   past that it is marked missed and shown to the owner.
5. The sender (its own notifications, `send` scope is enough) and the admin (any) can
   reschedule, edit, send now or cancel a notification while it is still scheduled.
6. The admin UI gets a **Scheduled** section to list and manage scheduled notifications.
7. Agents learn about it from the MCP server instructions and the `send_notification` tool
   description; humans from the README, `docs/architecture.md` and the server docs.

## Non-goals

- Recurring notifications (cron, "every weekday"). One-off only. Nothing here should make
  recurring harder later, but no data model is reserved for it.
- Quiet hours / "deliver when awake" logic.
- Editing attachments, reply settings, webhook, `data`, `source` or idempotency key after
  scheduling (cancel and schedule again instead).
- Fetching URL attachments at send time (they are fetched at scheduling time).
- A configurable time zone setting (the container's `TZ` is the one source).

## Anti-goals (failure even if it works)

- A scheduled notification that silently never fires: lost on restart, purged by retention,
  stuck because one bad row blocks the dispatcher, routed to nobody without anyone noticing,
  or released twice.
- A morning wake-up ping that arrives in the afternoon after an outage, whether hnl-notify or
  the provider was down (hence the lateness limit, enforced up to dispatch).
- An edit racing the release or another edit so that an invalid or half-edited notification goes
  out, or an edit reported as applied after the notification was already sent.
- Breaking any existing sender: without the new fields everything behaves exactly as in 0.7.0.

## Decided by the owner

- One-off only.
- Routing resolved at send time.
- Admin UI actions: list, cancel, send now, change the time, edit content.
- Discovery: REST + docs, MCP tool + server instructions (no global agent-instructions change).
- Missed due time: send late within a limit (sender-settable, default 1 h), else missed.
- Time input: absolute (`send_at`) or relative (`delay`), exactly one.
- Replies allowed; the reply handle is returned at scheduling.
- URL attachments fetched at scheduling.
- The sender may edit/reschedule/cancel its own via API/MCP; admin may change any. For a
  notification that is still `scheduled`, the `send` scope is enough (self-registered agents get
  `send`+`read`, not `manage`); `manage` stays required for retry/cancel of anything released.
- A scheduled notification that could not go out shows under Needs attention, can be dismissed,
  and can be retried; retry routes it now and sends it, ignoring the lateness limit.
- Own **Scheduled** menu section.
- Horizon: at most 1 year ahead.

## Interface

### Submitting (REST `POST /v1/notifications`, MCP `send_notification`)

New optional fields on `NotificationIn`:

| Field | Type | Meaning |
|---|---|---|
| `send_at` | ISO-8601 datetime | When to send. With offset/`Z`: taken as is. Without offset: local time in the service's zone (the container `TZ`, e.g. `Europe/Amsterdam`); a non-existent local time (DST gap) is rejected, an ambiguous one (DST fold) takes the first occurrence. |
| `delay` | int seconds, or string like `90s`, `45m`, `2h30m`, `1d` | Send this long after now. |
| `max_late_seconds` | int, 60 … 86 400, default 3 600 | How late it may still be dispatched (see Release). |

Rules:

- `send_at` and `delay` are mutually exclusive (422 if both).
- **One time-zone rule per request (HR-10):** in a request that carries `send_at` or `delay`,
  every offset-less datetime (`send_at`, `expires_at`) is read in the service's zone. Requests
  without a send time keep today's rule (offset-less `expires_at` = UTC), so existing senders are
  unaffected.
- The resolved time is stored in UTC. If it is ≤ now + 5 s (including up to 60 s in the past, for
  clock skew) the notification is **immediate**: no scheduling, exact 0.7.0 behaviour. More than
  60 s in the past → 422. More than 366 days ahead → 422.
- `ttl_seconds` counts from the send time. An `expires_at` must be after the send time (422).
  When both are given, the effective expiry is `min(expires_at, send time + ttl_seconds)`, as
  today.
- `max_late_seconds` without a send time is accepted and ignored.
- `dry_run` with a send time returns the usual routing preview (as it would route *now*) plus
  `send_at`.
- Caps per client (HR-11), 422 beyond them; admin callers and hnl-notify's own submissions are
  exempt: at most **100** notifications in `scheduled`, and at most **50 MiB** of attachment bytes
  held by its scheduled notifications.
- Idempotency (HR-21): a replay with the same key whose resolved send time differs from the
  stored one returns 409 "idempotency key already used for a different send time; use PATCH".
  (Same send time → normal replay.)

Validation at scheduling time: everything a normal submit validates (targets resolve, reply
handle gate for follow-ups, attachments allowed, sizes) runs now, so mistakes surface to the
sender immediately. Routing is resolved once for validation and for the `routing_preview` in the
response, but no deliveries are created.

Attachments (HR-3): for a scheduled request, URL attachments are **always** fetched and all bytes
are **always** stored, whatever the preview says (today both depend on there being deliveries,
`core.py` `submit_async` / `commit`). A preview that reaches nobody at scheduling time may well
reach a device at release.

Follow-ups: `in_reply_to` pointing at a notification that is itself still `scheduled` is
rejected (422: "cannot follow up a notification that has not been sent yet"): the follow-up's
email needs a sent parent to thread to. A scheduled follow-up of a sent notification is fine.

Response (202): the normal notification object with `status: "scheduled"`, `send_at`,
`max_late_seconds`, `deliveries: []`, a `routing_preview` (devices, suppressed, capability gaps
as if it routed now, labelled a preview), plus `reply_handle` / `webhook_secret` as today.

### Reading

- `GET /v1/notifications?status=scheduled` / MCP `list_notifications(status="scheduled")` returns
  **all** the caller's scheduled notifications sorted by `send_at` ascending, without a cursor
  (`before` is ignored, `next_before` is null; the caps bound the size) (HR-12). Other listings
  keep their current order and cursor and include scheduled ones (status `scheduled`).
- `get_notification` shows `send_at`, `max_late_seconds`, `deliver_by`, and after release
  `released_at`.

### Changing (sender with `send` on its own, admin on any)

`PATCH /v1/notifications/{id}` and MCP `update_scheduled_notification(notification_id, …)`.

Editable while `status == "scheduled"`: `send_at` / `delay` (relative to now), `max_late_seconds`,
`title`, `message`, `severity`, `targets`, `kinds`, `url`, `url_title`, `actions`, `tags`,
`require_ack`, `ttl_seconds` / `expires_at`. Plus `send_now: true` (release immediately; not
combinable with a new time). Omitted fields are unchanged; `null` clears an optional field.

**One transaction (HR-5):** read the row, merge the patch, validate the whole result (same rules
as a submit: targets resolve now, expiry after send time, horizon; the follow-up gate is not
re-run), write it, emit the event — all inside one `db.tx()` (BEGIN IMMEDIATE under the write
lock). Routing and planning are synchronous, so nothing needs to happen outside it. If the row is
no longer `scheduled` (released, cancelled, missed), 409 with its current status and nothing
changes. After commit: `wake` the dispatcher (send-now, or a time moved earlier). Response: the
updated notification with a fresh `routing_preview`.

TTL on reschedule: `ttl_seconds` and an explicit `expires_at` are both stored. On every change,
`expires_at_effective = min(explicit expires_at, send time + ttl_seconds)` is recomputed, so a
TTL moves with the send time and an explicit expiry stays fixed (and must still be after the new
send time; 422 otherwise, the admin UI shows that message and offers to clear the expiry).

Cancel: `POST /v1/notifications/{id}/cancel` / MCP `cancel_notification`. Today it only cancels
deliveries and returns 409 "nothing to cancel" without them, and the route requires `manage`
(HR-7). New rule, decided **inside the service, in one transaction**:
`UPDATE notifications SET status='cancelled', updated_at=? WHERE id=? AND status='scheduled' AND
(admin OR client_id=caller)`. If one row changed: emit the event, done (`send` scope suffices).
Otherwise the caller must have `manage` and the existing delivery cancel runs. The route and the
MCP tool therefore only require `send`; the `manage` check moves into the service. Retry on a
still-scheduled one → 409.

Authorization note: letting a `send`-scope client change `targets`, `severity` or `kinds` of its
own scheduled notification is **not** an escalation; a submit already allows any target and
severity. Do not add a `manage` gate for it later.

### Events and webhooks

New types registered in `models.py` `EVENT_TYPES` (otherwise subscribing to them is a 422) (HR-14):

- `notification.scheduled` at submit (instead of `notification.created`; payload adds `send_at`).
- `notification.updated` on each edit (payload: changed field names, old/new `send_at` when it
  moved, `by`: the client id that made the change).
- `notification.missed` when it is marked missed.
- At release: `notification.created` with the usual payload plus `scheduled: true`, then the usual
  `delivery.queued` events. Subscribers that only know `notification.created` see it at the
  moment it really goes out.
- `cancelled` as today.

Release notes must say that `*` and `notification.*` subscribers will start receiving the three
new types.

## Behaviour

### Storage

New columns on `notifications` (added by the existing column-migration helper) (HR-13):

| Column | Meaning |
|---|---|
| `send_at TEXT NULL` | UTC ISO; null for immediate notifications |
| `max_late_seconds INTEGER NULL` | as submitted (default applied) |
| `deliver_by TEXT NULL` | `send_at + max_late_seconds`, recomputed on edit |
| `ttl_seconds INTEGER NULL` | as submitted, so a TTL can move with the send time |
| `explicit_expires_at TEXT NULL` | as submitted; `expires_at` keeps holding the effective expiry |
| `released_at TEXT NULL` | when routing ran (set on success, miss and failure alike) |
| `dismissed_at TEXT NULL` | dismissed from Needs attention |

Plus an index on `(status, send_at)`. `status` is stored as `scheduled` explicitly; status
derivation from deliveries (`derive_notification_status`, `_refresh_status`) must never run on,
or overwrite, a scheduled notification (it would turn "no deliveries" into `suppressed`).

### Release: one door

There is exactly one release function, `release(nid, *, force=False)`, used by the scheduled
release, **send now** and **retry** of a missed/failed release (HR-19). It runs entirely inside
one `db.tx()` (HR-6; there is no intermediate `releasing` status):

1. Re-read the row; continue only if `status='scheduled'` (or, for retry, a missed/failed release
   with no deliveries). Otherwise return its current state.
2. Unless `force`: if `now > deliver_by` → status `expired`, routing note "missed: due at …, could
   not be sent until …", `released_at = now`, event `notification.missed`. Done.
3. If the sender's client no longer exists in config (and is not one of hnl-notify's internal
   clients) → status `failed`, note "sender no longer exists" (HR-8).
4. Otherwise resolve routing **now**, plan deliveries (same code as a submit), insert them,
   set `released_at`, emit events, derive status from the deliveries.
5. Routing error (a target deleted meanwhile) → status `failed`, note with the error.
6. **Zero deliveries** (all suppressed / disabled) → status `suppressed` as today, but it counts
   as a release problem (below) with the suppressed reasons (HR-4). A `reply=true` notification
   whose release produced no reply-capable (email) delivery is also a release problem, and its
   deliveries carry the usual capability gap.

`release_due()` runs at the start of each dispatcher pass and calls `release()` for every
scheduled notification with `send_at <= now`, **each in its own `try/except Exception`** (HR-1):
an unexpected error marks that one notification `failed` with note "release error: <type>:
<message>", emits an event, logs, and the loop continues with the next one and then with normal
dispatch. A bad row can never stop deliveries.

The loop already wakes at least once a second, which is the precision needed; `next_due_in()` is
not changed (HR-15). PATCH and send-now set `svc.wake` after commit. On start-up the first pass
releases everything overdue (subject to `deliver_by`). The service is a single process, so no
cross-process locking is needed.

### Lateness up to dispatch (HR-9)

`deliver_by` also bounds dispatch, not only release: a delivery of a scheduled notification that
has not been dispatched by `deliver_by` (provider down, network down, retries backing off) is
expired by the same sweep that handles `expires_at` (`expire_due`), and the retry backoff never
schedules an attempt past it. `deliver_by` is **never** passed to providers: `expires_at` is
sent to Pushover as a TTL, and using it here would make a delivered message vanish from the phone
an hour later. A delivery expired this way makes the notification a release problem too.

### Which clock counts (HR-2)

`created_at` keeps meaning "submitted". Everything that today means "when it went out" uses the
*effective time* `COALESCE(released_at, send_at, created_at)`:

- Retention: `retention_sweep` never deletes a `scheduled` notification; for all others it
  compares the effective time with `retention_days`. Because `released_at` is set on miss and
  failure too, a missed notification scheduled months ahead survives `retention_days` from the
  miss, with its reply conversation and webhook subscription.
- Attachment purge: skips `scheduled` notifications; others use the effective time against
  `attachment_retention_days`, so a retry soon after a miss still has its files. Docs state that
  a scheduled notification's attachments are kept until send time plus
  `attachment_retention_days`.
- Provider timestamps: `message_for` passes the effective time (Pushover `timestamp`, email
  `Date`), so a ping scheduled a week earlier is not dated a week ago.
- Listings keep `created_at` order and `since` filter, except `status=scheduled` (by `send_at`).

Required test: schedule 200 days ahead, let it be missed, run the retention sweep and the
attachment purge, assert the notification, its attachment bytes and its conversation are still
there.

### Replies

`reply=true` works unchanged: the handle is created at scheduling. Docs and the MCP instructions
tell agents that `notify-wait`'s default timeout is 60 min, so for a notification scheduled later
they pass a `--timeout` covering send time plus the expected answer time, or start `notify-wait`
near the send time; late replies are kept anyway.

A conversation that can never get a reply (HR-18): when the conversation has no notification that
was delivered or is still scheduled/pending (its root was cancelled or missed, and no follow-up
went out), the claim endpoint returns **410** with the reason, and `notify-wait` exits 1 with
"notification was cancelled/missed". Exact predicate left to the builder.

### Attention, health and metrics

- `GET /v1/attention` / `list_attention` get a new notification-level bucket `release_problems`
  (additive; existing buckets unchanged). It lists, not yet dismissed: missed notifications,
  release failures (routing error, sender gone, release error), releases that reached no device
  or no reply-capable device, deliveries expired by `deliver_by`, and any notification still
  `scheduled` more than 60 s past `send_at` (release stuck). Today attention, dismiss and retry
  work only on deliveries, and these notifications often have none: this needs new code.
- Dismiss: sets `notifications.dismissed_at`; the existing dismiss endpoint/UI action handles a
  notification without deliveries instead of returning 409.
- Retry on a missed or release-failed notification calls `release(nid, force=True)` (routing now,
  lateness ignored; an effective `expires_at` already past → 409 "expired, schedule a new one").
  Same scope as retry today (`manage` for the owner, admin any). On success it leaves the bucket.
- `/health/ready` reports not-ready when any notification is `scheduled` with
  `send_at < now − 60 s` (HR-1; the dispatcher heartbeat alone stays "ok" while a release loops).
- `/metrics`: the existing `notify_notifications{status="scheduled"}` already counts them
  (HR-16); add `notify_scheduled_next_seconds` (absent if none) and `notify_scheduled_overdue`.

## Admin UI

New menu section **Scheduled** between *Access requests* and *Activity*, with a count badge
when there are any.

- Header text: one sentence ("Notifications waiting for their send time. Who gets them is
  decided when they go out.").
- List sorted by send time. Each row: send time plus relative ("tomorrow 07:30 · in 14 h"; needs
  a future-aware formatter, the existing one clamps future times to "just now", HR-20), title (or
  first line of message), targets, severity, sender, markers for reply / attachments / follow-up.
- Times are shown in the service's zone (labelled, e.g. "Europe/Amsterdam"), and the UI sends
  ISO strings with an explicit offset (HR-10).
- Row actions: **Send now**, **Reschedule** (date + time input, quick buttons left to the
  builder), **Edit** (form: title, message, severity, targets, kinds, link url/title, TTL,
  max lateness), **Cancel** (confirmation dialog).
- After an action the list reloads; a 409 shows a toast "Already sent" / "Already cancelled" and
  reloads.
- The edit form shows a routing preview of the edited version using the existing dry-run submit /
  `preview_routing` path; no PATCH dry-run (HR-19).
- Empty state explains how to schedule (one line with `send_at`/`delay`, link to API docs).
- Activity shows scheduled notifications too, labelled "scheduled for …"; release problems appear
  under Needs attention with Dismiss and Retry.
- Deleting an API client: the confirmation says "N scheduled notifications from this client will
  be cancelled", and deleting cancels them (HR-8), so a later client with the same name cannot
  inherit them.

Admin endpoints follow the existing admin API pattern (session cookie or admin token, the
`X-Notify-Admin` header and same-origin checks for mutations) and call the same service methods
as the REST endpoints with an admin client.

## Discovery

- MCP server instructions: a short paragraph, with the zone filled in from the service's actual
  zone at start-up, not hard-coded (HR-10): "Scheduling: pass send_at (ISO time; without offset =
  <zone>) or delay ('2h', '30m', seconds) to send later … list with
  list_notifications(status='scheduled'), change with update_scheduled_notification, cancel with
  cancel_notification (your own scheduled ones need only the send scope). Routing is decided at
  send time. With reply=true, give notify-wait a --timeout that covers the wait."
- `send_notification` tool docstring documents `send_at`, `delay`, `max_late_seconds`.
- New MCP tool `update_scheduled_notification`.
- README: scheduling section with REST and MCP examples. `docs/architecture.md`: lifecycle
  (`scheduled → released → normal delivery states`, or `cancelled` / `expired (missed)`),
  storage, the effective-time rule, attachment retention for scheduled notifications. Server docs
  (`~/documentation/notifications.md`, not in the repo): how agents schedule.
- `notify-wait` help text mentions scheduled notifications, `--timeout`, and exit 1 on 410.

Duration grammar (HR-17): `delay` and `notify-wait --timeout` accept the same grammar (`90`,
`90s`, `45m`, `2h`, `1d`, combinations like `2h30m`). `notify-wait` is a standalone stdlib
script and keeps its own parser; one shared test table runs against both parsers.

## Calls made by the designer (not discussed with the owner)

- Lateness default 1 h, range 1 min … 1 day; it bounds dispatch as well as release.
- "Immediate" threshold: ≤ 5 s ahead or ≤ 60 s in the past; more than 60 s past → error.
- Caps per client: 100 scheduled, 50 MiB of scheduled attachment bytes. No global cap.
- Offset-less times use the container's zone; no setting.
- `in_reply_to` a still-scheduled notification is rejected.
- Event names `notification.scheduled`, `notification.updated`, `notification.missed`;
  `notification.created` moves to release time for scheduled ones.
- Editable field list above; attachments/reply/webhook/data/source not editable.
- TTL moves with the send time on reschedule; explicit `expires_at` does not.
- Retention, attachment purge and provider timestamps count from the effective time.
- The `release_problems` attention bucket; dismiss via `dismissed_at`.
- Deleting a client cancels its scheduled notifications.
- Idempotent replay with a different send time → 409.
- Version 0.8.0.

## Left to the builder

- Exact UI layout, quick-reschedule buttons, how the routing preview is shown in the edit form.
- Delay parser details beyond the shared test table (reject anything ambiguous).
- The exact "can never get a reply" predicate for the 410.
- Test layout, following the existing test suite.

## Review rulings not adopted

- **HR-19, "stuck scheduled" only in `/health/ready`, not in the UI bucket:** rejected. The
  owner asked to see and manage problems in the admin UI; it goes in both places, at almost no
  cost.
- **HR-11 global byte cap:** not adopted; one owner at household scale, the per-client cap is
  enough.
- **HR-12 `(send_at, id)` cursor:** not adopted; no cursor for `status=scheduled`, the caps bound
  the list.
