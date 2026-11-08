# Reply Mailbox Cleanup and Late Replies Implementation Plan

> Archived record: written when this project was called hnl-notify (renamed to Iris in 0.15.0). Names, commands and paths below are kept as they were decided; see docs/upgrading.md for what changed.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** hnl-notify 0.7.0: per-conversation reply handles that make late replies collectable only by
the agent that started a conversation (Part B), and automatic plus manual cleanup of the reply
mailbox's Processed and Quarantine folders (Part A).

**Architecture:** Part B adds a `conversations` table holding a hashed secret per conversation and
one access gate in `notify/replies.py` that every reply path goes through; the admin UI is told
apart from bearer tokens by an explicit `Client.via_session`. Part A adds `notify/mailbox_cleanup.py`
(pure selection + its own IMAP entry point), a config section `mailbox_cleanup`, a worker task, a
per-provider lock shared with the poller, admin endpoints and a UI panel.

**Tech Stack:** Python 3.12, FastAPI, SQLite (WAL), imaplib, pydantic v2, official MCP SDK, vanilla JS
admin UI, pytest (real uvicorn server fixtures in `tests/conftest.py`).

**Spec:** `docs/superpowers/specs/2026-10-26-mailbox-cleanup-and-late-replies-design.md` (rev 2).
Read it before any task; section names below refer to it.

Plan revision 2: plan staff review PR-1…PR-18, all adopted per the project ambassador's ruling
(PR-7 and PR-16 in modified form).

## Global Constraints

- Branch `reply-cleanup`. Version becomes `0.7.0` (`pyproject.toml`, `notify/__init__.py`), Task 10.
- Repo stays anonymised: only example.org / alex / placeholder names in code, tests, docs.
- Container budget 320 MiB: cleanup never downloads message bodies (sizes and dates only).
- The reply handle (secret) is never written to any DB column, event, log record, error text or URL.
- Handle format `<conversation id>:<secret>`; secret `secrets.token_urlsafe(24)`; stored as
  `hashlib.sha256(secret.encode()).hexdigest()`; compared with `hmac.compare_digest`.
- A value containing `:` in `in_reply_to` or in a reply route's path id is refused with a fixed
  message that never echoes the value: "in_reply_to takes a notification id; pass the handle as
  reply_handle" (submit) / plain `NotFound("notification not found")` (routes) (PR-15).
- Every refused handle answers like an unknown notification: REST/MCP `NotFound` → 404 on reply
  endpoints; on submit the existing `Invalid(f"in_reply_to: notification {id} not found")` (422).
- Cleanup defaults: processed 30 days, quarantine 90 days, processed cap 1024 MiB, quarantine cap
  500 MiB, enabled true; 0 = rule off. Ranges: days 0–3650, MiB 0–1048576.
- Autoreply text (non-legacy): "The agent isn't listening right now. Your reply has been kept and
  the agent can still pick it up later. Received {received}." Legacy keeps today's text.
- Size alert: title `Reply mailbox: {folder} over its size cap`, severity normal, tag
  `mailbox-cleanup`, idempotency key `mailbox-cleanup:{provider}:{folder}:{YYYY-MM-DD UTC}`.
- Tests: `.venv/bin/python -m pytest -p no:warnings -o addopts="" <paths> -q`, run under
  `systemd-run --user --scope -p MemoryMax=768M -p MemorySwapMax=0 --` (shared host). Full suite
  must stay green after every task. Existing tests whose assertions change **by design** are
  listed per task; change exactly those to the new rule, never weaken others.
- Commits: the repo owner is the only author, **no Co-Authored-By or "Generated with" lines**. Message style as in
  `git log` ("Replies: …", "Cleanup: …").
- Match surrounding code: comment density, naming, the `db.tx()` / `_emit` / `_after_commit` idioms.

## Review Focus

1. A handle pasted with surrounding whitespace or a trailing newline (env var, file) must still
   work: strip it. Test in Task 3 (submit) and Task 4 (notify-wait).
2. A first cleanup run on a folder with thousands of mails must finish in batches without the
   120 s session kill: timer re-armed per batch. Test in Task 7 (500 messages, batch 200 → 3 STORE
   rounds, timer re-armed 3 times).
3. Folder names that differ only by case (`processed` vs `Processed`) or contain spaces must be
   compared case-insensitively and quoted on the wire. Test in Task 7.
4. A server without UIDPLUS where the target folder already has `\Deleted` mail: plain `EXPUNGE`
   only in the selected target folder, never while INBOX is selected. Test in Task 7.
5. An agent that runs `notify-wait` with the handle while a previous waiter with a *wrong* handle
   keeps polling: the autoreply must still fire after the grace time. Test in Task 4.

---

### Task 1: Session marker and admin content lockout (SR-5)

**Files:**
- Modify: `notify/core.py:48-62` (`Client`), `notify/admin.py:33` (`UI_CLIENT`), `notify/admin.py:599-605` (`replies_view`), `notify/config.py:176-181` (reserved names)
- Test: `tests/test_admin.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `Client(id, scopes, via_session: bool = False)`; `UI_CLIENT.via_session is True`;
  `RESERVED_CLIENT_NAMES = frozenset({"admin-ui", "hnl-notify"})` in `notify/config.py`.
  `def require_session(client: Client) -> None` in `notify/admin.py` raising `HTTPException(403, "this needs the signed-in admin page")`.

- [ ] **Step 1: Failing tests**
  - `test_config.py::test_reserved_client_names`: a config with `clients: {"admin-ui": …}` and one with `"hnl-notify"` raise `ValueError` matching `"reserved"`.
  - `test_admin.py::test_bearer_admin_sees_no_reply_content`: with an email server, store a reply (reuse `send`/`deliver` helpers from `tests/test_replies_core.py`, import them), then `GET /admin/api/replies` with a bearer admin token → every item has `text is None`, `subject is None`, `attachments == []`, quarantine items have `from_address is None and subject is None and message_id is None`; the same call with the signed-in cookie session returns the text.
  - Change by design: `tests/test_admin.py::test_admin_replies_view` (≈283-297) uses a bearer admin and expects `text`; make it use the signed-in session for the text assertion and add the bearer case above.
- [ ] **Step 2:** Run them; expect FAIL.
- [ ] **Step 3: Implement.** Add `via_session: bool = False` as the last dataclass field of `Client`; `UI_CLIENT = Client("admin-ui", frozenset({Scope.admin}), via_session=True)`. Reserved-name check in `_references` next to the `_NAME_RE` check, for `clients` only. In `replies_view`, when `not client.via_session`, blank `subject`, `text`, `attachments`, `dropped_attachments` of replies and `from_address`, `subject`, `message_id` of quarantine rows (states, times, reasons, counts stay).
- [ ] **Step 4:** Run the new tests and the full suite; expect PASS.
- [ ] **Step 5: Commit** `Admin: explicit session marker; reply content only for the signed-in admin page`

### Task 2: Conversations table, migration and retention (SR-3, SR-11)

**Files:**
- Modify: `notify/db.py` (SCHEMA, `Database.__init__`, `SCHEMA_VERSION`), `notify/core.py:975-994` (`retention_sweep`)
- Test: `tests/test_conversations.py` (new)

**Interfaces:**
- Produces table `conversations(id TEXT PRIMARY KEY, handle_sha256 TEXT, handle_notification_id TEXT, legacy INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)` and column `replies.late_ok INTEGER NOT NULL DEFAULT 0`.

- [ ] **Step 1: Failing tests** in `tests/test_conversations.py`:
  - `test_backfill_marks_existing_reply_conversations_legacy`: simulate a 0.6.2 database: open `Database(path)`, insert one notification with `reply_wanted=1` (root `ntf_a`), one plain notification `ntf_b`, one reply with `state='unclaimed'`, then `DROP TABLE conversations` and close; reopen `Database(path)` → `conversations` has exactly one row `id='ntf_a', legacy=1, handle_sha256 IS NULL`; the reply has `late_ok=0`.
  - `test_backfill_runs_once`: insert another reply-enabled notification `ntf_d` without a conversation row, reopen → `ntf_d` still has no row (the backfill does not run again), `ntf_a` row unchanged.
  - `test_retention_keeps_conversation_while_followups_live`: conversation row for `ntf_a`, root `ntf_a` older than `retention_days`, follow-up `ntf_c` (`conversation_id='ntf_a'`) recent → after `retention_sweep()` the root is gone and the row stays; after the follow-up is old too and swept, the row is gone.
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3: Implement.** Add `late_ok` to the `replies` CREATE in SCHEMA and to `_add_missing_columns`. The `conversations` table is **not** in SCHEMA (PR-4: `executescript` autocommits, so a crash between create and backfill would skip the backfill). After `_add_missing_columns`, in one `with self.tx() as c:` — if `sqlite_master` has no `conversations` table: `CREATE TABLE conversations (…)` then `INSERT INTO conversations(id, legacy, created_at) SELECT DISTINCT conversation_id, 1, ? FROM notifications WHERE reply_wanted=1 AND conversation_id IS NOT NULL` (SQLite DDL is transactional, so table and backfill commit together or not at all). `late_ok` defaults to 0, so every existing `unclaimed` reply stays uncollectable without an UPDATE. Leave `SCHEMA_VERSION` alone. In `retention_sweep`, after deleting notifications: `DELETE FROM conversations WHERE NOT EXISTS (SELECT 1 FROM notifications n WHERE n.conversation_id=conversations.id)` (uses `ix_notifications_conversation`).
- [ ] **Step 4:** Run the new tests and the full suite; expect PASS.
- [ ] **Step 5: Commit** `Replies: conversations table, legacy backfill, retention by last notification`

### Task 3: Reply handle on submit, replay and follow-ups (SR-1, SR-2, SR-4)

**Files:**
- Modify: `notify/models.py:180-207` (`NotificationIn`), `notify/core.py` (`_replay`, `plan_submit`, `commit`, `_notification_out` ~line 749), `notify/replies.py:209-231` (`_autoreply` sets the internal flag)
- Create: `notify/handles.py`
- Test: `tests/test_reply_handles.py` (new)

**Interfaces:**
- Produces in `notify/handles.py`:
  `def new_handle(conversation_id: str) -> tuple[str, str]` → `(handle, sha256_hex)`;
  `def parse_handle(handle: str | None) -> tuple[str, str] | None` → `(conversation_id, sha256_hex)` after `.strip()`, `None` if malformed;
  `def handle_matches(handle: str | None, conversation_id: str, stored_sha256: str | None) -> bool`.
- Produces `NotificationIn.reply_handle: str | None = Field(default=None, max_length=200, description=…)` and `NotificationIn._internal: bool = PrivateAttr(default=False)` (hnl-notify's own submissions; REST/MCP cannot set it).
- Produces in `notify/replies.py`: `def conversation_gate(c: sqlite3.Connection, conv: str, handle: str | None, *, internal: bool = False) -> sqlite3.Row | None` — returns the conversation row (or `None` when there is none and no notification of the conversation has `reply_wanted=1`); raises `NotFound` when: no row but some notification of the conversation has `reply_wanted=1` (fail closed, PR-5); or the row has a `handle_sha256` and not `internal` and not `handle_matches(...)`. Legacy rows pass here; the token rule for them is applied by the callers. Used by `plan_submit`, again inside `commit`'s `tx()`, and by Task 4's `_access`.
- Send response gains `reply_handle` (string) only on the notification that created it; a replay of that notification returns `reply_handle: None, reply_handle_issued: True`.

- [ ] **Step 1: Failing tests** in `tests/test_reply_handles.py` (use `email_live`, `AGENT`/`OTHER` clients and helpers from `tests/test_replies_core.py`):
  - `test_root_reply_send_returns_handle_once`: POST reply:true → `out["reply_handle"].startswith(out["id"] + ":")`; `GET /v1/notifications/{id}` has no `reply_handle` key; DB `conversations.handle_sha256` = sha256 of the secret part; no column of `notifications`, no `events.data_json` contains the secret; with `caplog.set_level(logging.DEBUG)` no `record.getMessage()` and no value in `record.__dict__` contains it (PR-13).
  - `test_replay_returns_no_handle_and_keeps_old`: POST twice with the same `idempotency_key` → second response `reply_handle is None and reply_handle_issued is True`; `handle_matches(first_handle, …)` still true.
  - `test_idempotency_key_hidden_in_handle_conversation`: `GET /v1/notifications/{id}` → `idempotency_key is None`; for a plain notification it is still returned.
  - `test_followup_needs_handle`: follow-up with `in_reply_to=id` and no handle → 422 with `"not found"`; with another conversation's handle → 422; with `reply_handle=" " + h + "\n"` → 202 and no new `reply_handle` in its response.
  - `test_first_reply_followup_creates_handle`: root without reply, follow-up reply:true with `in_reply_to` → its response has `reply_handle` with the root's conversation id; a second follow-up without it → 422.
  - `test_legacy_conversation_followup_by_token`: mark the conversation row `legacy=1, handle_sha256=NULL` → follow-up by the same token without a handle → 202 and its response has **no** `reply_handle` even with `reply:true`; by `OTHER` → 422 as today.
  - `test_missing_row_fails_closed`: delete the conversation row of a reply conversation → follow-up with its (formerly valid) handle → 422 not found; no new row is created.
  - `test_handle_as_in_reply_to_not_echoed`: `in_reply_to=<handle>` → 422 with the fixed message; the response body does not contain the secret.
  - `test_handle_checked_again_at_commit`: monkeypatch so a handle is set on the conversation between `plan_submit` and `commit` (e.g. wrap `commit` to update the row first) → the follow-up without the new handle is refused.
  - `test_autoreply_passes_in_handle_conversation`: reply arrives, nobody listens, move the clock past `GRACE_SECONDS` (pattern from `test_replies_core.py`) → `sweep_unclaimed()` sends the autoreply notification (exists with tag `reply-autoreply`).
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3: Implement.**
  - `plan_submit`: refuse `':' in req.in_reply_to` with the fixed message (PR-15). When `req.in_reply_to` resolves to a conversation, call `conversation_gate(self.db.conn, conv, req.reply_handle, internal=req._internal)` and turn `NotFound` into the existing not-found `Invalid` (never echoing a handle). For a legacy row, the existing `_visible` check stays the rule.
  - `commit`, first thing inside the `tx()`: call `conversation_gate` again (closes the race with `submit_async`'s attachment fetch, PR-5). Then `INSERT OR IGNORE INTO conversations(id, legacy, created_at)` when `req.reply or req.in_reply_to` (only reached when the gate allowed it); when `req.reply` and the row is non-legacy without a handle, `new_handle(conv)` and `UPDATE conversations SET handle_sha256=?, handle_notification_id=? WHERE id=? AND handle_sha256 IS NULL AND legacy=0`; only if that updated a row put the handle in `out["reply_handle"]` (after `get`, like `webhook_secret`).
  - `_replay`: if `conversations.handle_notification_id == row.id`, add `reply_handle: None, reply_handle_issued: True`.
  - `_notification_out`: `idempotency_key` is `None` when the notification's conversation row has a handle.
  - `_autoreply`: `req._internal = True` next to `_auto_submitted`.
- [ ] **Step 4:** Run the new tests and the full suite; expect PASS. Existing tests that send `reply:true` follow-ups (`tests/test_replies_core.py` ≈96-100 and ≈240-257, and any in `test_replies_api.py`/`test_email_replies.py`) need the handle: make `send()` in `test_replies_core.py` record `out["reply_handle"]` in a module dict keyed by conversation id, and have the follow-up helpers and `claim()` look it up by default (no call-site signature churn). Keep their assertions.
- [ ] **Step 5: Commit** `Replies: per-conversation reply handle, returned once; internal submissions exempt`

### Task 4: One access gate, late pickup, REST and MCP (SR-11, SR-13, SR-17)

**Files:**
- Modify: `notify/replies.py` (`claim`, `_claim_once`, `list`, `attachment`, `_out`, `sweep_unclaimed`, `_autoreply`), `notify/api.py:57-60,107-131`, `notify/mcp_server.py` (instructions at 41-49, `_compact` 67-82, `send_notification` 106-147, `get_replies` 231-238), `scripts/notify-wait` (handle input and header only, PR-2)
- Test: `tests/test_reply_handles.py`, `tests/test_replies_api.py`, `tests/test_mcp.py`, `tests/test_notify_wait.py`

**Interfaces:**
- Consumes: `handles.handle_matches`, `replies.conversation_gate`, `conversations` table (Tasks 2-3).
- Produces in `scripts/notify-wait` (moved here from Task 5 so the suite stays green, PR-2): handle sources in this order: a positional argument containing `:` > `--handle-file FILE` > env `NOTIFY_REPLY_HANDLE`; values `.strip()`ped; a positional plain id is the notification id; if a plain id and a handle from file/env name different conversations, exit 1 with "the reply handle belongs to another conversation" (PR-10); with a handle, the notification id is its conversation id; `_request(method, url, token, timeout, handle=None)` sends `X-Reply-Handle` on claims and attachment downloads; the handle is never printed in output, logs or errors.
- Produces: `Replies._access(client: Client, nid: str, handle: str | None) -> sqlite3.Row` (the notification row; raises `NotFound`); `claim(client, nid, waiter, wait, handle=None)`, `list(client, nid, handle=None)`, `attachment(client, rid, idx, handle=None)`. `_out` gains `"late": bool(r["late_ok"])`. REST reads header `X-Reply-Handle` on claim, list and attachment, and on `POST /v1/notifications` (the body field `reply_handle` wins when both are given, like `Idempotency-Key` at api.py:57-60, PR-14). MCP `send_notification(..., reply_handle: str | None = None)` and `get_replies(notification_id, reply_handle: str | None = None)`.

- [ ] **Step 1: Failing tests:**
  - `test_other_agent_same_token_cannot_claim`: same token, no handle → `NotFound`; wrong handle → `NotFound`; right handle → reply.
  - `test_admin_token_no_bypass`: admin bearer client, no handle → `NotFound` on claim, list text, attachment.
  - `test_wrong_handle_does_not_refresh_listener` (Review Focus 5): claims with a wrong handle repeatedly; `listener_seen_at` stays `NULL`; after the grace time the reply becomes `unclaimed` and the autoreply goes out; then the right handle claims it with `late is True`.
  - `test_claim_order_waiting_before_late`: one swept late reply (older) and one waiting reply → first claim returns the waiting one, second the late one.
  - `test_legacy_unclaimed_never_handed_out`: legacy conversation, reply swept → `late_ok == 0`, claim by the sending token returns `None`, autoreply text is the old one.
  - `test_late_autoreply_text`: non-legacy sweep → autoreply message starts with "The agent isn't listening right now."
  - `test_list_hides_unclaimed_text_even_with_handle` and `test_list_shows_claimed_text_with_handle_only`.
  - `test_root_retained_away_still_needs_handle`: delete the root notification row (simulating retention) while the follow-up and its reply remain → claim with the handle works, without it `NotFound`; delete the conversation row → `NotFound` with the handle too.
  - REST (`test_replies_api.py`): claim/list/attachment with `X-Reply-Handle` work; without → 404; the handle never appears in the server's access log lines captured by the live fixture.
  - MCP (`test_mcp.py`): `send_notification(reply=True)` result has `reply_handle`; `get_replies` with it shows claimed text.
  - `test_legacy_admin_bearer_refused` (PR-17): legacy conversation, admin bearer (not the sender) → `NotFound` on claim and list text; the sender's token works.
  - `test_handle_works_after_root_retained` (PR-18): delete the root notification row; claim with `nid = <conversation id from the handle>` and the handle → still works (fallback to the newest notification of the conversation).
  - notify-wait (`test_notify_wait.py`): `test_wait_with_handle_env` (value with trailing `\n`), `test_wait_with_handle_file`, `test_positional_handle_beats_env`, `test_conflicting_id_and_env_handle_exit_1`, `test_handle_never_printed` (wrong handle → exit 1; secret absent from stdout and stderr), `test_attachment_download_sends_handle`.
  - Change by design: `tests/test_replies_core.py` ≈134-152 (autoreply text and `claim(...) is None` after the sweep → new text, and the late reply is claimable with the handle; keep a legacy variant asserting the old behaviour), ≈184-203 (ADMIN reads text/attachments → now `NotFound` without the handle); existing `tests/test_notify_wait.py` calls `nw.main([nid, …])` → pass the handle (positional) instead.
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3: Implement.** `_access`: path id containing `:` → `NotFound("notification not found")` (PR-15). `n = svc._notification_row(client, nid)` (token visibility as today); if that raises `NotFound` and a `conversations` row with `id == nid` exists, use the newest notification of that conversation instead (PR-18). `conv = n["conversation_id"] or n["id"]`; `row = conversation_gate(conn, conv, handle)` (fail-closed and handle rules); row `None` → `NotFound`; legacy row → require `client.id == n["client_id"]` (PR-17), else `NotFound`; return `n`. Call it first in `claim` (before the loop, so before any `listener_seen_at` update), `list` and `attachment` (look up the reply's notification, then `_access`; reply must be `claimed`). Remove the `client.admin` exemptions in `list` and `attachment`. `_claim_once` third query: `state='unclaimed' AND late_ok=1`. `sweep_unclaimed`: `UPDATE replies SET state='unclaimed', late_ok=? …` with 1 when the conversation row is non-legacy. `_autoreply` picks the text by legacy flag. MCP `send_notification`: add `reply_handle: str | None = None`, pass it into `NotificationIn`, and after `_compact(out)` copy `reply_handle` and `reply_handle_issued` from `out` when present (PR-3: `_compact` whitelists keys). Update MCP instructions and docstrings: `send_notification` returns `reply_handle` (keep it; it is shown once); wait with `NOTIFY_REPLY_HANDLE=<handle> notify-wait` or `notify-wait <handle>`; follow-ups pass `reply_handle`; late replies are kept and collectable.
- [ ] **Step 4:** Run the new tests and the full suite; expect PASS.
- [ ] **Step 5: Commit** `Replies: one access gate for claim, list, attachments; late replies collectable by the handle`

### Task 5: notify-wait late-reply output (SR-12)

**Files:**
- Modify: `scripts/notify-wait`
- Test: `tests/test_notify_wait.py`

**Interfaces:**
- Consumes: REST from Task 4 (`X-Reply-Handle`, `late` field, `attachments[].purged`).
- Consumes: handle input from Task 4.

- [ ] **Step 1: Failing tests** (existing harness in `tests/test_notify_wait.py`):
  - `test_late_reply_marked`: a late reply prints `Late reply (received <time>) to: <title>` before the text.
  - `test_purged_attachments_listed`: purged attachment printed as `<name>: not available any more`; text and `--json` modes.
  - `test_timeout_text`: on timeout the message says "Replies are kept; run notify-wait again later to collect them." (replaces "Later replies get an autoreply.", scripts/notify-wait ≈220).
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3: Implement** the output changes; update the module docstring/help for handles and late replies.
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5: Commit** `notify-wait: mark late replies, list purged files`

### Task 6: Cleanup config section and settings plumbing (SR-7, SR-16)

**Files:**
- Modify: `notify/config.py` (new model, `NotifyConfig.mailbox_cleanup`, validator), `notify/registrations.py:162` (extract helper), `notify/admin.py:285-292` (`delete_provider` drops the section), `notify/admin.py:226-230` (`_drop_refs` also strips the ref from every `mailbox_cleanup[*].alert_targets`)
- Test: `tests/test_config.py`, `tests/test_registrations.py`

**Interfaces:**
- Produces in `notify/config.py`:
  `class MailboxCleanupConfig(_Strict)`: `enabled: bool = True`, `processed_days: int = Field(30, ge=0, le=3650)`, `quarantine_days: int = Field(90, ge=0, le=3650)`, `processed_max_mib: int = Field(1024, ge=0, le=1048576)`, `quarantine_max_mib: int = Field(500, ge=0, le=1048576)`, `alert_targets: list[str] = []`;
  `NotifyConfig.mailbox_cleanup: dict[str, MailboxCleanupConfig]` (keys must be provider names; `alert_targets` validated exactly like `registration.notify_targets`, channels allowed, config.py ≈197-202);
  `NotifyConfig.cleanup_for(provider: str) -> MailboxCleanupConfig` (the section or defaults).
- Produces in `notify/registrations.py`: `def announce_targets(cfg: NotifyConfig) -> list[str]` (today's inline expression), used by the access-request announcement.

- [ ] **Step 1: Failing tests:** defaults via `cleanup_for("x")`; unknown provider key → `ValueError`; out-of-range values → `ValueError`; bad alert target → `ValueError`; `announce_targets` returns `notify_targets` or every `user:<name>`; saving a provider with `PUT /admin/api/providers/{name}` keeps an existing `mailbox_cleanup` section; deleting the provider removes it; deleting a user named in `alert_targets` succeeds and removes the ref; `channel:` targets are accepted.
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3: Implement** as in Interfaces. A `mailbox_cleanup` value of `{}` for a provider means defaults.
- [ ] **Step 4:** Run the new tests and the full suite; expect PASS.
- [ ] **Step 5: Commit** `Cleanup: config section per reply mailbox, shared announce targets`

### Task 7: Cleanup engine: selection and IMAP entry point (SR-8, SR-18)

**Files:**
- Create: `notify/mailbox_cleanup.py`
- Modify: `notify/providers/email_inbound.py` (export `mask_secret`; use it in `test`), `notify/worker.py:189-192` (use `mask_secret`)
- Test: `tests/test_mailbox_cleanup.py` (new, with its own imaplib-level fake)

**Interfaces:**
- Produces in `notify/providers/email_inbound.py`: `def mask_secret(text: str, secret: str) -> str` (replace when `len(secret) >= 4`).
- Produces in `notify/mailbox_cleanup.py`:
  - `@dataclass(frozen=True) class Msg: uid: bytes; size: int; arrived: datetime`
  - `def parse_internaldate(value: str) -> datetime` (`"14-Oct-2026 09:13:12 +0200"` → aware UTC datetime; no `time.mktime`)
  - `def select(msgs: list[Msg], now: datetime, days: int, cap_bytes: int) -> tuple[list[bytes], list[bytes]]` → `(by_age, by_cap)`
  - `SPECIAL_USE = frozenset({"\\sent", "\\trash", "\\junk", "\\all", "\\archive", "\\drafts", "\\flagged"})`
  - `class CleanupMailbox(host, port, user, password, *, verify_tls=True, timeout=SESSION_SECONDS, imap_factory=imaplib.IMAP4_SSL)` with `for_provider(p)`, `open()` (login, CAPABILITY, LIST → `self.folders: dict[str, set[str]]` lower-cased attrs), `refusal(inbox: str, processed: str, quarantine: str) -> str | None`, `scan(folder: str, *, readonly: bool) -> list[Msg]` (EXAMINE when `readonly`, else SELECT and keep it selected; `NO` on FETCH = empty), `delete_batch(folder: str, uids: list[bytes]) -> int` (raises `MailboxError` unless `self._selected == folder` and the selection is read-write; re-arms the timer via `_arm()`), `close()`. `self._selected: str | None` is set after an OK SELECT and cleared by EXAMINE or any failure; `select()` is never called without an argument (imaplib defaults to INBOX).
  - `BATCH = 200`
  - `async def run_pass(open_mailbox: Callable[[], CleanupMailbox], lock: asyncio.Lock, provider: EmailProvider, cfg: MailboxCleanupConfig, *, now: datetime, dry_run: bool, deadline: float | None = None) -> dict[str, Any]` → `{"refused": str | None, "folders": {name: {"messages", "bytes", "oldest", "removed_age", "removed_cap", "removed_bytes", "after_messages", "after_bytes", "cap_hit", "unfinished"}}}`. The lock is held per step, not per pass (PR-1, spec "Concurrency"): under the lock open + CAPABILITY/LIST + refusal check; per folder under the lock scan (SELECT for a real run) + `select()`; then each batch in its own `async with lock:` acquisition. Each locked step runs the blocking call as `t = asyncio.create_task(asyncio.to_thread(...))` and `await asyncio.shield(t)`, releasing the lock only when the thread has returned, also on cancellation (PR-8). The session stays open between batches (re-armed timer); if a step has to reopen the session, compare UIDVALIDITY with the scan and abort that folder on a mismatch. `deadline` (monotonic) stops between batches and sets `unfinished`. The mailbox is always closed in `finally`.

- [ ] **Step 1: Failing tests:**
  - `select` table test (parametrize): age only; cap only; ties broken by UID; `days=0` and `cap=0` off; total exactly at the cap → nothing; the oldest message bigger than the cap → it alone goes (fixture: it is the oldest); age-removed messages don't count toward the cap.
  - `parse_internaldate` with `+0200`, `-0700` and a space-padded day (`" 4-Oct-2026 09:13:12 +0200"`) → correct UTC, independent of locale (map month names yourself).
  - Fake IMAP: `refusal` for processed == inbox, `Processed`/`processed` equal, a folder with `\Sent` attribute, and the same with a case difference between the configured name and the LIST name; folder with a space is sent quoted.
  - `test_uidplus_from_post_login_capability` (PR-6): the fake's pre-login `capabilities` lack UIDPLUS, `capability()` after login returns it → the UID EXPUNGE path is used.
  - `delete_batch` without a prior read-write SELECT of that folder → `MailboxError`.
  - `test_poll_gets_lock_between_batches` (PR-1): 3-batch run; a coroutine waiting on the same lock acquires it before batch 2 starts.
  - `delete` with UIDPLUS → `UID STORE` + `UID EXPUNGE` per batch; without UIDPLUS → `EXPUNGE`; the fake asserts INBOX is never selected and STORE/EXPUNGE only happen in the SELECTed target (Review Focus 4); never `CREATE`.
  - 500 messages, batch 200 → 3 rounds and the kill timer is re-armed 3 times (Review Focus 2); `deadline` in the past after round 1 → `unfinished` true and 200 removed.
  - `run_pass(dry_run=True)` uses EXAMINE only and removes nothing; numbers match `select`.
  - Error text from a failed login contains `***`, not the password.
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3: Implement.** `_arm()` cancels and restarts the `threading.Timer` (as in `ImapMailbox._open`); called at open and before each batch. After login: `typ, data = self._imap.capability()` and parse `data[0].split()` upper-cased; never use `.capabilities` (filled before login only, PR-6). LIST with `LIST "" "*" RETURN (SPECIAL-USE)` when SPECIAL-USE is advertised, else plain LIST; look attributes up by folder name case-insensitively. Quote folder names with `_q` from `email_inbound`.
- [ ] **Step 4:** Run the new tests and the full suite; expect PASS.
- [ ] **Step 5: Commit** `Cleanup: selection rules and a read-only-by-default IMAP entry point`

### Task 8: Lock, worker task, alert and admin API (SR-6, SR-14, SR-15)

**Files:**
- Modify: `notify/core.py` (service state), `notify/providers/email_inbound.py` (`poll_once` takes the lock), `notify/worker.py` (new `_cleanup_loop` registered like the other loops), `notify/admin.py` (connection test under the lock; new endpoints), `tests/conftest.py` (`email_factory` sets a fake `open_cleanup_mailbox` and `cleanup_start_delay = 3600`, so no test reaches a real IMAP server)
- Test: `tests/test_mailbox_cleanup_service.py` (new)

**Interfaces:**
- Consumes: Task 6 config, Task 7 `CleanupMailbox`/`run_pass`, Task 3 `NotificationIn._internal`, Task 1 `require_session`.
- Produces on `NotifyService`: `mailbox_lock(name: str) -> asyncio.Lock` (created lazily per provider), `cleanup_status: dict[str, dict[str, Any]]` (`last_run`, `last_success`, `last_error`, `last_result`, `last_by`), `open_cleanup_mailbox: Any = None` (test seam, like `open_mailbox`), `async def run_mailbox_cleanup(name: str, *, dry_run: bool, by: str, deadline_seconds: float | None) -> dict[str, Any]` (calls Task 7's `run_pass` with `self.mailbox_lock(name)`, records status with `last_error` through `mask_secret`, sends alerts, writes the event).
- `poll_once(svc, provider, mailbox)` holds `svc.mailbox_lock(provider.name)` for its whole session (PR-9: tests call `poll_once` directly).
- `Workers` instance attributes (PR-9): `cleanup_start_delay = 600`, `cleanup_interval = 86400`, `cleanup_check_seconds = 60`, `cleanup_backoff_max = 21600`, `clock = time.monotonic`.
- Event (PR-16): one `events` row `type='mailbox.cleanup'`, `notification_id NULL`, `client_id=<by>`, `source='hnl-notify'`, `data_json` with provider, dry_run false, removed counts; inserted directly (not via `_emit`, which needs a notification) and not fanned out to webhooks. Only for real runs, automatic (`by='hnl-notify'`) and manual.
- Admin endpoints: `GET /admin/api/mailbox` (any admin; per provider: dry-run result, settings `cleanup_for`, status, alert-target warning; cached 30 s per provider), `PUT /admin/api/mailbox/{provider}` (body `MailboxCleanupConfig`; `require_session`; via `_commit`), `POST /admin/api/mailbox/{provider}/cleanup` (`require_session`; `deadline_seconds=60`). Unknown or not-`replies_ready` provider → 404.

- [ ] **Step 1: Failing tests** (live email server fixture + fake cleanup mailbox via `open_cleanup_mailbox`):
  - `test_poll_and_cleanup_never_overlap`: a fake whose `scan` blocks on a `threading.Event`; start a manual cleanup, call `poll_once` → the poll waits until the event is set (assert ordering with timestamps); and the other direction: a manual cleanup started during a blocked poll waits.
  - `test_manual_cleanup_busy_409`: lock held longer than the bound → POST answers 409 "mailbox busy, try again".
  - `test_cancel_keeps_lock_until_thread_returns` (PR-8): cancel the awaiting task during a blocked batch → the lock stays held until the thread returns.
  - `test_cache_invalidated_after_put_and_post`: GET, PUT, GET → the second GET shows the new settings.
  - `test_disabled_skips_automatic_but_manual_works` (PR-12).
  - `test_cleanup_event_names_client`: a manual run writes one `mailbox.cleanup` event with `client_id` of the session client.
  - `test_cap_hit_sends_one_alert_per_day`: two runs on the same UTC day with a cap hit → exactly one notification with tag `mailbox-cleanup` and the pinned title; targets from `alert_targets`, else `announce_targets`.
  - `test_no_targets_no_alert_and_warning`: no users, no targets → no notification; `GET /admin/api/mailbox` shows the warning.
  - `test_put_and_post_need_session`: bearer admin → 403 on PUT and POST; session → 200; GET works for both.
  - `test_automatic_run_once_a_day`: with `Workers.clock` replaced and the `cleanup_*` attributes shrunk: first run after the start delay, none again before the interval, back-off after a failure, polling unaffected by a failing cleanup.
  - `test_refused_provider_does_nothing`: processed == inbox → result `refused` set, no STORE.
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3: Implement.** Failure back-off 600 s doubling up to `cleanup_backoff_max`. The automatic loop skips providers with `cleanup_for(name).enabled` false (manual runs always work). Manual POST: `await asyncio.wait_for(lock.acquire(), 30)` before the run (release it before `run_pass` takes it per step; on timeout 409), `deadline_seconds=60`. PUT and POST invalidate the GET cache of that provider. The alert is `NotificationIn(targets=…, title=…, message=…, severity=normal, tags=["mailbox-cleanup"], idempotency_key=…, source="hnl-notify/mailbox-cleanup")` with `_internal = True`, submitted as `SYSTEM_CLIENT`. Message: `"{removed} mails ({mib:.1f} MiB) were removed from {folder} because it went over its {cap} MiB cap. It now holds {after_mib:.1f} MiB."`. Also log `mailbox cleanup` with `client`, `provider`, `dry_run`.
- [ ] **Step 4:** Run the new tests and the full suite; expect PASS.
- [ ] **Step 5: Commit** `Cleanup: daily run, manual run and preview under one mailbox lock; size alert`

### Task 9: Admin UI "Reply mailbox" panel

**Files:**
- Modify: `notify/admin_ui/app.js` (new section after "Providers", same `section-head` idiom), `notify/admin_ui/app.css` only if needed
- Test: `tests/test_admin.py` (served bundle contains the section and calls the endpoints)

**Interfaces:**
- Consumes: Task 8 endpoints.

- [ ] **Step 1: Failing test:** `GET /admin/static/app.js` contains `"Reply mailbox"`, `/api/mailbox` and `Clean up now`.
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3: Implement.** Shown when the GET returns at least one provider. Per provider: folder table (messages, size MiB, oldest), "would remove" line, refusal or alert-target warning, last automatic run, settings form (`fieldHtml` like the Settings section; labels "Keep handled replies (days)", "Keep rejected mail (days)", "Max size handled replies (MiB)", "Max size rejected mail (MiB)", "Clean up automatically", "Send size alerts to"; help text "0 switches the rule off"), "Clean up now" → the existing `confirmAction(title, body, label, fn)` (app.js ≈193) with the preview numbers → result line (says when unfinished). Copy follows the UI's plain style; everything escaped with `esc`.
- [ ] **Step 4:** Run; expect PASS. Manually: `scripts/dev_server.py --dir /tmp/notify-dev --demo --password devpassword123` and check the section renders without console errors (implementer reports what they saw).
- [ ] **Step 5: Commit** `Admin UI: Reply mailbox panel with preview, settings and manual cleanup`

### Task 10: Docs, version and release notes

**Files:**
- Modify: `README.md` ("Replies to agents", "MCP", "Operations"), `docs/architecture.md` if it describes reply access, `pyproject.toml`, `notify/__init__.py`
- Test: full suite

- [ ] **Step 1:** README: reply handle (returned once, `NOTIFY_REPLY_HANDLE` / `notify-wait <handle>`, follow-ups pass `reply_handle`, replay returns none), late replies, threat model sentence from the spec, admin-token change, mailbox cleanup (defaults, 0 = off, Gmail-style servers free no space, INTERNALDATE note), upgrade notes for 0.7.0 (first cleanup run removes old mail; scripts reading reply text with admin tokens stop getting it; pre-0.7.0 conversations keep the token rule). Placeholders only.
- [ ] **Step 2:** Version `0.7.0` in both files.
- [ ] **Step 3:** Full suite; expect PASS. no changed file contains the owner's real name or own domain, and `git diff main | grep -E "^\+.*192\.168"` → nothing new.
- README upgrade notes also say: agent instructions outside the repo that use `notify-wait <notification_id>` must switch to the handle.
- [ ] **Step 4: Commit** `Docs and version 0.7.0`
