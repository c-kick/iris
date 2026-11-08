# Email, attachments and replies

## Email and attachments

Without `kinds` a notification goes to your push devices exactly as before.
Add `kinds` to choose the path: `["email"]`, `["push"]` or `["push", "email"]`.
A person without a device of a requested kind shows up under `suppressed` in
the response. Naming an email device (`device:alex-email`) as a target also
works without `kinds`.

**Direct-only devices.** A device with `direct_only: true` is never reached
through its owner (`user:alex`), even when its kind is requested. A sender
reaches it only by naming it, as a target (`device:alex-mail2`) or through a
group or channel that lists `device:alex-mail2` as a member. With two email
devices where only `alex-mail2` is direct-only:
`["user:alex"]` + `kinds: ["email"]` mails `alex-email`,
`["device:alex-mail2"]` mails `alex-mail2`, and
`["user:alex", "device:alex-mail2"]` mails both. Which device means what is
up to the sender. `GET /v1/targets` and MCP `list_targets` show `direct_only` per device,
and a direct-only device skipped this way is listed under `suppressed`.

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
  or an IP/CIDR for URLs that use an IP address: ports 80/443 only, `cidr:port` for another one). Redirects are re-checked,
  loopback and link-local addresses are always refused, and the URL (which
  may hold a token) is never stored or logged.
- Pushover shows the first image up to 5 MiB; other attachments are listed in
  the delivery's `capability_gaps`. Email carries all of them.
- Email never reports more than "the mail server accepted it" (`dispatched`);
  there is no delivery, read or acknowledgement state and no recall.
- Attachment bytes are kept in the database for 7 days (*Settings → Keep
  attachments for*), then deleted. The database file does not shrink when that
  happens, so size the `data/` volume for your peak.

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
results) to every inbound message. Only the topmost `Authentication-Results` header counts, and it must
carry both results: a server that writes DKIM and DMARC in separate headers is not supported, because a
forged lower header could otherwise supply the missing one.

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
collect them). It always asks at least once, also with `--timeout 0`. A run stopped after the service handed it a
reply gives that reply to the next run with the same handle (notify-wait keeps the id of the last reply it printed in
`~/.cache/hnl-notify-wait`). Run it again, or send a follow-up with `in_reply_to` and `reply_handle`,
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
listed (reason, sender, subject) in the admin *Activity* list; its content is not
stored and agents never see it. Reasons: `automatic` (auto-replies, bulk,
bounces), `no_thread` (not a reply to our mail), `not_reply_enabled`,
`sender_mismatch` (not from the address the mail was sent to), `dkim_fail`,
`too_large` (over 15 MiB, or an oversized or unsized header), `malformed_headers` (a header
block that is not plain CRLF `Name: value` lines, or one the parser cannot read), `too_complex`
(more than 200 MIME parts), `header_mismatch`, `unreadable`. A message is listed once, even if it
is seen again. A message whose handling fails three passes in a row (for example a move the server
refuses) is skipped until the service restarts or the folder's UIDVALIDITY changes, and the admin page shows it as
the poller's last error. A server without UIDPLUS cannot expunge single messages: what was moved stays in the
polled folder flagged `\Deleted` (hnl-notify never expunges the whole folder), and the admin page shows how many;
remove them with a mail client. Two email providers may not read the same reply mailbox folder (same IMAP server,
port, user and folder): saving the second is refused, because each would quarantine the other's replies.
Reply text is capped at 8000 characters; at most 10 attachments
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

[← back to the README](../README.md)
