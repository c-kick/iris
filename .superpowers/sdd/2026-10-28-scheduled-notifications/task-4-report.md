# Task 4 report

Implemented `Scheduler.update` / `Scheduler.cancel` (notify/schedule.py), `Forbidden` and the new
`NotifyService.cancel` order (scheduled cancel first, then manage check, then delivery cancel) in
core.py, `PATCH /v1/notifications/{nid}` (send scope), cancel route now send scope, `Forbidden` -> 403 in api.py.

Decisions: merged content is validated by running it through `NotificationIn` (kinds dedupe/non-empty,
tag length, etc.); null on message/severity/targets/require_ack/max_late_seconds -> 422; null send_at/delay
is a no-op; a new time within 5 s of now becomes now; offset-less send_at/expires_at read in local_zone();
event `notification.updated` {fields (model_fields_set, sorted), by, send_at {from,to} if moved}; cancel emits
`notification.status_changed` scheduled->cancelled; wake after commit, update never calls release().

TDD: RED 19 failed/3 passed on tests/test_schedule_edit.py; GREEN 22 passed. Full suite: 525 passed.
Command: systemd-run --user --scope -p MemoryMax=768M -p MemorySwapMax=0 -- .venv/bin/python -m pytest -p no:warnings -o addopts= tests -q
Existing cancel tests (other->404, send-only on others) unchanged and passing.
Concern: concurrent-edit test is a weak race check (serialisation is guaranteed by db.tx BEGIN IMMEDIATE).
