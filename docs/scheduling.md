# Scheduled notifications

## Scheduling

Any sender can hold a notification back and let Iris send it later. Add **one** of
`send_at` or `delay` to a normal submit; everything else (targets, severity, `kinds`,
attachments, links, actions, `reply`, `webhook`, idempotency keys) works as before.

```sh
# in two hours (a bare number is seconds; 90s, 45m, 2h30m, 1d also work)
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' $N/v1/notifications -d '{
  "targets": ["user:alex"], "title": "Stand up", "message": "Time to stretch.", "delay": "2h"
}'

# at a clock time: with an offset (or Z) it is taken as is ...
#   "send_at": "2026-11-02T07:30:00+01:00"
# ... without one it is local time in the service's zone (the container's TZ, UTC if unset)
#   "send_at": "2026-11-02T07:30:00"
# -> 202 {"id": "ntf_...", "status": "scheduled", "send_at": "2026-11-02T06:30:00Z",
#         "max_late_seconds": 3600, "deliveries": [], "routing_preview": {...}}
```

From MCP, `send_notification(targets=["user:alex"], message="Merge the PR?", send_at="2026-11-02T09:00:00",
reply=true, kinds=["email"])` does the same; `delay` takes `"2h"`, `"30m"` or seconds.

How it behaves:

- **Rules.** `send_at` and `delay` are mutually exclusive. A time at most 5 s ahead (or up to 60 s in the
  past, for clock skew) is simply sent now, exactly as before. More than 60 s in the past or more than
  366 days ahead is a 422. A request that carries a send time reads **every** offset-less time in it
  (`send_at`, `expires_at`) in the service's zone; requests without a send time keep the old rule
  (offset-less `expires_at` is UTC). `ttl_seconds` counts from the send time.
- **Routing is decided when it goes out**, not when you schedule: a device added or disabled in between
  applies. The 202 carries a `routing_preview` of how it would route now. Targets, attachments (URLs are
  fetched at scheduling time), sizes and follow-up rules are validated immediately, so mistakes surface at once.
- **How late is still on time.** `max_late_seconds` (60 to 86 400, default 3 600) is how long after
  `send_at` it may still be sent, for example after an outage of Iris or the provider. The limit bounds
  dispatch as well as release: a delivery not dispatched by `send_at + max_late_seconds` expires. Past it,
  the notification is **missed** (status `expired`, routing note "missed: due at ...") and shows under Needs
  attention. A morning ping therefore never arrives in the afternoon.
- **Caps per client**, 422 beyond them (admin tokens and Iris' own notifications are exempt): at most
  100 notifications in `scheduled` and 50 MiB of attachment bytes held by them.
- **Idempotency.** Replaying a key with a send time that resolves differently answers 409 ("use PATCH").
- `in_reply_to` a notification that is itself still scheduled is a 422 (the mail needs a sent parent to thread to).

**List, change, cancel.**

```sh
curl -s -H "Authorization: Bearer $TOKEN" "$N/v1/notifications?status=scheduled"   # all of yours, by send_at, no cursor

# reschedule / edit (omitted fields stay, null clears an optional one); send_now releases it immediately
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' $N/v1/notifications/ntf_... \
  -d '{"delay": "30m", "message": "Stretch, then merge.", "ttl_seconds": null}'
curl -s -X PATCH ... -d '{"send_now": true}'

curl -s -X POST -H "Authorization: Bearer $TOKEN" $N/v1/notifications/ntf_.../cancel
```

Editable while `scheduled`: `send_at` / `delay`, `max_late_seconds`, `title`, `message`, `severity`,
`targets`, `kinds`, `url`, `url_title`, `actions` (REST only: the MCP tool has no `actions`), `tags`, `require_ack`, `ttl_seconds`, `expires_at`.
Attachments, reply settings, `webhook`, `data`, `source` and the idempotency key are not editable (cancel and
schedule again). A TTL moves with the send time; an explicit `expires_at` stays put and must remain after it.
A notification that has already left is a 409 with its current status and nothing changes. An idempotent replay
(same key) after the notification was rescheduled returns 409 ("use PATCH"). For notifications
that are still `scheduled` the **`send` scope is enough** to change or cancel your own (an admin can do it
for any); retry and cancelling released notifications still need `manage` (cancel accepts `send` or `manage` at
the door, so a `read,manage` token keeps cancelling sent ones as in 0.7.0). Cancelling a scheduled notification
frees its attachment bytes at once and it then ages out like any finished notification. Over MCP:
`update_scheduled_notification(notification_id, ..., clear=["url", "ttl_seconds"])` (`clear` names the
optional fields to remove), `list_notifications(status="scheduled")` and `cancel_notification`. The admin UI
has a **Scheduled** section (send now, reschedule, edit, cancel) and shows times in the service's zone.

**Replies.** `reply=true` works as usual and the `reply_handle` comes back at scheduling time. `notify-wait`
waits 60 minutes by default, so pass `--timeout` long enough to cover the time until the send plus the answer
(`notify-wait --timeout 20h`), or start it near the send time. In `notify-wait` a bare number is
**minutes**, in `delay` it is **seconds**; unit forms (`90s`, `45m`, `2h30m`, `1d`) mean the same in both.
If the notification was cancelled or missed and nothing else of the conversation went out, the claim endpoint
answers **410** and `notify-wait` exits 1 ("cancelled/missed"), because no reply can come.

**When it cannot go out.** Under *Needs attention* (and `list_attention`'s new `release_problems` bucket) you find
missed notifications, release failures (target deleted meanwhile, sender removed from the config, release error),
releases that reached no device (or, with `reply`, no email device), deliveries expired by the lateness limit, and
anything still `scheduled` more than 60 s past its time. They can be dismissed or retried; a retry routes and sends
now and ignores the lateness limit (not an already passed `expires_at`: 409). `/health/ready` reports
`checks.scheduler` as not ready while something is overdue, and `/metrics` has `notify_scheduled_next_seconds`
(absent when nothing is scheduled) and `notify_scheduled_overdue`.

**History.** The history (`GET /v1/notifications`, MCP `list_notifications`, the admin *Overview* and *Activity*) shows
what was sent at the moment it was sent, scheduled or not: it is ordered by `sent_at`, which `since` and `before` also
use. What still waits, or was cancelled while waiting, is left out; `status=scheduled` and `status=cancelled` still
list those. A scheduled notification that was missed or could not be released stays in, with `sent_at` at the moment
that happened.

**Admin Activity list.** *Activity* shows everything in one list, newest first, 25 to a page (Newer / Older): what was
sent (at its send time, as above), replies and rejected mail, each marked as sent or received. Bodies are shown as plain
text, never rendered, with *Show more* for long ones. It filters on All / Sent / Received and on sender; a reply counts
under the service whose message it answers, and rejected mail only shows without a sender filter, because the
notification it names comes from the unverified mail. *Find by ID* takes an exact `ntf_…` or `rpl_…` and also shows a
notification that was never sent, marked as such. The list comes from `GET /admin/api/timeline` (`limit`, `before`,
`after`, `direction`, `source`, `find`); as with `GET /admin/api/replies`, a bearer admin token gets states and times but
no reply text, sender or subject.

**Retention.** History retention, the attachment purge and the timestamp the provider shows all count from the
moment a notification went out (`released_at`, else `send_at`), not from when it was submitted. A scheduled
notification is never purged; its attachments are kept until its send time plus
`server.attachment_retention_days`.

**Events.** Scheduling adds `notification.scheduled` (at submit, instead of `notification.created`),
`notification.updated` (each edit) and `notification.missed`. `notification.created` now fires when a scheduled
notification is released (payload gains `scheduled: true`), followed by the usual `delivery.queued`.
Subscribers to `*` or `notification.*` therefore start receiving `notification.scheduled`,
`notification.updated` and `notification.missed`.

[← back to the README](../README.md)
