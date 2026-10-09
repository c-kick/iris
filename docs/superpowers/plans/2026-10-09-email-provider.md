# Email delivery path + attachments — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an SMTP email provider, per-request delivery `kinds` with per-device `opt_in`, and attachments (inline or allow-listed URL) to hnl-notify 0.5.0.

**Architecture:** Email is a new adapter behind the existing `Provider` contract (`notify/providers/`). Routing learns provider *kinds* from config. Attachments are validated/fetched in a new `notify/attachments.py` before a split submit (`plan_submit` → fetch → `commit`) and stored as SQLite BLOBs. The admin UI's Pushover-only pages are generalised to any registered provider type.

**Tech Stack:** Python 3.12 (venv locally may be 3.14), FastAPI, pydantic 2, httpx, stdlib `smtplib`/`email`, SQLite; tests: pytest, `httpx.MockTransport`, `aiosmtpd` (dev only); admin UI: vanilla JS (`notify/admin_ui/app.js`).

**Spec:** `docs/superpowers/specs/2026-10-09-email-provider-design.md` — read it before your task; it is authoritative where this plan is terse.

## Global Constraints

- Public REST/MCP never contain provider names as required input; kinds are `push` and `email` only.
- Request field is `kinds`; `via` keeps its existing meaning (expansion path) everywhere.
- Requests without `kinds` against configs without email devices must give output identical to 0.4.0.
- Attachment limits: max 10 per notification, 10 MiB (10485760 bytes) decoded total; MCP inline ≤ 256 KiB (262144 bytes) total; `data_base64` max 14 200 000 chars.
- Body limits: 15 MiB on `POST /v1/notifications`, 1 MiB elsewhere.
- URL attachments only from `server.attachment_url_hosts` (empty = disabled); max 3 redirects; 15 s total fetch budget per request; `trust_env=False`; URL never stored or logged.
- Always-denied addresses: `127.0.0.0/8`, `::1`, `0.0.0.0/8`, `::`, `169.254.0.0/16`, `fe80::/10`, and IPv4-mapped forms of these.
- Email provider `max_concurrency = 1`; attachment intake under a global `asyncio.Semaphore(1)`.
- Adapters never raise on sender input.
- Secrets: `password` is a secret setting; smtplib `debuglevel` stays 0.
- Run tests with: `cd <repo> && .venv/bin/python -m pytest -q -p no:warnings` (venv exists; `aiosmtpd` installed).
- Commits: author is the repo owner only — **no `Co-Authored-By` trailer, no "Generated with" line**. Short imperative messages like existing history.
- Match surrounding code style: dense, typed, `from __future__ import annotations`, short comments only where non-obvious.

## Review Focus

1. A sender passes a title/filename with CR/LF or emoji → email still sends (no adapter exception, no retry burn). Pinned in Task 6.
2. An existing integration (HA, *arr, registration alerts) sends exactly as before after an email device is added for the same person → still push only. Pinned in Task 1 (`test_existing_sender_unchanged_with_email_device`).
3. SMTP relay slower than `server.lease_seconds` → exactly one email, one attempt recorded. Pinned in Task 6 (`test_slow_smtp_not_reclaimed`).
4. Admin edits the email provider and leaves the password blank → stored password kept; select value outside options rejected. Pinned in Task 7.
5. Manual retry of a failed email after attachments were purged → sends without them and the gap says so. Pinned in Task 4.

---

## File structure

| File | Responsibility |
|---|---|
| `notify/models.py` (mod) | `KINDS`, `AttachmentIn`, `NotificationIn.kinds/attachments` |
| `notify/config.py` (mod) | `DeviceConfig.opt_in`, `ServerConfig.attachment_url_hosts/attachment_retention_days`, `device_kind`, `device_opt_in` |
| `notify/routing.py` (mod) | kinds filtering |
| `notify/bodylimit.py` (new) | ASGI request-size limit |
| `notify/attachments.py` (new) | validate/decode/fetch → `PreparedAttachment` |
| `notify/db.py` (mod) | `attachments` table, `journal_size_limit` |
| `notify/core.py` (mod) | `plan_submit`/`commit`/`submit_async`, attachments in messages/output, purge, retry gap, lease per provider |
| `notify/providers/base.py` (mod) | `kind`, `send_deadline_seconds`, `test_connection`, `AttachmentData`, `OutboundMessage.attachments` |
| `notify/providers/pushover.py` (mod) | image attachment |
| `notify/providers/email.py` (new) | SMTP adapter |
| `notify/providers/__init__.py` (mod) | register email |
| `notify/api.py`, `notify/mcp_server.py`, `notify/admin.py` (mod) | use `submit_async`; `kinds`; admin fields; test endpoint |
| `notify/app.py` (mod) | body-limit middleware, fetch client |
| `notify/admin_ui/app.js`, `app.css` (mod) | Providers page, device picker, settings |
| docs, README, example config, compose, pyproject, `notify/__init__.py` | 0.5.0 |

---

### Task 1: Kinds and opt-in routing

**Files:**
- Modify: `notify/models.py`, `notify/config.py`, `notify/routing.py`, `notify/core.py` (submit routing call, `routing_json`, `targets()`, `providers_info()`), `notify/providers/base.py` (`kind`), `notify/providers/pushover.py` (`kind = "push"`), `notify/mcp_server.py` (`kinds` on `send_notification` and `preview_routing`), `notify/api.py` (arr `kinds` query param)
- Test: `tests/test_routing.py` (new)

**Interfaces:**
- Produces: `models.KINDS = ("push", "email")`; `Provider.kind: ClassVar[str] = ""`; `DeviceConfig.opt_in: bool | None = None`; `NotifyConfig.device_kind(name: str) -> str`; `NotifyConfig.device_opt_in(name: str) -> bool`; `Router.resolve(targets: list[str], severity: Severity, kinds: list[str] | None = None) -> RoutePlan`; `NotificationIn.kinds: list[Literal["push","email"]] | None`.

Kind defaults: `{"push": False, "email": True}` (`KIND_OPT_IN_DEFAULT` in `models.py`). `device_kind` imports `REGISTRY` lazily (`from .providers import REGISTRY` inside the method) and returns `REGISTRY[self.providers[dev.provider].type].kind`, or `""` for an unknown type. Until Task 6 registers email, tests may register a tiny fake provider class into `REGISTRY` via `monkeypatch.setitem`.

Router algorithm (replace `resolve`/`_expand`; keep the existing suppression rules and order):

```python
def resolve(self, targets, severity, kinds=None):
    plan = RoutePlan()
    for t in targets:
        kind, name = self.normalize(t)
        top = f"{kind}:{name}"
        found: set[str] = set()  # kinds of devices reached (before severity/enabled filtering)
        self._expand(kind, name, severity, top, plan, set(), kinds, top_level=(kind == "device"), found=found)
        if kinds:
            for k in kinds:
                if k not in found:
                    plan.suppressed.append({"target": top, "via": top, "reason": f"no {k} device"})
    return plan
```

In `_expand` for `device`: compute `dk = cfg.device_kind(name)`; `found.add(dk)` only if `kinds` is None or `dk in kinds`. Then:
- `kinds` given and `dk not in kinds`: if `top_level` → suppressed `{"target": key, "via": via, "reason": f"device kind {dk} not requested"}`; return (no entry otherwise).
- `kinds` None and `cfg.device_opt_in(name)` and not `top_level`: return silently.
- then the existing enabled / `min_severity` checks and add.
Recursive calls for user/group/channel pass `top_level=False`. Note `found` records a kind even when the device is later suppressed by severity/disabled, so "no email device" only fires when none exists.

In `core.submit`: `self.router.resolve(req.targets, req.severity, req.kinds)`; add `"kinds": req.kinds` to `routing` **only when `req.kinds` is not None** (keeps 0.4.0 output identical). `targets()` device_out adds `"kind": cfg.device_kind(name), "opt_in": cfg.device_opt_in(name)`. `providers_info()` adds `"kind": p.kind`.

`NotificationIn.kinds` validator: de-duplicate preserving order; reject `[]` with "kinds must name at least one of push, email".

MCP `send_notification(..., kinds: list[Literal["push","email"]] | None = None)` and `preview_routing(..., kinds=...)`; docstring: "kinds: which delivery kinds to use; omitted = the default devices (push). email devices only receive when 'email' is listed." Arr ingest: `kinds: str | None = Query(default=None)` → split by comma → `body.kinds = [...]` (422 on unknown values via model validation: rebuild with `NotificationIn.model_validate({**body.model_dump(), "kinds": parsed})`).

- [ ] **Step 1: Write failing tests** in `tests/test_routing.py`. Build configs with `NotifyConfig.model_validate` and a fake provider type registered via `monkeypatch.setitem(REGISTRY, "fakemail", FakeMail)` where `class FakeMail(PushoverProvider): type = "fakemail"; kind = "email"`. Config: provider `pushover` (type pushover) and `mail` (type fakemail); devices `alex-phone` (pushover, owner alex), `alex-email` (mail, owner alex), `sam-phone` (pushover, owner sam); users alex, sam; group `household` members `user:alex`, `user:sam`, `device:alex-email`. Tests:
  - `test_default_skips_opt_in_email`: `resolve(["user:alex"], normal)` → devices == `{"alex-phone"}`, suppressed == [].
  - `test_kinds_email_only`: `kinds=["email"]` → `{"alex-email"}`.
  - `test_kinds_both`: `kinds=["push","email"]` → both alex devices.
  - `test_top_level_device_bypasses_opt_in`: `resolve(["device:alex-email"])` → `{"alex-email"}`.
  - `test_group_member_device_does_not_bypass`: `resolve(["group:household"])` → `{"alex-phone","sam-phone"}`.
  - `test_no_email_device_suppressed`: `resolve(["user:sam"], kinds=["email"])` → no devices, suppressed == `[{"target":"user:sam","via":"user:sam","reason":"no email device"}]`.
  - `test_top_level_device_kind_not_requested`: `resolve(["device:alex-email"], kinds=["push"])` → suppressed contains reason `"device kind email not requested"` and `"no push device"`.
  - `test_opt_in_override`: device `alex-email` with `opt_in: False` → default resolve includes it; device `alex-phone` with `opt_in: True` → default excludes it.
  - `test_kinds_validation`: `NotificationIn(targets=["x"], message="m", kinds=[])` raises; `kinds=["email","email"]` → `["email"]`; `kinds=["sms"]` raises.
  - API-level `test_existing_sender_unchanged_with_email_device` using the `live` fixture from `tests/conftest.py` (read it: `make_config`, `LiveServer`): add provider/device for email via config passed to the server, POST a notification to `user:alex` without kinds → exactly one delivery (`alex-phone`), and `routing` in `GET /v1/notifications/{id}` has no `kinds` key.
  - `test_targets_show_kind_and_opt_in`: `GET /v1/targets` device entries carry `kind` and `opt_in`.
- [ ] **Step 2:** Run `.venv/bin/python -m pytest -q -p no:warnings tests/test_routing.py` → fail.
- [ ] **Step 3:** Implement as above.
- [ ] **Step 4:** Run the new tests and the full suite → pass.
- [ ] **Step 5:** Commit `Route by delivery kind with per-device opt-in`.

---

### Task 2: Request body limit middleware

**Files:**
- Create: `notify/bodylimit.py`
- Modify: `notify/app.py` (add as the outermost middleware: `app.add_middleware(BodyLimit)` after the others so it wraps them)
- Test: `tests/test_bodylimit.py`

**Interfaces:**
- Produces: `class BodyLimit: def __init__(self, app: ASGIApp, default: int = 1 << 20, routes: dict[tuple[str, str], int] | None = None)`; default routes `{("POST", "/v1/notifications"): 15 << 20}`.

Behaviour: for `http` scopes, pick the cap; if a `content-length` header exceeds it → send `413` JSON `{"detail": "request body too large (max N bytes)"}` without calling the app. Otherwise wrap `receive` to count `http.request` body bytes; when the count exceeds the cap, stop reading and send 413 if the response has not started (track via wrapped `send`), else raise to abort. Implementation pattern: buffer is not needed — raise a private `_TooLarge` from the wrapped receive, catch it around `await self.app(...)`, and send 413 if `not started`.

- [ ] **Step 1: Failing tests** (pure ASGI, no server): build a tiny ASGI echo app that reads the full body and replies 200; wrap with `BodyLimit(app, default=100, routes={("POST","/v1/notifications"): 1000})`; drive it with `httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped))`:
  - content-length 101 on `/x` → 413; 100 → 200.
  - `/v1/notifications` POST 1000 → 200; 1001 → 413.
  - chunked body (pass an async generator as `content=`, so no content-length) of 150 bytes to `/x` → 413.
  - GET with no body → 200.
  - Live test: with the `live` fixture, POST `/mcp` with a 2 MiB body → 413.
- [ ] **Step 2:** Run → fail. **Step 3:** Implement. **Step 4:** Run file + full suite → pass.
- [ ] **Step 5:** Commit `Limit request body size`.

---

### Task 3: Attachment intake module

**Files:**
- Create: `notify/attachments.py`
- Modify: `notify/models.py` (`AttachmentIn`, `NotificationIn.attachments`), `notify/config.py` (`ServerConfig.attachment_url_hosts: list[str] = []`, `attachment_retention_days: int = Field(7, ge=1, le=90)`; validate host entries: either a CIDR (`ipaddress.ip_network(strict=False)`), or `host` / `host:port` matching `^[a-z0-9.-]+(:\d{1,5})?$` after lowercasing)
- Test: `tests/test_attachments.py`

**Interfaces:**
- Produces:
  ```python
  MAX_COUNT = 10; MAX_TOTAL = 10 * 1024 * 1024; MCP_INLINE_MAX = 256 * 1024
  FETCH_BUDGET_SECONDS = 15.0; MAX_REDIRECTS = 3

  class AttachmentError(ValueError): ...   # message is safe to return as 422

  @dataclass
  class PreparedAttachment:
      filename: str; content_type: str; data: bytes; source: str  # 'inline'|'url'
      source_host: str | None
      @property size(self) -> int; @property sha256(self) -> str

  def clean_filename(name: str | None, fallback: str) -> str
  def clean_content_type(ct: str | None, filename: str) -> str
  def decode_inline(items: list[AttachmentIn]) -> list[PreparedAttachment | None]  # None for URL items; checks count/size of inline part
  def check_url_allowed(url: str, allow: list[str]) -> None   # raises AttachmentError; host-level check only
  async def prepare(items: list[AttachmentIn], allow: list[str], http: httpx.AsyncClient,
                    resolve: Callable[[str], Awaitable[list[str]]] = default_resolve) -> list[PreparedAttachment]
  async def default_resolve(host: str) -> list[str]  # loop.getaddrinfo → unique IP strings
  def metadata(items: list[AttachmentIn]) -> list[dict]  # [{"filename","content_type","size" (int|None for url),"source"}] for planning
  ```
- `models.AttachmentIn` (`extra="forbid"`): `filename: str | None (≤ 255)`, `content_type: str | None (≤ 255)`, `data_base64: str | None (max_length=14_200_000)`, `url: str | None (≤ 2048, http(s) via check_http_url)`; model validator: exactly one of `data_base64`/`url`; `filename` required with `data_base64`. `NotificationIn.attachments: list[AttachmentIn] = Field(default_factory=list, max_length=10)`.

Rules:
- `clean_filename`: take basename after `/` and `\\`, replace control chars (`unicodedata.category(c).startswith("C")`) with `_`, strip, cut to 200 chars, fallback when empty or `.`/`..`.
- `clean_content_type`: lowercase, drop `;params`, strip; must match `^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$`; else `mimetypes.guess_type(filename)[0]` else `application/octet-stream`.
- Inline decode: `base64.b64decode("".join(s.split()), validate=True)`; error → `AttachmentError(f"attachment {i}: data_base64 is not valid base64")`. Running total > MAX_TOTAL → `AttachmentError("attachments exceed 10 MiB in total")`.
- Allowlist match: parse URL host (lowercase) and port (default 80/443). Entry with `:` port → host and port must match; plain host → host matches (any port); CIDR → host must be an IP literal inside it **or** every resolved IP inside it. Empty allowlist → `AttachmentError("URL attachments are not enabled; an admin can allow hosts under Settings")`.
- For every hop: allowlist check, then `ips = await resolve(host)` (IP literal → itself); refuse if any IP (after unwrapping `ipv4_mapped`) is in the deny ranges → `AttachmentError(f"attachment {i}: address not allowed")`. Request with `http.stream("GET", url)` on a client created as `httpx.AsyncClient(transport=..., trust_env=False, follow_redirects=False)` (constructed by app, passed in). 3xx with `location` → `urljoin`, count hops (> 3 → `AttachmentError(f"attachment {i}: too many redirects")`). Non-2xx → `AttachmentError(f"attachment {i}: fetch failed with HTTP {status}")`. Stream `aiter_bytes()`, abort if total exceeds remaining budget → size error. Whole `prepare` wrapped in `asyncio.timeout(FETCH_BUDGET_SECONDS)` → `AttachmentError("attachment URLs took longer than 15 s to fetch")`. `httpx.HTTPError` → `AttachmentError(f"attachment {i}: could not be fetched")`. Never put the URL in exception text or logs (host only).
- Filename for URL: given filename, else basename of final URL path, else `attachment-{i+1}`; content type: given, else response header, cleaned.

- [ ] **Step 1: Failing tests** (`pytest.mark.anyio` is not installed — use `asyncio.run(...)` inside sync tests, as other tests do; check `tests/test_api.py` for the existing style). Use `httpx.MockTransport` handlers and a fake `resolve` returning `["192.168.1.10"]` by default:
  - filename cleaning: `"../../etc/passwd"` → `"passwd"`; `"a\r\nb.txt"` → `"a__b.txt"`; `""` → fallback.
  - content type: `"Image/PNG; charset=x"` → `"image/png"`; `"bad type"` with `x.pdf` → `"application/pdf"`; unknown → `application/octet-stream`.
  - inline: valid base64 with line breaks decodes; invalid → AttachmentError mentioning index; 11 MiB total → error.
  - model: both `data_base64` and `url` → ValidationError; base64 without filename → ValidationError; `url="ftp://x"` → ValidationError.
  - allowlist empty → "not enabled"; host not listed → error; `ha.lan` listed allows `http://ha.lan:8123/x`; `ha.lan:8123` listed refuses port 80; CIDR `192.168.1.0/24` allows `http://192.168.1.10/x` and a hostname whose resolve returns `192.168.1.10`.
  - resolve returns `127.0.0.1` → "address not allowed"; returns `::ffff:127.0.0.1` → refused; returns `169.254.169.254` → refused.
  - redirect to a non-allowed host → refused; 4 redirects → "too many redirects"; redirect within allowlist → OK, filename from final path.
  - 404 → "HTTP 404"; streamed body bigger than remaining budget → size error.
  - budget: handler that sleeps (use `monkeypatch.setattr(attachments, "FETCH_BUDGET_SECONDS", 0.2)` and an async handler with `await asyncio.sleep(1)`) → timeout error.
  - error messages never contain the URL query (`?token=secret` absent from `str(e)`).
  - `ServerConfig(attachment_url_hosts=["HA.lan:8123", "10.0.0.0/8"])` normalises lowercase; `["bad host!"]` raises.
- [ ] **Step 2:** Run → fail. **Step 3:** Implement. **Step 4:** Run file + suite → pass.
- [ ] **Step 5:** Commit `Validate, decode and fetch notification attachments`.

---

### Task 4: Attachment storage, split submit, purge, retry gap

**Files:**
- Modify: `notify/db.py` (table in `SCHEMA` exactly as in spec §3 Storage, `PRAGMA journal_size_limit=67108864` at open), `notify/providers/base.py` (`AttachmentData`, `OutboundMessage.attachments`), `notify/core.py`, `notify/api.py`, `notify/mcp_server.py`, `notify/admin.py` (`send_test` → `submit_async`), `notify/app.py` (fetch client + semaphore wiring)
- Test: `tests/test_attachments_api.py`

**Interfaces:**
- Consumes: Task 3 `prepare`, `decode_inline`, `metadata`, `PreparedAttachment`, `AttachmentError`, `MCP_INLINE_MAX`.
- Produces:
  ```python
  # providers/base.py
  @dataclass
  class AttachmentData:
      filename: str; content_type: str; size: int | None; data: bytes | None = None
  OutboundMessage.attachments: list[AttachmentData] = field(default_factory=list)  # last field, default

  # core.py
  @dataclass
  class SubmitPlan: ...   # holds now, expires_at, route, nrow, deliveries (list of dicts as today)
  def plan_submit(self, client, req, attachment_meta: list[dict] | None = None) -> tuple[dict, bool] | SubmitPlan
      # returns the replay/dry-run result tuple exactly like submit() does today, or a SubmitPlan
  def commit(self, client, req, plan: SubmitPlan, attachments: list[PreparedAttachment]) -> tuple[dict, bool]
  async def submit_async(self, client, req, *, inline_limit: int | None = None) -> tuple[dict, bool]
  def submit(self, client, req) -> tuple[dict, bool]   # kept for internal callers; raises Invalid if req has URL attachments
  def message_for(self, n, delivery_id, provider, *, with_data: bool = False) -> OutboundMessage
  ```
- `NotifyService` gets `self.fetch_http: httpx.AsyncClient | None` and `self._intake = asyncio.Semaphore(1)` (create the semaphore lazily on first use inside `submit_async` to avoid binding to the wrong loop).

`submit_async` flow:
1. `meta = attachments.metadata(req.attachments)`; if `inline_limit` and the sum of inline decoded sizes (estimate `len(data_base64)*3//4`) > inline_limit → `Invalid("inline attachments over MCP are limited to 256 KiB; pass a url instead")`.
2. For URL items, call `check_url_allowed` for each (also for dry runs) → `Invalid(str(e))`.
3. `res = self.plan_submit(client, req, meta)`; if tuple → return it (replay or dry run; dry run still decodes inline via `decode_inline` to validate).
4. If `res.deliveries` and `req.attachments`: `async with sem: prepared = await attachments.prepare(req.attachments, self.config.server.attachment_url_hosts, self.fetch_http)`; then recompute each delivery's plan/gaps with real metadata (`[{"filename","content_type","size"}]` from prepared) — factor the per-device planning loop of today's `submit` into `_plan_deliveries(nrow, route, attachment_meta)` so both passes share it.
5. If no deliveries: prepared = [] but store metadata-only rows? Spec: "stores no attachment bytes (metadata only)" — store rows with `data=NULL` from `meta` (size may be NULL for URL → store 0). Keep simple: if no deliveries, store metadata rows with `data=NULL, purged_at=NULL, size=meta size or 0, sha256=""`.
6. `return self.commit(client, req, res, prepared)` (sync; wraps `AttachmentError` → `Invalid`).

`commit` = the transactional half of today's `submit` plus `INSERT INTO attachments(notification_id, idx, filename, content_type, size, sha256, source, source_host, data) VALUES (...)` per attachment inside the same `with self.db.tx()`. `submit` (sync) = `plan_submit` + `decode_inline` + `commit`, raising `Invalid` if any URL attachment is present.

`message_for(..., with_data)`: loads `SELECT idx, filename, content_type, size, data FROM attachments WHERE notification_id=? ORDER BY idx`; `AttachmentData(data=row["data"] if with_data else None)`; rows with `purged_at` set are **excluded** when `with_data=True`. When `n` is the in-memory `nrow` dict during planning, pass attachment meta explicitly instead: give `message_for` an optional `attachments: list[AttachmentData] | None` parameter used when provided. Worker `_send_one` calls `message_for(n, d["id"], d["provider"], with_data=True)`.

Output: `get()` (detail and summary forms) adds `"attachments": [{"filename","content_type","size","sha256","source","purged": purged_at is not None}]` — **only when the notification has attachments** (keeps 0.4.0 output identical otherwise).

Purge: in `retention_sweep`, after existing work, `UPDATE attachments SET data=NULL, purged_at=? WHERE data IS NOT NULL AND notification_id IN (SELECT id FROM notifications WHERE created_at < ?) AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.notification_id=attachments.notification_id AND d.state IN ('queued','sending'))` with cutoff `attachment_retention_days`. Return value unchanged (count of deleted notifications).

Retry gap: in `retry()`, before requeueing a FAILED delivery, count purged attachments for the notification; if > 0, set `capability_gaps_json` to the existing list + `f"attachments purged after {days} days: {k} not sent"` in the same `_transition` fields dict (`"capability_gaps_json": dumps(gaps)`).

Lease per provider: in `claim_due`, `lease = self.config.server.lease_seconds`; `p = self.providers.get(provider)`; `if p and p.send_deadline_seconds: lease = max(lease, p.send_deadline_seconds + 30)`. Add `send_deadline_seconds: float | None = None` to `Provider` in this task.

Callers: `api.submit` and `api` arr ingest → `await service(request).submit_async(client, body)` (keep `_errors` mapping; `Invalid` → 422). MCP `send_notification` gains `attachments: list[dict] | None = None` (validated via `NotificationIn`), calls `await svc().submit_async(client, req, inline_limit=MCP_INLINE_MAX)` directly (not through `call()` if `call` runs sync functions in a thread — read `mcp_server.py` and adapt). Admin `send_test` → `submit_async`. Registrations keep sync `submit`.

App: in `create_app` lifespan where the shared `httpx.AsyncClient` is built, also build `fetch_http = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False, timeout=httpx.Timeout(15.0))` and set `service.fetch_http`; close it on shutdown. Tests pass the fake transport, so the conftest fake handler must answer attachment hosts: extend `FakePushover.handler` in `tests/conftest.py` with `if request.url.host == "files.test": return httpx.Response(200, content=b"\x89PNG..." , headers={"content-type": "image/png"})` (and a `/big` path returning > 10 MiB, `/missing` → 404). Resolver: add `service.resolve` attribute defaulting to `attachments.default_resolve`; tests override with a fake returning `["192.168.1.10"]` because `files.test` does not resolve.

- [ ] **Step 1: Failing tests** in `tests/test_attachments_api.py` using the `live` fixture (read `tests/conftest.py` and `tests/test_api.py` for helpers):
  - inline attachment to `user:alex` → 202; `GET` shows `attachments[0]` with filename, size, sha256, `purged: false`; DB row has data.
  - URL attachment with allowlist empty → 422 "not enabled"; after setting `server.attachment_url_hosts=["files.test"]` (config in `make_config(attachment_url_hosts=[...])`) → 202 with `source: "url"`, filename from path.
  - `/missing` → 422 mentioning HTTP 404 and nothing stored (`GET /v1/notifications` count unchanged).
  - idempotent replay with the same key does not fetch again (count requests to `files.test` in the fake).
  - dry run with URL attachment → 200, no fetch.
  - all targets suppressed (severity below a channel floor) → no fetch, notification `suppressed`.
  - 0.4.0 shape: a notification without attachments has no `attachments` key in GET.
  - purge: insert notification with attachment, set its `created_at` 8 days back via direct DB update, call `service.retention_sweep()` → row `data IS NULL`, GET shows `purged: true`.
  - **Review Focus 5:** make Pushover fail permanently (queue `(400, {"status":0,"errors":["x"]})` in `FakePushover.responses`), wait for `failed`, purge as above, `POST /retry` → delivery `capability_gaps` contains `"attachments purged after 7 days: 1 not sent"`.
  - MCP: `send_notification` with inline 300 KiB → error mentioning 256 KiB (see `tests/test_mcp.py` for calling tools).
  - lease: `claim_due` with a provider having `send_deadline_seconds=120` sets `lease_until` ≥ now+150 s.
- [ ] **Step 2:** Run → fail. **Step 3:** Implement. **Step 4:** File + full suite → pass.
- [ ] **Step 5:** Commit `Store attachments with notifications; single async submit path`.

---

### Task 5: Pushover image attachment

**Files:**
- Modify: `notify/providers/pushover.py`
- Test: extend `tests/test_api.py` or new `tests/test_pushover_attachments.py`

**Interfaces:**
- Consumes: `OutboundMessage.attachments: list[AttachmentData]`.
- Produces: `PushoverProvider._image(msg) -> AttachmentData | None` (first with `content_type.startswith("image/")` and `size is not None and size <= 5242880`).

Changes: `MAX_ATTACHMENT = 5242880`. `capabilities()` adds `"attachments": {"max_count": 1, "max_bytes": MAX_ATTACHMENT, "types": ["image/*"]}`. `plan()`: if attachments: if any has `size is None` (URL, dry run) → gap `"attachments: type and size unknown until sent; pushover carries one image ≤ 5 MB"`; else `n = len(attachments) - (1 if image else 0)`; if n → gap `f"attachments: pushover carries one image ≤ 5 MB; {n} attachment(s) not sent"`. `build_form`: when `_image(msg)` has `data`: `form["attachment_base64"] = base64.b64encode(data).decode()`, `form["attachment_type"] = content_type`. `_redact`: replace `attachment_base64` by `attachment_bytes: len(decoded)` (compute as `len(v) * 3 // 4` approx or carry the size).

- [ ] **Step 1: Failing tests** (live fixture): inline PNG (any bytes, content_type image/png) to `user:alex` → fake pushover message form has `attachment_base64` decoding to the bytes and `attachment_type == "image/png"`; attempts' redacted summary has no base64 (check `GET /v1/notifications/{id}` attempts `request` lacks `attachment_base64`); a PDF only → no attachment field and a gap containing "1 attachment(s) not sent"; PNG of 5 MiB + 1 → gap, not sent; `GET /v1/providers` pushover capabilities has `attachments.max_count == 1`.
- [ ] **Step 2–4:** fail → implement → pass (file + suite).
- [ ] **Step 5:** Commit `Send the first image attachment with Pushover`.

---

### Task 6: Email provider

**Files:**
- Create: `notify/providers/email.py`
- Modify: `notify/providers/__init__.py` (register), `notify/providers/base.py` (`test_connection`)
- Test: `tests/test_email.py`; `requirements-dev.txt` add `aiosmtpd==1.4.6`

**Interfaces:**
- Consumes: `Provider` contract, `AttachmentData`, `send_deadline_seconds`.
- Produces: `EmailProvider` with `type="email"`, `kind="email"`, `label="Email (SMTP)"`, `max_concurrency=1`, `secret_settings=frozenset({"password"})`, `settings_schema`, `address_schema = {"to": {"label": "Email address", "kind": "text", "help": "One address; make several devices for several addresses."}}`; `build_message(msg, address) -> EmailMessage`; `_header_safe(s: str | None) -> str`; `async test_connection() -> dict`; base `Provider.test_connection` returns `{"supported": False}`.

`settings_schema` (exact; select options use the admin form's `[[value, label]]` format):

```python
settings_schema = {
    "host": {"label": "SMTP server", "kind": "text", "help": "Host name of your mail server or relay, e.g. smtp.example.org."},
    "port": {"label": "Port", "kind": "int", "default": 587},
    "security": {"label": "Encryption", "kind": "select", "default": "starttls",
                 "options": [["starttls", "STARTTLS (usually port 587)"], ["tls", "TLS from the start (usually port 465)"],
                             ["none", "None (local relay only)"]]},
    "username": {"label": "Username", "kind": "text", "help": "Leave empty if the server needs no login."},
    "password": {"label": "Password", "kind": "secret"},
    "from_address": {"label": "From address", "kind": "text"},
    "from_name": {"label": "From name", "kind": "text", "default": "hnl-notify"},
    "subject_prefix": {"label": "Subject prefix", "kind": "text", "help": "Optional, e.g. [home]."},
    "timeout_seconds": {"label": "Give up a send after (seconds)", "kind": "int", "default": 60},
    "verify_tls": {"label": "Check the server's TLS certificate", "kind": "check", "default": True},
}
```

`__init__`: read settings with defaults; clamp `timeout_seconds` to 10–300; `self.send_deadline_seconds = float(self.timeout)`; unknown keys (besides `type` and schema keys) → problem like Pushover. `config_problems`: host missing; from_address missing/invalid; security not in options; `security == "none" and username` → "login over an unencrypted connection would expose the password; use STARTTLS or TLS"; port 1–65535. `device_problems`: `to` missing/invalid (regex `^[^@\s]+@[^@\s]+\.[^@\s]+$`, no control chars).

`_header_safe`: `re.sub(r"\s+", " ", "".join(" " if unicodedata.category(c).startswith("C") else c for c in s or "")).strip()`.

`build_message` (policy `email.policy.SMTP`): headers as spec §2; subject = `_header_safe(prefix + " " + (title or first line))`, first line truncated to 120; `Message-ID` = `f"<{msg.delivery_id}@{domain}>"` where domain = from_address part after `@`; `Date` = `email.utils.format_datetime(msg.created_at)`. Body text: message + `\n\n` + link lines. HTML: `<div style="font-family:sans-serif">` + escaped message with `\n` → `<br>` + for each http(s) link `<p><a href="{escape(url, quote=True)}" style="display:inline-block;padding:8px 14px;background:#2563eb;color:#fff;border-radius:6px;text-decoration:none">{escape(label)}</a></p>`; non-http(s) links as escaped text. `set_content(text)`, `add_alternative(html, subtype="html")`, then for each attachment with data: `maintype, subtype = ct.split("/", 1)`; `add_attachment(data, maintype=..., subtype=..., filename=_header_safe(filename) or "attachment")`.

`send`: problems → retryable (as Pushover). Build message; `raw = msg.as_bytes()`; summary dict. `await asyncio.to_thread(self._send_sync, raw, from_address, to)` returning a `SendResult`. `_send_sync`: `deadline = time.monotonic() + self.timeout`; helper `left()` raises `TimeoutError("SMTP session exceeded N s")` if ≤ 0; context = `ssl.create_default_context()` (if not verify_tls: `check_hostname=False; verify_mode=CERT_NONE`); `SMTP_SSL(host, port, timeout=left(), context=ctx)` or `SMTP(host, port, timeout=left())`; `ehlo()`; if starttls: `if not s.has_extn("starttls"): raise smtplib.SMTPNotSupportedError("server does not offer STARTTLS")`, `s.starttls(context=ctx); s.ehlo()`; before each subsequent phase `s.sock.settimeout(left())`; SIZE: `lim = int(s.esmtp_features.get("size") or 0)`; if lim and len(raw) > lim → return permanent "message is X MB, server accepts at most Y MB"; login if username; `s.sendmail(from, [to], raw)`; `s.quit()` (ignore errors on quit); always `close()` in `finally`. Exception mapping exactly as spec §2 table (`SMTPAuthenticationError` before `SMTPResponseException`; `SMTPNotSupportedError` → retryable "server does not offer STARTTLS; choose another Encryption setting"; codes: `e.smtp_code`, ≥ 500 permanent). `SMTPRecipientsRefused` has `.recipients` dict → use the first code. Success → `provider_message_id` = Message-ID header.

`plan()`: observable `list(BASE_OBSERVABLE)`, tracking `none`; gaps: `require_ack` → "acknowledgement: email cannot report acknowledgement; not tracked"; actions without URL → `f"action {id!r} dropped (no url)"`; non-http(s) URL actions/url → "shown as text". `capabilities()` per spec §2.

`test_connection`: same connection sequence up to login, then `noop()`, `quit()` within the deadline, in `to_thread`; returns `{"supported": True, "ok": True, "tls": security, "size_limit": lim or None}` or `{"supported": True, "ok": False, "error": text}`; never includes the password.

- [ ] **Step 1: Failing tests** in `tests/test_email.py`:
  - `build_message` headers: Subject with prefix; From with name; Message-ID derived from delivery id; `Importance: high` at high; text and html parts present; http link is `<a href=...>`, `javascript:alert(1)` url is not in any `href`.
  - **Review Focus 1:** title `"Door\r\nBcc: evil@x.org 🚪"`, filename `"a\r\nb.png"`, content_type `"image/png"` → `build_message` does not raise; resulting bytes contain no `Bcc:` header line; subject contains `"Door Bcc: evil@x.org 🚪"`. Parametrise over a few hostile strings (`"\x00"`, `" "`, 2000-char title).
  - config problems: missing host; `security="none"` + username; bad from; good config → `[]`.
  - outcome mapping with a stub: `monkeypatch.setattr(email_mod.smtplib, "SMTP", FakeSMTP)` where `FakeSMTP` scripts behaviour (`has_extn`, `esmtp_features`, raising given exceptions from `sendmail`/`login`). Cases: accepted → success; `SMTPRecipientsRefused({"x": (550, b"no")})` → permanent; `(450, ...)` → retryable; `SMTPAuthenticationError(535, b"bad")` → retryable with `retry_after == 900`; no STARTTLS → retryable with "STARTTLS" in error; `ConnectionRefusedError` → retryable; SIZE 1000 with bigger message → permanent.
  - `test_connection` ok and auth failure (stub) → dict without password.
  - Integration with `aiosmtpd`: start `aiosmtpd.controller.Controller(handler, hostname="127.0.0.1", port=0)` (handler collects envelopes; `security="none"`, no username), build `EmailProvider("mail", {...}, http)` and `await provider.send(msg_with_png_attachment, {"to": "alex@example.org"})` → success; parse the received bytes with `email.message_from_bytes(..., policy=email.policy.default)` → one attachment with the PNG bytes.
  - **Review Focus 3** `test_slow_smtp_not_reclaimed`: live fixture with server `lease_seconds=10` and an email device; stub `_send_sync` to sleep 12 s then return success (monkeypatch on the class; use `time.sleep`); confirm exactly one call and one attempt row. If the full sleep makes the suite too slow, use `lease_seconds=10` minimum allowed and keep it — mark `@pytest.mark.slow` is not configured, so just accept ~12 s.
- [ ] **Step 2–4:** fail → implement → pass (file + suite).
- [ ] **Step 5:** Commit `Add email (SMTP) provider`.

---

### Task 7: Admin API for providers, devices and settings

**Files:**
- Modify: `notify/admin.py`
- Test: extend `tests/test_admin.py`

**Interfaces:**
- Consumes: `Provider.test_connection`, `EmailProvider.settings_schema`, `DeviceConfig.opt_in`, `ServerConfig.attachment_*`.
- Produces: `POST /admin/api/providers/{name}/test` → provider's `test_connection()` result (404 if not configured); `DeviceIn.opt_in: bool | None = None`; `ServerIn.attachment_retention_days: int | None`, `ServerIn.attachment_url_hosts: list[str] | None`; `GET /admin/api/state` (whatever the state endpoint is — read `admin.py` around line 200) device entries include `opt_in` and `kind`, provider entries include `kind`, `provider_types` entries include `kind` and the schemas (already), server includes the two new fields.

`put_provider` per schema key: `int` → `int(v)` (422 on failure, `None`/"" → default), `check` → `bool(v)`, `select` → must be in `[o[0] for o in spec["options"]]` else 422 `f"{spec['label']}: choose one of ..."`, `secret` blank → keep old (existing behaviour), `text` → `str(v).strip()`. `put_device` persists `opt_in` (omit the key when `None`). `put_server` sets the two fields when not `None` (validation happens through `NotifyConfig` on save — confirm `ConfigError` → 422 path).

- [ ] **Step 1: Failing tests** (follow existing admin test style, bearer admin token):
  - PUT provider `mail` type `email` with password → GET state shows `password: {set: true, hint: ...}` and no plaintext; PUT again with blank password → still set (**Review Focus 4**); `security: "bogus"` → 422; `verify_tls: false` stored as `False`; `port: "2525"` stored as `2525`.
  - PUT device `alex-email` provider `mail`, address `{"to": "alex@example.org"}`, `opt_in: false` → state shows `opt_in: false`, `kind: "email"`; omit opt_in → effective `true`.
  - PUT server `attachment_url_hosts: ["ha.lan:8123"]`, `attachment_retention_days: 3` → state reflects; `attachment_retention_days: 0` → 422.
  - POST `/admin/api/providers/mail/test` with `EmailProvider._test_sync` monkeypatched to return ok → 200 `{"supported": true, "ok": true, ...}`; pushover → `{"supported": false}`.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5:** Commit `Admin API: email provider settings, device opt-in, attachment settings`.

---

### Task 8: Admin UI

**Files:**
- Modify: `notify/admin_ui/app.js`, `notify/admin_ui/app.css` (only if needed), `notify/admin_ui/index.html` (only if nav is there)

**Interfaces:**
- Consumes: Task 7 API shapes.

Read `app.js` fully first. Required changes (spec §4 Admin UI):
1. Rename the Pushover page/nav to **Providers**: list every configured provider (name, type label, ready/problems, kind); "Add provider" with a type select from `state.provider_types` and a name field (default = type); edit form from `settings_schema` via `fieldHtml` passing `options` and `value` (`s.default` fallback; `check` values booleans); delete button (existing endpoint refuses when devices use it — show that error). Keep existing Pushover behaviour/text where it was Pushover-specific (help texts come from the schema).
2. "Test connection" button on provider forms when the type is `email` (or: always shown; result `{supported:false}` → hide). Show ok + size limit, or the error.
3. Device form: provider `select` of configured providers (default: the only one, or first); address fields re-rendered from that provider type's `address_schema` when the select changes; "check" button text neutral ("Check address"), only enabled when the provider supports `/check` (Pushover) — keep calling the existing endpoint and show `supported:false` gracefully; `opt_in` checkbox labelled "Only when an app asks for this kind explicitly", initial value = device's `opt_in` (effective), help "Email devices are opt-in by default so existing apps keep sending push only."
4. Dashboard pipeline: Provider hop lists every provider (not just Pushover).
5. Quick-send form: "Kinds" select: `""` default ("Default devices"), `push`, `email`, `push,email` ("Push and email") → send `kinds` (send through the admin test endpoint: add `kinds: list[str] | None` to `TestIn` in `admin.py` and pass through — include this small server change and a test in this task).
6. Settings: "Keep attachments for (days)" (int) and "Hosts allowed for attachment URLs" (textarea, one per line → list).
7. Notification detail view: if `attachments` present, list name, type, size (KB/MB), "purged" badge.

Verification (no browser automation available — puppeteer MCP is down): 
- [ ] **Step 1:** `node --check notify/admin_ui/app.js` passes.
- [ ] **Step 2:** start `.venv/bin/python scripts/dev_server.py --dir $SCRATCH/notify-dev --demo --password devpassword123` in the background; using `curl` with a session (log in via `/admin/api/login` as `app.js` does — read it for the exact call and the `X-Notify-Admin` header) exercise: create an email provider, a device with the picker data, settings save; `curl /admin/` and `/admin/app.js` return 200. Check that the CSP still holds (no inline scripts/styles added to `index.html`; inline `style=` attributes in JS-generated HTML are already used? — check the CSP header; if `style-src` forbids inline, use classes in `app.css`).
- [ ] **Step 3:** Commit `Admin UI: providers page, device provider picker, email settings`.

---

### Task 9: Docs, deployment config, version

**Files:**
- Modify: `docs/architecture.md`, `README.md`, `config/notify.example.yaml`, `compose.yaml` (`mem_limit`/`memswap_limit: 320m`, image tag `hnl-notify:0.5.0`), `pyproject.toml` (0.5.0), `notify/__init__.py` (0.5.0), `requirements-dev.txt` (if not done)

Content (spec §8): architecture: kinds/opt_in routing rule; table "What email can and cannot report" (accepted by SMTP = dispatched; no delivery/read/ack; no recall; errors 5xx permanent / 4xx retry / auth retry 15 min; SIZE; deadline + lease); attachments (limits, BLOB storage trade-off and that the DB file does not shrink, purge days, URL allowlist and deny ranges, URL not stored); body limits; diagram line `pushover | email (later ntfy/HA/webpush)`; REST table unchanged endpoints plus `kinds`/`attachments` note; MCP tool param note. README: setup of email in admin UI, `kinds` and attachment examples (curl JSON with base64 and with url), memory limit. Example config: `mail` provider (`type: email`, host, port, security, username, `password: ${SMTP_PASSWORD}`, from_address) and an `alex-email` device. Use anonymised names (`alex`, `notify.lan`, `example.org`) — the repo is anonymised for public release.

- [ ] **Step 1:** Edit files. **Step 2:** Full suite passes (version test uses `__version__`). `docker compose config -q` passes.
- [ ] **Step 3:** Commit `Document email and attachments; 0.5.0`.

---

### Task 10 (controller, not a subagent): verify, deploy, live check

- [ ] Full suite green; whole-branch review by a fresh reviewer.
- [ ] Back up DB: `sqlite3 data/notify.sqlite3 ".backup data/notify.sqlite3.bak.$(date +%Y%m%d)-pre-0.5.0"`.
- [ ] `docker compose up -d --build`; `scripts/smoke.sh`; `/health/ready`.
- [ ] Live Pushover attachment check (spec §3 Pushover: verify urlencoded `attachment_base64`): send a small PNG to `user:alex` via the API with an admin/sender token; confirm `dispatched` and (ask the owner in the notification) that the image showed. If Pushover rejects it, switch to multipart and redeploy.
- [ ] Update `~/documentation/projects.md` hnl-notify row.
- [ ] Notify the owner via hnl-notify with what is left for them: configure the SMTP server under Providers, add email devices, optionally allow attachment hosts.
