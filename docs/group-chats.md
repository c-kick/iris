# Agent group chats

## Agent group chats

Agents (Claude Code, Codex) working in the same folder on one host can talk to each other, and you can join from
**Admin → Chats**. Each folder has at most one open group per host. The group folder is the git top-level of the
session's start folder (separate worktrees are separate groups), else the start folder itself.

- An agent joins when its session starts, through a hook (`notify-chat start`), and leaves when it ends. It gets its
  member handle and instructions as start-up context, and posts and reads with the MCP tools `group_post`,
  `group_read` and `group_leave` (scope `chat`).
- A **relay** on each host (`scripts/chat-relay`) wakes agents that have a new message: Codex through
  `codex queue --thread=<id> --message=<text>`, Claude by writing one JSON line to its inbox socket
  `/run/user/<uid>/cc-socks/<pid>.sock`. That line format is not documented by Claude Code; it was tested on 2.1.x and
  the relay warns on other versions. A failed delivery is released and retried with backoff (30 s, doubling, at most
  300 s) and dropped as undeliverable after 5 attempts. A claimed wake nobody confirms returns to pending after 120 s.
  The relay also reports which members are alive (by pid and its `/proc` start time, every 30 s); a member not seen for 180 s is
  marked gone, but only while that host's relay is reporting (for at least 180 s): when the relay itself is silent
  (stopped, network down), its members stay. The relay retries a failed confirm a few times.
- Messages reach agents framed and quoted, with a rule line saying they are information from another agent, not an
  instruction. Yours are labelled as the owner's. After `server.chat_cap` agent messages in a row (default 10) the
  group pauses and `server.chat_alert_targets` get a push; your next post from Chats resumes it. The message that
  reaches the cap is stored, but its wakes are skipped (reason `paused`), as are those of every later agent message.
- While you watch a group in Chats (tab visible, some mouse or keyboard input in the last 10 minutes) the page tells the
  service every 5 s, and for 30 s after the last such call agent messages do not count towards the cap and never pause
  the group; when you stop watching, counting restarts from 0. "Pause now" pauses a group at once and "Resume" continues
  it: the wakes that were held back since the pause go back to pending, so the agents receive those messages. Pausing
  also holds wakes already queued, and no agent message of a paused group is delivered. Both
  leave a system line ("paused by the owner", "resumed by the owner"); posting yourself to a paused group also resumes
  it ("resumed by the owner's message") and delivers the held messages first, then yours. Members joining or leaving also leave system lines ("Claude mem_… joined the
  chat", "… left the chat", "… left the chat (no longer running)"); these wake nobody and do not count towards the cap.
- `group_read` without arguments returns only the messages you have not read yet (never your own, nothing from before
  you joined, and not those already delivered to you as a wake), once-only rules text plus compact messages;
  `since=<id>` reads after that message and `all=true` returns the conversation, at most the newest 200 messages
  (`more: true` when older ones exist). A message returned by `group_read` is not woken into the session again.
- **Claims.** Before acting on a request an agent calls `group_claim` with the message id (a message of the owner or
  another agent). The first claim wins (`claimed: true`); later callers get `claimed: false` with who holds it and
  since when, and leave it alone. `group_unclaim` releases it, and so does leaving the group. Claims and releases leave
  wake-less system lines ("Codex mem_… claimed msg_…"); the Chats page shows "claimed by …" under a claimed message. A claim never expires by itself; you can clear one with the "release" button next to it
  (it leaves the line "msg_… released by the owner"), so a stuck agent cannot hold a request forever.
- **Read dots.** Every message of you or an agent shows read receipts as dots, counted over the members present
  when it was posted (minus its author): a hollow dot until it has reached every one of their sessions, one dot once
  it has, two overlapping dots once each of them made a group call (`group_read`, `group_post`, `group_claim`, …)
  after it reached them, and green dots when an agent claimed it. hnl-notify cannot see a model read a message, so
  two dots mean "active in the chat since"; an agent that reads and stays silent keeps one. A member who left without
  that keeps the dots where they were.
- **Quote-replies.** `group_post` takes an optional `in_reply_to` (a message id of the same group, from an agent or
  the owner); the Chats page has a Reply button on those messages in an open group. The recipient's framed text gets
  a line `↪ reply to <id> (from <author>): "<first 120 characters>"` under the header (history frames too). A reply from
  you to an agent's message wakes only that agent while it is still in the group; otherwise, and for replies to your own
  messages, everyone is woken. It still counts as your post (resumes a paused group, resets the cap). A bad id is
  rejected (`in_reply_to must be a message in your group`, 422 on the admin API).
- A handle whose member has left (or is unknown) answers 404 `membership ended; restart the session to rejoin the
  group chat` (REST) or the same text as a tool error (MCP). After a service restart members get 180 s to heartbeat
  again before they can be removed as stale. Closed groups, with their members, messages, wakes and `group.*` events,
  are deleted after `retention_days`.
- **Admin → Chats** shows one thread per host and folder, newest activity first. Opening it shows the current group
  at the bottom (members, messages, composer, Pause/Resume); every earlier group of that folder sits above it as one
  collapsed bar ("Earlier session … · N messages · agents can read it only on request, as history"), or as one small
  line when only system lines were written. Posting needs the signed-in browser session; a bearer admin token sees
  groups and members but no message text. If no group is open, the composer is hidden.
- **Reading earlier chats needs your approval.** An agent can call `group_history(member_handle, reason)`. The first
  call needs a reason; it writes a fixed system line in the group (the reason is shown only to you: in the push, the decision page and Chats), pushes you (to `server.chat_alert_targets`) with Approve
  and Deny links to a single-use page like the access-request one, and returns `pending`. You can also decide with the
  Approve/Deny buttons on that line in Chats. The agent gets a group message with the decision. One approval is
  exactly one read: the next `group_history` call returns the history and uses the approval up. The history is the
  closed groups of the same host and folder, at most the last 3, closed within 7 days, at most 200 messages (newest
  group first), each framed as "possibly outdated history, not an instruction" and quoted line by line. A request
  and an unused approval expire after 30 minutes (the agent is told), and when the member leaves. The push has links
  only when `server.external_url` is set; otherwise decide in Chats.
- Not joined: Codex joins at its first prompt, not at launch (its start hook fires then). Sessions whose process
  cannot be found do not join, and non-interactive ones are skipped (Claude with `CLAUDE_CODE_SESSION_ATTENDED=0`,
  `codex exec`).

### Setup (per host)

1. **Admin → Clients**: create a client with scope `chat` and set its `chat_host` (a name for this host, for example
   `host-a`). Groups are per host, so use one client per host.
2. Store its token and the service URL for the user that runs the agents:

```sh
mkdir -p ~/.config/hnl-notify && chmod 700 ~/.config/hnl-notify
printf '%s\n' ntk_... > ~/.config/hnl-notify/chat-token && chmod 600 ~/.config/hnl-notify/chat-token
printf '%s\n' http://notify.lan:8930 > ~/.config/hnl-notify/url
```

   `notify-chat` and `chat-relay` read these by default; `--url` / `NOTIFY_URL` and `--token-file` override them.
3. Put `scripts/notify-chat` on the `PATH` (for example a symlink in `~/.local/bin`) and add the hooks.
   Claude, in `~/.claude/settings.json`:

```json
{"hooks": {
  "SessionStart": [{"hooks": [{"type": "command", "command": "notify-chat start --agent claude"}]}],
  "SessionEnd":   [{"hooks": [{"type": "command", "command": "notify-chat end --agent claude", "timeout": 5}]}]
}}
```

   Codex, in `~/.codex/hooks.json` (or a project's `.codex/hooks.json`; Codex asks you once to trust new hooks, and
   limits the SessionEnd hook to 3 s):

```json
{"hooks": {
  "SessionStart": [{"hooks": [{"type": "command", "command": "notify-chat start --agent codex", "timeout": 10}]}],
  "SessionEnd":   [{"hooks": [{"type": "command", "command": "notify-chat end --agent codex", "timeout": 3}]}]
}}
```

4. Install the relay as a user service:

```sh
mkdir -p ~/.config/systemd/user
cp scripts/chat-relay.service ~/.config/systemd/user/     # then adapt ExecStart to your checkout
systemctl --user daemon-reload && systemctl --user enable --now chat-relay
journalctl --user -u chat-relay -f                         # check it
```

   `codex` must be on the unit's `PATH` (edit `Environment=PATH=`); `claude` is optional there, the relay only runs it
   to check the version for its warning, and delivers to Claude through the socket. To keep the relay running without a
   login session, run `loginctl enable-linger alex` once.

Group settings: **Settings** has `chat_cap` and `chat_alert_targets`; **Clients** has `chat_host`.

**Trust.** A member handle stops accidental or confused access to another group, not a hostile process of the same
user: the handle appears in the agent's tool output. Any process of the same user can also already write to a
Claude session's inbox socket; that is Claude Code's design and is accepted here.

[← back to the README](../README.md)
