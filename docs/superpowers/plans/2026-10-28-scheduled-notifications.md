# Scheduled Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** hnl-notify 0.8.0: senders schedule one-off notifications (`send_at` / `delay`) over REST
and MCP; hnl-notify holds them, routes them when due, enforces a lateness limit up to dispatch,
lets the sender and admin change or cancel them, surfaces every problem under Needs attention, and
the admin UI gets a **Scheduled** section.

**Architecture:** A new module `notify/schedule.py` holds the time rules (pure functions) and a
`Scheduler` class (attached as `svc.scheduler`, like `svc.replies`) that owns the only release
door `release(nid, force)`, the scheduled-edit and scheduled-cancel transactions, and the
`release_problems` query. `core.py` gets small hooks: a scheduled branch in submit, the
effective-time rule, `deliver_by` in expiry/backoff, and cancel/retry/dismiss delegating to the
scheduler. The dispatcher calls `release_due()` at the start of each pass, isolating each row.

**Tech Stack:** Python 3.12, FastAPI, SQLite (WAL), pydantic v2, `zoneinfo` (+ `tzdata` wheel),
official MCP SDK, vanilla JS admin UI, pytest with real uvicorn fixtures (`tests/conftest.py`).

**Spec:** `docs/superpowers/specs/2026-10-27-scheduled-notifications-design.md` (rev 2). Read it
before any task; section names below refer to it, HR-n to its review findings.

## Global Constraints

- Branch `scheduling`. Version becomes `0.8.0` (`pyproject.toml`, `notify/__init__.py`), Task 11.
- Repo stays anonymised: only example.org / alex / placeholder names in code, tests, docs.
- Without `send_at` / `delay` every request behaves exactly as in 0.7.0 (including offset-less
  `expires_at` = UTC).
- Values: immediate if send time ≤ now + 5 s; > 60 s in the past → 422; > 366 days ahead → 422;
  `max_late_seconds` 60…86 400, default 3 600; per client (admin and internal exempt) ≤ 100
  scheduled and ≤ 50 MiB (52 428 800 bytes) of their attachment bytes; overdue = still
  `scheduled` more than 60 s after `send_at`.
- Time zone = `ZoneInfo(os.environ["TZ"])` when set and valid, else UTC. No setting.
- One transaction (`with svc.db.tx() as c:`) for: release, scheduled edit, scheduled cancel. No
  `releasing` status. `deliver_by` is never passed to providers.
- Effective time everywhere it means "went out": SQL `COALESCE(released_at, send_at, created_at)`.
- Tests: `.venv/bin/python -m pytest -p no:warnings -o addopts="" <paths> -q`, run under
  `systemd-run --user --scope -p MemoryMax=768M -p MemorySwapMax=0 --` (shared host). Full suite
  green after every task. Existing tests whose assertions change **by design** are named in the
  task; change exactly those.
- Commits: the repo owner is the only author, **no Co-Authored-By or "Generated with" lines**.
  Message style as in `git log` ("Scheduling: …", "Admin UI: …", "Docs: …").
- Match surrounding code: comment density, naming, the `db.tx()` / `_emit` / `_after_commit` idioms.

## Review Focus

1. Offset-less `send_at` on DST days: `2026-03-29T02:30` (gap) → 422; `2026-10-25T02:30` (fold)
   → the first occurrence, `00:30Z`. Test in Task 1.
2. Durations a person might type: `"0"`, `"-5m"`, `"1.5h"`, `""`, `"5 m"`, `"1h1h"` → 422;
   `"1H30M"` accepted (case-insensitive). Test in Task 1.
3. A restart with two overdue notifications, one within its limit and one beyond it: one pass
   releases the first and marks the second missed. Test in Task 3.
4. A PATCH that names an unknown target: 422, and the stored row (title, send time, everything)
   is unchanged. Test in Task 4.
5. The admin UI viewed from a browser in another zone: times shown and entered in the service
   zone, sent with an explicit offset. Server half tested in Task 10 (`PATCH` with a `+05:00`
   offset is stored as the right UTC); the browser half is a manual check step in Task 10.

---

### Task 1: Time rules, model fields, schema

**Files:**
- Create: `notify/schedule.py` (pure functions only in this task)
- Modify: `notify/models.py` (`NotificationIn`, new `ScheduleUpdate`, `EVENT_TYPES`),
  `notify/db.py` (`_add_missing_columns`, index), `requirements.txt` (`tzdata==2025.2`)
- Test: `tests/test_schedule_rules.py`

**Interfaces:**
- Produces (in `notify/schedule.py`):
  - constants `IMMEDIATE_AHEAD = 5`, `PAST_GRACE = 60`, `HORIZON_DAYS = 366`,
    `MAX_LATE_DEFAULT = 3600`, `MAX_SCHEDULED = 100`, `MAX_SCHEDULED_BYTES = 50 * 1024 * 1024`,
    `OVERDUE_SECONDS = 60`, `EFFECTIVE_SQL = "COALESCE(released_at, send_at, created_at)"`
  - `local_zone() -> ZoneInfo`
  - `parse_duration(v: int | str) -> int` (seconds > 0; `ValueError` otherwise). Grammar: a bare
    positive integer, or `^(\d+d)?(\d+h)?(\d+m)?(\d+s)?$` case-insensitive, at least one part.
  - `to_utc(dt: datetime, zone: ZoneInfo) -> datetime` (aware → UTC; naive → `zone`, fold=0;
    a non-existent local time raises `ValueError("… does not exist in <zone> (DST change)")`;
    detect the gap by round-tripping through UTC).
  - `resolve_send_at(send_at: datetime | None, delay: int | str | None, now: datetime,
    zone: ZoneInfo) -> datetime | None` — None = immediate; raises `ValueError` with the
    user-facing message for past / horizon / bad duration.
- Produces (models): `NotificationIn.send_at: datetime | None`, `.delay: int | str | None`,
  `.max_late_seconds: int | None (ge=60, le=86400)`; a `model_validator` rejecting both
  `send_at` and `delay`. `ScheduleUpdate` (extra forbid): optional `send_at`, `delay`,
  `send_now: bool = False`, `max_late_seconds`, `title`, `message`, `severity`, `targets`,
  `kinds`, `url`, `url_title`, `actions`, `tags`, `require_ack`, `ttl_seconds`, `expires_at`;
  validator: `send_now` excludes `send_at`/`delay`, and `send_at` excludes `delay`. Callers
  use `model_fields_set` to tell "omitted" from `null`.
- Produces (schema): `notifications` columns `send_at TEXT`, `max_late_seconds INTEGER`,
  `deliver_by TEXT`, `ttl_seconds INTEGER`, `explicit_expires_at TEXT`, `released_at TEXT`,
  `dismissed_at TEXT`, `release_problem TEXT` (reason text, null = none; this plan's addition so
  the attention query is one column check); index
  `ix_notifications_scheduled ON notifications(status, send_at)` created after the columns.
- Produces: `EVENT_TYPES` gains `notification.scheduled`, `notification.updated`,
  `notification.missed`.

- [ ] **Step 1: Write the failing tests** in `tests/test_schedule_rules.py`:
  `test_parse_duration_accepts` (`90→90`, `"90"→90`, `"90s"→90`, `"45m"→2700`,
  `"2h30m"→9000`, `"1d"→86400`, `"1H30M"→5400`); `test_parse_duration_rejects`
  (parametrized `0, -5, "0", "-5m", "1.5h", "", "5 m", "1h1h", "m"` → `ValueError`);
  `test_to_utc_dst` (`Europe/Amsterdam`: `2026-10-25T02:30` → `2026-10-25T00:30Z`;
  `2026-03-29T02:30` → `ValueError`; aware `+05:00` input converted unchanged);
  `test_resolve_send_at` (now+3 s → None; now−30 s → None; now−61 s → ValueError "in the past";
  now+367 d → ValueError "at most 366 days"; delay `"2h"` → now+2 h);
  `test_send_at_and_delay_exclusive` (`NotificationIn(..., send_at=…, delay=60)` →
  `ValidationError`); `test_local_zone` (monkeypatch `TZ=Europe/Amsterdam` → that key;
  `TZ=Nowhere/Bogus` and unset → `UTC`); `test_columns_migrate` (open a `Database` on a file
  created with the 0.7.0 `SCHEMA` only, assert all eight columns and the index exist).
- [ ] **Step 2: Run** the file; expect import / attribute failures.
- [ ] **Step 3: Implement** the functions, model fields and migration as listed.
- [ ] **Step 4: Run** the file and the full suite; expect PASS.
- [ ] **Step 5: Commit** `Scheduling: time rules, request fields and schema`.

### Task 2: Scheduled submit

**Files:**
- Modify: `notify/core.py` (`SubmitPlan`, `plan_submit`, `commit`, `submit_async`, `_replay`,
  `_notification_out`, `_refresh_status`)
- Test: `tests/test_schedule_submit.py`

**Interfaces:**
- Consumes: Task 1 functions and fields.
- Produces: `SubmitPlan.send_at: datetime | None`; a scheduled row has `status='scheduled'`,
  `send_at`, `max_late_seconds`, `deliver_by = send_at + max_late`, `ttl_seconds`,
  `explicit_expires_at`, `expires_at = min(explicit, send_at + ttl)`; no deliveries.
  `_notification_out` adds `send_at`, `max_late_seconds`, `deliver_by`, `released_at` (only when
  `send_at` is set, so immediate notifications keep their shape) and, for scheduled rows,
  `routing_preview` (`{"devices": [{device, provider, via, capability_gaps}], "suppressed": […]}`
  from the plan at submit / edit time, stored in `routing_json["preview"]`).
- Produces: `NotifyService.scheduled_preview(nrow: dict, targets, severity, kinds) -> dict`
  (resolve + `_plan_deliveries`, ids dropped), reused by Task 4.

Rules to implement (spec "Submitting"): resolve the send time first (`Invalid` on `ValueError`);
immediate → existing path untouched. Scheduled: offset-less `expires_at` read in `local_zone()`;
`expires_at` must be after the send time; ttl from send time; caps (`MAX_SCHEDULED` count and
`MAX_SCHEDULED_BYTES` summed over `attachments.size` of the client's scheduled rows plus this
request) unless `client.admin` or `req._internal`; `in_reply_to` whose target is `scheduled` →
`Invalid("cannot follow up a notification that has not been sent yet")`; `_replay` with the same
key and a different resolved send time → `Conflict("idempotency key already used for a different
send time; use PATCH")`; attachments always fetched and bytes always stored
(`if req.attachments and (res.deliveries or res.send_at)`, `p.data if (deliveries or scheduled)`);
emit `notification.scheduled` (payload: usual + `send_at`) instead of `notification.created`;
`dry_run` returns the preview plus `send_at`. `_refresh_status` returns early for a
`scheduled` row.

- [ ] **Step 1: Write the failing tests** (REST, `live` fixture): `test_schedule_by_delay`
  (POST `delay: "1h"` → 202, `status == "scheduled"`, `deliveries == []`, `send_at` ≈ now+1 h,
  `deliver_by` = `send_at`+1 h, `routing_preview.devices[0].device == "alex-phone"`, no
  Pushover call after 1.5 s); `test_immediate_threshold` (`delay: 3` → normal `pending`, sent);
  `test_past_and_horizon_422`; `test_naive_expires_uses_zone_only_when_scheduled` (TZ
  monkeypatched to `Europe/Amsterdam` before the server starts: scheduled
  `send_at 2026-12-01T07:30, expires_at 2026-12-01T08:00` → stored `06:30Z` / `07:00Z`; an
  immediate request with naive `expires_at` keeps UTC); `test_expires_before_send_422`;
  `test_ttl_counts_from_send_time`; `test_caps` (101st scheduled for `agent` → 422 naming the
  cap; admin token exempt); `test_byte_cap` (inline attachments summing over 50 MiB across
  scheduled rows → 422; use `MAX_SCHEDULED_BYTES` monkeypatched to 1 KiB for speed);
  `test_attachments_stored_when_preview_empty` (target `device:spare` (disabled) with an inline
  attachment → bytes stored: `SELECT data IS NOT NULL FROM attachments`);
  `test_follow_up_of_scheduled_rejected`; `test_replay_different_send_time_409` and same send
  time → 200 replay; `test_scheduled_event_type` (`/v1/notifications/{id}/events` has
  `notification.scheduled`, not `notification.created`); `test_reply_handle_at_schedule`
  (`email_live`, `kinds: ["email"], reply: true, delay: "1h"` → `reply_handle` present).
- [ ] **Step 2: Run** the file; expect FAIL.
- [ ] **Step 3: Implement** as above.
- [ ] **Step 4: Run** the file and the full suite; expect PASS.
- [ ] **Step 5: Commit** `Scheduling: scheduled submit with preview, caps and stored attachments`.

### Task 3: Release (one door) and lateness up to dispatch

**Files:**
- Modify: `notify/schedule.py` (`Scheduler`), `notify/core.py` (`__init__`: `self.scheduler =
  Scheduler(self)`; `expire_due`; `_complete_send` backoff; `message_for` timestamp),
  `notify/worker.py` (`_dispatch_loop`)
- Test: `tests/test_schedule_release.py`

**Interfaces:**
- Consumes: Task 2 rows; `NotifyService._plan_deliveries`, `_emit`, `_refresh_status`,
  `router.resolve`, `message_for`.
- Produces: `Scheduler.release(nid: str, *, force: bool = False) -> str` (resulting status) and
  `Scheduler.release_due() -> int` (number handled). Release steps exactly as spec "Release: one
  door" 1–6, inside one `svc.db.tx()`. Step-1 eligibility: `status='scheduled'`, or (force) a row
  with `release_problem IS NOT NULL` and no deliveries. `force=True` also sets `deliver_by=NULL`.
  On every outcome set `released_at=now`; set `release_problem` to:
  `"missed: due at {send_at}, could not be sent until {now}"`, `"sender no longer exists"`,
  `"routing failed: {error}"`, `"release error: {Type}: {msg}"`,
  `"routed to no device at send time: {reasons}"`,
  `"reply requested but no email device received it"`; success clears it and `dismissed_at`.
  Internal client ids that are never "gone": `SYSTEM_CLIENT.id` (`core.py`, "hnl-notify") and
  `UI_CLIENT.id` (`admin.py`, "admin-ui"; import-cycle-free: compare with the literal held in a
  constant in `schedule.py` and assert it equals `UI_CLIENT.id` in a test).
- Emits: `notification.missed` (missed), `notification.created` with `scheduled: true` then
  `delivery.queued` per delivery (success).
- `release_due()`: select ids with `status='scheduled' AND send_at <= now ORDER BY send_at`; for
  each `try: release(nid) except Exception:` log, and in a fresh tx mark it `failed` with
  `release_problem = "release error: …"`, emit `notification.status_changed`. Then
  `_after_commit(dispatch=True)` if any.
- `expire_due`: also expire queued deliveries whose notification has `deliver_by <= now`
  (reason `"lateness limit passed before dispatch"`), and set the notification's
  `release_problem` to `"not sent within its lateness limit"`.
- `_complete_send`: the retry ceiling is `min(expires_at, deliver_by)` (same EXPIRED branch,
  reason names which one).
- `message_for`: `created_at=` the effective time (`released_at or send_at or created_at`).
- Worker: `svc.scheduler.release_due()` is the first call in `once()`.

- [ ] **Step 1: Write the failing tests**: `test_released_when_due` (schedule `delay: 61`
  then `UPDATE notifications SET send_at=<past 1 s>, deliver_by=<future>` → Pushover receives
  it, status `dispatched`, events contain `notification.created` with `scheduled: true`,
  `released_at` set); `test_routing_decided_at_release` (schedule to `device:spare` while
  disabled; enable it through the admin API; make due → delivered to `spare`, with its
  attachment); `test_restart_overdue_two` (Review Focus 3: two rows, one 10 min late with
  limit 1 h, one 2 h late → one dispatched, one `expired` with `release_problem` starting
  `missed:` and a `notification.missed` event, in the first pass after start-up — build the
  rows, then start a server on the same DB file via `server_factory`); `test_sender_gone`
  (delete client `other` from config after it scheduled → `failed`, "sender no longer exists");
  `test_routing_error_at_release` (remove user `alex` from config → `failed`, "routing failed");
  `test_zero_devices_is_problem` (target `device:spare` stays disabled → `suppressed`, problem
  "routed to no device"); `test_bad_row_does_not_block` (monkeypatch `_plan_deliveries` to raise
  for one id → that one `failed` "release error: …", an immediate notification submitted at the
  same time is still dispatched); `test_released_once` (call `release(nid)` twice → second
  returns current status, one set of deliveries); `test_deliver_by_bounds_retries` (Pushover
  answers 500; `deliver_by` passed → delivery `expired`, problem set, no further attempts);
  `test_deliver_by_not_sent_to_provider` (Pushover request has no `ttl` field when only
  `max_late_seconds` applies); `test_timestamp_is_release_time` (Pushover `timestamp` ≈ release
  time, not submit time).
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement** as above.
- [ ] **Step 4: Run** the file and full suite; expect PASS.
- [ ] **Step 5: Commit** `Scheduling: release door, per-row isolation, lateness to dispatch`.

### Task 4: Edit, send now, cancel (send scope for own scheduled)

**Files:**
- Modify: `notify/schedule.py`, `notify/core.py` (`cancel`, new `Forbidden` exception),
  `notify/api.py` (`PATCH /v1/notifications/{nid}`, cancel route scope, `_errors` maps
  `Forbidden` → 403)
- Test: `tests/test_schedule_edit.py`

**Interfaces:**
- Consumes: `ScheduleUpdate`, `resolve_send_at`, `to_utc`, `scheduled_preview`.
- Produces: `Scheduler.update(client: Client, nid: str, patch: ScheduleUpdate) -> dict` (the
  notification out). One tx: read row (visible to client, else `NotFound`), not `scheduled` →
  `Conflict(f"notification is {status}", {"status": status})`; merge fields in
  `patch.model_fields_set`; recompute send time (`send_now` → now), `deliver_by`, `expires_at =
  min(explicit_expires_at, send_at + ttl_seconds)`; validate like submit (targets via
  `scheduled_preview`, expiry after send time, horizon; `Invalid` aborts the tx, row unchanged);
  write, store new preview, emit `notification.updated` with `{"fields": sorted(names),
  "send_at": {"from", "to"} (if moved), "by": client.id}`; after commit `svc.wake.set()`.
- Produces: `Scheduler.cancel(client: Client, nid: str) -> dict | None` — the single conditional
  UPDATE from spec "Cancel"; returns `{"notification": …, "results": [{"result": "cancelled"}]}`
  or None when no row changed. `NotifyService.cancel`: call it first; on None require
  `client.can(Scope.manage)` else `raise Forbidden("cancelling a sent notification needs the
  manage scope")`, then the existing delivery cancel.
- REST: `PATCH /v1/notifications/{nid}` (`require(Scope.send)`, body `ScheduleUpdate`); the
  cancel route now `require(Scope.send)`.

- [ ] **Step 1: Write the failing tests**: `test_reschedule_moves_ttl` (ttl 600, move +1 day →
  `expires_at` moves too); `test_explicit_expiry_stays_and_blocks` (explicit `expires_at`
  before the new time → 422, row unchanged); `test_edit_content` (title/message/severity/targets;
  event `notification.updated` with `fields` and `by: "agent"`); `test_null_clears_url`;
  `test_unknown_target_leaves_row_unchanged` (Review Focus 4: compare the full row before/after);
  `test_send_now` (released within 2 s); `test_patch_after_release_409`;
  `test_sender_scope_can_cancel_own_scheduled` (client `sender` (send only) schedules and
  cancels → 200, status `cancelled`); `test_sender_scope_cannot_cancel_sent` (immediate send by
  `sender`, cancel → 403); `test_other_client_cannot_touch` (`other` → 404 on PATCH and cancel);
  `test_admin_can_edit_any`; `test_concurrent_edits_serialise` (two threads PATCH send_at and
  expires_at at once → final row satisfies `expires_at > send_at` or one got 422).
  Changed by design: any existing test asserting that a `send`-only client gets 403 from the
  cancel route on a notification it does not own still gets 403/404 — check and keep.
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** file and full suite; PASS.
- [ ] **Step 5: Commit** `Scheduling: edit, send now and cancel of scheduled notifications`.

### Task 5: Effective time in retention and attachment purge

**Files:**
- Modify: `notify/core.py` (`retention_sweep`)
- Test: `tests/test_schedule_retention.py`

**Interfaces:** Consumes `EFFECTIVE_SQL`. Retention: skip `status='scheduled'`; compare
`EFFECTIVE_SQL < cutoff` instead of `created_at`. Attachment purge: skip notifications in
`scheduled`; compare `EFFECTIVE_SQL`.

- [ ] **Step 1: Write the failing tests**: `test_scheduled_far_ahead_survives_sweep` (row with
  `created_at` 200 days ago, `send_at` in 10 days → sweep keeps row and attachment bytes);
  `test_missed_survives` (spec "Required test": `created_at` 200 days ago, missed now → row,
  bytes and conversation survive `retention_sweep()`); `test_released_old_is_swept` (effective
  time 100 days ago, final → deleted).
- [ ] **Step 2: Run**; FAIL.  **Step 3: Implement.**  **Step 4: Run** file + suite; PASS.
- [ ] **Step 5: Commit** `Scheduling: retention and attachment purge count from the effective time`.

### Task 6: Release problems: attention, dismiss, retry, health, metrics

**Files:**
- Modify: `notify/schedule.py` (`problems`, `overdue_count`, `next_in`), `notify/core.py`
  (`attention`, `dismiss`, `retry`, `stats`), `notify/api.py` (`/health/ready`, `/metrics`)
- Test: `tests/test_schedule_attention.py`

**Interfaces:**
- Produces: `Scheduler.problems(client: Client) -> list[dict]` — rows with
  `release_problem IS NOT NULL AND dismissed_at IS NULL`, plus `scheduled` rows overdue by
  `OVERDUE_SECONDS` (reason `"overdue: still waiting {n} s after its send time"`), own-only for
  non-admin, newest first, limit 50. Item: `{notification_id, title, severity, source, client,
  status, send_at, reason, at}` (`at` = `released_at` or `send_at`).
  `attention()` adds key `release_problems`.
- `Scheduler.overdue_count() -> int`, `Scheduler.next_in() -> float | None` (seconds to the
  earliest `send_at`).
- `dismiss`: also sets `dismissed_at` on a notification with an undismissed `release_problem`;
  409 only if neither deliveries nor the notification were dismissed. Result
  `{"dismissed": deliveries + notification}`.
- `retry`: when the row has `release_problem` and no deliveries → `scheduler.release(nid,
  force=True)` (effective `expires_at` in the past → `Conflict("notification has expired; submit
  a new one instead")`), return `{"notification": …, "results": [{"result": "released"}]}`.
- `/health/ready`: `checks["scheduler"] = "ok"` or `f"{n} overdue"`; `ok` requires it.
- `/metrics`: `notify_scheduled_next_seconds` (omitted when None), `notify_scheduled_overdue`.

- [ ] **Step 1: Write the failing tests**: `test_missed_listed_and_dismissed` (`/v1/attention`
  `release_problems[0].reason` starts `missed:`; admin dismiss → gone; history unchanged);
  `test_retry_missed_sends` (retry → dispatched, problem cleared, lateness ignored);
  `test_retry_expired_409`; `test_overdue_in_attention_and_health` (stop the dispatcher task —
  monkeypatch `release_due` to a no-op — and make a row 2 min overdue → listed with "overdue",
  `/health/ready` 503 with `checks.scheduler == "1 overdue"`); `test_metrics_lines`;
  `test_problems_own_only` (`other` doesn't see `agent`'s).
- [ ] **Step 2: Run**; FAIL.  **Step 3: Implement.**  **Step 4: Run** file + suite; PASS.
- [ ] **Step 5: Commit** `Scheduling: release problems in attention, dismiss and retry; health and metrics`.

### Task 7: Listing scheduled notifications

**Files:** Modify `notify/core.py` (`list`), `notify/api.py` (`list_notifications`).
Test: `tests/test_schedule_submit.py` (add).

**Interfaces:** `list(..., status="scheduled")` (exactly that single status) orders by
`send_at, id`, ignores `before`, returns all rows (cap: 100 × clients for admin, so use a limit of
1000); the REST route returns `next_before: null` for it. Other listings unchanged.

- [ ] **Step 1: Test** `test_list_scheduled_sorted_without_cursor` (three rows scheduled in
  reverse order, `limit=2` → all three, ascending by `send_at`, `next_before` null).
- [ ] **Step 2–4:** run (FAIL), implement, run file + suite (PASS).
- [ ] **Step 5: Commit** `Scheduling: list scheduled notifications by send time`.

### Task 8: MCP surface and discovery text

**Files:** Modify `notify/mcp_server.py`. Test: `tests/test_mcp.py` (add).

**Interfaces:**
- `INSTRUCTIONS` becomes `instructions(zone: str) -> str`; `build_mcp` passes
  `local_zone().key`. Append the spec's "Discovery" paragraph with the zone filled in.
- `send_notification` gains `send_at: str | None = None`, `delay: int | str | None = None`,
  `max_late_seconds: int | None = None`; docstring documents them (one short paragraph:
  formats, "routing is decided at send time", lateness default 1 h, `--timeout` for replies).
- New tool `update_scheduled_notification(ctx, notification_id: str, send_at: str | None = None,
  delay: int | str | None = None, send_now: bool = False, title/message/severity/targets/kinds/
  url/url_title/tags/require_ack/ttl_seconds/expires_at/max_late_seconds (all `| None = None`),
  clear: list[str] | None = None)`: builds a `ScheduleUpdate` from the non-None arguments plus
  `None` for each name in `clear` (only optional fields: `title, url, url_title, ttl_seconds,
  expires_at, kinds, tags`; others → `ToolError`). Scope `send`.
- `cancel_notification` scope check becomes `send` (the service decides; `Forbidden` →
  `ToolError`). `list_notifications` docstring lists `scheduled` and its ordering.

- [ ] **Step 1: Tests**: `test_mcp_instructions_name_zone` (TZ=Europe/Amsterdam → text contains
  "without offset = Europe/Amsterdam" and "update_scheduled_notification");
  `test_mcp_schedule_update_cancel` (schedule via tool with `delay="2h"`, update title and
  `clear=["url"]`, cancel with a send-only client → cancelled); `test_mcp_clear_rejects_required`
  (`clear=["message"]` → error).
- [ ] **Step 2–4:** run (FAIL), implement, run file + suite (PASS).
- [ ] **Step 5: Commit** `MCP: scheduling parameters, update_scheduled_notification, discovery text`.

### Task 9: Replies: 410 for dead conversations; notify-wait and the duration grammar

**Files:** Modify `notify/replies.py` (`claim`), `notify/api.py` (claim route; `Gone` → 410),
`notify/core.py` (`class Gone(Exception)`), `scripts/notify-wait`. Test:
`tests/test_replies_api.py`, `tests/test_notify_wait.py` (add), `tests/test_schedule_rules.py`
(shared table).

**Interfaces:**
- `Replies.dead_reason(conv: str) -> str | None`: not None when no notification of the
  conversation is `scheduled` or has a delivery with `dispatched_at IS NOT NULL` or in
  `queued/sending`, and the conversation has no reply in `waiting/unclaimed`. Reason:
  `"notification was cancelled"` / `"notification was missed"` / `"notification was not sent"`
  from the root's status. `claim` raises `Gone(reason)` before waiting (and after a wait returns
  nothing).
- `notify-wait`: HTTP 410 → print `"No reply possible: {reason}."`, exit 1. Help text: one line
  on scheduled notifications and `--timeout`. Its `parse_duration` accepts the same grammar
  (`1d`, combinations, case-insensitive).
- Shared table `DURATIONS = [("90s", 90), ("45m", 2700), ("2h30m", 9000),
  ("1d", 86400), ("1H30M", 5400)]` in `tests/test_schedule_rules.py`, run against both
  `notify.schedule.parse_duration` and the script's parser (load the script with
  `importlib.machinery.SourceFileLoader` as `tests/test_notify_wait.py` already does). The
  script keeps its rule that a **bare number means minutes** (existing callers rely on it); the
  shared table therefore holds only forms with a unit, and a separate assertion pins
  `notify-wait "90"` → 5400 and server `delay: "90"` → 90.

- [ ] **Step 1: Tests**: `test_claim_410_after_cancel` (scheduled reply:true, cancel, claim →
  410 "cancelled"); `test_claim_still_204_while_scheduled`; `test_notify_wait_exit_1_on_410`;
  the shared table test.
- [ ] **Step 2–4:** run (FAIL), implement, run file + suite (PASS). Changed by design: an
  existing test that claims on a failed immediate reply notification and expects 204 now gets
  410 — update only that.
- [ ] **Step 5: Commit** `Replies: 410 when a conversation can never get a reply; notify-wait durations`.

### Task 10: Admin API and the Scheduled section

**Files:** Modify `notify/admin.py`, `notify/admin_ui/app.js`, `notify/admin_ui/app.css`
(only if needed). Test: `tests/test_admin.py` (add).

**Interfaces:**
- `GET /admin/api/scheduled` → `{"items": svc.list(admin, status="scheduled"), "timezone":
  local_zone().key}`.
- `PATCH /admin/api/notifications/{nid}` (body `ScheduleUpdate`, mutation checks as other admin
  writes) → `svc.scheduler.update`. `manage` action `send-now` → update with `send_now=True`.
  `Invalid` → 422, `Conflict` → 409.
- `_state` adds `scheduled_count` (all clients) and, per API client, `scheduled` (count).
  `delete_client`: after the config commit, cancel that client's scheduled rows in one tx
  (status `cancelled`, event with reason `"client deleted"`); response adds `cancelled_scheduled`.
- UI: `SECTIONS` gains `["scheduled", "Scheduled"]` before `"activity"`; badge = `scheduled_count`.
  Page per spec "Admin UI": header text verbatim, rows sorted by send time, future-aware
  `until(iso)` helper (`"in 14 h"`, `"in 5 min"`, `"overdue"`), times formatted with
  `Intl.DateTimeFormat([], {timeZone: tz, …})` and labelled with `tz`; Reschedule input
  (`datetime-local`) interpreted in `tz` and sent as ISO with that zone's offset at that
  moment (compute via `Intl` parts; DST-correct); Edit form fields per spec with preview from
  an admin dry-run (`dry_run: true` with the edited targets,
  severity, kinds) through a new `POST /admin/api/preview` (body: targets, severity, kinds,
  title, message) that runs `svc.submit_async(UI_CLIENT, NotificationIn(..., dry_run=True))` and
  returns its routing/deliveries; Cancel with the
  existing confirm dialog; 409 → toast "Already sent" / "Already cancelled", reload; empty
  state text. Activity rows with status `scheduled` show "scheduled for …"; Needs attention
  renders `release_problems` with Dismiss and Retry. Client delete confirm adds "N scheduled
  notifications from this client will be cancelled." when N > 0.

- [ ] **Step 1: Tests**: `test_admin_scheduled_list_and_zone`; `test_admin_patch_with_offset`
  (Review Focus 5: `send_at "…T12:00:00+05:00"` → stored `07:00Z`); `test_admin_send_now`;
  `test_admin_delete_client_cancels_scheduled`; `test_admin_state_counts`.
- [ ] **Step 2–4:** run (FAIL), implement API, run (PASS). Then implement the UI.
- [ ] **Step 5: Manual check** with `scripts/dev_server.py`: open `/admin`, schedule three
  notifications by curl (one with reply, one with an attachment), confirm the section, badge,
  relative times, reschedule (+1 h), edit with preview, send now, cancel; set the browser zone
  to another zone (DevTools → Sensors → Location/timezone override `Asia/Kolkata`) and confirm
  times still show and are entered in the service zone.
- [ ] **Step 6: Commit** `Admin UI: Scheduled section with reschedule, edit, send now and cancel`.

### Task 11: Docs and version

**Files:** `README.md`, `docs/architecture.md`, `pyproject.toml`, `notify/__init__.py`,
(changelog section in README if present).

- [ ] **Step 1:** README: "Scheduling" section with a REST example (`delay`, `send_at` with and
  without offset), MCP example, PATCH/cancel, `max_late_seconds`, the caps, replies +
  `notify-wait --timeout`, release notes line: `*`/`notification.*` subscribers receive
  `notification.scheduled`, `notification.updated`, `notification.missed`, and
  `notification.created` fires at release for scheduled ones.
- [ ] **Step 2:** `docs/architecture.md`: lifecycle (`scheduled → released → delivery states` /
  `cancelled` / `expired (missed)`), the new columns in the data model list, the effective-time
  rule, attachment retention for scheduled notifications, `deliver_by`, the release door and
  per-row isolation, the `release_problems` bucket.
- [ ] **Step 3:** Version `0.8.0` in both files; run the full suite; PASS.
- [ ] **Step 4: Commit** `Docs and version 0.8.0`.
- [ ] **Step 5 (at deploy, outside the repo):** add a scheduling section to
  `~/documentation/notifications.md` (how agents schedule, list, update, cancel; `--timeout`).
