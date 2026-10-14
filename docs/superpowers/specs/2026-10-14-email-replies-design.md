# Email replies to agents — design

Date: 2026-10-14 · Status: revision 2 (after staff review + ambassador ruling) ·
Target version: 0.6.0

## Intent

Agents (for example Claude Code sessions) email a person reports, previews and
choices through hnl-notify. The person wants to answer by pressing Reply in
their normal mail client, and have exactly that answer reach the agent that
sent the mail, as input it acts on.

Scope is the "agent is still running" case: the agent waits in the background
for an answer. When no agent is listening any more, hnl-notify answers the
reply itself, so the person always knows whether the reply landed.

Success criteria:

1. A sender can mark a notification `reply: true`; email deliveries then carry
   a `Reply-To` of the reply mailbox and a footer saying replies reach the agent.
   Paths that cannot carry replies report a `capability_gap`.
2. A reply from the exact address the mail was sent to, with a DKIM pass for
   that domain established by our own mail server, reaches a waiting agent as
   the new text only (no quoted history, no signature), with its attachments.
3. A waiting agent is woken by the reply without polling in its own context:
   a background `notify-wait` process exits with the reply on stdout.
4. A reply that no agent collects gets an autoreply in the same thread saying it
   was not delivered, and is never handed to an agent afterwards.
5. Mail that fails any check (unknown thread, wrong sender, no trusted DKIM pass,
   automatic mail) never reaches an agent and never triggers an autoreply.
6. Notifications without `reply` behave exactly as before.
7. The service stays within its container budget (320 MiB).

## Decisions

| Question | Decision |
|---|---|
| Agent lifecycle in scope | Agent still running and waiting. Resuming finished sessions is out of scope. |
| Reply after the agent stopped | Autoreply "not delivered" in the same thread; reply stored as `unclaimed`. |
| Inbound transport | IMAP over implicit TLS on a dedicated mailbox, polled every 30 s. No IDLE, no inbound SMTP, no MX change. |
| Reply matching | Delivery id parsed from our own Message-ID in `In-Reply-To` / `References`. Does not depend on `provider_message_id` having been committed. |
| Sender authentication | From equals the recipient address stored on that delivery at dispatch **and** the first `Authentication-Results` header, from the admin-configured authserv-id, shows `dkim=pass` for the From domain. |
| Conversations | Notifications chained by `in_reply_to` share a `conversation_id` (the root notification id). Listening, claiming and the grace period apply per conversation. |
| Agent wake-up | Background CLI `notify-wait` long-polls a claim endpoint and exits on a reply. Blocking MCP tool and Claude Code channels rejected (MCP HTTP timeouts; channels are research preview and unproven for a shared remote server). |
| Listener liveness | A conversation is listened to while a claim poll happened in the last 2 min. |
| Grace for unlistened replies | 10 min from arrival; then `unclaimed` + autoreply. |
| Multiple replies | Stored in arrival order; each claim returns the oldest waiting one. |
| Reply attachments | Kept: up to 10 files / 10 MiB, in a separate `reply_attachments` table, purged with the same retention as outgoing attachments. |
| Raw mail | Not stored; only selected headers. Oversized mail is refused before download. |
| Autoreply | An ordinary internal notification from the system client through the normal pipeline. |
| Who may collect | The client that sent the notification, or an `admin` token. In a typical agent setup all sessions share one token, so this separates applications, not sessions. |
| Provider-agnostic surface | New capability key `replies`. Email declares it when a reply mailbox is configured; Pushover does not. |

## 0. Pre-implementation check (blocks the DKIM design)

Before the plan is executed, verify against the real mail server, with one
real reply from the person's mail provider into the reply mailbox:

1. Does the server prepend its own `Authentication-Results` header to every
   inbound message, and with which authserv-id?
2. Does it remove or rename inbound `Authentication-Results` headers that claim
   its authserv-id? (Send one test mail with such a forged header.)
3. Do replies land in INBOX, or in a junk folder?

Outcomes:

- (1) yes: §3 step 5 stands as written; (2) only changes how much we rely on
  "first header" (it is required either way).
- (1) no: stop and revise §3 step 5 to verify DKIM in-process (`dkimpy`, DNS
  lookups) before implementing. Not built speculatively.
- (3) junk: the person adds a server-side rule, or the junk folder is added to
  the polled folders; decided then.

Results go into the host documentation, not into tracked files.

**Result (2026-10-14, checked against the deployment's server):** (1) yes, the
server prepends its own `Authentication-Results` (Postfix + DKIM/DMARC milter)
to every inbound message, directly below its own `Received` header; a genuine
reply showed `dkim=pass` for the sender's domain. (2) No, forged
`Authentication-Results` headers claiming the server's authserv-id are **not**
removed; they survive directly below the genuine one. The first-header rule in
§3 step 5 is therefore required, and it rejected the forgery in the test.
(3) Replies land in INBOX. Decision: no in-process DKIM verification.

**Accepted residual risk:** if the server's milter ever passes a message
without adding its header (milter failure with an accepting default action),
a forged header becomes the first one. Forging a reply then additionally needs
the Message-ID of a live, reply-enabled delivery (delivery ids carry 40 random
bits and only appear in mail to the recipient) and the recipient's exact
address. In-process DKIM verification (`dkimpy`) remains the fix if this risk
is no longer acceptable.

## 1. Configuration

The email provider's settings (Admin → Providers → Email) gain a **Reply
mailbox** section, stored in the `settings` table like the SMTP settings:

- `imap_host`, `imap_port` (default 993), `imap_user`, `imap_password`
  (secret, AES-256-GCM like other provider secrets), `reply_address`
  (the `Reply-To` value, e.g. `replies@example.org`).
- `authserv_id`: the mail server's authserv-id, **entered by the admin**. The
  Test button shows the authserv-ids it sees in the newest inbox message, as a
  hint; it never fills the field.
- Folders: `inbox` (default `INBOX`), `processed` (default `Processed`),
  `quarantine` (default `Quarantine`); created on first use if missing.
- Implicit TLS, certificate verified unless the existing `verify_tls` email
  setting is off.
- **Test** button: log in, select the inbox, check folders, show authserv-ids,
  log out.

Replies are available when the reply mailbox and `authserv_id` are set; the
email provider then declares the capability `replies: true`.

## 2. Sending side

New request fields on `POST /v1/notifications` and MCP `send_notification`:

- `reply: bool = false`. On a device whose provider declares `replies`: email
  adds `Reply-To: <reply_address>` and appends to text and HTML parts "Reply to
  this email to answer the agent." On any other device: capability gap
  `replies: not supported on this path` (push) or `replies: reply mailbox not
  configured` (email without mailbox); the message is still sent.
- `in_reply_to: str | null`. A notification id of the same client (the system
  client may reference any). The new notification gets the same
  `conversation_id`. Its email to a device sets `In-Reply-To` to the Message-ID
  of the referenced notification's delivery to the same device (derived from
  that delivery id, so no committed `provider_message_id` is needed) and
  `References` to the chain. Unknown id or another client's id → 422.

Stored on `notifications`: `reply_wanted`, `in_reply_to`, `conversation_id`
(own id when not a follow-up), `listener_seen_at`.

Stored on `deliveries`: `recipient_address`, written in the same transaction
that claims the delivery for sending (from the device config at that moment).

Internal only (not in the public request model): `auto_submitted: bool` on
`OutboundMessage`; the email provider adds `Auto-Submitted: auto-replied` when
set.

## 3. Inbound processing

New `notify/providers/email_inbound.py`: an IMAP poller started by the worker
when the email provider has a reply mailbox (restarted on config change).
imaplib runs in a worker thread with a hard session deadline per poll, as
smtplib does in the email provider. Every 30 s: connect, select inbox, list
UIDs with `RFC822.SIZE`, process each, log out. Connection errors back off
(10 s × 3ⁿ, cap 15 min); the admin UI shows last success and last error.

Per message, in order; the first failing step decides:

1. **Size**: `RFC822.SIZE` over 15 MiB → move to Quarantine (`too_large`)
   without fetching the body.
2. **Automatic mail** (headers only): `Auto-Submitted` other than `no`,
   `Precedence: bulk|junk|list`, empty or `MAILER-DAEMON`/`postmaster` return
   path, `multipart/report` → Quarantine (`automatic`).
3. **Thread**: Message-IDs from `In-Reply-To`, then `References` newest first;
   the first whose local part is a delivery id of an email delivery selects it.
   None → Quarantine (`no_thread`). Notification without `reply_wanted` →
   Quarantine (`not_reply_enabled`).
4. **Sender**: bare From address (case-insensitive) must equal the delivery's
   `recipient_address`. Otherwise → Quarantine (`sender_mismatch`).
5. **DKIM**: the **first** `Authentication-Results` header of the message must
   have authserv-id equal to `authserv_id` and contain `dkim=pass` with
   `header.d` equal to, or a parent of, the From domain. Any other case →
   Quarantine (`dkim_fail`). (Subject to §0.)
6. **Duplicate**: reply Message-ID (or, when absent, SHA-256 of the headers)
   already stored → move to Processed.
7. **Accept**: fetch body, parse and store (§4), commit, move to Processed.

Moves use `UID MOVE` (fallback `COPY` + `\Deleted` + `EXPUNGE`). The commit
precedes the move; a crash in between ends in step 6 on the next poll.

Quarantined messages are recorded in `reply_quarantine` (received at, from,
subject, reason, Message-ID). Content is not stored and never shown to agents.

After processing the inbox, the poller runs the unclaimed check (§5).

## 4. Reply content (`notify/replies.py`)

- Text: the `text/plain` part; if only HTML, a plain-text conversion.
- Quote stripping: cut at the first reply-header line (`On … wrote:`, Dutch
  `Op … schreef …:`, Outlook `-----Original Message-----` / `From: … Sent: …`
  block), drop trailing `>`-quoted blocks, cut at the signature separator
  `-- `, trim. If nothing is left, keep the unstripped text, `stripped: false`.
- Max 8 000 characters after stripping; longer is cut, `truncated: true`.
- Attachments: non-inline parts with a filename plus inline images, up to 10
  files / 10 MiB total; the rest are listed in `dropped_attachments`.
  Parts are decoded one at a time and released, as with outgoing attachments.

Tables:

- `replies`: `id` (`rpl_…`), `conversation_id`, `notification_id`,
  `delivery_id`, `message_id`, `from_address`, `subject`, `received_at`,
  `text`, `stripped`, `truncated`, `dropped_attachments_json`, `state`
  (`waiting` | `claimed` | `unclaimed`), `claimed_at`, `claimed_by` (client
  id), `waiter_id`, `autoreply_notification_id`.
- `reply_attachments`: `reply_id`, `idx`, `filename`, `content_type`, `size`,
  `sha256`, `data`, `purged_at`; covered by the existing retention purge.

## 5. Listening, claiming, autoreply

**Claim**: `POST /v1/notifications/{id}/replies/claim?wait=N&waiter=W`
(N 0–55 s, default 50; W an opaque id from the caller, required). `read` scope;
caller must be the notification's client or `admin`. Acts on the notification's
conversation:

- Sets `listener_seen_at = now` on the conversation's root.
- If a reply in the conversation is `claimed` with `waiter_id = W`, return it
  again (retry after a lost response).
- Else, if a `waiting` reply exists, atomically mark the oldest `claimed`
  (`waiter_id = W`, `claimed_by`, `claimed_at`) and return it (200).
- Else wait up to N s on an in-process condition signalled when a reply is
  stored, then 204. (Valid because the service runs as one process; noted in
  the architecture doc.)
- Response: reply id, conversation id, notification id and title, from,
  received_at, text, flags, attachments as `{id, filename, content_type, size,
  url}` with `url` = `GET /v1/replies/{reply_id}/attachments/{idx}`
  (same access rule).

`GET /v1/notifications/{id}/replies` lists the conversation's replies with
state (no claim, no listener refresh). MCP `get_replies(notification_id)`.

**Listened to**: `now - listener_seen_at ≤ 2 min` on the conversation root.

**Unclaimed check** (each poll, §3): a `waiting` reply older than 10 min in a
conversation that is not listened to becomes `unclaimed`. A reply is never
swept while its conversation is listened to.

**Autoreply** on becoming `unclaimed`: the service submits a notification as
the system client: target `device:<the delivery's device>`, `kinds: ["email"]`,
`in_reply_to` = the reply's notification, `auto_submitted`, title
"Re: <original title>", message "This agent is no longer listening, so your
reply was not delivered. Received <time>." It is durable and retried like any
notification. At most one autoreply per conversation per hour; later unclaimed
replies in that hour are only recorded.

**States and events**: the first claimed reply moves the email delivery from
`dispatched` (or `delivered`) to `interacted` with
`interaction_json = {"reply_id": …}`. Email's `observable_states` include
`interacted` when the notification has `reply_wanted` and the path declares
`replies`. `interacted` stays final; later replies are stored, claimable and
evented without state change. New events `reply.received`, `reply.claimed`,
`reply.unclaimed`, `reply.quarantined` (no content), via SSE and webhooks.

## 6. `notify-wait` CLI

`scripts/notify-wait`, one stdlib-only Python 3 file.

```
notify-wait NOTIFICATION_ID [--timeout 60m] [--save-dir DIR] [--url URL] [--json]
```

- Base URL: `--url`, else `NOTIFY_URL`, else the `hnl-notify` MCP entry in
  `~/.claude.json` (strip `/mcp`). Token: `NOTIFY_TOKEN`, else that entry's
  `Authorization` header. The token is never printed or logged.
- Generates a waiter id once per run; loops claim calls (`wait=50`) until a
  reply or `--timeout`; network errors retried with short backoff inside the
  2-min listener window, reusing the same waiter id.
- Reply: downloads attachments into `--save-dir` (default
  `tempfile.mkdtemp(prefix="notify-reply-")`). Filenames are sanitised: base
  name only, control characters and path separators removed, no leading dot,
  empty → `attachment`, collisions get `-2`, `-3`. Prints and exits 0:

  ```
  Reply from alex@example.org (2026-10-14 15:02 CEST) to "Pick a header design" [rpl_…]:
  Go with B, but use the darker blue.
  Attachments: /tmp/notify-reply-x1y2/screenshot.png
  ```
  `--json` prints the claim response plus local attachment paths instead.
- Timeout: "No reply within 60 min; listener stopped. Later replies get an
  autoreply." exit 2. Errors (auth, 404, config) exit 1 with the reason.

## 7. MCP

- `send_notification` gains `reply` and `in_reply_to`; its description and the
  server instructions describe the pattern: send with `kinds` including
  `"email"` and `reply=true`, start `notify-wait <id>` as a background job, act
  on the reply when it finishes, run it again (or send a follow-up with
  `in_reply_to`) to continue; keep a waiter running during long work, or later
  replies are autoreplied. They state that reply text passed sender and DKIM
  checks and comes from the recipient.
- New tool `get_replies(notification_id)`.

## 8. Admin UI

- Email provider page: Reply mailbox section, Test button (with authserv-id
  hint), poller status (last success, last error).
- Notification detail: the conversation's replies with state, text,
  attachments and autoreply link.
- Quarantine list (reason, from, subject, time) on the email provider page.

## 9. Error handling

| Situation | Result |
|---|---|
| Mailbox unreachable / login fails | Backoff, status in admin UI; outgoing mail unaffected. |
| Crash after SMTP accept, before commit | Matching uses the delivery id in the Message-ID; reply still matches. |
| Crash between DB commit and IMAP move | Duplicate check on next poll; no double processing. |
| Claim response lost | `notify-wait` retries with the same waiter id and gets the same reply. |
| Reply to an older mail of the conversation | Matched to the conversation; the active waiter gets it. |
| Agent busy > 10 min with no waiter | Reply becomes `unclaimed`, autoreply sent. |
| Device address changed after sending | Sender is checked against the stored `recipient_address`. |
| Reply to a mail sent before 0.6.0 | `not_reply_enabled` → Quarantine. |

## 10. Migration and rollback

Schema additions (new columns, `replies`, `reply_attachments`,
`reply_quarantine`) are applied in place on start. Rolling back to 0.5.1
requires the pre-0.6.0 database backup, because 0.5.1 rejects the new email
provider settings.

## 11. Testing

- Unit: quote stripping (English/Dutch, Outlook, signatures, HTML-only);
  delivery id parsing from Message-IDs (other domains, malformed); automatic
  mail detection; sender rule against `recipient_address`;
  Authentication-Results (first header only, wrong authserv-id, forged header
  below and above the server's, parent domain); attachment caps.
- Service, with an injectable clock: claim order and atomicity, waiter-id
  re-delivery, access rule, conversation-wide listening and claiming,
  unclaimed after 10 min only without listener, autoreply as a system
  notification with `Auto-Submitted` and its hourly limit, transition to
  `interacted`, events.
- Poller against an in-process fake IMAP server: size refusal before fetch,
  move fallback, crash between commit and move.
- `notify-wait`: exit codes, token discovery, hostile filenames, retry with
  the same waiter id, against the dev server.
- Live end-to-end after deploy: agent sends `reply=true`, person replies with
  text and an image, waiter wakes; a reply to the older mail of a follow-up
  thread reaches the waiter; a reply after the waiter stopped gets the
  autoreply; a mail from another address lands in Quarantine.

## Out of scope

- Resuming or starting agent sessions for replies after the agent stopped.
- Replies on push paths.
- Claude Code channels as a delivery path (revisit when stable).
- In-process DKIM verification, unless §0 shows it is needed.
- Replies from anyone other than the delivery's recipient address.
