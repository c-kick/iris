# Email replies to agents — design

Date: 2026-10-14 · Status: revision 1 (brainstorm approved) ·
Target version: 0.6.0

## Intent

Agents (mostly Claude Code sessions on flax-core-next and flax-dev) email the
owner reports, previews and design choices through hnl-notify. The owner wants
to answer by pressing Reply in their normal mail client, and have exactly that
answer reach the agent that sent the mail, as input it acts on.

Scope is the "agent is still running" case: the agent waits in the background
for an answer. When no agent is listening any more, hnl-notify answers the
reply itself, so the owner always knows whether the reply landed.

Success criteria:

1. A sender can mark an email notification `reply: true`; the mail then carries
   a `Reply-To` of the reply mailbox and a footer saying replies reach the agent.
2. A reply from the recipient, sent from an address configured as one of their
   email devices with a passing DKIM signature, is delivered to a waiting agent
   as the new text only (no quoted history, no signature), with its attachments.
3. A waiting agent is woken by the reply without polling in its own context:
   a background `notify-wait` process exits with the reply on stdout.
4. A reply that no agent collects gets an autoreply in the same thread saying it
   was not delivered, and is never handed to an agent afterwards.
5. Mail that fails any check (unknown thread, wrong sender, no DKIM pass,
   automatic mail) never reaches an agent and never triggers an autoreply.
6. Notifications without `reply` behave exactly as before.

## Decisions

| Question | Decision |
|---|---|
| Agent lifecycle in scope | Agent still running and waiting. Resuming finished sessions is out of scope. |
| Reply after the agent stopped | Autoreply "not delivered" in the same thread; reply stored as `unclaimed`. |
| Inbound transport | IMAP over TLS (993) on the dedicated mailbox `replies@example.org` (the household mail server, IP-limited to the LAN). No inbound SMTP, no MX change. |
| Reply matching | `In-Reply-To` / `References` must name one of our Message-IDs (derived from the delivery id). |
| Sender authentication | From address equals an email device address of the person the notification went to **and** `Authentication-Results` shows `dkim=pass` for that domain. |
| Agent wake-up | Background CLI `notify-wait` long-polls a claim endpoint and exits on a reply (Claude Code re-invokes the session when a background job exits). Blocking MCP tool and Claude Code channels rejected (MCP HTTP timeouts; channels are research preview and unproven for a shared remote server). |
| Listener liveness | A notification has a listener while a claim poll happened in the last 2 min. |
| Grace for unlistened replies | 10 min from arrival; then `unclaimed` + autoreply. |
| Multiple replies | Stored in arrival order; each claim returns the oldest unclaimed one. |
| Reply attachments | Kept: up to 10 files / 10 MiB, same storage and 7-day purge as outgoing attachments. |
| Who may collect | The client that sent the notification, or an `admin` token. |

## 1. Configuration

Admin → Providers → Email gets a **Reply mailbox** section, stored in the
`settings` table like the SMTP settings:

- `imap_host`, `imap_port` (default 993), `imap_user`, `imap_password`
  (secret, AES-256-GCM like other provider secrets), `reply_address`
  (the `Reply-To` value, e.g. `replies@example.org`).
- Folders: `inbox` (default `INBOX`), `processed` (default `Processed`),
  `quarantine` (default `Quarantine`); created on first use if missing.
- TLS only (implicit TLS on the port), certificate verified with the system
  store unless the existing `verify_tls` email setting is off.
- A **Test** button: log in, select the inbox, check folders, log out.

Reply support is off until the mailbox is configured. `reply: true` with no
mailbox configured is a capability gap ("replies: reply mailbox not
configured"), the mail is still sent without `Reply-To`.

## 2. Sending side

New request fields on `POST /v1/notifications` and MCP `send_notification`:

- `reply: bool = false`. For email deliveries: adds `Reply-To: <reply_address>`
  and appends to both text and HTML parts: "Reply to this email to answer the
  agent." Push deliveries ignore it; if the routing produced no email delivery
  the response reports the gap "replies: no email delivery".
- `in_reply_to: str | null`. A notification id of the same client. The email
  sets `In-Reply-To` to that notification's email Message-ID (of the delivery to
  the same device) and `References` to its references chain plus that id, so
  mail clients thread the follow-up. Unknown id or another client's id → 422.
  Usable with or without `reply`.

Stored on `notifications`: `reply_wanted INTEGER`, `in_reply_to TEXT`.
The email delivery's `capability`/plan gains `replies` in its observable states
(see §5).

## 3. Inbound processing

New module `notify/inbound.py`, one asyncio task started with the other
workers when a reply mailbox is configured (and restarted on config change).
imaplib runs in a worker thread, as smtplib does in the email provider, with a
hard session deadline enforced the same way.

Loop: connect, `SELECT inbox`, process all messages, then `IDLE` (29 min
refresh); if the server lacks IDLE or IDLE fails, poll every 60 s. On
connection errors back off (10 s × 3ⁿ, cap 15 min) and log; the admin UI shows
the last error and last successful check.

Per message, in order. The first failing step decides the outcome:

1. **Size**: over 15 MiB → Quarantine (`too_large`).
2. **Automatic mail**: `Auto-Submitted` other than `no`, `Precedence:
   bulk|junk|list`, empty or `MAILER-DAEMON`/`postmaster` return path,
   `multipart/report` → Quarantine (`automatic`). Never autoreplied.
3. **Thread**: take Message-IDs from `In-Reply-To` then `References` (newest
   first); the first one matching an email delivery's `provider_message_id`
   selects the delivery and notification. None → Quarantine (`no_thread`).
   Matched notification without `reply_wanted` → Quarantine (`not_reply_enabled`).
4. **Sender**: the bare From address (case-insensitive) must equal the `to`
   address of an email device belonging to a person that notification was
   routed to (directly or via group/channel). Otherwise → Quarantine
   (`sender_mismatch`).
5. **DKIM**: the topmost `Authentication-Results` header whose authserv-id is
   the configured mail server (admin setting `authserv_id`, prefilled from the
   first header seen on Test) must contain `dkim=pass` with `header.d` equal to,
   or a parent of, the From domain. Otherwise → Quarantine (`dkim_fail`).
   Headers from other authserv-ids are ignored, so a forged header added by the
   sender cannot pass.
6. **Duplicate**: the reply's own Message-ID already stored → move to Processed,
   nothing else.
7. **Accept**: parse and store (§4), move to Processed.

Moves use `UID MOVE` (fallback `COPY` + `\Deleted` + `EXPUNGE`). The database
commit happens before the move; a crash between them leads to step 6 on the
next pass, never to a double reply.

Quarantined messages are recorded in a `reply_quarantine` table (id, received
at, from, subject, reason, Message-ID) for the admin UI. Their content is not
stored and never shown to agents.

## 4. Reply content

- Text: the `text/plain` part; if only HTML, a plain-text conversion of it.
- Quote stripping: cut at the first line matching a reply header
  (`On … wrote:` in English and Dutch `Op … schreef …:`, Outlook
  `-----Original Message-----` / `From: … Sent: …` block), drop trailing
  `>`-quoted blocks, cut at the signature separator `-- `. Trim whitespace.
  If stripping leaves nothing, keep the unstripped text and flag
  `stripped: false`.
- Max 8 000 characters after stripping; longer text is cut and flagged
  `truncated: true`.
- Attachments: every non-inline part with a filename, plus inline images,
  up to 10 files / 10 MiB in total; beyond that the rest is dropped and listed
  in `dropped_attachments`. Stored in the existing `attachments` storage with a
  `reply_id` owner; purged after the same retention (7 days).
- The raw message (RFC 822 bytes) is kept 7 days for debugging, then purged.

Table `replies`: `id` (`rpl_…`), `notification_id`, `delivery_id`,
`message_id`, `from_address`, `received_at`, `text`, `stripped`, `truncated`,
`dropped_attachments_json`, `state` (`waiting` | `claimed` | `unclaimed`),
`claimed_at`, `claimed_by` (client id), `autoreplied_at`, `raw` (BLOB, purged).

## 5. Listeners, claiming and the autoreply

**Claim endpoint**: `POST /v1/notifications/{id}/replies/claim?wait=N`
(N 0–55 s, default 50). Requires `read` scope and that the caller is the
notification's client (or `admin`). Behaviour:

- Records `listener_seen_at = now` on the notification (every call, including
  ones that return nothing).
- If a `waiting` reply exists, atomically marks the oldest `claimed` and returns
  it (200). Otherwise waits up to N s for one (woken by an in-process
  condition when inbound stores a reply), then 204.
- Response: reply id, notification id and title, from, received_at, text,
  flags, and attachments as `{id, filename, content_type, size, url}` where
  `url` is `GET /v1/replies/{reply_id}/attachments/{attachment_id}` (same access
  rule).

`GET /v1/notifications/{id}/replies` lists all replies with state (no claim,
no listener refresh); MCP `get_replies(notification_id)` wraps it. This is how
an agent recovers a reply whose claim response it lost.

**Listener rule**: a notification is *listened to* while
`now - listener_seen_at ≤ 2 min`.

**Unclaimed sweep** (existing expiry sweeper, every 30 s): a `waiting` reply
becomes `unclaimed` when it is older than 10 min **and** its notification is
not listened to. A reply arriving while a listener is active is never swept
while the listener stays active.

**Autoreply** on becoming `unclaimed`: one email via the configured SMTP
provider to the reply's From address, `Subject: Re: <original subject>`,
`In-Reply-To`/`References` set to the reply, `Auto-Submitted: auto-replied`,
`X-Notify-Notification` header, text: "This agent is no longer listening, so
your reply was not delivered. Received <time>." At most one autoreply per
notification per hour; further unclaimed replies within the hour are only
recorded. Autoreplies are sent directly (not as deliveries) and logged as
attempts on the original delivery; a failure is retried with the normal
backoff (8 attempts at most).

**States and events**: the first claimed reply moves the email delivery from
`dispatched` to `interacted` with `interaction_json = {"reply_id": …}`.
Email's observable states become `dispatched`, plus `interacted` when the
notification has `reply_wanted`. New events: `reply.received`,
`reply.claimed`, `reply.unclaimed`, `reply.quarantined` (without content),
delivered via SSE and webhooks like the others. `INTERACTED` is final in the
current state machine; later replies are still stored and claimable and emit
their events, but do not change delivery state.

## 6. `notify-wait` CLI

`scripts/notify-wait`, a single stdlib-only Python 3 file, installed (symlink
or copy) into `~/scripts/bin/` on flax-core-next and flax-dev, with a row in
`~/scripts/README.md`.

```
notify-wait NOTIFICATION_ID [--timeout 60m] [--save-dir DIR] [--url URL] [--json]
```

- Base URL: `--url`, else `NOTIFY_URL`, else the `hnl-notify` MCP entry in
  `~/.claude.json` (strip `/mcp`).
- Token: `NOTIFY_TOKEN`, else the `Authorization` header of that entry. Never
  printed or logged.
- Loops claim calls (`wait=50`) until a reply or `--timeout`. Network errors are
  retried with short backoff, staying inside the 2-min listener window.
- Reply: saves attachments to `--save-dir` (default a new
  `tempfile.mkdtemp(prefix="notify-reply-")`), prints, exits 0:

  ```
  Reply from alex@… (2026-10-14 15:02 CEST) to "Pick a header design" [rpl_…]:
  Go with B, but use the darker blue.
  Attachments: /tmp/notify-reply-x1y2/screenshot.png
  ```
  `--json` prints the claim response plus local attachment paths instead.
- Timeout: prints "No reply within 60 min; listener stopped. Later replies get
  an autoreply." and exits 2. Errors (auth, 404, config) exit 1 with the reason.

## 7. MCP

- `send_notification` gains `reply` and `in_reply_to`; its description and the
  server instructions describe the pattern: send with `kinds` including
  `"email"` and `reply=true`, start `notify-wait <id>` as a background Bash
  job, act on the reply when the job finishes, run it again (or send a
  follow-up with `in_reply_to`) to continue. They state that reply text passed
  sender and DKIM checks and comes from the recipient.
- New tool `get_replies(notification_id)` (non-blocking list).
- `~/documentation/notifications.md` gets the same usage notes for agents.

## 8. Admin UI

- Email provider page: Reply mailbox section, Test button, inbound status
  (last check, last error, IDLE or polling).
- Notification detail: replies with state, text, attachments, autoreply time.
- A Replies page: recent replies and the quarantine list (reason, from,
  subject, time).

## 9. Error handling summary

| Situation | Result |
|---|---|
| Mailbox unreachable / login fails | Backoff, status in admin UI; outgoing mail unaffected. |
| Claim response lost (crash, network) | Reply stays `claimed`; `get_replies` shows it. |
| Crash between DB commit and IMAP move | Duplicate check on next pass; no double processing. |
| Agent busy longer than 10 min between waits, no listener | Reply becomes `unclaimed`, autoreply sent. Agents keep a waiter running while busy if they expect slow work. |
| Autoreply SMTP failure | Logged as attempt; retried with the normal backoff. |
| Reply to a mail sent before 0.6.0 | `not_reply_enabled` → Quarantine. |

## 10. Testing

- Unit: quote stripping (Gmail EN/NL, Outlook, signatures, HTML-only),
  thread matching, automatic-mail detection, sender rule (direct, group,
  channel), Authentication-Results parsing (authserv-id, parent domain,
  forged extra header), attachment caps.
- Service: claim semantics (oldest first, atomic, access rule, listener
  refresh), unclaimed sweep timing with a fake clock, autoreply rate limit,
  state transition to `interacted`, events.
- Inbound task against an in-process fake IMAP server (IDLE and polling paths,
  move fallback, crash-before-move).
- `notify-wait`: exit codes, attachment saving, token discovery, against the
  dev server.
- Live end-to-end after deploy: agent sends `reply=true` to Gmail, owner
  replies with text and an image, waiter wakes; second reply after the waiter
  stopped receives the autoreply; a mail from another address lands in
  Quarantine.

## Out of scope

- Resuming or starting agent sessions for replies after the agent stopped.
- Replies to push notifications (Pushover cannot carry them).
- Claude Code channels as a delivery path (revisit when stable).
- Replies from people other than the notification's recipients.
