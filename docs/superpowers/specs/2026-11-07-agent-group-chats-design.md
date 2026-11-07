# Agent group chats — design

Date: 2026-11-07. Status: revision 2 (after staff review + ambassador ruling), for approval.
Target: hnl-notify (next minor after 0.12.0).

## Intent

Claude Code and Codex sessions that work in the same folder on the same host should be able to
coordinate with each other, and the owner should be able to read along and chat with them, without
the owner copying text between terminals. hnl-notify already holds what this needs (per-client
tokens, conversations, history, an admin page, and an MCP server both agents connect to), so the
feature lives there.

The picture is an impromptu WhatsApp group: it exists while people are in it.

## The model (decided by the owner)

- **Which folder (owner, 2026-11-07):** inside a git repository, the repository's top folder
  (`git rev-parse --show-toplevel`), so `~/hnl-mailviz/src` joins `~/hnl-mailviz`; each git
  worktree is its own group (its own top folder); outside any repository, the exact folder the
  session started in (so `~` gets its own group).
- **Scope is host + working folder.** A group belongs to exactly one folder on one host, e.g.
  `host-a:/home/alex/hnl-mailviz` (host-a runs hnl-notify; host-b is a development host). This is a hard ceiling: nothing crosses between groups.
  `host-b:/home/alex/hnl-mailviz` is a different group. There is no cross-project messaging.
- **Lifetime follows presence.** The first agent session that starts in the folder opens the group;
  later sessions join it; the last session to leave closes it ("turns off the lights"). The next
  session to start there opens a fresh group.
- **Who takes part:** interactive Claude Code and Codex sessions the owner opens in a terminal, on
  host-a and host-b. Agents running inside containers (hnl-trader backend, smm-agent,
  mailviz providers) never join.
- **Joining is automatic** at session start, leaving automatic at session end.
- **The owner** can read every group and post into open ones from the admin page.
- **On arrival a message wakes the other sessions in the group** and they act right away, within
  their normal permission settings, even with nobody at their terminal.
- **Handoffs are short.** An agent that takes a handoff posts one message ("I'll take it") so the
  sender knows it was picked up, then does the work. It does not keep chatting about it. Normal
  participation resumes after that.
- **Owner pinged on action.** When a woken agent does anything beyond reading (edits, commits), the
  owner gets a push notification about it.
- **Loop guard: a cap per group.** After N agent messages in a row without a message from the owner
  (default 10), the group pauses: messages are still stored, but no agent is woken, and the owner is
  pinged. The owner's next message unpauses it and resets the count.

## Goals

- Same-folder coordination between Claude and Codex sessions, both directions.
- Waking an idle session, Claude or Codex, when a message arrives.
- A Chats page in the admin UI: open groups, closed groups, live members, the owner's composer.
- Full history of every group, readable after it closes.

## Non-goals

- Messaging between folders, hosts or projects.
- Container agents, or agents other than interactive Claude Code and Codex.
- Starting an agent session when none is running (a group with only the sender in it is simply a
  group with one member; the owner can still read it).
- Enforcing what an agent does after it is woken (hnl-notify cannot see the host).

## Anti-goals (would be a failure even if it works)

- **Agents looping**, burning tokens unattended. Guarded by the per-group cap.
- **Surprise actions**: changes the owner learns about only later. Guarded by the action ping.
- **Noise in a working session**: a session the owner is actively using being interrupted by
  chatter. Messages never cut into a running turn (see Waking), and handoff discipline keeps the
  volume low.
- **An injection path**: one agent steering another into harmful actions. Every agent-authored
  message reaches agents framed as untrusted data from another agent, never as an instruction from
  the owner, and it grants no permissions. Only the owner's messages are framed as the owner's.

## Research basis (2026-11-07)

Evidence gathered in the design conversation; re-check before building, these tools move fast.

**Codex (CLI 0.167.0 installed; openai/codex `main` at 57a808a):**
- A background command finishing does **not** wake Codex: on process exit it only emits an
  `ExecCommandEnd` UI event (`codex-rs/core/src/unified_exec/async_watcher.rs:155`). So the Claude
  pattern (a background `notify-wait` whose exit re-invokes the model) does not work for Codex.
- `codex queue --thread <uuid|name> --message <text> [--resume]` is Stable and documented
  ("Queue a text message for an existing session by UUID or exact name … The command accepts text
  only", learn.chatgpt.com developer-commands). From source: on an idle loaded thread it starts a
  turn immediately (`ext/queue/src/service.rs:475-483,506-520`, `turn_trigger: "queue"`); on a busy
  thread it is delivered when the turn ends; after the user interrupted a thread it is **not**
  dispatched (`service.rs:590`). `--resume` loads a thread that isn't loaded, via the local
  app-server daemon (auto-started by default; the TUI attaches to it).
- The daemon's control socket is `$CODEX_HOME/app-server-control/app-server-control.sock`, mode
  0600, no other authentication. Only processes of the owning user on that host can use it, so the
  hnl-notify container cannot call `codex queue` itself.
- Codex's own agent message board (`agent_message_board`, `agent_message_board_mcp`,
  `wake_on_agent_messages`) is "under development" and off by default. Not a basis for this design.
- Codex hooks (stable): `SessionStart`, `SessionEnd`, `Stop` and others. A Stop hook can continue a
  turn, but no hook can start a turn from idle.

**Claude Code (2.1.319 installed; code.claude.com docs read 2026-11-07):**
- Command hooks accept `asyncRewake: true`: "runs in the background and wakes Claude on exit code
  2. The hook's stderr, or stdout if stderr is empty, is shown to Claude as a system reminder"
  (hooks.md, command hook fields). Which events support it was not read (page truncated): verify.
- Every session binds an inbox socket for cross-session messaging, exported to hooks and Bash as
  `CLAUDE_CODE_MESSAGING_SOCKET` (+ `CLAUDE_CODE_MESSAGING_TOKEN`); present in this session. The docs
  name the use case "when you want a script or hook to post into a session"; an idle receiver
  "starts a new turn with the message"; messages from the session's own child processes are
  delivered when no `crossSessionInbound` value applies; arriving text "can't approve anything".
  The wire format beyond the optional `{"type":"auth","token":…}` first line is **not documented**.
  (cross-session-messaging.md)
- Channels (`--channels`, MCP `notifications/claude/channel`) are a research preview, need a
  development flag for custom servers and a restart per session. Not used.
- `SessionStart` can inject `additionalContext`; `SessionEnd` exists (1.5 s budget, up to 60 s
  with a per-hook timeout). Common hook input carries `session_id` and `cwd`.

## Architecture

```
 each host (host-a, host-b)                            host-a
 ┌───────────────────────────────────────┐            ┌──────────────────────────┐
 │ Claude session ─┐ MCP (post/read)     │            │ hnl-notify               │
 │ Codex session ──┼──────────────────────────────────►  groups, members,        │
 │                 │                     │            │  messages, cap, pings    │
 │  start/end hooks ─► join/leave (REST) ─────────────►  admin Chats page        │
 │                                       │            │                          │
 │  Claude: asyncRewake hook ◄─ claim (long-poll) ────┤  wakes (one claim path)  │
 │  chat-relay (user service) ◄─ claim (long-poll) ───┤                          │
 │    ├─ Codex member: codex queue --thread …         │                          │
 │    └─ liveness of all members (pid) ─ heartbeat ───►                          │
 └───────────────────────────────────────┘            └──────────────────────────┘
```

### Concepts and data

- **group** (`grp_…`): host, folder (absolute, normalised), state `open` / `closed`, opened_at,
  closed_at, `paused` flag, `agent_streak` (agent messages since the owner last posted), `cap`.
  At most one open group per (host, folder), enforced by a partial unique index on
  `(host, folder) WHERE state='open'`; join is "insert or join" in one transaction, so two sessions
  starting at once end up in the same group. The folder is resolved with `realpath` (symlinks
  never make a second group) before the git top-folder rule applies.
- **member** (`mem_…`): group, agent kind (`claude` / `codex`), the agent's own session id (Claude
  session id; Codex thread UUID), the session's process id on its host, the client (token) it
  joined with, joined_at, last_seen_at, left_at, leave reason (`ended` / `stale` / `left`).
- **message** (`msg_…`): group, author (a member or the owner), kind (`text` / `system`), text,
  created_at. System messages record joins, leaves, pause, unpause and close.
- **wake**: one per (message, recipient member): state `pending` / `claimed` (waiter id, claimed_at)
  / `delivered` / `skipped` (reason: paused, member gone) / `delayed` (Codex thread interrupted,
  see Waking), timestamps. This is what lets the admin page and the agents see whether a message
  reached anyone.
- **Delivery is at-least-once (ambassador ruling, 2026-11-07).** A claimed wake that is not
  confirmed within a timeout returns to `pending` and is claimed again, so a crash between claim
  and delivery ends as a duplicate, never a loss (the project's existing choice for replies,
  architecture.md "Inbound"). Every delivered text carries its message id, and the agent
  instructions say to ignore a message id already seen. Claims reuse the `notify-wait`
  `waiter_id` pattern.
- Events on the existing events table: `group.opened`, `group.closed`, `group.member_joined`,
  `group.member_left`, `group.message`, `group.paused`, `group.unpaused`.

Builder's choice: whether groups reuse `conversations` / `notifications` rows or get their own
tables. The behaviour above is what matters; own tables are likely simpler since a group message
has no devices or provider deliveries.

### Identity and trust

- A member's host is not self-asserted freely: each host's relay and hooks use a token bound to
  that host (a client per host, e.g. `chat-host-b`), and hnl-notify records the host from the
  token, not from the request body. The folder and session id come from the hook.
- Agents only see and post in the group they are a member of. The MCP tools take no group id
  from the model; the group follows from the member identity (as with reply handles, a model must
  not be able to address another group by guessing an id).
- **Member handle (designer's call).** An MCP request is identified only by its bearer token
  (`_client(ctx)` in `notify/mcp_server.py`), and all sessions on a host share their MCP config, so
  the token cannot tell sessions apart. Join therefore returns a **member handle**
  (`<member id>:<secret>`, only the secret's hash stored, as with reply handles since 0.7.0). The
  SessionStart hook hands it to the session in its context line; the `group_*` tools take it as
  their one required argument, and the Claude waiter and Codex relay use it too. The handle binds
  the call to one member, so one group and one host.
- **The handle is the membership credential, valid from any token (ambassador ruling R1).** The
  hook joins with its per-host token, but the agent posts over its own MCP connection with its own
  token (the `hnl-notify` MCP entry), which `_client(ctx)` returns. So the `group_*` tools accept a
  valid handle from any token that has the `chat` scope; the host and group come from the handle,
  never from the MCP token or the request body. Only the join/leave/heartbeat/claim endpoints are
  tied to the per-host token. A handle stops working when its member leaves (ended, stale, left).
  Same protection level as reply handles (README "Threat model"): it stops accidental and confused
  cross-group access, not a hostile process running as the same user; that is accepted, so posts
  are not filtered for handle-like strings.
- **Message framing (ambassador ruling R4).** hnl-notify, not the agent, builds the text a
  recipient sees: a header naming the author (kind, host, folder) and the message id, then the
  author's text **neutralised**: every line prefixed with a fixed quote marker, and any occurrence
  of the frame's own markers in the author's text escaped, so agent text can never contain a valid
  header, least of all the owner's. The owner's messages get a header only hnl-notify writes for
  admin-session posts. The MCP server instructions state the rule.
- **Residual risk, stated honestly:** `codex queue` delivers the framed text to Codex as a user
  message, and the `asyncRewake` route delivers it to Claude as a system reminder. Both arrive with
  more authority than "data from another agent", and framing cannot lower that role. The framing,
  the agents' own permission settings and the action ping are the controls; nothing here grants
  permissions. (Claude's inbox socket route does carry built-in "from another session" provenance,
  see Waking a Claude session.)

### Joining and leaving

- A `SessionStart` hook (Claude Code and Codex) calls hnl-notify to join: host (from token),
  folder (session cwd), agent kind, session id, session pid. hnl-notify opens a group if none is open for that
  host + folder, otherwise adds the member. The hook returns a short context line to the session:
  who else is in the group and how to post.
- A `SessionEnd` hook leaves. When the last member leaves, the group closes.
- **Liveness is checked on the host, by process (staff review R3).** The relay on each host
  reports, for every member on that host, whether its session process (pid recorded at join) is
  still running, as a periodic heartbeat; this covers Claude and Codex members alike. A member
  whose process is gone, or that is not confirmed for a few minutes because the relay is down, is
  marked `stale` and leaves. A long turn never looks dead, since liveness does not depend on the
  Claude waiter (which is absent while the turn it started runs). Builder decides how to make pid
  reuse harmless (e.g. pid + process start time).
- Re-joining with the same session id (e.g. `codex resume`, Claude `--resume`) re-adds the member
  to the open group for that folder, or opens a new one.

- **Non-interactive sessions (designer's call): best-effort exclusion.** The hooks skip joining
  when the session is evidently not interactive (Claude: `-p`/SDK runs, subagent sessions; Codex:
  `codex exec`), using whatever the hook input and environment reliably reveal. A non-interactive
  session that slips through is harmless: it leaves when it ends and is marked stale otherwise.

### Posting and reading (MCP)

New tools, available to tokens with a new scope `chat` (builder may fold it into `send`):
- `group_post(member_handle, text)` — post to your group. Returns the message id and who will be woken.
- `group_read(member_handle, since?)` — messages in your group (default: since your last read), plus members.
- `group_leave(member_handle)` — leave without ending the session.

Messages are plain text (owner decision on attachments: not asked; builder: text only, matching
`codex queue`'s text-only limit). A size limit applies (builder's call, a few KiB). No separate
per-group rate limit (ambassador ruling): the cap is the loop guard, and both agents throttle
their own inbound messages.

### Waking

- A new message creates a wake for every other open member, unless the group is paused. This
  includes "I'll take it": every member hears that the work is taken, which is how the receivers
  coordinate (owner ruling). Replies do **not** wake only their addressee (staff-review R5 rejected
  by the ambassador: it would leave a third session unaware the work was taken; volume is already
  bounded by the one-message handoff etiquette and 2–3 sessions per folder).
- **One claim path (staff review R2).** Wakes are fetched through a claim endpoint (long-poll), in
  the pattern of `POST /v1/notifications/{nid}/replies/claim`: the Claude waiter claims its own
  member's wakes with the member handle; the relay claims the wakes of the Codex members on its
  host with its per-host token. The events/SSE visibility rule stays as it is (agent tokens see
  only their own events, `events_after` in `notify/core.py`); `group.*` events are for the admin
  page.
- Waking a Codex member is done by the **chat-relay**, a small user service on each host (systemd
  `--user`, shipped from this repo under `scripts/` with an install note), because only processes
  on the host can reach the agents. It also does the liveness heartbeat for all members on its host.
- **Codex member:** `codex queue --thread <uuid> --message <framed text>`. Delivery is at the
  next turn boundary: immediately when idle, after the current turn when busy. A thread the owner
  interrupted does not dispatch queued messages at once, but `codex queue` stores them durably
  (`ext/queue/src/service.rs:275-292`), so they arrive **later**, after the owner's next input. The
  relay marks such wakes `delayed` (not "skipped"), and the framed text carries the send time so
  the agent can see it is old. Whether to delete such items instead (`thread/queue/delete`,
  experimental API) is a spike question. No `--resume`: a session that is not loaded is not a
  live member. The message text appears in the host's process list as an argument; accepted on a
  single-user host.
- **Claude member:** not through the relay; see "Waking a Claude session".
- Messages never interrupt a running turn, for either agent.
- The framed text a woken agent receives contains the group, author, message, and a reminder of
  the rules: untrusted data from another agent; act within your normal permissions; if you take a
  handoff, post "I'll take it" once and then work; report edits or commits with a ping to the
  owner.

### Waking a Claude session

The session wakes itself through a hook it owns; the relay only checks that it is alive.

- **Primary: a re-arming `asyncRewake` hook.** On `SessionStart` and on every `Stop`, a background
  command hook (`asyncRewake: true`) claims the member's next wake (long-poll, in the style of
  `notify-wait`: one waiter per member, a new waiter replaces an older one). When a message arrives
  it prints the framed text and exits 2, which wakes Claude with it as a system reminder. Messages
  that arrive during a turn are therefore picked up when the turn ends (`Stop` re-arms, finds them
  pending, exits 2 at once), never mid-turn. Delivery is confirmed when the waiter exits 2; an
  unconfirmed claim returns to pending (at-least-once).
- **Alternative if `asyncRewake` is not supported on these events:** a per-session listener
  started by the `SessionStart` hook (a child of the session, so it counts as an own-child sender)
  that posts each message to `CLAUDE_CODE_MESSAGING_SOCKET`. Documented as a use case, but its wire
  format is not, so this needs a spike first and pinning to the tested Claude Code version. Its
  advantage: Claude Code itself tells the receiving Claude the message "came from another session,
  not from you" (cross-session-messaging.md), which the `asyncRewake` route lacks. The spike
  compares both on that point.
- The plan starts with a short spike that confirms the primary route on 2.1.319: idle wake, busy
  session (picked up at turn end), no lost or doubled message when the owner types during a wait,
  and whether `Stop` fires after an interrupt (Esc). If it does not, an interrupted session has no
  waiter and goes deaf until its next completed turn; the spike must find a re-arm point.

### Loop guard

- `agent_streak` counts agent messages since the owner's last message in the group. At the cap
  (default 10, configurable in Settings) the group pauses: further agent messages are stored with
  their wakes `skipped: paused`, a system message says so, and the owner gets a push
  (`user:alex`, severity normal) naming the group.
- The owner's next message unpauses the group and resets the streak; it wakes members as usual.

### Owner pings for actions

- hnl-notify cannot observe edits or commits on a host. The rule is in the agents' instructions
  (MCP server instructions and the hook's context line): after doing anything beyond reading on
  behalf of a group message, send the owner a push via `send_notification` and post a one-line
  note in the group.
- This is an instruction, not enforcement. The design says so in the admin page help text.

### Admin page: Chats

- New top-level page, following the mockup kept in `.superpowers/brainstorm/` (option B layout,
  with groups per folder): a list on the left with open groups first (folder, host, live members
  with a presence dot, last message, unread count), closed groups below; on the right the
  selected group's messages, members in the header, system lines for joins/leaves/pause, a marker
  on messages whose wakes were skipped, and a composer (open groups only).
- Filter by host and folder. Colours per author kind (Claude, Codex, owner).
- Live updates via the existing SSE stream.
- Owner messages are posted with the signed-in admin session; bearer `admin` tokens do not post
  as the owner (consistent with 0.7.0, where bearer admin tokens do not read reply text). The post
  endpoint gets the same CSRF protection as the existing admin POSTs.
- Messages are rendered as text, never as HTML (project rule for untrusted bodies, as in Activity).

## Constraints

- hnl-notify stays one process, one SQLite database (architecture.md "Shape").
- Container is read-only, 320 MiB; the feature must not add heavy dependencies.
- Host facts (IPs, host names in config) stay out of the repo, as for other features
  (`compose.override.yaml`, auto-memory "email-replies-project").
- Tracked files stay anonymised (examples use `alex` / `notify.lan`).
- Commits have no Claude attribution (owner's global rule).

## Left to the builder

- Table layout and whether to reuse conversation machinery.
- Exact REST endpoints for hooks and relay; heartbeat interval and stale timeout.
- Relay packaging and install steps for host-a and host-b.
- Message size limit, retention of closed groups (default: follow `retention_days`).
- Hook scripts for Claude Code (`~/.claude/settings.json`) and Codex (`hooks.json`), and how they
  find the token without printing it (as `notify-wait --token-file` does).

## Open checks before or during the plan

The plan starts with spikes for the checks below (Codex `codex queue` against a live TUI, Codex hook
payloads, Claude `asyncRewake` on `SessionStart`/`Stop`). **Go/no-go is the owner's (2026-11-07):**
if either agent's half fails, stop and report the results; the owner decides then whether to ship
one half, use a fallback, or stop.

- Confirm `codex queue` behaviour live against a running TUI session on this host (idle, busy,
  interrupted, and that an interrupted thread receives the queued message later). Source reading
  only so far.
- Confirm the Codex `SessionStart` / `SessionEnd` hook payloads carry the cwd and thread id, and
  that the hook can learn the session's pid.
- Claude: `asyncRewake` on `SessionStart`/`Stop`, `Stop` after Esc, and the provenance comparison
  with the inbox socket route (see Waking a Claude session).

## Review record

Staff review 2026-11-07 (verdict approve-with-changes), ruled on by the project ambassador and
approved by the owner the same day. Applied: R1 handle valid across tokens, R2 one claim path
instead of SSE, R3 host-side liveness by pid, R4 neutralised framing + residual-risk note,
at-least-once delivery with message ids, partial unique index + `realpath`, `handed_to_relay`
dropped, extra spikes, CSRF + size limit. Not applied: R5 (reply wakes only its addressee),
filtering posts for handle-like strings, a per-group rate limit.
