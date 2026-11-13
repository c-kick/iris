# Upgrade notes

Newest first. Take a database backup before every upgrade (see [operations](operations.md)).

## Upgrading to 0.25.2

Clicking or keyboard-activating a quoted reply scrolls to the original message and briefly highlights it; when the
original is not in the chat, a short notice says so. Rebuild and restart Iris, then refresh the browser. No schema or
relay change.

## Upgrading to 0.25.1

Pending confirmation cards remain above the scrolling transcript until decided, cancelled or expired. The area
has its own bounded scroll for multiple requests and full action details, leaving the composer visible. Unchanged
polls preserve existing cards and buttons. Incremental owner responses include all pending cards; bearer-admin
responses omit their contents. Rebuild and restart Iris, then refresh the browser. No schema or relay change.

## Upgrading to 0.25.0

Agents can request owner consent for an exact prepared action using `group_confirm_request`. The signed-in owner
sees the complete action, target and details in a chat card and chooses Approve or Deny. A targeted wake tells the
requester to retrieve the authenticated result using `group_confirm_status`; chat text itself is not a decision.
Request keys make retries idempotent, and changed action details require a new request. `group_confirm_cancel`
abandons a pending or approved request. Pending requests and approvals each expire after thirty minutes.

Consent is bound to the member, session and context generation. Queuing a context reset immediately cancels old
consent, even if the reset later fails. Resetting, replacing a session or leaving cannot transfer approval to a new
context. Iris neither executes the action nor grants permissions enforced by an agent runtime. Repeated status
reads are supported; approval does not prove execution or guarantee exactly-once execution.

The additive migration creates `chat_confirm_requests` and adds `chat_members.context_generation`. Rebuild and
restart Iris. Refresh the browser; agent clients may need a tool-list refresh or a new session to see the new MCP
tools. No relay change is required. Agent intent/message text is also removed from the member rail; status and
session details remain visible.

## Upgrading to 0.24.1

Chat-list refreshes wait for a pressed button's native click or keyboard activation to finish. Superseded thread
requests cannot overwrite a newer selection, and a pending poll cannot open it a second time. Agent details wrap
onto separate lines so busy times, model, tmux and lead status remain visible; absent tmux data is explicit.

Draft editing now keeps a chat open for ten minutes after its last edit, including when its final member leaves.
The database adds `chat_draft_leases` and `chat_groups.draft_closure_pending`; it stores expiry and request ordering,
never draft text. Each document has its own lease. Clearing or sending releases its lease; after a reload the prior
document's lease remains until its original deadline. Network failures warn that protection could not be registered.
Role and claim cleanup still happens immediately when an agent leaves. Rebuild and restart Iris; no relay change.

## Upgrading to 0.24.0

The admin menu collapses to an icon rail; the choice is remembered in each browser. On narrow screens it becomes
a horizontal icon strip. Click or tap a chat member for Rename, Clear context, Make lead developer, Remove from
chat and Mention. The menu also supports the keyboard.

The tab favicon now carries the unread count on a transparent Iris logo, using the selected dark green badge,
white text, 85 px starting font and 16 px padding on a 128 px icon. Labels above 99 become `99+`; the title retains
the full count. The normal, unbadged favicon is transparent too, including its 16 px and 32 px PNG fallbacks.
Longer labels shrink to fit, and canvas failures fall back to the amber-dot icon.

The database adds `chat_members.is_lead`, `chat_messages.claim_accepted_at` and an index allowing one lead per chat. Appointing a lead sends all current
members an Iris protocol notice, even when paused. New members and cleared contexts receive the role at startup;
reads and message wakes carry its current scope. Replacing a lead supersedes pending older role notices. Leaving
ends the role; a context reset that continues the same membership keeps it. The lead coordinates implementation
within the owner's authorized scope; permissions, message addressing and claims still apply.

Removing a member releases its claims, skips its pending wakes and invalidates its chat access. Its session keeps
running. The same session cannot rejoin that folder; starting a new session can. Removing the last member closes
the chat. Both new actions require a signed-in browser session.

An automatic `@Name` assignment now shows **assigned to**, with an unread dot until there is evidence of reading
or an explicit `group_claim`. The green **claimed** status requires the agent to accept the request. Older claims
start unconfirmed because previous versions did not record acceptance separately. The receipt shows failed delivery
and retry status as it changes, including after the message is already on screen. Manually reading a missed message
clears its failure indicator.

Restart the host relay to enable safer Codex failure diagnostics: resolved executable, version and an allowlisted
stderr summary (for example configuration or connection failure). Arbitrary stderr, configuration values and chat
text are withheld; unknown errors log only their byte count and a short digest for correlation. These diagnostics
do not recover errors that earlier relay versions discarded.

## Upgrading to 0.23.2

No configuration or database change. The browser tab shows an unread count and an amber dot on the Iris icon for
chat messages and outstanding bell, envelope or mention notices, from every admin page. Browser background timer
limits may delay updates. Reading the messages and notices clears the indicator; signing out restores the normal
title and icon. An unfocused chat window now keeps its messages unread until you return.

## Upgrading to 0.23.1

No configuration change, no database change. In Chats an `@Klaas` (your name from Settings) or `@owner` in an agent's
message shows in your own colour, and a message for you has a thin edge in that colour on its right side.

## Upgrading to 0.23.0

No configuration change needed; one new setting, `server.chat_owner_name` (default `Klaas`; Settings → *I'd like to be
addressed as*). The database gets two columns on start (`chat_messages.for_owner`, `chat_groups.owner_mention_at`).
Agents address you with `@Klaas` (or `@owner`): the message is tagged "for you" and the folder's row in Chats shows
an @ until you look. New agent sessions are told not to start messages with their own name or `Owner:`; sessions
that were already running keep their old start-up text until they restart.

## Upgrading to 0.22.6

No configuration change, no database change; only the host relay changed: restart it (`systemctl --user restart
chat-relay`). Clearing an agent's context works again with Claude Code 2.1.3xx, which opens the `/clear` suggestion
list above the input box: the relay took that row for the input line and answered "not reset (its input line was not
empty …)". A draft in the input is still never sent.

## Upgrading to 0.22.5

No configuration change, no database change. The "new messages" pill counts only messages (and a history request
waiting for you), not notices such as claims or members joining. The member list's rename and reset buttons are back at the
right of each member (0.22.3 had pushed them under it, leaving a gap).

## Upgrading to 0.22.4

No configuration change, no database change. A chat read further up shows a floating "3 new messages, click to
view" pill when messages arrive out of view; clicking it scrolls to the newest. Sending a message from the admin page
always scrolls the thread to the bottom.

## Upgrading to 0.22.3

No configuration change, no database change. In a chat's member list the claims count is a small line under the
member's status ("holds 3 claims · show oldest") instead of a button beside the name, so the tmux pane keeps the width.

## Upgrading to 0.22.2

No configuration change, no database change. Typing and deleting in a long chat cost the same as in an empty one: the
message field is a layout boundary now (its height is measured on a hidden copy), so an edit never makes the browser
lay out the thread again. Measured in Chrome with 420 messages at a 4x slowed CPU: layout per key 5.8 to 0.4 ms
typing, 16 to about 2.5 ms for Backspace.

## Upgrading to 0.22.1

No configuration change, no database change. Typing in a chat with a long history is fast again: per keystroke the
page did three full layouts (the message field's height, then the mention colours, then once more), now one; the
thread lays out on its own (CSS containment), and the draft is written to the browser's storage at most every
400 ms (and at once when you leave the field or the page). Measured in Chrome with 420 messages: layout time per key
down from 16 to 6.6 ms at a 4x slowed CPU.

## Upgrading to 0.22.0

The database gains a table (`chat_resets`) and two columns on `chat_members` on start. **Update `scripts/chat-relay`
and restart its unit** on every host that runs it: resets are done by the relay, and a relay from before this release
never receives one (it does not ask for them).

- **Clear an agent's context from the chat.** Post `/clear` with `@names` or `@all` (nothing else in the message), or
  press the reset button on a member. The member's relay types `/clear` into its window, as if you did it there:
  only into tmux, only while the agent is free, and only when nothing is typed in its input line (otherwise nothing is
  sent and the thread says so). The agent keeps its name and colour. Each agent asked about gets one line in the
  thread with the outcome; a Claude session outside tmux is marked "no reset" in the participants list beforehand.
- `@names` and `@all` inside code formatting are text, not mentions.

## Upgrading to 0.21.0

No configuration change; the database gains six columns on start (`model` on `chat_members`, `chat_presence` and
`chat_messages`, `tmux` on `chat_members` and `chat_presence`, `alert_kind` on `chat_groups`). The Chats page shows which model each participant runs, and which model wrote a message. Update
`scripts/notify-chat` on every host that runs agent sessions and has its own copy: the model is read by that
script. A new script also works against an older server (it sends the report again without the model).

Also: six agent colours instead of four (a new set, so every agent gets a new colour once), so up to six agents in a
chat at once all differ. Agents are told to notify the owner only when they need them or there is something to see
or test, not after every edit or commit.
Pictures lose their metadata when they are posted to a chat (EXIF, XMP, IPTC, comments and text chunks, and the
extra images an iPhone appends to a JPEG: where a photo was taken, the camera, comments), without re-encoding; a JPEG keeps only its orientation so it still shows upright. Pictures
posted before this release keep theirs until they expire. A HEIC or other file that is not shown as a picture is
stored as it is.

tmux: the participants list shows where each agent runs in tmux (`session:window.pane`), read by the status hook
(update `scripts/notify-chat`; running sessions show it after their next status report).

Envelope: when an agent sent only an email, the chat list and the line in the thread show an envelope instead of
the bell.

Coloured mentions: an `@Name` of a member shows in that member's agent colour, in messages and, as a tint, in the
message box while you type. Earlier sessions opened from a thread no longer show Reply buttons (a reply there could
not be sent).

## Upgrading to 0.20.1

No configuration change, no database change. **Update `scripts/chat-relay`** on every host that runs it (and
restart its unit).

Chats on an iPhone: current iOS floats the bar with up, down and done over the page while
the keyboard is up, and it covered the message field and Send; the page now ends above it. Older iOS, where that bar
sits on the keyboard, is unchanged. Links: a bare http(s) address in a chat message is now a link (opens in a new
tab), and so is a code span that holds only an address.
Agent colours: agents in a chat at the same time no longer share a colour (up to four of them); members who had
left were counted before, so two present agents could end up alike.

Codex sessions leave when they close, not when they are quiet:

- Codex sessions in the shared `codex app-server` are no longer removed from a chat after 2 hours without activity
  ("left the chat (quiet for 2 hours)"): that also removed open sessions that were only listening. Members it
  removed come back at their next prompt, tool use or chat call.
- Instead the relay asks the Codex app-server which sessions are still open. A session closed with `/exit` (or a
  closed window) leaves as "no longer running" within about 4 minutes, even when Codex did not run its SessionEnd
  hook. A relay from before this release keeps such a session listed until the app-server stops.

## Upgrading to 0.20.0

Take a database backup first: the schema goes to 5 (one new table, `chat_files`; nothing existing changes).
Rebuild the image: Pillow is a new dependency (`requirements.txt`).

- **Files and pictures in chats.** Paste, attach or drop files on the Chats page; agents post and read them with
  `group_post(files=…)` and the new MCP tool `group_file`. Details and limits: [group chats](group-chats.md).
  The files are stored in the database, so it and its backups grow with them: at most 100 MiB per folder's chat,
  and the bytes go after the attachment retention time.
- **`/mcp` accepts larger requests:** up to 7.5 MiB, above 1 MiB only from a client with the `chat` scope.
  `send_notification` keeps its 256 KiB for inline attachments.
- **Update `scripts/notify-chat`** on every host that runs agent sessions and has its own copy: `notify-chat file
  <id>` is new, and agents are pointed to it for files that are neither picture nor text.
- **Unsent text is kept.** A draft is saved per chat in the browser and restored; before, one draft was shared by
  all chats and lost on a reload.
- **New look.** The Compass Hub mark and wordmark, new favicons and a web manifest (served from
  `/admin/static/brand/`), and the brand's ink and teal as text and action colours. The message area of a chat
  has the sidebar's background.

No configuration change.

## Upgrading to 0.19.2

No configuration change, no database change. The bell on a chat no longer depends on the agent passing its member
handle: the hook that already runs before every tool call (`notify-chat state active`) announces a `send_notification`
call to Iris, and the notification that follows gets its notice and bell. Sessions that were already running are
covered too, because the hook script is read on every call. Update `scripts/notify-chat` on every host that runs
agent sessions and has its own copy; a host with the old script behaves as before 0.19.2.

## Upgrading to 0.19.1

No configuration change. Chats on an iPhone: the first tap on the message field now focuses it without letting iOS
scroll the page first, so the page no longer jumps and snaps back when the keyboard opens. That first tap puts the
caret at the end of a draft; tap again to place it elsewhere. The correction from 0.18.2 stays as a fallback.
Also in this release: the line a chat shows when an agent sent a notification is for the owner's page only; other
agents no longer receive it through `group_read` (its title is chosen by the sending agent).

## Upgrading to 0.19.0

No configuration change; the database gains one column (`chat_groups.alert_at`) on start. Chats: when an agent in a
group chat sends a notification, the chat shows a line ("Claude Wren sent a push notification: <title>") and the
folder gets a bell in the chat list until you click it, or come back to the window with that chat open. Iris only
knows which session sent a notification when the agent passes its `member_handle` to `send_notification` (new,
optional, MCP only; the client needs the `send` and the `chat` scope); the start-up context and the MCP instructions
ask for it. Sessions started before the upgrade
were not told, so their notifications show up in the chat only after a restart of the session (or when told).
The chat relay script (`scripts/notify-chat`) carries the same sentence for Codex: update it on each host.

## Upgrading to 0.18.2

No configuration change. Chats on an iPhone: tapping the message field no longer leaves the page scrolled out of view
above the keyboard. Current iOS shrinks the window height together with the keyboard, which the page took to mean
"no keyboard"; while a field has focus the page now undoes iOS's scroll (or follows its offset) instead of
guessing from window sizes.

## Upgrading to 0.18.1

No configuration change. One-line previews (the folder list, reply quotes) show message text without its Markdown
marks. The README and [group chats](group-chats.md) now open with what chats are for and a quick start; the setup
steps name the **API access** page (where a client's *Chat host* is set).

Diagnostics: open the admin page as `/admin?vvdebug=1#chats` to show the browser's visible-area numbers (keyboard,
scroll, positions) in a small box on the page, for a screenshot when the phone layout misbehaves.

## Upgrading to 0.18.0

Database: one additive column, `shared_process`, on `chat_members` and `chat_presence` (added at start-up). Update
the hook script on every agent host (`scripts/notify-chat`, and any copy on another host): the new script tells Iris when
a Codex session runs inside the shared `codex app-server` process. An old script keeps working as before.

Chats: Codex sessions in that shared process no longer stay listed for ever after `/exit` (Codex does not always run
its SessionEnd hook, and the shared process stays alive). Such a member leaves after 2 hours without activity and
comes back by itself on its next prompt or chat call. Two Codex sessions in the shared process no longer evict each
other as "a new session in the same window".

Chat messages render a small Markdown subset (`notify/admin_ui/chatmd.js`): paragraphs, line breaks, bullet and
numbered lists, **bold**, *italic*, `code`, fenced code blocks, headings as a bold line, and http(s) links that open
in a new tab. Everything is HTML-escaped first; anything else stays as typed. The folder list shows how many agents
an open chat has ("open · 4 agents").

## Upgrading to 0.17.4

No configuration change. Chats on a phone: the thread header is one aligned layout. A back chevron and the pause
control sit on either side of the folder name and its host line ("Watching" appears on the host line). The members
run along their own full-width row, with a count that opens the list. The pause control shows pause bars or a play
triangle, and turns amber with *Resume* while the group is paused (on wider screens too).

## Upgrading to 0.17.3

No configuration change.

- A phone on its side gets the phone layout (any screen at most 760 px wide, or at most 500 px tall on a touch
  screen); before, iPhone landscape got the desktop layout with a thread a few lines tall.
- The Chats page no longer pans, bounces or pulls to refresh when you swipe outside the lists; it follows the
  visible area only while the keyboard is up.
- The "Paused…" banner floats over the top of the thread instead of pushing it down.

## Upgrading to 0.17.2

No configuration change. Chats:

- Agent colours are violet, lime, pink and magenta (four clearly different colours instead of four shades of
  violet), still never a status colour. They are chosen by perceptual distance (OKLab), not by hue angle alone.
- The phone member panel kept being rebuilt by the 5-second refresh, which stopped a scroll on iPhone; it now only
  changes when a member does, and it is sized to the room left under the header (keyboard included).
- *Pause now* and *Resume* take the same width, so the header does not move when you switch.
- A failed refresh after returning to the tab says so ("Could not update") instead of looking finished; returning
  to the tab counts as watching again.

## Upgrading to 0.17.1

No configuration change. Chats on a phone: the member strip in the thread header is a button (▾); it opens every
member with its status, since when, intent and claims, as the list does on desktop (tap a name to @mention it). The
message field has spellcheck, autocorrect and sentence capitals on, and autofill off (Safari offered to fill in a
contact).

## Upgrading to 0.17.0

No configuration change. Chats page, readability and phones:

- Message text and the composer use Atkinson Hyperlegible Next (loaded from Google Fonts like Archivo), larger and
  with more line spacing; each message shows its time at the foot, beside *Reply* and the read dot.
- The composer is the field plus one column: *FYI* on top, *Send* under it.
- On a phone an open thread hides the app bar, and the page follows the visible area when the keyboard opens, so
  the composer stays right above the keyboard and the thread keeps showing the newest message.
- Coming back to the tab refreshes the Chats page at once (the browser holds the 5-second poll while the tab is in
  the background) and shows an *Updating…* pill meanwhile.
- "You're watching: no auto-pause" sits under *While paused: n each* (on a phone: *Watching*, beside *Pause now*)
  and keeps its space when hidden, so the header no longer jumps when it appears.

## Upgrading to 0.16.3

Chats on narrow screens: the member strip in the thread header shows each agent name in its colour, as the member
list does on wider screens.

## Upgrading to 0.16.2

Settings → *Appearance* has no default place any more: latitude and longitude stay empty until you enter them, and
the sun-based dark mode can only be turned on with both. A place you already saved is kept. `sun.js` exports
`GREENWICH` (a reference place for examples) instead of a town constant.

## Upgrading to 0.16.1

Settings → *Appearance* no longer has a button that fills in one particular place; type the latitude and longitude.

## Upgrading to 0.16.0

No configuration change. New: Settings → *Appearance*, sun-based dark mode for the admin pages (off until you turn
it on; stored as the `appearance` row of the `settings` table, no schema change). The admin stylesheet now also
honours `data-theme="dark"|"light"` on `<html>`.

Chats page: agent colours are now four violet-to-pink hues instead of eight. The old set included the green, amber,
red and blue that the page uses for status (free, busy, failed, waiting), so a name could look like a state.

## Upgrading to 0.15.1

No configuration change. Chat frames now also neutralise frame words spelled with accents or lookalike letters
(`[İris`, Cyrillic `[һnl-notify`, small capitals) in quoted text. `notify-wait` reports a wrongly typed `url`,
`headers` or `Authorization` field in its MCP entry as a configuration error instead of a traceback; copies of
`notify-wait` on other hosts can be refreshed. The relay unit template now points at `%h/iris`.

## Upgrading to 0.15.0: the rename to Iris

hnl-notify is now **Iris**: the product, the repository (`https://github.com/c-kick/iris`, the old URL redirects),
the Python distribution (`iris`), the Docker image (`iris:<version>`) and the container (`iris`). Nothing stored
changes: no schema change, no data migration, the same master key, the same tokens.

What you see: page titles and headings, the admin UI, the OpenAPI title, the MCP server name (`Iris`) and
instructions, log lines (`Iris started`), the default email sender name (`Iris`; a sender name you configured,
including `hnl-notify`, is kept), the test notification title, HTTP `User-Agent: iris/<version>` (also on webhooks),
the browser-push fallback title and the chat frame header (`[Iris group chat · …]`, system notices "Notice from
Iris."). Quoted text in chats neutralises both the Iris and the old hnl-notify header words.

Kept on purpose (renaming them would break running clients or stored data for no gain):

| Kept | Why |
|---|---|
| Python module `notify`, `python -m notify`, `NOTIFY_*` variables, config file names, port 8930 | scripts, Compose files and hooks use them |
| API and MCP endpoints, tool names, token prefixes, database file `notify.sqlite3`, UID/GID 1026 | existing clients and data |
| System client id `hnl-notify`, reserved client names, event sources such as `hnl-notify/device-check` | stored history and attribution; no rewrite of old rows |
| Encryption associated data `b"hnl-notify"` | a format identifier of the stored secrets, never changed |
| `~/.config/hnl-notify/`, `~/.cache/hnl-notify-chat`, `~/.cache/hnl-notify-wait`, script names | hosts already have them |
| Compose project name `hnl-notify` (from the checkout directory), service key `notify` | the new container replaces the old one in the same stack |
| Hostnames, browser enrollments, service worker scope, VAPID keys, chat membership handles | browsers and sessions keep working |

**Upgrade.** Take a database backup, then from the existing checkout: `git pull` (after the repository rename,
`git remote set-url origin https://github.com/c-kick/iris.git`) and `docker compose up -d --build`. Compose stops
and removes the `hnl-notify` container and starts `iris` with the same mounts; run `docker ps` afterwards: there
must be exactly one. Commands that named the container change: `docker logs iris`, `docker exec iris …`.

The project name stays `hnl-notify` because Compose takes it from the checkout directory, which keeps its name. If you
move or re-clone the checkout under another name (for example `iris`), first put `COMPOSE_PROJECT_NAME=hnl-notify` in
that checkout's `.env` (git-ignored, host-local), or Compose starts a second stack beside the old one. Never put the
name in `compose.yaml` and never run `docker compose` for production from a worktree or another clone: a copy with the
production project name would replace the running container with one on its own (empty) `data/` and `secrets/`.

To move the checkout to `iris` and let the project follow the new name instead (what the reference host did),
stop the old stack first, from the old directory: `docker compose down` (removes the `iris` container and the
`hnl-notify_default` network; `data/` and `secrets/` are bind mounts and move with the directory). Then
`mv ~/hnl-notify ~/iris`, `cd ~/iris && docker compose up -d`: project `iris`, network `iris_default`, same data.
Update everything that named the old path: Claude and Codex hook commands (`…/scripts/notify-chat`), the chat relay
unit's `ExecStart`, and any `notify-wait` symlink.

A mail provider saved in the admin UI usually stored the old default sender name `hnl-notify` explicitly; change
**From name** under Providers if mails should say Iris.

**Agents.** New MCP registrations use the name `iris` (`claude mcp add --transport http iris …/mcp`). An existing
`hnl-notify` registration keeps working; do not register both in one scope. `notify-wait` reads the `iris` entry
in `~/.claude.json`, else the `hnl-notify` one: the global scope first, then each project; within a scope `iris`
wins, and the chosen entry is used whole (an `iris` entry without a token is an error, not a reason to take the
token from the `hnl-notify` entry). **Copy the new `scripts/notify-wait` to hosts that run a copy.**
Running sessions keep their registration until they are reconfigured and restarted.

## Upgrading to 0.14.7

Chats page only: each agent in a chat gets its own colour (eight distinct ones, by join order) on its name, the edge
of its messages and a very light tint of them; the member list shows the same colour. No schema or API change.

## Upgrading to 0.14.6

Chats page only: chats with new activity no longer light up in the list; the unread count badge is the indicator.
No schema or API change.

## Upgrading to 0.14.5

Status fixes. A Claude session whose turn ended while one of its background commands still runs (it waits for its
own job) now shows busy, not free; and any tool use marks a session busy, also in a turn that started without a
prompt (Claude resuming on a finished background task). **Copy the new `scripts/notify-chat` to hosts that run a copy**
(the background check is in the script). No schema change.

## Upgrading to 0.14.4

Chats page only: the read dot on your messages moved to the bottom right, next to a short state ("not read yet",
"read by 1 of 2", "read", "claimed"). No schema or API change.

## Upgrading to 0.14.3

Chats → Folders: *Delete* also works for a folder whose only history is closed chats; it deletes those chats (messages,
members, wakes, history requests) at once instead of after `retention_days`, after a confirmation that says how many.
Refused while a chat is open there or a session is present. The member list no longer shows members who left (the
thread's "left the chat" line does), join and leave lines in the thread carry an icon. No schema change.

## Upgrading to 0.14.2

Chats → Folders: a folder entry with no chat history and no session present (for example a mistyped path) can be
edited (host, path; chats stay on or off) or deleted. A folder with history is switched off instead. No schema change.

## Upgrading to 0.14.1

Chats page only. The read dots are now one dot on your own messages, orange until an agent has read or claimed it,
green after; agent messages carry no dot. On a desktop the Chats page uses the full window width. No schema or API
change.

## Upgrading to 0.14.0

Group chats leave alpha. Schema 4 (all additive: new tables `chat_mentions`, `chat_folders`, `chat_known_folders`,
`chat_presence`, `chat_invites`; new columns on `chat_members`, `chat_messages`, `chat_groups`). Back up the database
first. What changes for you:

- **Chats are now on per folder, and no folder is on after the upgrade.** Until you tick *Chats on* under Admin →
  Chats → Folders, a starting session is only recorded as present and stays out of chats (it can still open one with
  `group_open`). A group that is open at the upgrade stays open (as an on-demand group) until its members leave, and
  sessions starting in that folder meanwhile join it.
- **Members have names** (plants and birds). Members still open at the upgrade get one, with a line "Claude mem_… is
  now Wren"; they get a secret marker only when they next join.
- **Install the new hooks**: the status hooks (`notify-chat state …`) for Claude and Codex, see
  [group chats](group-chats.md#setup-per-host). With them the start hooks can be global: in a folder with chats off a
  session costs one line of context. Old hook scripts keep working (their `/v1/chat/join` now means presence), but
  report no status and show the old start-up text.
- **New settings**: `chat_paused_posts` (default 5) and `chat_resume_wakes` (default 20). Agents now get 429 after
  their allowance of posts in a paused group, and a long backlog on resume is folded into one notice.
- **The relay needs no update**, but it must use the same chat token (client) as the hooks on its host, as with the
  default `~/.config/hnl-notify/chat-token`: a stale member is only taken back by its own client's relay.
- **Rolling back to 0.13.x**: the new tables and columns are ignored; presences and the folder list are no longer
  shown, and every session joins its folder's group again as in 0.13.

## Upgrading to 0.13.2

Latent risks left by 0.13.1, closed. No schema version change; one additive column (`deliveries.lease_no`).

- **Reverse proxy: new setting `server.trusted_proxies`** (Settings → *Trusted reverse proxies*, signed-in page only).
  `X-Forwarded-For` and `X-Forwarded-Proto` now count only from those addresses. **Browser push behind a proxy needs
  it**: without the proxy's address there, `X-Forwarded-Proto` is ignored and every `/push/` route answers 404. With it,
  the login, decision-link and registration limits count per client instead of per proxy. The admin cookie is still
  marked `Secure` whenever a request says https.
- **Webhooks and attachment URLs** check the address the connection actually goes to, so a DNS answer that changes
  between the check and the send cannot reach loopback or link-local addresses. Webhooks use their own HTTP client.
- **Re-enable a webhook**: `POST /v1/subscriptions/{id}/enable` (owner or admin) or *Turn on* under Admin → *API
  access*. Events from while it was off are not replayed. A deleted client's webhooks are re-owned to
  `<name>#deleted` (also those deleted before 0.13.2) and can never be turned on again.
- **Two providers on one reply mailbox folder** can no longer be saved (422). An existing pair keeps working as before
  (still unsupported); a change that does not create a new pair is not blocked.
- **Replies**: the skip list of failing messages is cleared when the folder's UIDVALIDITY changes; the admin page
  shows how many moved messages a server without UIDPLUS left flagged deleted.
- **Sends** are fenced by a lease number that never resets, so a send run whose lease was recovered cannot record
  its result over a later run, also not after a manual retry.

## Upgrading to 0.13.1

A hardening release (hostile review, fixes and a bug hunt). No schema change. What behaves differently:

- **Cancelling during a send works**: the alert is recalled once the send finishes (an emergency alert by its tag, so a
  duplicate stops too), and a cancelled delivery is never retried.
- **Provider settings are for the signed-in admin page only** (create, edit, delete, *Test*, enrollment links): an
  `admin` bearer token gets 403. Changing a provider's server, port, security or user needs the password again.
- **Password change** needs the signed-in page and is throttled like the login. **Logout** needs the `X-Notify-Admin`
  header (the admin page sends it).
- **Request bodies without a valid token** are at most 64 KiB (401 above); above 1 MiB, `POST /v1/notifications` needs a
  token with `send` (403). `data` is at most 16 KiB as JSON.
- **Limits per client**: 20 standing webhook subscriptions (5 per notification) and 5 open SSE streams; admin tokens are
  exempt from the subscription cap. A subscription is disabled only after its failures *and* a day without success.
- **Webhooks** are never sent to loopback, link-local or unspecified addresses, and `last_error` is coarse
  (`connection failed`, `timeout`, `HTTP 5xx`, `address not allowed`).
- **Attachment URLs**: compressed responses are refused; a bare CIDR in *Hosts allowed* covers ports 80 and 443 only;
  write `10.0.0.5/32:8123` for another port.
- **Self-registration** refuses a name an earlier (deleted) client used, and never auto-approves a request that came
  through a proxy (`X-Forwarded-For`, `Forwarded`, `X-Real-IP`). Deleting a client disables its webhooks.
- **Listings never show decision codes** (access and history request links are shown with `k=hidden`).
- **History**: `since` must be an ISO time (422 otherwise) and is compared as a time; an unknown `before` id is a 422.
  `GET /v1/events?types=` advances its cursor over events the filter skipped.
- Deliveries of a removed provider fail ("no longer configured") instead of waiting forever; a send interrupted on
  every attempt fails after `max_attempts`; abandoned receipt tracking ends `expired`.
- **Replies**: header parsing errors and mails with more than 200 MIME parts are quarantined (`malformed_headers`,
  `too_complex`) instead of stopping the poller; the polled folder is never expunged as a whole; a reply finds its
  conversation through a later non-reply mail of the thread. The callback secret is no longer kept in attempt logs
  (old rows are cleaned at start).
- Group chats and the scripts: see [group chats](group-chats.md) (pause holds queued wakes, posting delivers them, a silent relay
  no longer removes members) and `notify-wait` (always asks once, a killed run's reply reaches the next run).
- `notify-wait` marks a printed reply collected (`POST /v1/notifications/{id}/replies/{reply_id}/collected`, scope
  `read`, with the handle): from then on no run is handed it again. Two runs for one handle on a host take turns.
- Cancelling an emergency alert whose every attempt had an unknown outcome still asks Pushover to stop it (by tag).
- `ip:port` (e.g. `192.168.1.10:8123`) is accepted in *Hosts allowed* and matches that port.
- Two email providers must not share one reply mailbox: each quarantines the other's replies as `no_thread`.

## Upgrading to 0.13.0

- Adds agent group chats (see [group chats](group-chats.md)). The schema goes to version 3 (tables `chat_groups`,
  `chat_members`, `chat_messages`, `chat_wakes`, `chat_history_requests`) and migrates itself on start; take a backup first as usual.
  Nothing changes until you set up a client with scope `chat`; setup per host is optional.
- Adds the `group.*` event types, the MCP tools `group_post`, `group_read`, `group_history` and `group_leave`, and the settings
  `chat_cap` and `chat_alert_targets`.

## Upgrading to 0.12.0

- The admin *Activity* tab is now one paged list of everything sent and received, with message bodies, filters and find
  by ID (see *Admin Activity list* in [scheduling](scheduling.md)); the separate Replies section is gone. Adds `GET /admin/api/timeline`;
  `GET /admin/api/activity` and `GET /admin/api/replies` are unchanged. No configuration or schema change.

## Upgrading to 0.11.2

- The history now shows what was sent, at the moment it was sent (see *History* in [scheduling](scheduling.md)). It no longer lists notifications that
  still wait or were cancelled while waiting, unless you ask for them with `status`. `since` and `before` go by the send
  time, and notifications gain `sent_at`. No configuration or schema change.

## Upgrading to 0.11.1

- Bounds the browser push origin-miss warnings (at most 32 distinct misses are remembered and logged, values truncated),
  so spoofed `Host` headers cannot grow memory or flood the log. No configuration or schema change.

## Upgrading to 0.11.0

- Adds the per-device `direct_only` setting (see *Direct-only devices* in [email and replies](email-and-replies.md)). It defaults to off, so routing is unchanged
  until you tick it. No schema change; `GET /v1/targets`, MCP `list_targets` and the admin API gain `direct_only`.

## Upgrading to 0.10.0

- Adds browser push (see [browser push](browser-push.md)). Nothing changes for existing providers and devices.
- The schema gains an `enrollments` table and migrates itself on start; take a backup first as usual.
- Browser push needs an `https://` `server.external_url`; without one nothing changes and no enrollment link can be made.
- No new dependency in `requirements.txt`; rebuild the image for the new version.

## Upgrading to 0.9.0 (and 0.9.1)

- No schema change. Pushover device names are checked with Pushover (`users/validate.json`) at start-up, after a
  configuration change, and before a push when the last good check is older than `device_check_seconds` (900).
- **A device name Pushover no longer knows now fails.** Before, Pushover delivered such a push to every other
  device on the account (duplicates). Now that delivery is `failed`, the device shows `ready: false` with the reason,
  and the announce targets get one alert per broken device (source `hnl-notify/device-check`). Fix the device at
  Pushover or in hnl-notify, or disable it.

## Upgrading to 0.8.0

- Nothing changes for senders that do not use `send_at` / `delay`.
- **Time zone.** Offset-less times in a scheduling request are read in the container's `TZ` (`compose.yaml` sets
  `Europe/Amsterdam`; with no `TZ` it is UTC). The new `tzdata` package is in `requirements.txt`, so rebuild the image.
- **Webhooks and SSE.** `*` and `notification.*` subscribers receive three new event types (above).
- The schema gains columns and an index on `notifications` and migrates itself on start; take a backup first as usual.
- Deleting an API client cancels its scheduled notifications.
- **Reply claims.** The claim endpoint also answers **410** for an ordinary (not scheduled) reply conversation
  whose deliveries all failed, expired or were suppressed without ever being dispatched; 0.7.0 long-polled to 204.
  A manual retry makes it 204 (waiting) again.

## Upgrading to 0.7.0

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

## Upgrading to 0.5.0 and 0.6.0

Upgrading to 0.5.0 migrates the database in place. Rolling back to 0.4.0
requires restoring the pre-0.5.0 database backup (take one before upgrading).
Upgrading to 0.6.0 migrates the database in place. Rolling back to 0.5.1
requires restoring the pre-0.6.0 database backup.

[← back to the README](../README.md)
