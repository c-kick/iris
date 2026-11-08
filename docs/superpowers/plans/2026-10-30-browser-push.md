# Browser push (Web Push) Implementation Plan

> Archived record: written when this project was called hnl-notify (renamed to Iris in 0.15.0). Names, commands and paths below are kept as they were decided; see docs/upgrading.md for what changed.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** hnl-notify 0.10.0: a `webpush` provider that turns enrolled desktop browsers into push devices that report *shown* and *clicked*.

**Architecture:** A provider adapter (`notify/providers/webpush.py`) with its own crypto module (RFC 8291 encryption, RFC 8292 VAPID), a new delivery tracking mode `report` handled in core (early reports, lifetime sweep, attention), and a small no-login browser surface under `/push/` (enrollment page, service worker, report endpoint) guarded by the HTTPS external URL.

**Tech Stack:** Python 3.12, FastAPI, httpx, `cryptography` (already a dependency), SQLite; plain JS for the page and service worker.

**Spec:** `docs/superpowers/specs/2026-10-30-browser-push-design.md` (revision 3; its R1–R15 override earlier sections).

## Global Constraints

- No new Python dependencies; crypto with `cryptography` only.
- Tracked files stay anonymous: examples use `alex` / `notify.example.org`; never real host names, IPs or the owner's name.
- Provider identifiers stay inside `notify/providers/` (core only knows the `report` tracking mode and generic hooks).
- Push endpoints: `https` only, host allowlist `fcm.googleapis.com`, `*.push.services.mozilla.com`, `*.notify.windows.com`, `web.push.apple.com`.
- `default_ttl_seconds` default 86400; enrollment link lifetime 24 h, single use.
- Encrypted push body ≤ 4096 bytes.
- Run tests with `.venv/bin/python -m pytest -q -p no:warnings`. `tests/test_mailbox_cleanup_service.py::test_automatic_run_once_a_day_with_backoff` is a known flaky test (fails ~2/3 also before this work); ignore it.
- Commits: author is the repo's configured user; **no** `Co-Authored-By` or "Generated with" lines.

**Deviation from spec R3 (forced by R2):** the report token is `HMAC-SHA256(key, delivery_id)` (base64url, 22 chars from the first 16 bytes), key = SHA-256 of the provider's VAPID private key bytes. A report carries `delivery_id` + token, so it can be verified while the delivery is still `sending` (before anything is stored). `deliveries.receipt` stays NULL for webpush.

## Review Focus

1. A *shown* report arriving before the worker commits `dispatched` → delivery still ends `delivered` (Task 3 test `test_report_before_dispatch_is_applied`).
2. A forged or replayed report (wrong token, other delivery, finished delivery) → 204, nothing changes (Task 4 `test_report_forged_or_late_is_ignored`).
3. A subscription endpoint on a non-push host (e.g. `https://192.168.1.10/`) at enrollment or in config → refused / permanent failure, no request made (Task 2 `test_ssrf_endpoint_refused`, Task 4 `test_enroll_rejects_non_push_endpoint`).
4. An enrollment link used twice concurrently → exactly one device (Task 4 `test_enroll_code_single_use`).
5. A 4-byte-char 1024-char message → body ≤ 4096 bytes, ends with `…` (Task 2 `test_payload_fits_4096`).

---

### Task 1: Web Push crypto

**Files:**
- Create: `notify/webpush_crypto.py`
- Test: `tests/test_webpush_crypto.py`

**Interfaces:**
- Produces:
  - `generate_vapid_keys() -> tuple[str, str]`: (private key as base64url of the 32-byte scalar, public key as base64url of the 65-byte uncompressed point).
  - `encrypt(plaintext: bytes, p256dh: str, auth: str, *, salt: bytes | None = None, sender_private: str | None = None) -> bytes`: RFC 8291 `aes128gcm` body (header salt‖rs=4096‖idlen=65‖sender pubkey, then one record, padding delimiter 0x02). `salt`/`sender_private` are test seams.
  - `vapid_authorization(endpoint: str, private_key: str, contact: str, now: int) -> str`: `"vapid t=<jwt>, k=<public b64url>"`, ES256 JWT with `aud` = endpoint scheme+host, `exp` = now + 12 h, `sub` = contact.
  - `b64u(b: bytes) -> str` / `unb64u(s: str) -> bytes` (no padding).

- [ ] **Step 0:** Fetch the exact Appendix A values from https://www.rfc-editor.org/rfc/rfc8291.txt (WebFetch); copy them verbatim, never from memory.
- [ ] **Step 1: Write failing tests**: `test_rfc8291_appendix_a_vector` (use the RFC 8291 Appendix A plaintext "When I grow up, I want to be a watermelon", receiver `p256dh`/`auth`, sender private key and salt from the RFC; assert `b64u(encrypt(...)) ==` the RFC's body); `test_vapid_jwt_verifies` (generate keys, build header, split `t=`, verify the ES256 signature with the public key, assert claims `aud == "https://fcm.googleapis.com"`, `sub == "mailto:alex@example.org"`, `exp == now + 43200`); `test_generated_keys_shape` (len(unb64u(priv)) == 32, len(unb64u(pub)) == 65 and starts with 0x04).
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_webpush_crypto.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** with `cryptography` (ECDH P-256, HKDF-SHA256, AESGCM; ES256 via `ec.ECDSA(hashes.SHA256())` then convert DER→raw r‖s with `decode_dss_signature`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `Web push: RFC 8291 encryption and VAPID signing`.

### Task 2: WebPushProvider

**Files:**
- Create: `notify/providers/webpush.py`
- Modify: `notify/providers/__init__.py` (register), `notify/providers/base.py` (OutboundMessage gains `external_url: str | None = None`), `notify/core.py` `message_for` (fill `external_url` from `config.server.external_url` or env `NOTIFY_EXTERNAL_URL`, stripped of trailing `/`)
- Modify: `tests/conftest.py` (`FakePushover.handler`: requests to host `fcm.googleapis.com` are recorded in `push_requests: list[httpx.Request]` and answered from `push_responses: list[tuple[int, dict[str,str]]]` (status, headers), default 201)
- Test: `tests/test_webpush_provider.py`

**Interfaces:**
- Consumes: Task 1 functions; `AddressCheck`, `SendResult`, `DeliveryPlan` from base.
- Produces:
  - `WebPushProvider(Provider)`: `type="webpush"`, `kind="push"`, `max_concurrency=4`; `settings_schema` keys `vapid_private_key` (secret), `vapid_public_key` (text, help "generated; changing it breaks every enrolled browser"), `contact` (text, required, `mailto:`), `default_ttl_seconds` (int, 86400); `secret_settings={"vapid_private_key"}`; `address_schema` keys `endpoint`, `p256dh`, `auth`, `user_agent`; `secret_address_fields={"p256dh","auth"}`.
  - `ALLOWED_PUSH_HOSTS` + `push_endpoint_ok(url: str) -> bool` (module-level; reused by Task 4).
  - `report_token(delivery_id: str) -> str` and `report_ok(delivery_id: str, token: str) -> bool` (constant-time compare). See Deviation above.
  - `plan()` → `DeliveryPlan(tracking="report", observable_states=BASE_OBSERVABLE + [DELIVERED, ACKNOWLEDGED])`; every attachment → gap `attachments: not supported on browser push`.
  - `lifetime_seconds(msg_expires_at, dispatched_at) -> int` is not needed: core uses `provider.default_ttl_seconds` (attribute).
  - `config_problems()`: missing key pair or `contact` not starting with `mailto:`.
  - `device_problems(address)`: missing fields or `push_endpoint_ok` false.
  - `address_problems(address)` / `check_address` → like Pushover's `_invalid` map keyed by endpoint; `check_address` always returns None (nothing to ask ahead).
- Payload JSON (UTF-8), keys: `{"v":1,"id":delivery_id,"t":title,"b":body,"u":url|null,"s":severity,"ri":bool,"r":report_url,"k":token}`; `report_url = external_url + "/push/report"`; `ri` true for high/critical or require_ack; `url` = msg.url else first action with a url.
- HTTP: POST endpoint with headers `TTL` (remaining expiry, else `default_ttl_seconds`), `Urgency` (min→very-low, low→low, normal→normal, high/critical→high), `Content-Encoding: aes128gcm`, `Content-Type: application/octet-stream`, `Authorization` from `vapid_authorization`. Outcomes: 201/202 success + `address_check=AddressCheck(True)`; 404/410 permanent + `AddressCheck(False, "browser subscription is gone (HTTP 410): re-enroll this browser")`; 413 permanent; 429 retryable with `retry_after` from `Retry-After`; 5xx/network retryable; other 4xx permanent. No external URL (or not https) → permanent "browser push needs an https external URL".
- `request_summary`: `{"endpoint": host + "/…" + last 6 chars, "ttl", "urgency", "bytes"}` (R12).

- [ ] **Step 1: Write failing tests**: `test_send_encrypts_and_signs` (fake records one request; headers Content-Encoding aes128gcm, TTL "86400", Urgency "normal", Authorization starts "vapid t="; decrypting is not required, assert body length ≤ 4096); `test_plan_tracking_report_and_attachment_gap`; `test_gone_subscription_flags_device` (fake 410 → permanent, `address_check.valid is False`, `address_problems` non-empty; next 201 clears it); `test_429_retry_after` (headers `Retry-After: 120` → retryable, `retry_after == 120`); `test_ssrf_endpoint_refused` (endpoint `https://192.168.1.10/x` → permanent, no request recorded; `device_problems` non-empty); `test_payload_fits_4096` (message `"😀"*1024`, title 250 chars → body ≤ 4096 and decoded payload `b` ends with `…`; use `encrypt` seam or check plaintext via a `build_payload(msg, delivery_id) -> bytes` method); `test_report_token_roundtrip` (`report_ok(id, report_token(id))`, false for another id); `test_no_https_external_url_fails`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**. Truncation: shrink `b` by characters until the JSON plaintext ≤ 3993 bytes (4096 − 86 header − 16 tag − 1 delimiter), appending `…`.
- [ ] **Step 4: Run** the new tests and `tests/test_device_check.py` → PASS.
- [ ] **Step 5: Commit** `Web push: provider adapter`.

### Task 3: Core: `report` tracking, early reports, lifetime sweep, attention

**Files:**
- Modify: `notify/core.py`, `notify/worker.py` (call the sweep in `_dispatch_loop` next to `svc.expire_due()`)
- Test: `tests/test_webpush_lifecycle.py` (live server via a fixture that adds a `webpush` provider + device `alex-browser` (owner alex, endpoint `https://fcm.googleapis.com/fcm/send/abc`, test keys) to `make_config`, `server.external_url="https://notify.example.org:8931"`)

**Interfaces:**
- Consumes: Task 2 provider (`default_ttl_seconds`, `report_ok`).
- Produces:
  - `NotifyService.apply_report(delivery_id: str, kind: Literal["shown","clicked"], device: str) -> None`: DISPATCHED/DELIVERED → via existing `apply_receipt(..., ReceiptStatus(delivered_at=now) or ReceiptStatus(delivered_at=now, acknowledged_at=now, acknowledged_by=device), via="report")`; in SENDING: only store `delivered_at` (and `acknowledged_at`/`acknowledged_by` for clicked) on the row; any other state: ignore. Clicked implies shown.
  - In `_complete_send`, after the DISPATCHED transition for `tracking='report'`: if the row has `delivered_at` / `acknowledged_at` from an early report, call the same transitions (DELIVERED, then ACKNOWLEDGED).
  - `NotifyService.expire_reports() -> int`: for `tracking='report'` rows in DISPATCHED/DELIVERED whose horizon passed (horizon = notification `expires_at` if set else `dispatched_at + provider.default_ttl_seconds`): DISPATCHED → EXPIRED (`reason "not shown within its lifetime"`); DELIVERED and (severity high/critical or require_ack) → EXPIRED (`"not acknowledged within its lifetime"`, keep delivered_at); other DELIVERED → set `finalized_at` only.
  - `attention()["awaiting_acknowledgement"]`: `d.tracking='receipt' OR (d.tracking='report' AND (n.severity IN ('high','critical') OR n.require_ack=1))`, states DISPATCHED/DELIVERED. (Check the notifications column name for require_ack in `db.py`.)
  - `_transition` must not finalize `report` deliveries at DISPATCHED (already true: only `tracking == "none"` finalizes; add a test).

- [ ] **Step 1: Write failing tests**: `test_shown_then_clicked` (send, wait dispatched; `apply_report(id,"shown","alex-browser")` → delivered; `"clicked"` → acknowledged, `acknowledged_by == "alex-browser"`); `test_report_before_dispatch_is_applied` (call `apply_report` while the row is SENDING by doing it inside a patched provider `send`, then let dispatch commit → state `delivered`); `test_unshown_push_expires` (set `dispatched_at` 2 days back, run `expire_reports()` → expired); `test_unacked_high_expires_but_normal_delivered_finalizes`; `test_attention_awaits_ack_only_for_high` (normal webpush delivered → not listed; high → listed); `test_report_delivery_not_final_at_dispatch` (`finalized_at` NULL after dispatch).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** new tests + `tests/test_api.py` → PASS.
- [ ] **Step 5: Commit** `Core: report tracking (early reports, lifetime sweep, attention)`.

### Task 4: Browser surface: enrollment, service worker, reports

**Files:**
- Create: `notify/webpush_routes.py` (FastAPI router, included in `notify/app.py`), `notify/push_ui/enroll.html`, `notify/push_ui/enroll.js`, `notify/push_ui/sw.js`
- Modify: `notify/db.py` (new table `enrollments(id TEXT PRIMARY KEY, provider TEXT, owner TEXT, code_sha256 TEXT, created_at TEXT, expires_at TEXT, used_at TEXT, device TEXT)` via the existing schema-version migration pattern; bump `SCHEMA_VERSION`), `notify/admin.py` (`POST /admin/api/providers/{name}/enrollments` body `{"owner": str}` → `{"url", "expires_at"}`; 409 when `external_url` is not https; generate VAPID keys in `put_provider` when `type=="webpush"` and no stored key), `notify/app.py`
- Test: `tests/test_webpush_enroll.py`

**Interfaces:**
- Consumes: Task 2 `push_endpoint_ok`, `report_token/report_ok`, `generate_vapid_keys`; Task 3 `apply_report`; `commit_config`; `Throttle` from admin.py; `hash_token`.
- Produces routes (all no-login, all behind the host guard: scheme://host:port of the request, honouring `X-Forwarded-Proto` and `Host`, must equal `server.external_url`, else 404):
  - `GET /push/enroll?c=CODE` → `enroll.html` (CSP `default-src 'self'; script-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'`), shows the VAPID public key via `data-` attribute, a *Enable notifications on this browser* button, and the note that Chrome/Edge need "continue running background apps" and Firefox must stay open.
  - `GET /push/enroll.js`, `GET /push/sw.js` (`Cache-Control: no-cache`, `Service-Worker-Allowed: /push/`; registered with scope `/push/`).
  - `POST /push/enroll` JSON `{"code", "subscription": {"endpoint", "keys": {"p256dh", "auth"}}, "user_agent"}` → 201 `{"device": name}`; 400 bad endpoint (allowlist); 410 code unknown/used/expired (throttled per address on failure). Consumes the code and writes the device in one step under a module `threading.Lock` (code `used_at` set only if `commit_config` succeeds). Device name `<owner>-browser`, `-2`, … ; `description` from a short user-agent label ("Edge on Windows", "Firefox on Linux", else "Browser"); device added to the owner's `devices`.
  - `POST /push/report` JSON `{"d": delivery_id, "k": token, "e": "shown"|"clicked"}` → always 204; applies via `apply_report` only when the delivery exists, its provider is a `WebPushProvider` and `report_ok` holds; per-address limit 120/min.
- `sw.js`: on `push` → parse JSON, `showNotification(t, {body: b, tag: id, requireInteraction: ri, data: {u, r, k, id}})` then POST `{d,k,e:"shown"}` to `r`; on `notificationclick` → close, POST `"clicked"`, `clients.openWindow(u)` when `u`.

- [ ] **Step 1: Write failing tests** (live server, `external_url` set): `test_enroll_flow_creates_device` (admin creates link → URL contains `/push/enroll?c=`; POST `/push/enroll` with Host header matching → 201, device `alex-browser` owned by alex with `description`, `user:alex` targets it); `test_enroll_code_single_use` (two threads POST the same code → one 201, one 410; one device); `test_enroll_rejects_non_push_endpoint`; `test_enroll_link_needs_https_external_url` (409); `test_host_guard` (`GET /push/sw.js` with `Host: 127.0.0.1:port` → 404; with matching Host + `X-Forwarded-Proto: https` → 200 and `Service-Worker-Allowed: /push/`); `test_report_applies` (send to alex-browser, POST valid report → delivered); `test_report_forged_or_late_is_ignored` (bad token, unknown id, acknowledged delivery → 204, no change); `test_vapid_generated_once` (PUT provider twice → same public key; private key never in GET config).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** new tests + full suite → PASS (flaky test excepted).
- [ ] **Step 5: Commit** `Web push: enrollment, service worker and report endpoint`.

### Task 5: Admin UI, MCP text, docs, version

**Files:**
- Modify: `notify/admin_ui/app.js` (webpush provider card: show `vapid_public_key` read-only with the "changing it breaks every browser" note; *Create enrollment link* → pick person → show the link with copy button and expiry; devices list needs no change unless it breaks on missing `user_key`), `notify/mcp_server.py` `_INSTRUCTIONS` (one paragraph: browser devices are `kind: push`, report `delivered` when shown and `acknowledged` when clicked at every severity; no attachments), `README.md` (*Browser push* section: HTTPS requirement, enrollment, browser-running caveat; *Upgrading to 0.10.0*), `docs/architecture.md` (tracking `report` in the data model; a "What browser push can and cannot report" table like Pushover's), `config/notify.example.yaml` (commented webpush provider), `pyproject.toml` + `notify/__init__.py` → `0.10.0`
- Test: `tests/test_mcp.py` (assert instructions mention "browser")

- [ ] **Step 1:** Add the MCP assertion; run → FAIL.
- [ ] **Step 2:** Implement the UI, text and docs changes; run `.venv/bin/python scripts/dev_server.py --demo` briefly (start, `curl -s 127.0.0.1:18931/admin/static/app.js | node --check` via a temp file, stop) to catch JS syntax errors.
- [ ] **Step 3:** Full suite → PASS (flaky test excepted).
- [ ] **Step 4: Commit** `Web push: admin UI, MCP text, docs and version 0.10.0`.
