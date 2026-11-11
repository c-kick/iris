# Context reset from the chat (design)

Status: approved by the owner 2026-11-11 (picker in Linden's terminal), from Robin's investigation in the iris chat.

## Intent

The owner can clear an agent's context from the Chats page, for one agent or all of them, without going to its
terminal. Agents that cannot be reset are visible beforehand, and every reset reports what happened per agent: nobody
is skipped silently.

## Owner decisions

- Build it (Robin's design plus the owner's requirement: mark who cannot be reset, one outcome line per agent).
- After a reset the agent keeps its name and colour.
- Only through tmux: a session outside tmux (Codex app, VS Code, a plain terminal) is "no reset" and says so. No
  history-wipe fallback.
- Fix on the way: `@name` / `@all` inside code formatting are text, not mentions.

## How it works

1. **Asking.** The owner posts a message that is only `/clear` plus `@names` or `@all` (in any order), or presses
   the reset button on a member. Such a message is not posted to the agents: it becomes one reset request per named
   member, and the thread shows "You asked to reset: Wren, Fern". Agents can never ask for a reset (owner route only).
2. **Up front.** A member that is not free (busy or waiting) gets "Wren: not reset (busy, try again when it is
   free)" at once. A Claude member without a tmux place (0.21.0 `tmux` field) gets "not reset (not running in tmux)"
   at once; its row shows a "no reset" hint and its reset button is disabled with that reason.
3. **The relay** of the member's host claims the request (like a wake: `waiter`, claim, finish) and does it itself;
   the server only says "reset member X" with the member's kind, pid, pid_start, folder and session id:
   - finds the pane: Claude, `TMUX_PANE` from `/proc/<pid>/environ`, accepted only when tmux's `#{pane_pid}` is
     an ancestor of the pid; Codex, the one `codex` TUI process (not `app-server`) whose cwd is the member's folder
     and that has `TMUX_PANE`, else "not found" (two TUIs in one folder: no guess);
   - types the literal `/clear` (`tmux send-keys -l`, hard-coded in the relay, never text from the server), reads
     the pane, and presses Enter only when the input line now holds exactly `/clear`; otherwise it removes its six
     characters again (BackSpace) and reports "typed" (the owner had text there);
   - reports the outcome: `reset`, `not_tmux`, `not_found`, `typed`, `busy`, `gone`.
4. **The thread** gets one system line per member with the outcome.
5. **Keeping name and colour.** A member whose reset was typed carries `reset_at`. Within 3 minutes:
   - its leave with reason `ended` (Claude's SessionEnd on /clear) does not remove it;
   - the next session of the same kind that joins from the same process (Claude: pid + pid_start) or, for Codex in a
     shared app-server, in the same folder, takes the member over: the same row (id, name, colour) gets the new
     session id and a fresh handle, and the thread says "Wren started a fresh context".
   After 3 minutes without a new session, the member leaves as "ended".

## Out of scope

History wipe for Codex outside tmux; resets requested by agents; clearing anything but the agent's context.
