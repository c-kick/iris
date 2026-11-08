# Reply mailbox cleanup and late replies — design

> Archived record: written when this project was called hnl-notify (renamed to Iris in 0.15.0). Names, commands and paths below are kept as they were decided; see docs/upgrading.md for what changed.

Date: 2026-10-26 · Status: revision 2 (after builder check, staff review and ambassador ruling) ·
Target version: 0.7.0 (on top of 0.6.2)

Two parts of the reply feature (spec `2026-10-14-email-replies-design.md`), shipped together:
**A** keeps the reply mailbox from growing without limit, **B** lets an agent still collect a
reply that arrived after it stopped listening, while only the agent that started a conversation
can ever collect, read or continue it.

Revision 2 folds in staff-review findings SR-1…SR-18 as ruled by the project ambassador
(rejected: a cap floor under SR-9 and a per-mail IMAP keyword under SR-8; see the end).

## Part A — Reply mailbox cleanup

### Intent

The poller moves every handled reply to the Processed folder and every rejected mail to the
Quarantine folder, and nothing ever deletes them. Measured on a live install after 12 days:
51 mails / 110 MiB in Processed, 11 / 27 MiB in Quarantine (replies carry images), so roughly
4 GB a year. The mailbox had no IMAP quota, so the real limit is the mail server's disk, which
it shares with other things. The owner wants hnl-notify to keep the mailbox tidy by itself, and
to be able to see and clean it from the admin UI. Protecting that disk is the primary goal.

What the mailbox copy is (SR-9):

- **Processed** duplicates what hnl-notify stored when it accepted the reply (text and
  attachments), and later replies are matched by Message-ID from the database, so deleting it
  never affects what an agent can collect. But reply attachment bytes in the database are purged
  after `server.attachment_retention_days` (default 7); after that, Processed holds the only copy
  of the files.
- **Quarantine** holds the **only** copy of rejected mail, including genuine replies rejected
  for reasons like `dkim_fail`; the database keeps headers only. That is why it is kept longer.

### Goals

1. hnl-notify deletes old mail from the Processed and Quarantine folders by itself, by age, with a
   size cap per folder as a safety net.
2. The admin UI has a **Reply mailbox** panel that shows the folders' state, holds the cleanup
   settings, and has a "Clean up now" button with a preview.
3. When a size cap forces deletions, the admin gets a push notification: that only happens when
   something unusual is filling the folder.

### Non-goals

- Cleaning hnl-notify's own database. It already has retention: notifications (and with them
  replies) are deleted after `server.retention_days` (default 90), reply and outgoing attachment
  bytes after `server.attachment_retention_days` (default 7), quarantine rows after
  `retention_days`. No change.
- Folders other than Processed and Quarantine. Not touched.
- Browsing or picking individual mails in the UI.
- A Trash folder or any undo.

### Anti-goals (a failure even if it works)

- Ever deleting from the folder the poller reads (INBOX by default): a reply not yet handled
  would be lost.
- Deleting from a folder that is not hnl-notify's own (a misconfigured folder name pointing at
  Sent, Archive, Trash…).
- Blocking or slowing reply handling: a slow or failing cleanup must not delay the poll.
- Leaking the IMAP password into errors, logs or the UI.

### Decisions (the owner's)

| Question | Decision |
|---|---|
| Mode | Automatic, plus a manual "Clean up now" in the admin UI. |
| Processed | Mail older than **30 days** is deleted. |
| Quarantine | Mail older than **90 days** is deleted (useful for diagnosing rejections weeks later). |
| INBOX | Never touched. |
| How | Permanent delete: flag `\Deleted`, expunge. Space is freed at once; no Trash. |
| Size guard | Per-folder size cap; over it, the oldest mail goes first until under the cap; the admin gets a push notification. No age floor under the cap (confirmed after SR-9): a flood of mail to the reply address may push out recent Quarantine mail, and the alert says so; the disk is protected either way. |
| Manual UI | Per-folder overview (messages, size, oldest) and "Clean up now", which first shows what the current rules would remove (N mails, X MiB) and asks to confirm. |
| Settings | On the new Reply mailbox panel (not with the provider's connection settings). |

Agreed defaults (proposed by the designer, accepted by the owner):

- The automatic cleanup runs about once a day.
- Size caps: **1024 MiB** for Processed, **500 MiB** for Quarantine.
- Age is the server's arrival time (IMAP `INTERNALDATE`), not the sender's `Date` header.
- The size alert goes to the admin at severity `normal`, at most once per folder per day.
- A value of 0 switches that rule off (0 days = no age rule, 0 MiB = no cap).

### Behaviour

**Scope.** Per email provider that has a reply mailbox configured (`replies_ready`). Cleanup acts
on exactly two folders: `reply_processed` and `reply_quarantine`. It refuses to run for a
provider (and the panel says why) when:

- either folder equals `reply_inbox`, or the two equal each other (case-insensitive), or
- the server's `LIST` reply marks either folder with a SPECIAL-USE attribute (`\Sent`, `\Trash`,
  `\Junk`, `\All`, `\Archive`, `\Drafts`, `\Flagged`) (SR-8).

**Settings** (SR-7) live in their own config section, `mailbox_cleanup.<provider name>`, not in
the provider's settings (the provider form rebuilds its settings from its schema on every save,
`admin.py:254-271`, and would drop them). Validated with the rest of the config, removed when the
provider is deleted, follows config export/import.

| Setting | Default | Range |
|---|---|---|
| `processed_days` | 30 | 0–3650 |
| `quarantine_days` | 90 | 0–3650 |
| `processed_max_mib` | 1024 | 0–1048576 |
| `quarantine_max_mib` | 500 | 0–1048576 |
| `enabled` | true | automatic run on/off; manual cleanup always available |
| `alert_targets` | `[]` | targets for the size alert; empty → the same fallback the access-request notifications use (`registrations.py:162` helper, reused, SR-16) |

A provider with replies configured and no section uses the defaults, so existing installs get
cleanup on upgrade, and the first automatic run removes everything past the rules. The release
notes say so.

**IMAP access for cleanup** (SR-8). Cleanup has its own entry point next to `ImapMailbox`, not
`ImapMailbox.open()` (which creates folders and selects INBOX read-write):

- Log in, read `CAPABILITY` and `LIST` once.
- Preview: `EXAMINE` the target folder (read-only). Delete: `SELECT` the target folder only.
  Never `CREATE`, never select `reply_inbox`. Before any `STORE`/`EXPUNGE`, assert the selected
  folder is the target.
- UIDPLUS is decided from `CAPABILITY`, never by catching an exception. With UIDPLUS:
  `UID EXPUNGE <set>`. Without: plain `EXPUNGE` in the selected target folder (only the messages
  flagged by this pass, plus any already flagged there).
- A `NO` on `FETCH` of an empty folder counts as empty (as `ImapMailbox.list` already does).
- Password masking goes through one shared helper used by the poller, the connection test and
  cleanup (today copied in `worker.py:189-192` and `email_inbound.py:174-176`).
- Use the `svc.open_mailbox` seam pattern so tests can inject a fake.

**One cleanup pass for a folder:**

1. Fetch `UID`, `RFC822.SIZE`, `INTERNALDATE` for all messages.
2. Select with a pure function `select(messages, now, days, cap_bytes) -> (by_age, by_cap)`:
   age rule first (every message older than now − days), then, over the rest, if the total size
   is above the cap, the oldest (by `INTERNALDATE`, then UID) until the total is at or under the
   cap. `INTERNALDATE` is parsed explicitly to UTC.
3. Delete the selected UIDs in batches. The session deadline timer is re-armed per batch (today
   it is armed once at `open()`), or each batch uses its own session.
4. Result per folder: messages and bytes before, removed by age and by cap, after, errors.

A preview runs steps 1–2 only.

**Concurrency** (SR-6). One `asyncio.Lock` per provider, held by the poll, the cleanup, the
preview and the admin connection test, so no two IMAP sessions of hnl-notify act on the same
mailbox at once. The automatic cleanup runs in its own worker task, not inline in
`_inbound_loop` (which handles providers one after another), so a long cleanup never delays a
poll by more than the lock wait of one batch.

**Automatic run.** At most once a day per provider, first ~10 minutes after service start, then
when a day has passed since the last successful run. Failures are retried with back-off and never
affect polling. Last run time, result and error are shown on the panel; whether they survive a
restart is the builder's call (a restart may cause an early run, which is harmless).

**Manual run** (SR-14). `POST …/cleanup` runs under the lock, bounded to one session of at most
60 s; whatever is left stays for the next run, and the result says so. No background job and
polling. `GET /api/mailbox` results are cached for ~30 s per provider so opening the panel
repeatedly doesn't hammer the server.

**Size alert** (SR-15). When a pass removed mail by cap, submit an internal notification (see
Part B, "Internal submissions") to the alert targets: title "Reply mailbox: <folder> over its size
cap", message with the folder, what was removed (count, MiB), the size now and the cap. Severity
`normal`, tag `mailbox-cleanup`, idempotency key `mailbox-cleanup:<provider>:<folder>:<UTC date>`,
which gives the once-a-day limit across restarts without extra state. Manual runs alert the same
way.

**Admin API.** Reading (`GET /api/mailbox`) accepts any admin client. Changing settings
(`PUT /api/mailbox/{provider}`) and deleting (`POST /api/mailbox/{provider}/cleanup`) require the
signed-in browser session (the `via_session` marker of Part B), consistent with the owner's
"reply content only in the admin UI" decision, and each emits an event naming the client.

- `GET /api/mailbox` → per provider: folders (messages, bytes, oldest), the preview of what the
  rules would remove, settings, last run, and any reason cleanup refuses to run.
- `PUT /api/mailbox/{provider}` → save the settings.
- `POST /api/mailbox/{provider}/cleanup` → run a pass now (recomputes the selection), return the
  result.

**Admin UI.** A new "Reply mailbox" section, shown when at least one provider has replies
configured: per provider a table of folders (messages, size, oldest), the "would remove" preview,
the settings form, the last automatic run, and "Clean up now" → confirm dialog with the preview
numbers → result. Same look and idioms as the existing sections.

### Notes (SR-18)

- `INTERNALDATE` survives MOVE, so mail that waited long in INBOX (e.g. during an outage) can be
  deleted soon after it reaches Processed. Acceptable.
- On servers where EXPUNGE only removes a label (Gmail-style), cleanup frees no space. README
  says so.

### Constraints

- Container memory budget 320 MiB: never download message bodies; sizes and dates only.
- Repo stays anonymised: example.org / placeholder names in code, tests and docs.
- Tests use fakes, no live server. `FakeMailbox` and `FakeIMAP` need extending: per-folder
  messages with `INTERNALDATE`, a `CAPABILITY` set with and without UIDPLUS, `STORE` flags, a
  `LIST` with SPECIAL-USE attributes, and an assertion that `STORE`/`EXPUNGE` never happen while
  INBOX is selected.

## Part B — Late replies and conversation ownership

### Intent

Today a reply that nobody collects within 10 minutes becomes `unclaimed`, the person gets
"This agent is no longer listening, so your reply was not delivered", and the reply is never
handed to an agent afterwards (success criterion 4 of the 0.6.0 spec). In practice agents stop
listening (a `notify-wait` timeout, a busy stretch, a new session continuing the work) and come
back later; the answer is then lost to them. The owner wants an agent to still be able to collect
such a reply. This part replaces criterion 4 and the "Reply after the agent stopped" and "Who may
collect" decisions of the 0.6.0 spec.

At the same time, Agent B must never be able to pick up, read or continue Agent A's
conversation, also not when A has stopped listening. Today "who may collect" is the sending
client or an admin token, and agent sessions usually share one token, so B can already take A's
waiting replies.

### Threat model (SR-10)

hnl-notify is meant for a trusted LAN. The reply handle protects against **accidental or
confused** cross-agent access: a wrong id, a new session reusing an old one, a second agent on
the same token. It does not protect against a hostile agent running as the same user: the handle
appears in the sending agent's tool output, which its transcript keeps on disk. That is accepted.

### Decisions (the owner's)

| Question | Decision |
|---|---|
| Autoreply | Stays, but honest: the agent isn't listening right now; the reply is kept and the agent may still pick it up later. |
| Pickup | `notify-wait` (the claim endpoint) returns `waiting` replies first, then late (`unclaimed`) ones, oldest first. Agents need nothing new. |
| How long collectable | As long as the reply is stored: until the normal database retention deletes it; attachments follow `attachment_retention_days` (reported as `purged`). |
| Ownership | A reply conversation belongs to the agent that started it, proven by a **secret reply handle**. |
| Applies to | All replies of a conversation: `waiting`, `unclaimed` and already `claimed`. |
| Follow-ups | Sending into a reply conversation needs its handle. |
| Upgrade | Conversations started before 0.7.0 keep the old token rule. Replies already `unclaimed` before the upgrade stay uncollectable (the person was told "not delivered"). |
| Admin tokens | Reply text, attachments and the quarantine view need the owner's signed-in admin UI session. A bearer admin token sees reply states and times only. |
| Retry | Revised after SR-1: the handle is returned **once**. An idempotent replay of the send never returns it and never rotates it. (Rotation would let any agent sharing the token replay A's send and take the conversation.) |

### Conversations table (SR-3)

A new table `conversations(id PRIMARY KEY, handle_sha256, legacy, created_at)`, `id` = the
conversation id (the root notification id, as today).

- `retention_sweep` deletes a conversation row only when no notification of that conversation is
  left (notifications are deleted one by one by their own age, `core.py:975-989`, and
  `replies.conversation_id` has no foreign key).
- Access to a reply-enabled conversation without a row is refused (fail closed, `404`).
- Migration: in the step that creates the table, insert a `legacy=1` row for every existing
  conversation that has a reply-enabled notification, and mark every reply that is `unclaimed`
  at that moment as not collectable late (a column added in the same step, e.g.
  `late_ok INTEGER NOT NULL DEFAULT 0`, set to 1 by the sweep from 0.7.0 on). This is the first
  data backfill in `db.py`'s migrations; it must run exactly once, in the same step that adds the
  table and column.
- A legacy conversation stays legacy, also when a `reply: true` follow-up arrives after the
  upgrade: the old token rule, and its `unclaimed` replies are never handed out (SR-11).

### Reply handle

- **Created** on the first `reply: true` notification in a conversation: the root, or a
  `reply: true` follow-up into a non-legacy conversation that had no handle yet (that follow-up
  passes on the token rule; from then on the handle is required). At least 128 random bits.
  Stored as a SHA-256 hash on the conversation row; never stored, logged or shown in plain text.
- **Returned once**, in the send response of the notification that created it (REST and MCP
  `send_notification`), as `reply_handle`, in the form `<conversation id>:<secret>`. Follow-ups
  do not return it again. Following the `webhook_secret` precedent (`core.py:399-400`), an
  idempotent replay (`_replay`, `core.py:254`) returns `reply_handle: null` and
  `reply_handle_issued: true`, so an agent that lost the response knows to start a new
  conversation. `idempotency_key` is left out of read outputs for notifications in a
  non-legacy conversation (defence in depth).
- **Sent separately, never via `in_reply_to`** (SR-4). `in_reply_to` stays a notification id
  (it is stored, returned to readers, and limited to 64 characters). The handle travels in a
  `reply_handle` request field (MCP argument) or an `X-Reply-Handle` header (REST); never in a
  URL. The server checks that the handle's conversation equals the conversation of
  `in_reply_to` / the notification in the path. The handle is never written to any column,
  event or log record.
- **One access gate** (SR-13): `Replies._access(client, notification_id, handle)` used by all
  four paths: claim, listing (`GET …/replies`, MCP `get_replies`), attachment download (today
  by reply id, `replies.py:152-157`) and follow-up submission. It runs **before** anything else,
  in particular before the claim's `listener_seen_at` update (`replies.py:119`), so a waiter with
  a wrong handle can't keep a conversation looking "listened to" and suppress the autoreply.
  The sending-token check stays in addition to the handle.
- **Errors** (SR-17): a missing or wrong handle, and a plain id where a handle is needed, all
  answer `404` like an unknown notification.
- **Lost handle**: agents can no longer collect that conversation; the owner still sees it in
  the admin UI. No recovery.

### Session vs bearer (SR-5)

`Client` gets an explicit `via_session: bool`, set to true only where the admin dependency
accepts the browser cookie session (`admin.py:85-89`). Equality with `UI_CLIENT` is not used to
tell them apart (`Client` is a frozen dataclass compared on `(id, scopes)`, and a configured
client may be called `admin-ui`). Config validation reserves the client names `admin-ui` and
`hnl-notify`.

- Reply text, attachments and quarantine details in admin endpoints (`GET /admin/api/replies`,
  attachments) are shown only to `via_session` clients; bearer admin tokens get states, times and
  counts. The `client.admin` exemptions in `Replies.list` and `Replies.attachment` go.
- An admin bearer token gets no bypass of the handle for claims or follow-ups.
- Release notes: scripts that read reply text with an admin token stop getting it.

### Internal submissions (SR-2)

hnl-notify's own notifications (the autoreply, the mailbox size alert) are exempt from the handle
check through a private flag on `NotificationIn` (like the existing `_auto_submitted`) that REST
and MCP cannot set, not through `client.admin`. Without this, the autoreply
(`replies.py:222-229`, `in_reply_to` with no handle) would be refused and the error swallowed by
the sweep (`replies.py:202-205`).

### Behaviour

- **Claim** (`POST /v1/notifications/{id}/replies/claim`, used by `notify-wait`), after the
  access gate: (1) the reply this waiter already holds, (2) the oldest `waiting` reply of the
  conversation, (3) the oldest `unclaimed` reply with `late_ok=1`. Claiming a late reply works
  exactly like a `waiting` one: state `claimed`, `reply.claimed` event, the delivery moves to
  `interacted` if still `dispatched`/`delivered`. The claim response says whether the reply is
  late (it was `unclaimed`).
- **Sweep and grace** unchanged: 10 min without a listener → `unclaimed` (with `late_ok=1` in
  non-legacy conversations) + autoreply, at most one per conversation per hour.
- **Autoreply text**: "The agent isn't listening right now. Your reply has been kept and the agent
  can still pick it up later. Received <time>." Title unchanged. Legacy conversations keep the old
  text, since their late replies stay uncollectable.
- **Listing**: text of a reply that is not claimed stays hidden from agents, also with the
  handle; an agent reads it by claiming it with `notify-wait`. Claimed replies' text needs the
  handle. MCP `get_replies` gains a `reply_handle` argument.
- **No second mail** when a late reply is collected.

### notify-wait (SR-12)

- Takes the handle from `NOTIFY_REPLY_HANDLE`, `--handle-file FILE`, or as the positional
  argument (single-user host, and the handle is in the transcript anyway). A positional plain
  notification id still works for legacy conversations. The handle is never printed in output,
  logs or errors.
- Sends the handle as `X-Reply-Handle` on claims and on attachment downloads.
- Marks late replies in its output: "Late reply (received <time>) to: <notification title>", so
  an agent that has since asked a follow-up question can tell which question it answers.
- Lists purged attachments by name as "not available any more" (today they are dropped
  silently).

### Docs

README, MCP server instructions and tool descriptions, and `notify-wait` help are updated for the
handle, late replies and the admin-token change. Deployment note: agent instructions outside the
repo that say `notify-wait <notification_id>` need the same change.

## Tests that must exist

- `select()` table tests: age only, cap only, ties by UID, 0 = off, total at or under the cap, one
  message bigger than the cap.
- Cleanup never `STORE`/`EXPUNGE`s while INBOX is selected; refuses overlapping folder names and
  SPECIAL-USE folders; plain-`EXPUNGE` path without UIDPLUS; empty-folder `NO`.
- A manual cleanup during a poll waits for the lock.
- Saving the provider form keeps the cleanup settings.
- Autoreply still goes out in a handle conversation.
- Replay returns no handle, and the original handle still works.
- A bearer client named `admin-ui` is refused by config validation; a bearer admin token gets no
  reply content.
- Root notification removed by retention while follow-ups remain → access still needs the
  handle; conversation row gone → `404`.
- A wrong handle does not refresh `listener_seen_at`.
- The handle never appears in the `in_reply_to` column, events or captured log records.
- Legacy conversation: old token rule, pre-upgrade `unclaimed` replies never handed out, a
  post-upgrade `reply: true` follow-up stays legacy.
- notify-wait marks late replies and lists purged attachments.

## What's left to the builder

- Exact field, column and endpoint naming where the above clashes with existing conventions.
- Whether cleanup's last-run state is persisted.
- Batch size and back-off details.
- UI layout within the existing admin style.

## Designer's calls (not discussed with the owner)

- Cleanup refuses overlapping folder names.
- Manual cleanup can also send the size alert (same daily key).
- Agents can't read unclaimed reply text through the listing; only via a claim.
- No "picked up after all" follow-up mail.
- The handle is created at the first `reply: true` in a conversation, not only at its root.
- Legacy conversations keep the old autoreply text.

## Review rulings not adopted

- **SR-9 cap floor** (never delete mail younger than N days by cap): rejected; it lets a flood
  fill the disk, the failure this part exists to prevent. Owner confirmed.
- **SR-8 `$HnlNotify` keyword** on every handled mail, with cleanup deleting only flagged mail:
  rejected as more machinery than needed; the folder checks above cover the misconfiguration
  risk.
- **SR-14 background job with polling** for manual cleanup: replaced by a bounded 60 s run.
