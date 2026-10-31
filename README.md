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
Upgrading to 0.6.0 migrates the database in place. Rolling back to 0.5.1
requires restoring the pre-0.6.0 database backup.

Open **http://notify.lan:8930/admin**, enter the setup code and choose an
admin password. Then, in the admin UI:

1. **Providers**: for push, paste the API token of an application created at
   [pushover.net/apps/build](https://pushover.net/apps/build). For email, add
   an *Email (SMTP)* provider: host (e.g. `smtp.example.org`), port, security
   (`starttls`, `tls` or `none`), username and password if the server wants
   a login, and the sender address; then press *Test*. Give the
   session as much time as a slow relay needs (`timeout_seconds`, default 60).
2. **People & groups → Add person**: add yourself (e.g. `alex`).
3. **Devices → Add device**: name it (e.g. `alex-phone`), let it belong to
   you, paste your Pushover user key, press *Find devices* and pick the phone
   (leave it empty to reach all your Pushover devices). hnl-notify re-checks a
   picked device name with Pushover at most every 15 minutes before a push: if
   the device is gone from your account, the push fails and you get one alert,
   instead of Pushover quietly sending it to all your other devices. For email, add a
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

## Browser push

A person's own browser (Chrome, Edge or Firefox, on a computer or phone) can be a push device, with no extra
app or account. It reports back when a message was shown (`delivered`) and when it was clicked (`acknowledged`),
at every severity.

- **HTTPS is required.** Browsers only allow push on a secure origin, so `server.external_url` (Settings →
  "Address of this service") must be an `https://` address that reaches this service through your reverse proxy,
  for example `https://notify.example.org`. Enrollment links are refused (409) otherwise.
- **Set it up.** Admin → Providers → Add provider → Browser push (enter a `mailto:` contact such as
  `mailto:alex@example.org`; the key pair is generated for you and must never change, or every enrolled browser
  stops working). Then press **Enrollment link**, pick the person, and open the link in the browser to enroll.
  The link works once, expires after 24 hours and needs no login. The browser appears as the device
  `alex-browser` (then `alex-browser-2`, …), which you can describe, disable or remove like any device.
- **Routing.** A browser device is `kind: push`, so it is reached like a phone. Attachments are not carried.
- **The browser must be running.** Chrome and Edge only receive notifications while the browser process runs:
  keep "continue running background apps when the browser is closed" on. Firefox must stay open. Messages wait at
  the browser vendor's push service for their lifetime (the notification's expiry, else `default_ttl_seconds`,
  24 h) and are then dropped; a delivery never shown by then ends `expired`.
- **Dead subscriptions.** When the push service answers 404/410 the subscription is gone (browser data cleared,
  permission revoked): the device shows as not ready and the announce targets get one alert. Enroll the browser
  again.
- Only push services of the browser vendors are contacted (FCM, Mozilla, Windows, Apple); an enrollment from
  any other host is refused.

## Replies to agents

An agent can ask a question by email and get the answer back: the person presses
Reply in their normal mail client and the new text (and attachments) reaches the
agent that is waiting for it. Email only; the agent must still be running.

**Setup (admin).** Create a dedicated mailbox (e.g. `replies@example.org`) on a
mail server you run, with IMAP over TLS. In *Providers → Email*, fill the reply
fields: *Reply mailbox address* (the `Reply-To`), IMAP server, port (993),
username and password, and optionally the folder names (`INBOX`, `Processed`,
`Quarantine`; the last two are created if missing). Press *Test*: it
logs in, checks the folders and lists the `Authentication-Results` server names
found in the newest message. Enter the one your mail server writes as
*Mail server name in Authentication-Results* (`authserv_id`, e.g.
`mx.example.org`); it is never filled in for you. Replies are accepted only when
the mail server itself reports `dkim=pass` and `dmarc=pass` for the sender's
domain, so this must be a server you trust to add that header (with DKIM and DMARC
results) to every inbound message.

**Sending.** Add `"reply": true` and an email kind:

```sh
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' $N/v1/notifications -d '{
  "targets": ["user:alex"], "kinds": ["email"], "reply": true,
  "title": "Pick a header design", "message": "A or B? Reply with your choice."
}'
# -> 202 {"id": "ntf_...", ..., "reply_handle": "ntf_...:<secret>"}
# a follow-up in the same thread: "in_reply_to": "<id of the first notification>",
# "reply_handle": "<the handle>" (or the header X-Reply-Handle). Your own notifications only.
```

**The reply handle.** The first `reply: true` notification of a conversation returns
`reply_handle` (`<conversation id>:<secret>`) **once**; keep it, it is not stored in
readable form and cannot be recovered (a replay of the same `idempotency_key` returns
`reply_handle: null` and `reply_handle_issued: true`; start a new conversation if you
lost it). The handle is what lets an agent collect replies and send follow-ups: claim,
listing, attachment download and follow-ups all need it, together with the token that sent
the conversation. A wrong or missing handle answers like an unknown notification (404 on
the reply endpoints, 422 on a follow-up). Send it in the `reply_handle` field or the
`X-Reply-Handle` header, never in `in_reply_to` and never in a URL. A follow-up with
`reply: true` into a conversation that has no handle yet creates one.

*Threat model.* hnl-notify is meant for a trusted LAN. The reply handle protects against
**accidental or confused** cross-agent access: a wrong id, a new session reusing an old
one, a second agent on the same token. It does not protect against a hostile agent running
as the same user: the handle appears in the sending agent's tool output, which its
transcript keeps on disk. That is accepted.

The mail gets `Reply-To` and a footer ("Reply to this email to answer the
agent."). A push device or an email provider without a reply mailbox shows the
gap `replies` in `capability_gaps`; the notification is still sent. Once an
agent collects the first reply, the email delivery becomes `interacted`.

**Waiting.** Start `scripts/notify-wait` as a background job and act when it
exits:

```
NOTIFY_REPLY_HANDLE=<handle> notify-wait [--timeout 60m] [--save-dir DIR] [--url URL] [--json]
notify-wait --handle-file FILE      # the handle read from a file
notify-wait <handle>                # works, but the handle shows in `ps`

Reply from alex@example.org (2026-10-14 15:02 CEST) to "Pick a header design" [rpl_...]:
Go with B, but use the darker blue.
Attachments: /tmp/notify-reply-x1y2/screenshot.png
```

It reads the service address and token from `NOTIFY_URL` / `NOTIFY_TOKEN`, or from
the `hnl-notify` MCP entry in `~/.claude.json`; `--token-file PATH` reads it from a file instead
(for agents without that entry, e.g. Codex). The token is never printed.
The handle comes from a positional argument containing `:`, else `--handle-file`, else
`NOTIFY_REPLY_HANDLE`, and is never printed. Prefer the environment variable or the file: the
positional form is visible to every local user in the process list. A plain notification id
still works for conversations from before 0.7.0. A reply that arrived while nobody was
waiting is printed with a `Late reply (received ...) to: <title>` line first, and
attachments that were already purged are listed as `<name>: not available any more`.
Attachments are saved under sanitised names without overwriting anything.
`--timeout` defaults to 60 minutes; a bare number means minutes (`90s`, `30m`, `2h` also work).
If saving an attachment fails the reply is still printed, a note goes to stderr and the exit code is 0.
Exit codes: **0** reply printed, **1** error (credentials, unknown notification,
bad response), **2** no reply before `--timeout` (replies are kept; run it again later to
collect them). Run it again, or send a follow-up with `in_reply_to` and `reply_handle`,
to continue. `GET /v1/notifications/{id}/replies`
and the MCP tool `get_replies` list the replies of a thread; agents see a
reply's subject, text and attachments only once it has been collected. A reply to an older mail of a thread reaches the
current waiter.

**When nobody is waiting.** A conversation counts as listened to while a waiter
polled in the last 2 minutes. A reply that no waiter collects within 10 minutes
is marked `unclaimed` and the sender gets an automatic mail in the same thread
saying the agent isn't listening right now and the reply has been kept (at most one per
conversation per hour). **The reply can still be collected later**: `notify-wait` with the
same handle hands out waiting replies first, then these late ones. Text of a reply that was
not collected is never shown in listings, only by collecting it. A late reply is kept as long as the
notification it answers (`retention_days`, default 90), its attachments for `attachment_retention_days`.
Conversations that existed
before 0.7.0 keep the old behaviour: their unclaimed replies are never handed out.

**Admin tokens.** Reply text, attachments and quarantine details are shown only in the
signed-in admin page. A bearer token with the `admin` scope gets states, times and counts
(`GET /admin/api/replies`), and no bypass of the reply handle. Scripts that read reply text
with an admin token stop getting it.

**Quarantine.** Mail that fails a check is moved to the `Quarantine` folder and
listed (reason, sender, subject) under *Activity → Replies*; its content is not
stored and agents never see it. Reasons: `automatic` (auto-replies, bulk,
bounces), `no_thread` (not a reply to our mail), `not_reply_enabled`,
`sender_mismatch` (not from the address the mail was sent to), `dkim_fail`,
`too_large` (over 15 MiB, or an oversized or unsized header), `header_mismatch`,
`unreadable`. Reply text is capped at 8000 characters; at most 10 attachments
and 10 MiB are kept.

### Reply mailbox cleanup

hnl-notify moves handled replies to `Processed` and rejected mail to `Quarantine` and, from
0.7.0, deletes old mail from those two folders by itself (never from the folder it reads).

| Setting (per email provider) | Default | Meaning |
|---|---|---|
| `processed_days` / `quarantine_days` | 30 / 90 | delete mail older than this (arrival time, IMAP `INTERNALDATE`) |
| `processed_max_mib` / `quarantine_max_mib` | 1024 / 500 | size cap per folder; over it the oldest mail goes first |
| `enabled` | true | the automatic daily run; *Clean up now* always works |
| `alert_targets` | none | who hears about a folder over its cap (default: whoever hears about access requests) |

`0` switches a rule off. The settings live in the admin UI (*Providers* → *Manage…* on the email provider, with a preview of what the
rules would remove and a *Clean up now* button) or in the `mailbox_cleanup:` section of the config, keyed by
provider name. The automatic run happens about once a day, the first one roughly 10 minutes after start.
When a size cap forces deletions you get a notification (tag `mailbox-cleanup`, at most once per folder per day).
*Clean up now* runs for at most about 60 seconds; if it did not finish, the result says so and you run it again to continue. While a poll or another cleanup holds the mailbox it answers 409 "mailbox busy, try again". Deletion is permanent (`\Deleted` + expunge, no Trash). Cleanup refuses to run when a folder is the folder
the poller reads, when Processed and Quarantine are the same, or when the server marks a folder as
Sent, Trash, Junk, All, Archive, Drafts or Flagged, or when a folder does not exist on the server. Message bodies are never downloaded, only sizes and dates.

Notes: mail that waited long in INBOX keeps its original arrival date, so it can be deleted soon after it reaches
Processed. On servers where expunge only removes a label (Gmail style) cleanup frees no space. `Quarantine` holds
the only copy of rejected mail, which is why it is kept longer.

### Upgrading to 0.7.0

- **The first cleanup run deletes old mail.** A provider with a reply mailbox gets the defaults on upgrade: Processed
  older than 30 days and Quarantine older than 90 days. The first automatic run is about 10 minutes after the service
  starts. Right after starting 0.7.0, open *Providers* → *Manage…*, check the preview ("would delete ..."), and adjust the values
  or untick *Clean up automatically* before then.
- **Reply handles.** Agents must keep the `reply_handle` of each conversation. Agent instructions outside this
  repository that run `notify-wait <notification_id>` must switch to the handle: `NOTIFY_REPLY_HANDLE=<handle>
  notify-wait` or `notify-wait --handle-file FILE`. Follow-ups pass `reply_handle`.
- **Admin tokens no longer read reply text.** Scripts that did so with an `admin` bearer token must read it as the
  agent that sent the conversation (with the handle), or in the admin page.
- **Existing conversations keep the old token rule**, without a handle, and their unclaimed replies stay
  uncollectable. Start a new conversation to get a handle.
- Client names `admin-ui` and `hnl-notify` are reserved in the config.

## Scheduling

Any sender can hold a notification back and let hnl-notify send it later. Add **one** of
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
  `send_at` it may still be sent, for example after an outage of hnl-notify or the provider. The limit bounds
  dispatch as well as release: a delivery not dispatched by `send_at + max_late_seconds` expires. Past it,
  the notification is **missed** (status `expired`, routing note "missed: due at ...") and shows under Needs
  attention. A morning ping therefore never arrives in the afternoon.
- **Caps per client**, 422 beyond them (admin tokens and hnl-notify's own notifications are exempt): at most
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

**Retention.** History retention, the attachment purge and the timestamp the provider shows all count from the
moment a notification went out (`released_at`, else `send_at`), not from when it was submitted. A scheduled
notification is never purged; its attachments are kept until its send time plus
`server.attachment_retention_days`.

**Events.** Scheduling adds `notification.scheduled` (at submit, instead of `notification.created`),
`notification.updated` (each edit) and `notification.missed`. `notification.created` now fires when a scheduled
notification is released (payload gains `scheduled: true`), followed by the usual `delivery.queued`.
Subscribers to `*` or `notification.*` therefore start receiving `notification.scheduled`,
`notification.updated` and `notification.missed`.

### Upgrading to 0.10.0

- Adds browser push (see *Browser push*). Nothing changes for existing providers and devices.
- The schema gains an `enrollments` table and migrates itself on start; take a backup first as usual.
- Browser push needs an `https://` `server.external_url`; without one nothing changes and no enrollment link can be made.
- No new dependency in `requirements.txt`; rebuild the image for the new version.

### Upgrading to 0.9.0 (and 0.9.1)

- No schema change. Pushover device names are checked with Pushover (`users/validate.json`) at start-up, after a
  configuration change, and before a push when the last good check is older than `device_check_seconds` (900).
- **A device name Pushover no longer knows now fails.** Before, Pushover delivered such a push to every other
  device on the account (duplicates). Now that delivery is `failed`, the device shows `ready: false` with the reason,
  and the announce targets get one alert per broken device (source `hnl-notify/device-check`). Fix the device at
  Pushover or in hnl-notify, or disable it.

### Upgrading to 0.8.0

- Nothing changes for senders that do not use `send_at` / `delay`.
- **Time zone.** Offset-less times in a scheduling request are read in the container's `TZ` (`compose.yaml` sets
  `Europe/Amsterdam`; with no `TZ` it is UTC). The new `tzdata` package is in `requirements.txt`, so rebuild the image.
- **Webhooks and SSE.** `*` and `notification.*` subscribers receive three new event types (above).
- The schema gains columns and an index on `notifications` and migrates itself on start; take a backup first as usual.
- Deleting an API client cancels its scheduled notifications.
- **Reply claims.** The claim endpoint also answers **410** for an ordinary (not scheduled) reply conversation
  whose deliveries all failed, expired or were suppressed without ever being dispatched; 0.7.0 long-polled to 204.
  A manual retry makes it 204 (waiting) again.

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
`delivery.failed`, `delivery.expired`, `delivery.cancelled`, `reply.received`,
`reply.claimed`, `reply.unclaimed`, `reply.quarantined`, and (0.8.0) `notification.scheduled`,
`notification.updated`, `notification.missed`.

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
`delay` and `max_late_seconds` (see Scheduling). Tools:
`send_notification`, `preview_routing`, `get_notification`,
`list_notifications`, `list_attention`, `list_targets`, `retry_notification`,
`cancel_notification`, `update_scheduled_notification`, `get_replies` (takes `reply_handle`). Resources: `notify://targets`,
`notify://notifications/{id}`. The server instructions tell agents not to
report `dispatched` as `delivered` and to reserve `critical` for emergencies.

## Pushover acknowledgement callbacks (optional)

Critical alerts are tracked by polling Pushover receipts every 30 s. For
instant acknowledgement, expose `/v1/callbacks/` through a public HTTPS proxy
and set the public address under **Settings** (a random callback secret is
generated). Without that, nothing changes except latency.

## Operations

- Logs: `docker logs hnl-notify` (one JSON object per line; every state change is an `event` line with ids).
- Health: `/health/live`, `/health/ready` (includes `checks.scheduler`). Metrics (Prometheus text): `/metrics` (incl. `notify_scheduled_next_seconds`, `notify_scheduled_overdue`).
- Admin UI: `/admin` (session login, CSRF header, strict CSP). Admin API calls also accept a bearer token with the `admin` scope.
- Backup: `sqlite3 data/notify.sqlite3 ".backup data/notify-$(date +%F).sqlite3"` **plus** `secrets/master.key` (without it the stored Pushover and SMTP secrets cannot be decrypted). Backups include attachment bytes that have not yet been purged. History is kept 90 days (Settings).
- Signed-in admin page only: changing cleanup settings, *Clean up now* and reading reply contents need the browser session; an admin bearer token gets 403 on the first two and states, times and counts only for replies.
- Reply mailbox: *Providers* → *Manage…* in the admin UI (also linked from the size alert) shows the Processed and Quarantine folders, the cleanup
  settings and the last run; the poll, the connection test and cleanup share one lock per mailbox, so only one IMAP
  session of hnl-notify acts on it at a time.
- Stopping: a stop during a mailbox poll or cleanup step waits for that step to finish (bounded by the 120 s IMAP session limit), so `compose.yaml` sets `stop_grace_period: 130s`.
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
