# Email replies to agents — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a person answer an agent's email by pressing Reply; hnl-notify 0.6.0 reads the reply from an IMAP mailbox, checks it, and hands exactly that text (plus attachments) to the agent waiting with `notify-wait`, or answers with an autoreply when nobody is waiting.

**Architecture:** Sending: `reply`/`in_reply_to` request fields, a `replies` capability, and `Reply-To` / `In-Reply-To` / `References` / `Auto-Submitted` headers in the email adapter. Receiving: `notify/providers/email_inbound.py` (the only IMAP code) feeds a provider-agnostic `notify/replies.py` (matching, storage, claiming, unclaimed sweep, autoreply through the normal pipeline), with pure parsing in `notify/replyparse.py`. A new worker loop polls every 30 s. Agents long-poll `POST /v1/notifications/{id}/replies/claim` through the stdlib CLI `scripts/notify-wait`.

**Tech Stack:** Python 3.12 (local venv may be 3.14), FastAPI, pydantic 2, SQLite, stdlib `imaplib` / `email` / `html.parser` / `urllib`; tests: pytest, `httpx.MockTransport`, fake mailbox and fake `imaplib` client; admin UI: vanilla JS.

**Spec:** `docs/superpowers/specs/2026-10-14-email-replies-design.md` (revision 2 + §0 result). Read it before your task; it is authoritative where this plan is terse.

## Global Constraints

- Tracked files stay anonymised: examples use `alex`, `example.org`, `notify.lan`, `mx.example.org`; never the owner's name, own domain, or LAN IPs.
- Notifications without `reply` / `in_reply_to` keep their exact 0.5.1 response shape (no new keys) and email headers.
- Public REST/MCP stay provider-agnostic; reply support is the capability key `replies` (`{"supported": bool, "note": str | None}`).
- Limits: inbound mail over 15 MiB (15728640 bytes, from `RFC822.SIZE`) is quarantined without fetching the body; reply text max 8000 characters after stripping; reply attachments max 10 files / 10 MiB (10485760 bytes).
- Timings: poll every 30 s; listener window 120 s; grace before unclaimed 600 s; at most one autoreply per conversation per 3600 s; claim `wait` 0–55 s (default 50).
- DKIM rule: only the **first** `Authentication-Results` header counts; its authserv-id must equal the admin-entered `authserv_id`; it must contain `dkim=pass` with `header.d` equal to or a parent of the From domain.
- Sender rule: bare From address (lower-cased) must equal the delivery's stored `recipient_address`.
- Delivery ids are `dlv_` + 22 hex chars (`notify/db.py:new_id`); our Message-ID is `<{delivery_id}@{domain of from_address}>`.
- Quarantined content is never stored beyond from/subject/Message-ID/reason, and never shown to agents.
- `notify-wait` never prints or logs the token; it writes attachments with exclusive create (`open(path, "xb")`) under sanitised names.
- Run tests with: `cd <repo> && .venv/bin/python -m pytest -q -p no:warnings`.
- Commits: author is the repo owner only — **no `Co-Authored-By` trailer, no "Generated with" line**. Short imperative messages like existing history.
- Match surrounding code style: dense, typed, `from __future__ import annotations`, short comments only where non-obvious.

## Review Focus

1. **Image-only reply** (the person deletes all text and attaches a screenshot): stored with empty text and the image; `notify-wait` prints the empty reply plus the saved path. Test in Task 3 (`test_image_only_reply`) and Task 7 (`test_cli_saves_attachment`).
2. **Two replies before the agent collects** (a quick correction "…and use blue"): claims return them oldest first, one per waiter run, and neither is swept while a listener is active. Test in Task 4 (`test_two_replies_in_order`).
3. **Reply without a Message-ID or with an odd charset** (some clients, `iso-8859-1` or an unknown charset): stored once (header-hash key), text decoded with replacement instead of a crash. Tests in Task 3 (`test_unknown_charset_decodes`, `test_message_key_without_message_id`).
4. **Notification to a person with push and email devices** with `reply=true`: push delivery gets the gap `replies: not supported on this path`, email gets `Reply-To`, and the reply still matches. Test in Task 2 (`test_reply_gap_on_push_and_recipient_on_email`).
5. **Service restarted while `notify-wait` is waiting**: connection errors are retried with the same waiter id until the timeout, ending with exit 2, not a crash. Test in Task 7 (`test_cli_unreachable_service_times_out`).

---

### Task 1: Data model, request fields, conversations

**Files:**
- Modify: `notify/db.py` (SCHEMA, `Database.__init__`)
- Modify: `notify/models.py` (`NotificationIn`, `EVENT_TYPES`)
- Modify: `notify/core.py` (`SYSTEM_CLIENT`, `plan_submit`, `commit`, `_notification_out`)
- Modify: `notify/registrations.py:28,40` (import `SYSTEM_CLIENT` from core)
- Test: `tests/test_replies_intake.py`

**Interfaces:**
- Produces: columns `notifications.reply_wanted INTEGER`, `in_reply_to TEXT`, `conversation_id TEXT`, `listener_seen_at TEXT`, `auto_submitted INTEGER`; `deliveries.recipient_address TEXT`; tables `replies`, `reply_attachments`, `reply_quarantine`.
- Produces: `NotificationIn.reply: bool`, `NotificationIn.in_reply_to: str | None`, private `NotificationIn._auto_submitted: bool`.
- Produces: `notify.core.SYSTEM_CLIENT: Client` (`id="hnl-notify"`, admin scope).
- Produces: notification output gains `reply`, `in_reply_to`, `conversation_id` only when `reply` is true or `in_reply_to` is set.
- Produces: event types `reply.received`, `reply.claimed`, `reply.unclaimed`, `reply.quarantined`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_replies_intake.py`:

```python
from __future__ import annotations

from notify.db import Database


def test_reply_fields_and_conversation(live):
    with live.client() as c:
        r = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "pick", "reply": True})
        assert r.status_code == 202, r.text
        first = r.json()
        assert first["reply"] is True and first["conversation_id"] == first["id"] and first["in_reply_to"] is None
        r = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "next", "in_reply_to": first["id"]})
        assert r.status_code == 202, r.text
        nxt = r.json()
        assert nxt["conversation_id"] == first["id"] and nxt["in_reply_to"] == first["id"] and nxt["reply"] is False
        r = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "third", "in_reply_to": nxt["id"]})
        assert r.json()["conversation_id"] == first["id"]


def test_plain_notification_shape_unchanged(live):
    with live.client() as c:
        out = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "hi"}).json()
    assert not {"reply", "in_reply_to", "conversation_id"} & set(out)


def test_in_reply_to_other_client_or_unknown(live):
    with live.client("other") as o:
        theirs = o.post("/v1/notifications", json={"targets": ["user:alex"], "message": "x"}).json()["id"]
    with live.client() as c:
        for ref in (theirs, "ntf_doesnotexist"):
            r = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "y", "in_reply_to": ref})
            assert r.status_code == 422 and "in_reply_to" in r.text


def test_system_client_may_follow_up_any_notification(live):
    from notify.core import SYSTEM_CLIENT
    from notify.models import NotificationIn
    with live.client("other") as o:
        theirs = o.post("/v1/notifications", json={"targets": ["user:alex"], "message": "x"}).json()["id"]
    req = NotificationIn(targets=["user:alex"], message="auto", in_reply_to=theirs)
    req._auto_submitted = True
    out, _ = live.call(lambda: live.service.submit(SYSTEM_CLIENT, req))
    row = live.service.db.one("SELECT auto_submitted, conversation_id FROM notifications WHERE id=?", (out["id"],))
    assert row["auto_submitted"] == 1 and row["conversation_id"] == theirs


def test_migration_adds_columns_to_old_database(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE notifications (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, idempotency_key TEXT,"
                      " source TEXT NOT NULL, title TEXT, message TEXT NOT NULL, severity TEXT NOT NULL, url TEXT,"
                      " url_title TEXT, actions_json TEXT NOT NULL DEFAULT '[]', data_json TEXT,"
                      " tags_json TEXT NOT NULL DEFAULT '[]', targets_json TEXT NOT NULL,"
                      " require_ack INTEGER NOT NULL DEFAULT 0, expires_at TEXT, status TEXT NOT NULL,"
                      " routing_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);")
    con.close()
    db = Database(path)
    cols = {r[1] for r in db.query("PRAGMA table_info(notifications)")}
    assert {"reply_wanted", "in_reply_to", "conversation_id", "listener_seen_at", "auto_submitted"} <= cols
    assert "recipient_address" in {r[1] for r in db.query("PRAGMA table_info(deliveries)")}
    db.close()
```

The test uses `live.call`, which runs a function on the server's event loop (every service method that commits calls `_after_commit`, which touches asyncio events and must run on that loop). Add it to `tests/conftest.py` now:

In `tests/conftest.py`, add `import asyncio` and `import inspect` to the imports, and add this method to `LiveServer` after `client`:

```python
    def call(self, fn: Callable[[], Any], timeout: float = 70.0) -> Any:
        """Run fn (sync, or returning an awaitable) on the server's event loop, as the app itself would."""
        async def run() -> Any:
            res = fn()
            return await res if inspect.isawaitable(res) else res
        return asyncio.run_coroutine_threadsafe(run(), self.app.state.loop).result(timeout)
```

In `notify/app.py` `lifespan`, right after `holder["service"] = service`, add:

```python
        app.state.loop = asyncio.get_running_loop()
```

and add `import asyncio` to the imports of `notify/app.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_replies_intake.py`
Expected: FAIL (422 "extra inputs are not permitted" for `reply`, ImportError for `SYSTEM_CLIENT`).

- [ ] **Step 3: Schema and migration**

In `notify/db.py`, append to `SCHEMA` (before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS replies (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    notification_id TEXT NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    delivery_id TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    from_address TEXT NOT NULL,
    subject TEXT,
    received_at TEXT NOT NULL,
    text TEXT NOT NULL,
    stripped INTEGER NOT NULL DEFAULT 1,
    truncated INTEGER NOT NULL DEFAULT 0,
    dropped_attachments_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL,
    claimed_at TEXT,
    claimed_by TEXT,
    waiter_id TEXT,
    autoreply_notification_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_replies_conversation ON replies(conversation_id, state, received_at);

CREATE TABLE IF NOT EXISTS reply_attachments (
    reply_id TEXT NOT NULL REFERENCES replies(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    data BLOB,
    purged_at TEXT,
    PRIMARY KEY (reply_id, idx)
);

CREATE TABLE IF NOT EXISTS reply_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    from_address TEXT,
    subject TEXT,
    message_id TEXT,
    reason TEXT NOT NULL,
    notification_id TEXT
);
```

In `Database.__init__`, replace the `_add_missing_columns` call with:

```python
        self._add_missing_columns({
            "registrations": {"decision_sha256": "TEXT"},
            "notifications": {"reply_wanted": "INTEGER NOT NULL DEFAULT 0", "in_reply_to": "TEXT",
                              "conversation_id": "TEXT", "listener_seen_at": "TEXT",
                              "auto_submitted": "INTEGER NOT NULL DEFAULT 0"},
            "deliveries": {"recipient_address": "TEXT"},
        })
        # after the columns exist (an index in SCHEMA would fail on a pre-0.6.0 database)
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_notifications_conversation ON notifications(conversation_id)")
```

The migration test creates only `notifications`; `CREATE TABLE IF NOT EXISTS deliveries` in SCHEMA creates the full new table, and `_add_missing_columns` skips columns that exist.

- [ ] **Step 4: Request fields and events**

In `notify/models.py`, add `PrivateAttr` to the pydantic import, append to `EVENT_TYPES`:

```python
    "reply.received",
    "reply.claimed",
    "reply.unclaimed",
    "reply.quarantined",
```

and add to `NotificationIn` after `kinds`:

```python
    reply: bool = Field(default=False, description="Email: the recipient can answer by replying; collect replies"
                                                   " with POST /v1/notifications/{id}/replies/claim")
    in_reply_to: str | None = Field(default=None, max_length=64,
                                    description="Notification id this one follows up: threads the email and joins its conversation")
    _auto_submitted: bool = PrivateAttr(default=False)  # internal: the service's own automatic replies
```

- [ ] **Step 5: SYSTEM_CLIENT in core, intake and output**

In `notify/core.py`, after the `Client` dataclass:

```python
SYSTEM_CLIENT = Client("hnl-notify", frozenset({Scope.admin}))  # the service's own notifications
```

In `notify/registrations.py`, delete line 40 (`SYSTEM_CLIENT = Client(...)`) and change line 28 to:

```python
from .core import SYSTEM_CLIENT, Client, Invalid
```

In `NotifyService.plan_submit`, after the `try: route = ... except RoutingError` block, add:

```python
        conversation_id = None
        if req.in_reply_to:
            prev = self.db.one("SELECT id, client_id, conversation_id FROM notifications WHERE id=?", (req.in_reply_to,))
            if prev is None or not self._visible(client, prev["client_id"]):
                raise Invalid(f"in_reply_to: notification {req.in_reply_to} not found")
            conversation_id = prev["conversation_id"] or prev["id"]
```

and right after the `nrow = {...}` literal:

```python
        nrow.update({"reply_wanted": int(req.reply), "in_reply_to": req.in_reply_to,
                     "conversation_id": conversation_id or nrow["id"], "auto_submitted": int(req._auto_submitted)})
```

In `commit`, replace the `INSERT INTO notifications` statement with:

```python
                c.execute(
                    "INSERT INTO notifications(id, client_id, idempotency_key, source, title, message, severity, url,"
                    " url_title, actions_json, data_json, tags_json, targets_json, require_ack, expires_at, status,"
                    " routing_json, created_at, updated_at, reply_wanted, in_reply_to, conversation_id, auto_submitted)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (nid, client.id, req.idempotency_key, source, req.title, req.message, req.severity.value,
                     req.url, req.url_title, nrow["actions_json"], nrow["data_json"], nrow["tags_json"],
                     nrow["targets_json"], nrow["require_ack"], nrow["expires_at"], status, dumps(routing),
                     nrow["created_at"], nrow["updated_at"], nrow["reply_wanted"], nrow["in_reply_to"],
                     nrow["conversation_id"], nrow["auto_submitted"]))
```

In `_notification_out`, add as the last entry of the returned dict (after `**self._attachments_out(n["id"])`):

```python
            **({"reply": bool(n["reply_wanted"]), "in_reply_to": n["in_reply_to"],
                "conversation_id": n["conversation_id"] or n["id"]}
               if n["reply_wanted"] or n["in_reply_to"] else {}),
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_replies_intake.py tests/test_registrations.py tests/test_api.py`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add notify/db.py notify/models.py notify/core.py notify/registrations.py notify/app.py tests/conftest.py tests/test_replies_intake.py
git commit -m "Replies: schema, reply/in_reply_to fields, conversations"
```

---

### Task 2: Sending side — capability, recipient address, threading headers

**Files:**
- Modify: `notify/providers/base.py` (`OutboundMessage`, `Provider.recipient`)
- Modify: `notify/providers/email.py` (settings, capabilities, plan, `build_message`, `message_id`, `recipient`)
- Modify: `notify/core.py` (`message_for`, `_thread_delivery_ids`, `_plan_deliveries`, `claim_due`)
- Modify: `notify/worker.py` (`_send_one` passes `device=`)
- Modify: `tests/conftest.py` (email fixtures)
- Test: `tests/test_email_replies.py`

**Interfaces:**
- Consumes: Task 1 columns (`reply_wanted`, `in_reply_to`, `auto_submitted`, `recipient_address`).
- Produces: `OutboundMessage.reply: bool = False`, `OutboundMessage.thread_delivery_ids: list[str]` (oldest first), `OutboundMessage.auto_submitted: bool = False`.
- Produces: `Provider.recipient(address: dict) -> str | None` (base returns `None`).
- Produces: `EmailProvider.replies_problems() -> list[str]`, `EmailProvider.replies_ready: bool` (property), `EmailProvider.message_id(delivery_id: str) -> str`, attributes `reply_address`, `imap_host`, `imap_port`, `imap_user`, `imap_password`, `authserv_id`, `reply_inbox`, `reply_processed`, `reply_quarantine`.
- Produces: `NotifyService.message_for(n, delivery_id, provider, *, with_data=False, atts=None, device=None)`.
- Produces (tests): `tests/conftest.py` — `MAIL`, `REPLY_MAILBOX`, `make_email_config(reply_mailbox=True, **server)`, fixtures `sent_mail` (`SentMail` with `.raws`, `.last()`), `email_factory(reply_mailbox=True, **server_cfg)`, `email_live`. Every server from `email_factory` has `srv.mailbox` (a `FakeMailbox` from `tests/mailfactory.py`, created in Task 5; until then the attribute is set to `None`).
- Constants: `REPLY_FOOTER = "Reply to this email to answer the agent."` in `notify/providers/email.py`.

- [ ] **Step 1: Test fixtures**

Append to `tests/conftest.py` (add `import email`, `import email.policy` to the imports):

```python
MAIL = {"type": "email", "host": "mail.test", "port": 25, "security": "none", "from_address": "hnl@example.org"}
REPLY_MAILBOX = {"reply_address": "replies@example.org", "imap_host": "imap.test", "imap_user": "replies",
                 "imap_password": "imap-secret", "authserv_id": "mx.example.org"}


def make_email_config(reply_mailbox: bool = True, **server: Any) -> NotifyConfig:
    d = make_config(**server).model_dump(mode="json", exclude_none=True)
    d["providers"]["mail"] = {**MAIL, **(REPLY_MAILBOX if reply_mailbox else {})}
    d["devices"]["alex-email"] = {"provider": "mail", "owner": "alex", "address": {"to": "alex@example.org"}}
    d["users"]["alex"]["devices"] = ["alex-phone", "alex-email"]
    return NotifyConfig.model_validate(d)


@dataclass
class SentMail:
    raws: list[bytes] = field(default_factory=list)

    def last(self) -> Any:
        return email.message_from_bytes(self.raws[-1], policy=email.policy.default)


@pytest.fixture
def sent_mail(monkeypatch) -> SentMail:
    from notify.providers.base import SendResult
    from notify.providers.email import EmailProvider
    box = SentMail()

    def capture(self: Any, raw: bytes, sender: str, to: str) -> SendResult:
        box.raws.append(raw)
        return SendResult(outcome="success")
    monkeypatch.setattr(EmailProvider, "_send_sync", capture)
    return box


@pytest.fixture
def email_factory(tmp_path, fake, sent_mail):
    servers: list[LiveServer] = []

    def make(reply_mailbox: bool = True, **server_cfg: Any) -> LiveServer:
        app = create_app(make_email_config(reply_mailbox, **server_cfg),
                         db_path=str(tmp_path / f"mail{len(servers)}.sqlite3"),
                         key_path=str(tmp_path / "master.key"), transport=httpx.MockTransport(fake.handler))
        srv = LiveServer(app, fake)
        srv.start()
        srv.service.resolve = fake_resolve
        srv.mailbox = None  # replaced by a FakeMailbox once tests/mailfactory.py exists (Task 5)
        servers.append(srv)
        return srv

    yield make
    for s in servers:
        s.stop()


@pytest.fixture
def email_live(email_factory) -> LiveServer:
    return email_factory()
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_email_replies.py`:

```python
from __future__ import annotations

import email
import email.policy
from datetime import UTC, datetime
from typing import Any

from notify.models import INTERACTED, Severity
from notify.providers.base import OutboundMessage
from notify.providers.email import REPLY_FOOTER, EmailProvider
from tests.conftest import MAIL, REPLY_MAILBOX, wait_for

TO = {"to": "alex@example.org"}
DLV, DLV_A, DLV_B = ("dlv_" + "0" * 20 + x for x in ("aa", "0a", "0b"))


def prov(**over: Any) -> EmailProvider:
    return EmailProvider("mail", {**MAIL, **REPLY_MAILBOX, **over}, None)  # type: ignore[arg-type]


def msg(**over: Any) -> OutboundMessage:
    m = OutboundMessage("ntf_1", DLV, "Pick", "A or B?", Severity.normal, None, None, [], [],
                        False, datetime(2026, 10, 14, 12, 0, tzinfo=UTC), None)
    for k, v in over.items():
        setattr(m, k, v)
    return m


def parsed(m: OutboundMessage, p: EmailProvider | None = None):
    return email.message_from_bytes((p or prov()).build_message(m, TO).as_bytes(), policy=email.policy.default)


def test_reply_to_and_footer_when_configured():
    em = parsed(msg(reply=True))
    assert em["Reply-To"] == "replies@example.org"
    assert REPLY_FOOTER in em.get_body(("plain",)).get_content()
    assert REPLY_FOOTER in em.get_body(("html",)).get_content()


def test_no_reply_headers_without_reply_or_mailbox():
    plain = parsed(msg())
    assert plain["Reply-To"] is None and REPLY_FOOTER not in plain.get_body(("plain",)).get_content()
    bare = EmailProvider("mail", dict(MAIL), None)  # type: ignore[arg-type]
    em = parsed(msg(reply=True), bare)
    assert em["Reply-To"] is None and REPLY_FOOTER not in em.get_body(("plain",)).get_content()


def test_threading_and_auto_submitted_headers():
    em = parsed(msg(thread_delivery_ids=[DLV_A, DLV_B], auto_submitted=True))
    assert em["In-Reply-To"] == f"<{DLV_B}@example.org>"
    assert em["References"] == f"<{DLV_A}@example.org> <{DLV_B}@example.org>"
    assert em["Auto-Submitted"] == "auto-replied"
    assert em["Message-ID"] == f"<{DLV}@example.org>"


def test_capability_plan_and_problems():
    assert prov().capabilities()["replies"] == {"supported": True, "note": None}
    assert prov().recipient(TO) == "alex@example.org"
    assert INTERACTED in prov().plan(msg(reply=True), TO).observable_states
    assert INTERACTED not in prov().plan(msg(), TO).observable_states
    bare = EmailProvider("mail", dict(MAIL), None)  # type: ignore[arg-type]
    assert bare.capabilities()["replies"] == {"supported": False, "note": "reply mailbox not configured"}
    assert INTERACTED not in bare.plan(msg(reply=True), TO).observable_states
    half = prov(imap_password="", authserv_id="")
    assert not half.replies_ready
    assert half.capabilities()["replies"]["note"] == "imap_password is not set; authserv_id is not set"
    assert half.config_problems() == []  # half-configured replies never stop normal email


def test_reply_gap_on_push_and_recipient_on_email(email_live):
    with email_live.client() as c:
        r = c.post("/v1/notifications", json={"targets": ["user:alex"], "message": "pick", "reply": True,
                                               "kinds": ["push", "email"]})
        assert r.status_code == 202, r.text
        nid = r.json()["id"]
        ds = {d["device"]: d for d in r.json()["deliveries"]}
        assert ds["alex-phone"]["capability_gaps"] == ["replies: not supported on this path"]
        assert ds["alex-email"]["capability_gaps"] == []
        assert "interacted" in ds["alex-email"]["observable_states"]
        wait_for(lambda: all(d["state"] == "dispatched" for d in c.get(f"/v1/notifications/{nid}").json()["deliveries"]))
    row = email_live.service.db.one("SELECT recipient_address FROM deliveries WHERE id=?", (ds["alex-email"]["id"],))
    assert row["recipient_address"] == "alex@example.org"


def test_follow_up_threads_under_first_mail(email_live, sent_mail):
    with email_live.client() as c:
        first = c.post("/v1/notifications", json={"targets": ["device:alex-email"], "message": "one",
                                                   "kinds": ["email"], "reply": True}).json()
        wait_for(lambda: len(sent_mail.raws) == 1)
        c.post("/v1/notifications", json={"targets": ["device:alex-email"], "message": "two", "kinds": ["email"],
                                          "in_reply_to": first["id"]})
        wait_for(lambda: len(sent_mail.raws) == 2)
    first_dlv = first["deliveries"][0]["id"]
    em = sent_mail.last()
    assert em["In-Reply-To"] == f"<{first_dlv}@example.org>" and em["Reply-To"] is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_email_replies.py`
Expected: FAIL (ImportError `REPLY_FOOTER`).

- [ ] **Step 4: Base contract**

In `notify/providers/base.py`, add to `OutboundMessage` after `attachments`:

```python
    reply: bool = False
    thread_delivery_ids: list[str] = field(default_factory=list)  # earlier deliveries to this device, oldest first
    auto_submitted: bool = False
```

and to `Provider`, after `device_problems`:

```python
    def recipient(self, address: dict[str, Any]) -> str | None:
        """The concrete address a delivery goes to, when replies can come back from it."""
        return None
```

- [ ] **Step 5: Email adapter**

In `notify/providers/email.py`:

Add `INTERACTED` to the `..models` import (`from ..models import INTERACTED, Severity`).

Below `MAX_ATTACHMENT_BYTES`, add:

```python
REPLY_FOOTER = "Reply to this email to answer the agent."
_REPLY_KEYS = ("reply_address", "imap_host", "imap_user", "imap_password", "authserv_id")
```

Extend `_KEYS` with `"reply_address", "imap_host", "imap_port", "imap_user", "imap_password", "authserv_id", "reply_inbox", "reply_processed", "reply_quarantine"`.

Change `secret_settings = frozenset({"password", "imap_password"})`.

Append to `settings_schema` (after `verify_tls`):

```python
        "reply_address": {"label": "Reply mailbox address", "kind": "text",
                          "help": "Optional. Set this and the IMAP fields to let people answer agents by replying, e.g. replies@example.org."},
        "imap_host": {"label": "Reply mailbox IMAP server", "kind": "text", "help": "TLS from the start (IMAPS)."},
        "imap_port": {"label": "IMAP port", "kind": "int", "default": 993},
        "imap_user": {"label": "IMAP username", "kind": "text"},
        "imap_password": {"label": "IMAP password", "kind": "secret"},
        "authserv_id": {"label": "Mail server name in Authentication-Results", "kind": "text",
                        "help": "The name your mail server writes first in the Authentication-Results header it adds. Test connection shows what it sees."},
        "reply_inbox": {"label": "Folder to read", "kind": "text", "default": "INBOX"},
        "reply_processed": {"label": "Folder for handled replies", "kind": "text", "default": "Processed"},
        "reply_quarantine": {"label": "Folder for rejected mail", "kind": "text", "default": "Quarantine"},
```

In `__init__`, before `self._unknown = ...`:

```python
        self.reply_address = str(settings.get("reply_address") or "").strip()
        self.imap_host = str(settings.get("imap_host") or "").strip()
        self.imap_port = _int(settings.get("imap_port", 993), 0)
        self.imap_user = str(settings.get("imap_user") or "")
        self.imap_password = str(settings.get("imap_password") or "")
        self.authserv_id = str(settings.get("authserv_id") or "").strip()
        self.reply_inbox = str(settings.get("reply_inbox") or "INBOX")
        self.reply_processed = str(settings.get("reply_processed") or "Processed")
        self.reply_quarantine = str(settings.get("reply_quarantine") or "Quarantine")
```

After `device_problems`, add:

```python
    def recipient(self, address: dict[str, Any]) -> str | None:
        return str(address.get("to") or "").strip().lower() or None

    def replies_problems(self) -> list[str]:
        """Why replies are off; never part of config_problems, so ordinary email keeps working."""
        if not any(getattr(self, k) for k in _REPLY_KEYS):
            return ["reply mailbox not configured"]
        problems = [f"{k} is not set" for k in _REPLY_KEYS if not getattr(self, k)]
        if self.reply_address and not _valid_address(self.reply_address):
            problems.append("reply_address is not a valid email address")
        if not 1 <= self.imap_port <= 65535:
            problems.append("imap_port must be between 1 and 65535")
        return problems

    @property
    def replies_ready(self) -> bool:
        return not self.replies_problems()

    def message_id(self, delivery_id: str) -> str:
        return f"<{delivery_id}@{self.from_address.rpartition('@')[2]}>"
```

In `capabilities()`, add before `"severities"`:

```python
            "replies": {"supported": self.replies_ready,
                        "note": None if self.replies_ready else "; ".join(self.replies_problems())},
```

In `plan()`, replace the final `return DeliveryPlan(...)` with:

```python
        observable = list(BASE_OBSERVABLE) + ([INTERACTED] if msg.reply and self.replies_ready else [])
        return DeliveryPlan(tracking="none", observable_states=observable, capability_gaps=gaps)
```

In `build_message()`, replace the `out["Message-ID"] = ...` line with:

```python
        out["Message-ID"] = self.message_id(msg.delivery_id)
        if msg.thread_delivery_ids:
            refs = [self.message_id(i) for i in msg.thread_delivery_ids]
            out["In-Reply-To"] = refs[-1]
            out["References"] = " ".join(refs)
        if msg.auto_submitted:
            out["Auto-Submitted"] = "auto-replied"
        reply_on = msg.reply and self.replies_ready
        if reply_on:
            out["Reply-To"] = self.reply_address
```

Then, in the same method, change the text/HTML assembly: after `text += "\n\n" + ...links...` block add

```python
        if reply_on:
            text += "\n\n" + REPLY_FOOTER
```

and replace `parts.append("</div>")` with:

```python
        if reply_on:
            parts.append(f'<p style="color:#666;font-size:90%">{html.escape(REPLY_FOOTER)}</p>')
        parts.append("</div>")
```

- [ ] **Step 6: Core: gap, thread ids, recipient address**

In `notify/core.py` `message_for`, add the keyword `device: str | None = None` to the signature and replace the `return OutboundMessage(...)` with:

```python
        return OutboundMessage(
            notification_id=n["id"], delivery_id=delivery_id, title=n["title"], message=n["message"],
            severity=Severity(n["severity"]), url=n["url"], url_title=n["url_title"],
            actions=loads(n["actions_json"], []), tags=loads(n["tags_json"], []),
            require_ack=bool(n["require_ack"]), created_at=parse_iso(n["created_at"]),  # type: ignore[arg-type]
            expires_at=parse_iso(n["expires_at"]), callback_url=self.callback_url(provider),
            attachments=atts, reply=bool(n["reply_wanted"]), auto_submitted=bool(n["auto_submitted"]),
            thread_delivery_ids=self._thread_delivery_ids(n["in_reply_to"], device) if device else [],
        )

    def _thread_delivery_ids(self, ref: str | None, device: str) -> list[str]:
        """Deliveries to the same device along the in_reply_to chain, oldest first (for email threading)."""
        ids: list[str] = []
        seen: set[str] = set()
        while ref and ref not in seen and len(seen) < 20:
            seen.add(ref)
            prev = self.db.one("SELECT in_reply_to FROM notifications WHERE id=?", (ref,))
            if prev is None:
                break
            d = self.db.one("SELECT id FROM deliveries WHERE notification_id=? AND device=? ORDER BY created_at, id"
                            " LIMIT 1", (ref, device))
            if d is not None:
                ids.append(d["id"])
            ref = prev["in_reply_to"]
        return ids[::-1]
```

In `_plan_deliveries`, after `gaps = list(plan.capability_gaps)`:

```python
            if nrow.get("reply_wanted"):
                replies = provider.capabilities().get("replies") or {}
                if not replies.get("supported"):
                    gaps.append(f"replies: {replies.get('note') or 'not supported on this path'}")
```

In `claim_due`, replace the loop body `for d in rows:` with:

```python
            for d in rows:
                dev = self.config.devices.get(d["device"])
                recipient = p.recipient(dev.address) if p is not None and dev is not None else None
                c.execute("UPDATE deliveries SET state=?, attempts=attempts+1, lease_until=?, recipient_address=?,"
                          " updated_at=? WHERE id=?", (SENDING, in_seconds(lease), recipient, now, d["id"]))
                claimed.append(c.execute("SELECT * FROM deliveries WHERE id=?", (d["id"],)).fetchone())
```

In `notify/worker.py` `_send_one`, change the `message_for` call to:

```python
                msg = svc.message_for(n, d["id"], d["provider"], with_data=True, device=d["device"])
```

- [ ] **Step 7: Run tests**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_email_replies.py tests/test_email.py tests/test_attachments.py tests/test_api.py`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add notify/providers/base.py notify/providers/email.py notify/core.py notify/worker.py tests/conftest.py tests/test_email_replies.py
git commit -m "Replies: Reply-To, threading headers, replies capability, recipient address"
```

---

### Task 3: Reply parsing (pure functions)

**Files:**
- Create: `notify/replyparse.py`
- Create: `tests/mailfactory.py` (raw reply builder; `FakeMailbox` is added in Task 5)
- Test: `tests/test_replyparse.py`

**Interfaces:**
- Produces in `notify/replyparse.py`:
  - constants `MAX_TEXT = 8000`, `MAX_ATTACHMENTS = 10`, `MAX_ATTACHMENT_BYTES = 10485760`
  - `parse_headers(raw: bytes) -> EmailMessage`, `parse_message(raw: bytes) -> EmailMessage`
  - `header(msg, name: str) -> str` (never raises)
  - `thread_delivery_ids(msg) -> list[str]`
  - `from_address(msg) -> str` (lower-cased bare address, `""` if none)
  - `message_key(msg, raw_headers: bytes) -> str`
  - `is_automatic(msg) -> bool`
  - `dkim_passes(msg, authserv_id: str, from_domain: str) -> bool`
  - `strip_quotes(text: str) -> tuple[str, bool]`
  - `body_text(msg) -> str`
  - `@dataclass ReplyAttachment(filename: str, content_type: str, data: bytes)`
  - `extract_attachments(msg) -> tuple[list[ReplyAttachment], list[str]]`
  - `@dataclass ParsedReply(from_address, subject, text, stripped, truncated, attachments, dropped)`
  - `parse_reply(raw: bytes) -> ParsedReply`
- Produces in `tests/mailfactory.py`: `reply_raw(delivery_id: str, *, sender="alex@example.org", text=..., auth_results=None, extra=(), message_id="<r1@mail.example.org>", attachment=None, html=None) -> bytes`, `split_headers(raw: bytes) -> bytes`.

- [ ] **Step 1: Test helper**

Create `tests/mailfactory.py`:

```python
"""Builders for inbound reply mail used across reply tests."""

from __future__ import annotations

from email import policy
from email.message import EmailMessage
from typing import Iterable

GMAIL_QUOTE = "On Wed, 14 Oct 2026 at 09:39, hnl-notify <hnl@example.org> wrote:\n> Pick A or B\n"


def reply_raw(delivery_id: str, *, sender: str = "alex@example.org", text: str = "Go with B.\n\n" + GMAIL_QUOTE,
              auth_results: list[str] | None = None, extra: Iterable[tuple[str, str]] = (),
              message_id: str | None = "<r1@mail.example.org>", attachment: bytes | None = None,
              html: str | None = None) -> bytes:
    """A reply as the receiving server stores it: its own Authentication-Results first."""
    m = EmailMessage()
    domain = sender.rpartition("@")[2]
    for ar in (auth_results if auth_results is not None else
               [f"mx.example.org; dkim=pass (2048-bit key) header.d={domain} header.s=s1; spf=pass smtp.mailfrom={sender}"]):
        m["Authentication-Results"] = ar
    m["From"] = f"Alex <{sender}>"
    m["To"] = "replies@example.org"
    m["Subject"] = "Re: [home] Pick a design"
    if message_id:
        m["Message-ID"] = message_id
    m["In-Reply-To"] = f"<{delivery_id}@example.org>"
    m["References"] = f"<{delivery_id}@example.org>"
    for name, value in extra:
        m[name] = value
    if html is not None and text == "":
        m.set_content(html, subtype="html")
    else:
        m.set_content(text)
        if html is not None:
            m.add_alternative(html, subtype="html")
    if attachment is not None:
        m.add_attachment(attachment, maintype="image", subtype="png", filename="shot.png")
    return m.as_bytes(policy=policy.SMTP)


def split_headers(raw: bytes) -> bytes:
    """What IMAP BODY.PEEK[HEADER] returns: the header block including the blank line."""
    end = raw.find(b"\r\n\r\n")
    return raw if end < 0 else raw[:end + 4]
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_replyparse.py`:

```python
from __future__ import annotations

from notify import replyparse as rp
from tests.conftest import PNG
from tests.mailfactory import reply_raw, split_headers

DLV = "dlv_0192837465abcdef012345"


def head(raw: bytes):
    return rp.parse_headers(split_headers(raw))


def test_thread_ids_in_reply_to_then_references_newest_first():
    raw = reply_raw(DLV, extra=[])
    assert rp.thread_delivery_ids(head(raw)) == [DLV]
    a, b = "dlv_" + "0" * 21 + "a", "dlv_" + "0" * 21 + "b"
    m = rp.parse_headers(f"In-Reply-To: <CAFoo@mail.example.net>\r\nReferences: <{a}@x.org> <junk@x> <{b}@x.org>\r\n\r\n".encode())
    assert rp.thread_delivery_ids(m) == [b, a]
    assert rp.thread_delivery_ids(rp.parse_headers(b"In-Reply-To: <dlv_short@x.org>\r\n\r\n")) == []


def test_from_and_message_key():
    m = head(reply_raw(DLV, sender="Alex@Example.org"))
    assert rp.from_address(m) == "alex@example.org"
    assert rp.message_key(m, b"ignored") == "<r1@mail.example.org>"


def test_message_key_without_message_id():
    raw = reply_raw(DLV, message_id=None)
    key = rp.message_key(head(raw), split_headers(raw))
    assert key.startswith("sha256:") and key == rp.message_key(head(raw), split_headers(raw))


def test_automatic_mail():
    for extra in ([("Auto-Submitted", "auto-replied")], [("Precedence", "bulk")], [("Return-Path", "<>")],
                  [("Return-Path", "<MAILER-DAEMON@mx.example.org>")]):
        assert rp.is_automatic(head(reply_raw(DLV, extra=extra))), extra
    assert not rp.is_automatic(head(reply_raw(DLV, extra=[("Auto-Submitted", "no")])))
    assert not rp.is_automatic(head(reply_raw(DLV)))
    assert rp.is_automatic(head(reply_raw(DLV, sender="postmaster@example.org")))


def test_dkim_first_header_only():
    good = head(reply_raw(DLV))
    assert rp.dkim_passes(good, "mx.example.org", "example.org")
    assert rp.dkim_passes(good, "MX.example.org", "example.org")
    assert not rp.dkim_passes(good, "other.example.org", "example.org")
    assert not rp.dkim_passes(good, "mx.example.org", "evil.org")
    # the server's own verdict (fail) comes first; a forged pass below it must not count
    forged = head(reply_raw(DLV, auth_results=[
        "mx.example.org; dkim=none; dmarc=fail header.from=example.org",
        "mx.example.org; dkim=pass header.d=example.org"]))
    assert not rp.dkim_passes(forged, "mx.example.org", "example.org")
    assert not rp.dkim_passes(head(reply_raw(DLV, auth_results=[])), "mx.example.org", "example.org")
    parent = head(reply_raw(DLV, sender="alex@mail.example.org",
                            auth_results=["mx.example.org; dkim=pass header.d=example.org"]))
    assert rp.dkim_passes(parent, "mx.example.org", "mail.example.org")
    assert not rp.dkim_passes(parent, "", "mail.example.org")


def test_strip_gmail_english_and_dutch_wrapped():
    assert rp.strip_quotes("Go with B.\n\n" + "On Wed, 14 Oct 2026 at 09:39, hnl-notify <hnl@example.org> wrote:\n> x") \
        == ("Go with B.", True)
    wrapped = "Yes\n\nOn Wed, Oct 14, 2026 at 9:39 AM hnl-notify <notifications@example.org>\nwrote:\n\n> x\n"
    assert rp.strip_quotes(wrapped) == ("Yes", True)
    dutch = "Doe B\n\nOp wo 14 okt 2026 om 09:39 schreef hnl-notify <\nhnl@example.org>:\n\n> x"
    assert rp.strip_quotes(dutch) == ("Doe B", True)


def test_strip_outlook_signature_and_trailing_quotes():
    assert rp.strip_quotes("ok\n\n-----Original Message-----\nFrom: x")[0] == "ok"
    assert rp.strip_quotes("ok\n\nFrom: hnl-notify <a@b>\nSent: Wednesday\nTo: me\n\nold")[0] == "ok"
    assert rp.strip_quotes("ok\n-- \nAlex\nPhone")[0] == "ok"
    assert rp.strip_quotes("ok\n> quoted\n>more\n")[0] == "ok"
    assert rp.strip_quotes("> only quote") == ("> only quote", False)
    assert rp.strip_quotes("From: me, about the plan: fine") == ("From: me, about the plan: fine", True)


def test_parse_reply_text_and_attachment():
    p = rp.parse_reply(reply_raw(DLV, attachment=PNG))
    assert p.text == "Go with B." and p.stripped and not p.truncated
    assert [(a.filename, a.content_type, a.data) for a in p.attachments] == [("shot.png", "image/png", PNG)]
    assert p.from_address == "alex@example.org" and p.subject == "Re: [home] Pick a design"


def test_image_only_reply():
    p = rp.parse_reply(reply_raw(DLV, text="", attachment=PNG, html=None))
    assert p.text == "" and len(p.attachments) == 1


def test_html_only_reply():
    html = "<div>Use <b>B</b><br>please</div><div class=gmail_quote>On Wed, 14 Oct 2026, X &lt;a@b&gt; wrote:<br>&gt; old</div>"
    p = rp.parse_reply(reply_raw(DLV, text="", html=html))
    assert p.text == "Use B\nplease"


def test_long_text_truncated():
    p = rp.parse_reply(reply_raw(DLV, text="x" * 9000))
    assert len(p.text) == rp.MAX_TEXT and p.truncated


def test_attachment_caps():
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"] = "alex@example.org"
    m.set_content("see files")
    for i in range(12):
        m.add_attachment(b"a" * 10, maintype="application", subtype="octet-stream", filename=f"f{i}.bin")
    kept, dropped = rp.extract_attachments(m)
    assert len(kept) == 10 and dropped == ["f10.bin", "f11.bin"]
    big = EmailMessage()
    big.set_content("big")
    big.add_attachment(b"b" * (rp.MAX_ATTACHMENT_BYTES + 1), maintype="application", subtype="zip", filename="big.zip")
    assert rp.extract_attachments(big) == ([], ["big.zip"])


def test_unknown_charset_decodes():
    raw = (b"From: alex@example.org\r\nMessage-ID: <c@x>\r\nMIME-Version: 1.0\r\n"
           b"Content-Type: text/plain; charset=x-unknown-charset\r\n\r\nCaf\xe9 B\r\n")
    assert rp.parse_reply(raw).text == "Caf� B"
    latin = (b"From: alex@example.org\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=iso-8859-1\r\n\r\n"
             b"Caf\xe9\r\n")
    assert rp.parse_reply(latin).text == "Caf\xe9"


def test_hostile_headers_never_raise():
    m = rp.parse_headers(b"From: =?utf-8?q?\xff\xfe?= <\r\nIn-Reply-To: \x00<<>>\r\nAuthentication-Results: ;;;\r\n\r\n")
    assert rp.from_address(m) == "" or "@" in rp.from_address(m)
    assert rp.thread_delivery_ids(m) == []
    assert not rp.dkim_passes(m, "mx.example.org", "example.org")
    assert rp.is_automatic(m) in (True, False)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_replyparse.py`
Expected: FAIL (`ModuleNotFoundError: notify.replyparse`).

- [ ] **Step 4: Implement `notify/replyparse.py`**

```python
"""Parse inbound reply mail: thread ids, sender, authentication verdict, automatic mail, text, attachments.

Pure functions over email.message.EmailMessage; no database, no network. Every function tolerates
malformed input: a hostile header yields "" / False / [] instead of an exception.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from html.parser import HTMLParser

MAX_TEXT = 8000
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 10485760
_DELIVERY_ID = re.compile(r"^dlv_[0-9a-f]{22}$")
_MSGID = re.compile(r"<([^<>@\s]+)@[^<>\s]+>")
_AUTO_LOCALS = {"mailer-daemon", "postmaster"}
_OUTLOOK_SEP = re.compile(r"^-{2,}\s*(original message|oorspronkelijk bericht)\s*-{2,}$", re.I)
_HEADER_START = re.compile(r"^(from|van):\s", re.I)
_HEADER_NEXT = re.compile(r"^(sent|verzonden|date|datum):\s", re.I)
_QUOTE_INTRO = re.compile(r"^(on|op)\s", re.I)
_QUOTE_VERB = re.compile(r"\b(wrote|schreef)\b", re.I)


def parse_headers(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)  # type: ignore[return-value]


def parse_message(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw)  # type: ignore[return-value]


def header(msg: EmailMessage, name: str) -> str:
    try:
        return str(msg.get(name) or "")
    except Exception:  # malformed encoded words and the like
        return ""


def thread_delivery_ids(msg: EmailMessage) -> list[str]:
    """Delivery ids named in In-Reply-To, then in References newest first."""
    refs = _MSGID.findall(header(msg, "In-Reply-To")) + _MSGID.findall(header(msg, "References"))[::-1]
    out: list[str] = []
    for local in refs:
        if _DELIVERY_ID.match(local) and local not in out:
            out.append(local)
    return out


def from_address(msg: EmailMessage) -> str:
    addr = parseaddr(header(msg, "From"))[1].strip().lower()
    return addr if "@" in addr else ""


def message_key(msg: EmailMessage, raw_headers: bytes) -> str:
    return header(msg, "Message-ID").strip()[:300] or "sha256:" + hashlib.sha256(raw_headers).hexdigest()


def is_automatic(msg: EmailMessage) -> bool:
    auto = header(msg, "Auto-Submitted").strip().lower()
    if auto and auto != "no":
        return True
    if header(msg, "Precedence").strip().lower() in ("bulk", "junk", "list"):
        return True
    if msg.get("Return-Path") is not None:
        rp = parseaddr(header(msg, "Return-Path"))[1].strip().lower()
        if not rp or rp.partition("@")[0] in _AUTO_LOCALS:
            return True
    if from_address(msg).partition("@")[0] in _AUTO_LOCALS:
        return True
    return msg.get_content_type() == "multipart/report"


def dkim_passes(msg: EmailMessage, authserv_id: str, from_domain: str) -> bool:
    """Only the first Authentication-Results header counts: the receiving server prepends its own,
    and anything below it may have been written by the sender."""
    if not authserv_id or not from_domain:
        return False
    try:
        first = next((str(v) for k, v in msg.raw_items() if k.lower() == "authentication-results"), None)
    except Exception:
        return False
    if first is None:
        return False
    parts = [p.strip() for p in re.sub(r"\s+", " ", first).split(";")]
    if parts[0].split(" ")[0].lower() != authserv_id.strip().lower():
        return False
    from_domain = from_domain.lower().rstrip(".")
    for p in parts[1:]:
        if re.match(r"dkim\s*=\s*pass\b", p, re.I):
            m = re.search(r"\bheader\.d\s*=\s*([a-z0-9.-]+)", p, re.I)
            d = m.group(1).lower().rstrip(".") if m else ""
            if d and (from_domain == d or from_domain.endswith("." + d)):
                return True
    return False


def strip_quotes(text: str) -> tuple[str, bool]:
    """Cut quoted history and signature; (text, stripped). Nothing left -> original text, False."""
    lines = text.replace("\r\n", "\n").split("\n")
    cut = len(lines)
    for i, line in enumerate(lines):
        s = line.strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if line in ("-- ", "--") or _OUTLOOK_SEP.match(s):
            cut = i
            break
        if _QUOTE_INTRO.match(s) and _QUOTE_VERB.search(f"{s} {nxt}") and (s.endswith(":") or nxt.endswith(":")):
            cut = i
            break
        if _HEADER_START.match(s) and any(_HEADER_NEXT.match(x.strip()) for x in lines[i + 1:i + 4]):
            cut = i
            break
    kept = lines[:cut]
    while kept and (not kept[-1].strip() or kept[-1].lstrip().startswith(">")):
        kept.pop()
    out = "\n".join(kept).strip()
    return (out, True) if out else (text.strip(), False)


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("br", "p", "div", "tr", "li", "blockquote"):
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in ("p", "div", "tr", "li"):
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


def html_to_text(s: str) -> str:
    p = _Text()
    p.feed(s)
    p.close()
    lines = [ln.strip() for ln in "".join(p.out).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _content(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError, KeyError, AssertionError):
        payload = part.get_payload(decode=True) or b""
        try:
            return payload.decode(part.get_content_charset() or "utf-8", "replace")
        except LookupError:
            return payload.decode("utf-8", "replace")


def body_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        return _content(part)  # type: ignore[arg-type]
    part = msg.get_body(preferencelist=("html",))
    return html_to_text(_content(part)) if part is not None else ""  # type: ignore[arg-type]


@dataclass
class ReplyAttachment:
    filename: str
    content_type: str
    data: bytes


def extract_attachments(msg: EmailMessage) -> tuple[list[ReplyAttachment], list[str]]:
    """Named parts plus inline images, up to the caps; one decoded part in memory at a time."""
    bodies = {id(b) for b in (msg.get_body(preferencelist=("plain",)), msg.get_body(preferencelist=("html",))) if b}
    kept: list[ReplyAttachment] = []
    dropped: list[str] = []
    total = images = 0
    for part in msg.walk():
        if part.is_multipart() or id(part) in bodies:
            continue
        ctype = part.get_content_type()
        try:
            name = part.get_filename()
        except Exception:
            name = None
        if not name and not ctype.startswith("image/"):
            continue
        if not name:
            images += 1
            name = f"image-{images}.{ctype.partition('/')[2] or 'bin'}"
        data = part.get_payload(decode=True) or b""
        if len(kept) >= MAX_ATTACHMENTS or total + len(data) > MAX_ATTACHMENT_BYTES:
            dropped.append(name[:255])
            continue
        total += len(data)
        kept.append(ReplyAttachment(name[:255], ctype, data))
    return kept, dropped


@dataclass
class ParsedReply:
    from_address: str
    subject: str
    text: str
    stripped: bool
    truncated: bool
    attachments: list[ReplyAttachment] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def parse_reply(raw: bytes) -> ParsedReply:
    """Full parse of an accepted reply. CPU and memory heavy for big mail: call it off the event loop."""
    msg = parse_message(raw)
    text, stripped = strip_quotes(body_text(msg))
    atts, dropped = extract_attachments(msg)
    return ParsedReply(from_address(msg), header(msg, "Subject")[:300], text[:MAX_TEXT], stripped,
                       len(text) > MAX_TEXT, atts, dropped)
```

- [ ] **Step 5: Run tests and fix until green**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_replyparse.py`
Expected: PASS. If `test_html_only_reply` leaves extra blank lines, adjust `html_to_text` (not the test): the expected text is exactly `"Use B\nplease"`.

- [ ] **Step 6: Commit**

```bash
git add notify/replyparse.py tests/mailfactory.py tests/test_replyparse.py
git commit -m "Replies: parse thread ids, sender, DKIM verdict, text and attachments"
```

---

### Task 4: Replies core — classify, store, claim, sweep, autoreply

**Files:**
- Create: `notify/replies.py`
- Modify: `notify/core.py` (`NotifyService.__init__` creates `self.replies`, `self.inbound_status`, `self.open_mailbox`; `retention_sweep`)
- Test: `tests/test_replies_core.py`

**Interfaces:**
- Consumes: `replyparse.*` (Task 3), Task 1 tables, `SYSTEM_CLIENT`, `NotificationIn._auto_submitted`.
- Produces in `notify/replies.py`:
  - constants `LISTEN_SECONDS = 120`, `GRACE_SECONDS = 600`, `AUTOREPLY_EVERY_SECONDS = 3600`
  - `@dataclass Verdict(action: Literal["accept", "quarantine", "duplicate"], reason: str | None = None, delivery: Row | None = None, notification: Row | None = None)`
  - `class Replies(service)` with attribute `now: Callable[[], datetime]` (defaults to `utcnow`; tests replace it) and methods:
    - `classify(msg: EmailMessage, raw_headers: bytes, authserv_id: str) -> Verdict`
    - `quarantine(msg: EmailMessage, verdict: Verdict) -> None`
    - `store(verdict: Verdict, key: str, parsed: ParsedReply) -> str` (reply id; raises `sqlite3.IntegrityError` on a duplicate key)
    - `async claim(client: Client, nid: str, waiter: str, wait: float) -> dict | None`
    - `list(client: Client, nid: str) -> list[dict]`
    - `attachment(client: Client, rid: str, idx: int) -> tuple[str, str, bytes]`
    - `sweep_unclaimed() -> int`
    - `recent(limit: int = 25) -> list[dict]`, `quarantined(limit: int = 25) -> list[dict]` (admin)
  - reply dict keys: `id, conversation_id, notification_id, notification_title, from, subject, received_at, state, text, stripped, truncated, dropped_attachments, claimed_at, claimed_by, autoreply_notification_id, attachments[{idx, filename, content_type, size, purged, url}]`
- Produces on `NotifyService`: `self.replies: Replies`, `self.inbound_status: dict[str, dict]`, `self.open_mailbox: Callable[[Provider], Any] | None = None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_replies_core.py`:

```python
from __future__ import annotations

from datetime import timedelta

import pytest

from notify import replyparse as rp
from notify.core import Client, NotFound
from notify.db import iso, utcnow
from notify.models import Scope
from notify.replies import GRACE_SECONDS, LISTEN_SECONDS
from tests.conftest import PNG, wait_for
from tests.mailfactory import reply_raw, split_headers

AGENT = Client("agent", frozenset({Scope.send, Scope.read, Scope.manage}))
OTHER = Client("other", frozenset({Scope.send, Scope.read, Scope.manage}))


def send(srv, **body) -> tuple[str, str]:
    """Reply-enabled email notification; returns (notification id, delivery id) once dispatched."""
    with srv.client() as c:
        out = c.post("/v1/notifications", json={"targets": ["device:alex-email"], "message": "Pick", "title": "Pick",
                                                 "kinds": ["email"], "reply": True, **body}).json()
        wait_for(lambda: c.get(f"/v1/notifications/{out['id']}").json()["deliveries"][0]["state"] == "dispatched")
    return out["id"], out["deliveries"][0]["id"]


def deliver(srv, raw: bytes) -> str | None:
    """classify + store, the way the poller does; returns the reply id or None."""
    head = rp.parse_headers(split_headers(raw))
    replies = srv.service.replies
    v = replies.classify(head, split_headers(raw), "mx.example.org")
    if v.action == "quarantine":
        srv.call(lambda: replies.quarantine(head, v))
        return None
    if v.action == "duplicate":
        return None
    return srv.call(lambda: replies.store(v, rp.message_key(head, split_headers(raw)), rp.parse_reply(raw)))


def claim(srv, nid, waiter="w" * 12, wait=0.0, client=AGENT):
    return srv.call(lambda: srv.service.replies.claim(client, nid, waiter, wait))


def test_accept_claim_and_interacted(email_live):
    nid, did = send(email_live)
    rid = deliver(email_live, reply_raw(did, attachment=PNG))
    got = claim(email_live, nid)
    assert got["id"] == rid and got["text"] == "Go with B." and got["stripped"] and got["state"] == "claimed"
    assert got["notification_title"] == "Pick" and got["attachments"][0]["filename"] == "shot.png"
    assert claim(email_live, nid)["id"] == rid  # same waiter: same reply again
    assert claim(email_live, nid, waiter="x" * 12) is None
    d = email_live.service.db.one("SELECT state, interaction_json FROM deliveries WHERE id=?", (did,))
    assert d["state"] == "interacted" and rid in d["interaction_json"]
    types = [e["type"] for e in email_live.service.get(AGENT, nid)["events"]]
    assert "reply.received" in types and "reply.claimed" in types and "delivery.interacted" in types


@pytest.mark.parametrize("make,reason", [
    (lambda did: reply_raw(did, extra=[("Auto-Submitted", "auto-replied")]), "automatic"),
    (lambda did: reply_raw("dlv_" + "f" * 22), "no_thread"),
    (lambda did: reply_raw(did, sender="mallory@example.org"), "sender_mismatch"),
    (lambda did: reply_raw(did, auth_results=["mx.example.org; dkim=none",
                                              "mx.example.org; dkim=pass header.d=example.org"]), "dkim_fail"),
    (lambda did: reply_raw(did, auth_results=[]), "dkim_fail"),
])
def test_quarantine_reasons(email_live, make, reason):
    nid, did = send(email_live)
    assert deliver(email_live, make(did)) is None
    row = email_live.service.db.one("SELECT reason, subject FROM reply_quarantine ORDER BY id DESC LIMIT 1")
    assert row["reason"] == reason and row["subject"] == "Re: [home] Pick a design"
    assert email_live.service.db.scalar("SELECT COUNT(*) FROM replies") == 0


def test_not_reply_enabled_and_duplicate(email_live):
    nid, did = send(email_live, reply=False)
    assert deliver(email_live, reply_raw(did)) is None
    assert email_live.service.db.scalar("SELECT reason FROM reply_quarantine") == "not_reply_enabled"
    nid2, did2 = send(email_live)
    assert deliver(email_live, reply_raw(did2, message_id="<dup@x>")) is not None
    assert deliver(email_live, reply_raw(did2, message_id="<dup@x>")) is None
    assert email_live.service.db.scalar("SELECT COUNT(*) FROM replies") == 1


def test_conversation_reply_to_older_mail(email_live):
    first, did1 = send(email_live)
    second, _ = send(email_live, in_reply_to=first)
    rid = deliver(email_live, reply_raw(did1))
    assert claim(email_live, second)["id"] == rid


def test_two_replies_in_order(email_live):
    nid, did = send(email_live)
    r1 = deliver(email_live, reply_raw(did, message_id="<a@x>", text="B"))
    r2 = deliver(email_live, reply_raw(did, message_id="<b@x>", text="and blue"))
    assert claim(email_live, nid, waiter="first-run-1")["id"] == r1
    assert claim(email_live, nid, waiter="second-run2")["id"] == r2
    assert [r["state"] for r in email_live.service.replies.list(AGENT, nid)] == ["claimed", "claimed"]


def test_other_client_cannot_claim_or_list(email_live):
    nid, did = send(email_live)
    deliver(email_live, reply_raw(did))
    with pytest.raises(NotFound):
        claim(email_live, nid, client=OTHER)
    with pytest.raises(NotFound):
        email_live.service.replies.list(OTHER, nid)


def test_long_poll_wakes_on_store(email_live):
    nid, did = send(email_live)
    import threading
    out = {}
    t = threading.Thread(target=lambda: out.update(r=claim(email_live, nid, wait=20)))
    t.start()
    import time
    time.sleep(0.5)
    rid = deliver(email_live, reply_raw(did))
    t.join(10)
    assert out["r"]["id"] == rid


def test_unclaimed_after_grace_without_listener_gets_one_autoreply(email_live, sent_mail):
    nid, did = send(email_live)
    r1 = deliver(email_live, reply_raw(did, message_id="<a@x>"))
    replies = email_live.service.replies
    later = utcnow() + timedelta(seconds=GRACE_SECONDS + 60)
    replies.now = lambda: later
    assert email_live.call(replies.sweep_unclaimed) == 1
    row = email_live.service.db.one("SELECT state, autoreply_notification_id FROM replies WHERE id=?", (r1,))
    assert row["state"] == "unclaimed" and row["autoreply_notification_id"]
    wait_for(lambda: len(sent_mail.raws) == 2)
    em = sent_mail.last()
    assert em["Auto-Submitted"] == "auto-replied" and em["In-Reply-To"] == f"<{did}@example.org>"
    assert "no longer listening" in em.get_body(("plain",)).get_content()
    assert em["Reply-To"] is None
    assert claim(email_live, nid) is None  # unclaimed replies are never handed out
    deliver(email_live, reply_raw(did, message_id="<b@x>"))
    replies.now = lambda: later + timedelta(seconds=GRACE_SECONDS + 60)
    assert email_live.call(replies.sweep_unclaimed) == 1
    assert email_live.service.db.scalar("SELECT COUNT(*) FROM replies WHERE autoreply_notification_id IS NOT NULL") == 1


def test_active_listener_prevents_sweep(email_live):
    nid, did = send(email_live)
    deliver(email_live, reply_raw(did))
    replies = email_live.service.replies
    later = utcnow() + timedelta(seconds=GRACE_SECONDS + 60)
    with email_live.service.db.tx() as c:
        c.execute("UPDATE notifications SET listener_seen_at=? WHERE id=?",
                  (iso(later - timedelta(seconds=LISTEN_SECONDS - 10)), nid))
    replies.now = lambda: later
    assert email_live.call(replies.sweep_unclaimed) == 0


def test_attachment_download_and_purge(email_live):
    nid, did = send(email_live)
    rid = deliver(email_live, reply_raw(did, attachment=PNG))
    assert email_live.service.replies.attachment(AGENT, rid, 0) == ("shot.png", "image/png", PNG)
    with pytest.raises(NotFound):
        email_live.service.replies.attachment(OTHER, rid, 0)
    with email_live.service.db.tx() as c:
        c.execute("UPDATE replies SET received_at='2000-01-01T00:00:00.000000Z'")
    email_live.call(email_live.service.retention_sweep)
    with pytest.raises(NotFound):
        email_live.service.replies.attachment(AGENT, rid, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_replies_core.py`
Expected: FAIL (`ModuleNotFoundError: notify.replies`).

- [ ] **Step 3: Implement `notify/replies.py`**

```python
"""Replies to notifications: match inbound mail to a delivery, store it, hand it to a waiting agent,
and answer replies nobody collects. Provider-agnostic: the email inbound poller feeds it.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import timedelta
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any, Literal

from . import replyparse
from .db import dumps, iso, loads, new_id, parse_iso, utcnow
from .models import DELIVERED, DISPATCHED, INTERACTED, NotificationIn

if TYPE_CHECKING:
    from .core import Client, NotifyService

log = logging.getLogger("notify.replies")

LISTEN_SECONDS = 120
GRACE_SECONDS = 600
AUTOREPLY_EVERY_SECONDS = 3600


@dataclass
class Verdict:
    action: Literal["accept", "quarantine", "duplicate"]
    reason: str | None = None
    delivery: sqlite3.Row | None = None
    notification: sqlite3.Row | None = None


class Replies:
    def __init__(self, service: NotifyService):
        self.svc = service
        self.now = utcnow  # seam: tests move the clock

    # ------------------------------------------------------------ inbound
    def classify(self, msg: EmailMessage, raw_headers: bytes, authserv_id: str) -> Verdict:
        """Spec §3 steps 2-6 on the header block only; the body is fetched only for an accept."""
        if replyparse.is_automatic(msg):
            return Verdict("quarantine", "automatic")
        db = self.svc.db
        d = None
        for did in replyparse.thread_delivery_ids(msg):
            d = db.one("SELECT * FROM deliveries WHERE id=?", (did,))
            if d is not None:
                break
        if d is None:
            return Verdict("quarantine", "no_thread")
        n = db.one("SELECT * FROM notifications WHERE id=?", (d["notification_id"],))
        if n is None or not n["reply_wanted"]:
            return Verdict("quarantine", "not_reply_enabled", d, n)
        sender = replyparse.from_address(msg)
        if not sender or sender != (d["recipient_address"] or "").strip().lower():
            return Verdict("quarantine", "sender_mismatch", d, n)
        if not replyparse.dkim_passes(msg, authserv_id, sender.rpartition("@")[2]):
            return Verdict("quarantine", "dkim_fail", d, n)
        if db.scalar("SELECT 1 FROM replies WHERE message_id=?", (replyparse.message_key(msg, raw_headers),)):
            return Verdict("duplicate", None, d, n)
        return Verdict("accept", None, d, n)

    def quarantine(self, msg: EmailMessage, verdict: Verdict) -> None:
        n, d = verdict.notification, verdict.delivery
        with self.svc.db.tx() as c:
            c.execute("INSERT INTO reply_quarantine(received_at, from_address, subject, message_id, reason,"
                      " notification_id) VALUES (?,?,?,?,?,?)",
                      (iso(self.now()), replyparse.from_address(msg)[:320], replyparse.header(msg, "Subject")[:300],
                       replyparse.header(msg, "Message-ID")[:300], verdict.reason, n["id"] if n is not None else None))
            if n is not None:
                self.svc._emit(c, "reply.quarantined", n, d["id"] if d is not None else None, {"reason": verdict.reason})
        self.svc._after_commit()
        log.warning("reply quarantined", extra={"reason": verdict.reason,
                                                "notification_id": n["id"] if n is not None else None})

    def store(self, verdict: Verdict, key: str, p: replyparse.ParsedReply) -> str:
        d, n = verdict.delivery, verdict.notification
        assert d is not None and n is not None
        rid = new_id("rpl")
        with self.svc.db.tx() as c:
            c.execute("INSERT INTO replies(id, conversation_id, notification_id, delivery_id, message_id, from_address,"
                      " subject, received_at, text, stripped, truncated, dropped_attachments_json, state)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, n["conversation_id"] or n["id"], n["id"], d["id"], key, p.from_address, p.subject,
                       iso(self.now()), p.text, int(p.stripped), int(p.truncated), dumps(p.dropped), "waiting"))
            c.executemany("INSERT INTO reply_attachments(reply_id, idx, filename, content_type, size, sha256, data)"
                          " VALUES (?,?,?,?,?,?,?)",
                          [(rid, i, a.filename, a.content_type, len(a.data), hashlib.sha256(a.data).hexdigest(), a.data)
                           for i, a in enumerate(p.attachments)])
            self.svc._emit(c, "reply.received", n, d["id"], {"reply_id": rid, "attachments": len(p.attachments)})
        self.svc._after_commit()
        return rid

    # ------------------------------------------------------------ agents
    async def claim(self, client: Client, nid: str, waiter: str, wait: float) -> dict[str, Any] | None:
        """Long-poll: the oldest waiting reply of the conversation, or the one this waiter already holds."""
        n = self.svc._notification_row(client, nid)
        conv = n["conversation_id"] or n["id"]
        deadline = time.monotonic() + max(0.0, min(wait, 55.0))
        while True:
            got = self._claim_once(client, conv, waiter)
            if got is not None:
                return self._out(got)
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            await self.svc.bus.wait(min(left, 5.0))  # every commit (including a stored reply) wakes it

    def _claim_once(self, client: Client, conv: str, waiter: str) -> sqlite3.Row | None:
        now = iso(self.now())
        with self.svc.db.tx() as c:
            c.execute("UPDATE notifications SET listener_seen_at=? WHERE id=?", (now, conv))
            r = c.execute("SELECT * FROM replies WHERE conversation_id=? AND state='claimed' AND waiter_id=?"
                          " ORDER BY received_at, id LIMIT 1", (conv, waiter)).fetchone()
            if r is not None:
                return r
            r = c.execute("SELECT * FROM replies WHERE conversation_id=? AND state='waiting'"
                          " ORDER BY received_at, id LIMIT 1", (conv,)).fetchone()
            if r is None:
                return None
            c.execute("UPDATE replies SET state='claimed', claimed_at=?, claimed_by=?, waiter_id=? WHERE id=?",
                      (now, client.id, waiter, r["id"]))
            n = c.execute("SELECT * FROM notifications WHERE id=?", (r["notification_id"],)).fetchone()
            self.svc._emit(c, "reply.claimed", n, r["delivery_id"], {"reply_id": r["id"], "client": client.id})
            d = c.execute("SELECT * FROM deliveries WHERE id=?", (r["delivery_id"],)).fetchone()
            if d is not None and d["state"] in (DISPATCHED, DELIVERED):
                self.svc._transition(c, d, INTERACTED, {"interacted_at": now,
                                                        "interaction_json": dumps({"reply_id": r["id"]})},
                                     {"reply_id": r["id"]})
            r = c.execute("SELECT * FROM replies WHERE id=?", (r["id"],)).fetchone()
        self.svc._after_commit()
        return r

    def list(self, client: Client, nid: str) -> list[dict[str, Any]]:
        n = self.svc._notification_row(client, nid)
        rows = self.svc.db.query("SELECT * FROM replies WHERE conversation_id=? ORDER BY received_at, id",
                                 (n["conversation_id"] or n["id"],))
        return [self._out(r) for r in rows]

    def attachment(self, client: Client, rid: str, idx: int) -> tuple[str, str, bytes]:
        from .core import NotFound
        r = self.svc.db.one("SELECT notification_id FROM replies WHERE id=?", (rid,))
        if r is None:
            raise NotFound(f"reply {rid} not found")
        self.svc._notification_row(client, r["notification_id"])
        a = self.svc.db.one("SELECT filename, content_type, data FROM reply_attachments WHERE reply_id=? AND idx=?",
                            (rid, idx))
        if a is None or a["data"] is None:
            raise NotFound(f"attachment {idx} of reply {rid} not found (or purged)")
        return a["filename"], a["content_type"], a["data"]

    def _out(self, r: sqlite3.Row) -> dict[str, Any]:
        n = self.svc.db.one("SELECT title FROM notifications WHERE id=?", (r["notification_id"],))
        atts = self.svc.db.query("SELECT idx, filename, content_type, size, purged_at FROM reply_attachments"
                                 " WHERE reply_id=? ORDER BY idx", (r["id"],))
        return {
            "id": r["id"], "conversation_id": r["conversation_id"], "notification_id": r["notification_id"],
            "notification_title": n["title"] if n is not None else None, "from": r["from_address"],
            "subject": r["subject"], "received_at": r["received_at"], "state": r["state"], "text": r["text"],
            "stripped": bool(r["stripped"]), "truncated": bool(r["truncated"]),
            "dropped_attachments": loads(r["dropped_attachments_json"], []), "claimed_at": r["claimed_at"],
            "claimed_by": r["claimed_by"], "autoreply_notification_id": r["autoreply_notification_id"],
            "attachments": [{"idx": a["idx"], "filename": a["filename"], "content_type": a["content_type"],
                             "size": a["size"], "purged": a["purged_at"] is not None,
                             "url": f"/v1/replies/{r['id']}/attachments/{a['idx']}"} for a in atts],
        }

    # ------------------------------------------------------------ unclaimed
    def sweep_unclaimed(self) -> int:
        now = self.now()
        rows = self.svc.db.query(
            "SELECT r.* FROM replies r LEFT JOIN notifications root ON root.id=r.conversation_id"
            " WHERE r.state='waiting' AND r.received_at <= ?"
            " AND (root.listener_seen_at IS NULL OR root.listener_seen_at < ?) ORDER BY r.received_at",
            (iso(now - timedelta(seconds=GRACE_SECONDS)), iso(now - timedelta(seconds=LISTEN_SECONDS))))
        swept = 0
        for r in rows:
            with self.svc.db.tx() as c:
                if c.execute("SELECT state FROM replies WHERE id=?", (r["id"],)).fetchone()["state"] != "waiting":
                    continue
                c.execute("UPDATE replies SET state='unclaimed' WHERE id=?", (r["id"],))
                n = c.execute("SELECT * FROM notifications WHERE id=?", (r["notification_id"],)).fetchone()
                self.svc._emit(c, "reply.unclaimed", n, r["delivery_id"], {"reply_id": r["id"]})
            swept += 1
            self._autoreply(r)
        if swept:
            self.svc._after_commit()
        return swept

    def _autoreply(self, r: sqlite3.Row) -> None:
        from .core import SYSTEM_CLIENT, Invalid
        db = self.svc.db
        last = db.scalar("SELECT MAX(n.created_at) FROM replies x JOIN notifications n"
                         " ON n.id=x.autoreply_notification_id WHERE x.conversation_id=?", (r["conversation_id"],))
        if last and parse_iso(last) > self.now() - timedelta(seconds=AUTOREPLY_EVERY_SECONDS):  # type: ignore[operator]
            return
        d = db.one("SELECT device FROM deliveries WHERE id=?", (r["delivery_id"],))
        n = db.one("SELECT title FROM notifications WHERE id=?", (r["notification_id"],))
        if d is None or n is None:
            return
        received = parse_iso(r["received_at"]).strftime("%Y-%m-%d %H:%M UTC")  # type: ignore[union-attr]
        req = NotificationIn(
            targets=[f"device:{d['device']}"], kinds=["email"], in_reply_to=r["notification_id"],
            title=f"Re: {n['title'][:990]}" if n["title"] else None, source="hnl-notify/replies",
            message=f"This agent is no longer listening, so your reply was not delivered. Received {received}.",
            tags=["reply-autoreply"], data={"reply_id": r["id"]})
        req._auto_submitted = True
        try:
            out, _ = self.svc.submit(SYSTEM_CLIENT, req)
        except Invalid as e:
            log.warning("autoreply not sent", extra={"reply_id": r["id"], "error": str(e)})
            return
        with db.tx() as c:
            c.execute("UPDATE replies SET autoreply_notification_id=? WHERE id=?", (out["id"], r["id"]))

    # ------------------------------------------------------------ admin
    def recent(self, limit: int = 25) -> list[dict[str, Any]]:
        return [self._out(r) for r in self.svc.db.query("SELECT * FROM replies ORDER BY received_at DESC LIMIT ?",
                                                         (max(1, min(limit, 100)),))]

    def quarantined(self, limit: int = 25) -> list[dict[str, Any]]:
        return [dict(r) for r in self.svc.db.query("SELECT * FROM reply_quarantine ORDER BY id DESC LIMIT ?",
                                                    (max(1, min(limit, 100)),))]
```

- [ ] **Step 4: Wire into the service and retention**

In `notify/core.py` `NotifyService.__init__`, append:

```python
        from .replies import Replies  # replies imports core for types; import here to avoid a cycle
        self.replies = Replies(self)
        self.inbound_status: dict[str, dict[str, Any]] = {}  # per provider: last_success, last_error, last_stats
        self.open_mailbox: Any = None  # seam: tests return a fake mailbox; None = the provider's own
```

In `retention_sweep`, inside the `with self.db.tx() as c:` block, after the `UPDATE attachments ...` statement:

```python
            c.execute("UPDATE reply_attachments SET data=NULL, purged_at=? WHERE data IS NOT NULL AND reply_id IN"
                      " (SELECT id FROM replies WHERE received_at < ?)", (now_iso(), att_cutoff))
            c.execute("DELETE FROM reply_quarantine WHERE received_at < ?", (cutoff,))
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_replies_core.py tests/test_replies_intake.py`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add notify/replies.py notify/core.py tests/test_replies_core.py
git commit -m "Replies: classify, store, claim per conversation, unclaimed sweep and autoreply"
```

---

### Task 5: IMAP mailbox and inbound poller

**Files:**
- Create: `notify/providers/email_inbound.py`
- Modify: `notify/providers/email.py` (`mailbox()`, `test_connection` adds IMAP)
- Modify: `notify/worker.py` (`_inbound_loop`, `inbound_interval`)
- Modify: `tests/mailfactory.py` (`FakeMailbox`), `tests/conftest.py` (`email_factory` sets `srv.mailbox` and `service.open_mailbox`)
- Test: `tests/test_inbound.py`

**Interfaces:**
- Consumes: `Replies.classify/quarantine/store` (Task 4), `replyparse` (Task 3), `EmailProvider` reply attributes (Task 2).
- Produces in `notify/providers/email_inbound.py`:
  - `MAX_MESSAGE_BYTES = 15728640`, `SESSION_SECONDS = 120`
  - `class MailboxError(Exception)`
  - `class ImapMailbox(host, port, user, password, *, verify_tls=True, inbox="INBOX", processed="Processed", quarantine="Quarantine", timeout=SESSION_SECONDS)` with attributes `inbox`, `processed`, `quarantine` and methods `open()`, `close()`, `list() -> list[tuple[bytes, int]]`, `fetch_headers(uid: bytes) -> bytes`, `fetch(uid: bytes) -> bytes`, `move(uid: bytes, folder: str) -> None`, `test() -> dict`; classmethod `for_provider(p) -> ImapMailbox`.
  - `async poll_once(svc: NotifyService, provider: EmailProvider, mailbox) -> dict[str, int]` (`accepted`, `quarantined`, `duplicates`).
- Produces: `EmailProvider.mailbox() -> ImapMailbox`.
- Produces: `Workers.inbound_interval: float = 30.0`; worker heartbeat name `"inbound"`.
- Produces (tests): `FakeMailbox` with `.add(raw: bytes, size: int | None = None) -> bytes` (uid), `.folders: dict[str, list[bytes]]`, `.fail_move_once: bool`, same methods as `ImapMailbox` (`open`, `close`, `list`, `fetch_headers`, `fetch`, `move`) and attributes `inbox`, `processed`, `quarantine`.

- [ ] **Step 1: Fake mailbox and fixture wiring**

Append to `tests/mailfactory.py`:

```python
class FakeMailbox:
    """In-memory stand-in for ImapMailbox."""

    def __init__(self) -> None:
        self.inbox, self.processed, self.quarantine = "INBOX", "Processed", "Quarantine"
        self.messages: dict[bytes, bytes] = {}
        self.sizes: dict[bytes, int] = {}
        self.folders: dict[str, list[bytes]] = {self.inbox: [], self.processed: [], self.quarantine: []}
        self.fail_move_once = False
        self.fetched: list[bytes] = []
        self.opened = self.closed = 0
        self._next = 1

    def add(self, raw: bytes, size: int | None = None) -> bytes:
        uid = str(self._next).encode()
        self._next += 1
        self.messages[uid] = raw
        self.sizes[uid] = len(raw) if size is None else size
        self.folders[self.inbox].append(uid)
        return uid

    def open(self) -> None:
        self.opened += 1

    def close(self) -> None:
        self.closed += 1

    def list(self) -> list[tuple[bytes, int]]:
        return [(u, self.sizes[u]) for u in self.folders[self.inbox]]

    def fetch_headers(self, uid: bytes) -> bytes:
        return split_headers(self.messages[uid])

    def fetch(self, uid: bytes) -> bytes:
        self.fetched.append(uid)
        return self.messages[uid]

    def move(self, uid: bytes, folder: str) -> None:
        if self.fail_move_once:
            self.fail_move_once = False
            raise OSError("connection reset during MOVE")
        self.folders[self.inbox].remove(uid)
        self.folders[folder].append(uid)
```

In `tests/conftest.py` `email_factory.make`, replace `srv.mailbox = None  # ...` with:

```python
        from tests.mailfactory import FakeMailbox
        srv.mailbox = FakeMailbox()
        srv.service.open_mailbox = lambda provider, box=srv.mailbox: box
        srv.app.state.workers.inbound_interval = 3600.0  # tests poll explicitly unless they shorten this
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_inbound.py`:

```python
from __future__ import annotations

import imaplib
from typing import Any

import pytest

import notify.providers.email_inbound as inbound
from notify.core import Client
from notify.models import Scope
from tests.conftest import PNG, wait_for
from tests.mailfactory import reply_raw, split_headers

AGENT = Client("agent", frozenset({Scope.send, Scope.read, Scope.manage}))


def send(srv) -> tuple[str, str]:
    with srv.client() as c:
        out = c.post("/v1/notifications", json={"targets": ["device:alex-email"], "message": "Pick", "kinds": ["email"],
                                                 "reply": True}).json()
        wait_for(lambda: c.get(f"/v1/notifications/{out['id']}").json()["deliveries"][0]["state"] == "dispatched")
    return out["id"], out["deliveries"][0]["id"]


def poll(srv) -> dict:
    p = srv.service.providers["mail"]
    return srv.call(lambda: inbound.poll_once(srv.service, p, srv.mailbox))


def test_poll_sorts_mail(email_live):
    nid, did = send(email_live)
    mb = email_live.mailbox
    good = mb.add(reply_raw(did, attachment=PNG))
    forged = mb.add(reply_raw(did, message_id="<f@x>", auth_results=["mx.example.org; dkim=none",
                                                                       "mx.example.org; dkim=pass header.d=example.org"]))
    auto = mb.add(reply_raw(did, message_id="<a@x>", extra=[("Auto-Submitted", "auto-replied")]))
    huge = mb.add(reply_raw(did, message_id="<h@x>"), size=inbound.MAX_MESSAGE_BYTES + 1)
    stray = mb.add(reply_raw("dlv_" + "f" * 22, message_id="<s@x>"))
    assert poll(email_live) == {"accepted": 1, "quarantined": 4, "duplicates": 0}
    assert mb.folders["Processed"] == [good]
    assert sorted(mb.folders["Quarantine"]) == sorted([forged, auto, huge, stray])
    assert mb.fetched == [good]  # bodies of rejected mail are never downloaded
    reasons = {r[0] for r in email_live.service.db.query("SELECT reason FROM reply_quarantine")}
    assert reasons == {"dkim_fail", "automatic", "too_large", "no_thread"}
    got = email_live.call(lambda: email_live.service.replies.claim(AGENT, nid, "w" * 12, 0))
    assert got["text"] == "Go with B." and got["attachments"][0]["size"] == len(PNG)
    assert mb.opened == mb.closed == 1


def test_crash_between_commit_and_move_is_not_processed_twice(email_live):
    nid, did = send(email_live)
    mb = email_live.mailbox
    uid = mb.add(reply_raw(did))
    mb.fail_move_once = True
    with pytest.raises(OSError):
        poll(email_live)
    assert mb.closed == 1
    assert poll(email_live) == {"accepted": 0, "quarantined": 0, "duplicates": 1}
    assert mb.folders["Processed"] == [uid]
    assert email_live.service.db.scalar("SELECT COUNT(*) FROM replies") == 1


def test_unreadable_message_is_quarantined(email_live, monkeypatch):
    nid, did = send(email_live)
    mb = email_live.mailbox
    uid = mb.add(reply_raw(did))

    def boom(raw: bytes) -> Any:
        raise ValueError("parser exploded")
    monkeypatch.setattr(inbound.replyparse, "parse_reply", boom)
    assert poll(email_live)["quarantined"] == 1
    assert mb.folders["Quarantine"] == [uid]
    assert email_live.service.db.scalar("SELECT reason FROM reply_quarantine") == "unreadable"


def test_worker_loop_polls_and_reports_status(email_live):
    nid, did = send(email_live)
    email_live.mailbox.add(reply_raw(did))
    email_live.app.state.workers.inbound_interval = 0.1
    wait_for(lambda: email_live.service.inbound_status.get("mail", {}).get("last_success"), timeout=10)
    assert email_live.service.inbound_status["mail"]["last_error"] is None
    assert email_live.service.db.scalar("SELECT COUNT(*) FROM replies") == 1


# --- ImapMailbox against a fake imaplib client --------------------------------

class FakeIMAP:
    instances: list["FakeIMAP"] = []
    move_ok = True
    login_ok = True

    def __init__(self, host: str, port: int, ssl_context: Any = None, timeout: float | None = None):
        self.calls: list[tuple] = []
        self.sock = type("S", (), {"shutdown": lambda s, h: None, "close": lambda s: None})()
        self.folders = [b'(\\HasNoChildren) "." INBOX', b'(\\HasNoChildren) "." "Processed"']
        type(self).instances.append(self)

    def login(self, u, p):
        if not self.login_ok:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Authentication failed.")
        return ("OK", [b"ok"])

    def select(self, name, readonly=False):
        self.calls.append(("select", name))
        return ("OK", [b"2"])

    def list(self):
        return ("OK", self.folders)

    def create(self, name):
        self.calls.append(("create", name))
        return ("OK", [b""])

    def uid(self, cmd, *args):
        self.calls.append(("uid", cmd, *args))
        if cmd == "FETCH" and args[1] == "(RFC822.SIZE)":
            return ("OK", [b"1 (UID 7 RFC822.SIZE 1234)", b"2 (UID 9 RFC822.SIZE 99)"])
        if cmd == "FETCH":
            return ("OK", [(b"1 (UID 7 BODY[HEADER] {5}", b"X: y\r\n"), b")"])
        if cmd == "MOVE":
            if not self.move_ok:
                raise imaplib.IMAP4.error("MOVE not supported")
            return ("OK", [b""])
        return ("OK", [b""])

    def expunge(self):
        self.calls.append(("expunge",))
        return ("OK", [b""])

    def logout(self):
        return ("BYE", [b""])


@pytest.fixture
def fake_imap(monkeypatch):
    class F(FakeIMAP):
        instances: list = []
        move_ok = True
        login_ok = True
    monkeypatch.setattr(inbound.imaplib, "IMAP4_SSL", F)
    return F


def box() -> inbound.ImapMailbox:
    return inbound.ImapMailbox("imap.test", 993, "u", "p")


def test_imap_open_list_fetch_creates_missing_folder(fake_imap):
    mb = box()
    mb.open()
    try:
        assert mb.list() == [(b"7", 1234), (b"9", 99)]
        assert mb.fetch_headers(b"7") == b"X: y\r\n"
    finally:
        mb.close()
    calls = fake_imap.instances[0].calls
    assert ("create", '"Quarantine"') in calls and ("create", '"Processed"') not in calls
    assert ("select", '"INBOX"') in calls


def test_imap_move_falls_back_to_copy_delete(fake_imap):
    fake_imap.move_ok = False
    mb = box()
    mb.open()
    mb.move(b"7", "Processed")
    mb.close()
    calls = fake_imap.instances[0].calls
    assert ("uid", "COPY", b"7", '"Processed"') in calls
    assert ("uid", "STORE", b"7", "+FLAGS.SILENT", "(\\Deleted)") in calls and ("expunge",) in calls


def test_imap_login_failure_raises(fake_imap):
    fake_imap.login_ok = False
    mb = box()
    with pytest.raises(imaplib.IMAP4.error):
        mb.open()
    mb.close()


def test_imap_test_reports_authserv_ids(fake_imap, monkeypatch):
    monkeypatch.setattr(FakeIMAP, "uid", lambda self, cmd, *a: (
        ("OK", [b"1 (UID 7 RFC822.SIZE 10)"]) if a[1] == "(RFC822.SIZE)" else
        ("OK", [(b"1 (UID 7 BODY[HEADER] {9}", b"Authentication-Results: mx.example.org; dkim=pass\r\n\r\n"), b")"])))
    out = box().test()
    assert out["ok"] and out["messages"] == 1 and out["authserv_ids"] == ["mx.example.org"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_inbound.py`
Expected: FAIL (`ModuleNotFoundError: notify.providers.email_inbound`).

- [ ] **Step 4: Implement `notify/providers/email_inbound.py`**

```python
"""Reply mailbox of the email provider: IMAP over implicit TLS (imaplib, run in worker threads).

`ImapMailbox` is the only code that speaks IMAP. `poll_once` makes one pass: list, judge each message on
its headers, fetch and parse accepted mail off the event loop, store, then move it to Processed or
Quarantine. The database commit always happens before the move, so a crash in between ends as a
duplicate on the next pass, never as a second reply.
"""

from __future__ import annotations

import asyncio
import imaplib
import logging
import re
import sqlite3
import ssl
import threading
from typing import TYPE_CHECKING, Any

from .. import replyparse
from ..replies import Verdict

if TYPE_CHECKING:
    from ..core import NotifyService
    from .email import EmailProvider

log = logging.getLogger("notify.inbound")

MAX_MESSAGE_BYTES = 15728640
SESSION_SECONDS = 120


class MailboxError(Exception):
    pass


def _q(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ok(resp: tuple[str, Any], what: str) -> Any:
    typ, data = resp
    if typ != "OK":
        raise MailboxError(f"{what} failed: {data!r}"[:300])
    return data


class ImapMailbox:
    def __init__(self, host: str, port: int, user: str, password: str, *, verify_tls: bool = True,
                 inbox: str = "INBOX", processed: str = "Processed", quarantine: str = "Quarantine",
                 timeout: float = SESSION_SECONDS):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.verify_tls = verify_tls
        self.inbox, self.processed, self.quarantine = inbox, processed, quarantine
        self.timeout = timeout
        self._imap: Any = None
        self._timer: threading.Timer | None = None

    @classmethod
    def for_provider(cls, p: EmailProvider) -> ImapMailbox:
        return cls(p.imap_host, p.imap_port, p.imap_user, p.imap_password, verify_tls=p.verify_tls,
                   inbox=p.reply_inbox, processed=p.reply_processed, quarantine=p.reply_quarantine)

    def _kill(self) -> None:
        """Hard session deadline: close the socket so a stalled server cannot hold the poller."""
        sock = getattr(self._imap, "sock", None)
        for op in (lambda: sock.shutdown(2), lambda: sock.close()):
            try:
                op()
            except Exception:
                pass

    def open(self) -> None:
        ctx = ssl.create_default_context()
        if not self.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._timer = threading.Timer(self.timeout, self._kill)
        self._timer.daemon = True
        self._timer.start()
        self._imap = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ctx, timeout=30)
        self._imap.login(self.user, self.password)
        have = self.folders()
        for f in (self.processed, self.quarantine):
            if f not in have:
                self._imap.create(_q(f))  # result ignored: it may exist with other flags or case
        _ok(self._imap.select(_q(self.inbox)), f"select {self.inbox}")

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None

    def folders(self) -> set[str]:
        out = set()
        for line in _ok(self._imap.list(), "list") or []:
            if isinstance(line, bytes):
                m = re.match(rb'\([^)]*\) (?:"[^"]*"|NIL) (.+)$', line)
                if m:
                    out.add(m.group(1).decode(errors="replace").strip('"'))
        return out

    def list(self) -> list[tuple[bytes, int]]:
        typ, data = self._imap.uid("FETCH", "1:*", "(RFC822.SIZE)")
        if typ != "OK":
            return []  # some servers answer NO on an empty mailbox
        out = []
        for item in data or []:
            line = item[0] if isinstance(item, tuple) else item
            if isinstance(line, bytes):
                u, s = re.search(rb"UID (\d+)", line), re.search(rb"RFC822\.SIZE (\d+)", line)
                if u:
                    out.append((u.group(1), int(s.group(1)) if s else 0))
        return out

    def _fetch(self, uid: bytes, what: str) -> bytes:
        data = _ok(self._imap.uid("FETCH", uid, what), f"fetch {uid!r}")
        part = next((x for x in data or [] if isinstance(x, tuple)), None)
        if part is None:
            raise MailboxError(f"message {uid!r} vanished")
        return part[1]

    def fetch_headers(self, uid: bytes) -> bytes:
        return self._fetch(uid, "(BODY.PEEK[HEADER])")

    def fetch(self, uid: bytes) -> bytes:
        return self._fetch(uid, "(BODY.PEEK[])")

    def move(self, uid: bytes, folder: str) -> None:
        try:
            if self._imap.uid("MOVE", uid, _q(folder))[0] == "OK":
                return
        except imaplib.IMAP4.error:
            pass  # no MOVE extension
        _ok(self._imap.uid("COPY", uid, _q(folder)), f"copy to {folder}")
        _ok(self._imap.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)"), "flag deleted")
        self._imap.expunge()

    def test(self) -> dict[str, Any]:
        """Admin connection test: log in, check folders, show which authserv-ids the newest message carries."""
        try:
            self.open()
            items = self.list()
            ids: list[str] = []
            if items:
                head = replyparse.parse_headers(self.fetch_headers(items[-1][0]))
                for k, v in head.raw_items():
                    if k.lower() == "authentication-results":
                        ident = str(v).split(";")[0].strip().split(" ")[0]
                        if ident and ident not in ids:
                            ids.append(ident)
            return {"ok": True, "messages": len(items), "folders": sorted(self.folders()), "authserv_ids": ids}
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if len(self.password) >= 4:
                err = err.replace(self.password, "***")
            return {"ok": False, "error": err[:300]}
        finally:
            self.close()


async def poll_once(svc: NotifyService, provider: EmailProvider, mailbox: Any) -> dict[str, int]:
    replies = svc.replies
    stats = {"accepted": 0, "quarantined": 0, "duplicates": 0}
    await asyncio.to_thread(mailbox.open)
    try:
        for uid, size in await asyncio.to_thread(mailbox.list):
            raw_headers = await asyncio.to_thread(mailbox.fetch_headers, uid)
            head = replyparse.parse_headers(raw_headers)
            try:
                verdict = (Verdict("quarantine", "too_large") if size > MAX_MESSAGE_BYTES
                           else replies.classify(head, raw_headers, provider.authserv_id))
                if verdict.action == "accept":
                    raw = await asyncio.to_thread(mailbox.fetch, uid)
                    parsed = await asyncio.to_thread(replyparse.parse_reply, raw)
                    del raw
                    try:
                        replies.store(verdict, replyparse.message_key(head, raw_headers), parsed)
                    except sqlite3.IntegrityError:
                        verdict = Verdict("duplicate")
            except (MailboxError, OSError, imaplib.IMAP4.error):
                raise  # connection trouble: stop this pass, retry later
            except Exception:
                log.exception("reply could not be read", extra={"uid": uid.decode(errors="replace")})
                verdict = Verdict("quarantine", "unreadable")
            if verdict.action == "quarantine":
                replies.quarantine(head, verdict)
                stats["quarantined"] += 1
            else:
                stats["accepted" if verdict.action == "accept" else "duplicates"] += 1
            folder = mailbox.quarantine if verdict.action == "quarantine" else mailbox.processed
            await asyncio.to_thread(mailbox.move, uid, folder)
    finally:
        await asyncio.to_thread(mailbox.close)
    return stats
```

Note: a quarantined `unreadable` message after a failed `store` cannot happen (store is the last step), so no reply row is left behind.

- [ ] **Step 5: Provider hooks**

In `notify/providers/email.py`, add after `message_id`:

```python
    def mailbox(self) -> Any:
        from .email_inbound import ImapMailbox
        return ImapMailbox.for_provider(self)
```

and replace `test_connection` with:

```python
    async def test_connection(self) -> dict:
        problems = self.config_problems()
        if problems:
            return {"supported": True, "ok": False, "error": "; ".join(problems)}
        out = await asyncio.to_thread(self._test_sync)
        if any(getattr(self, k) for k in _REPLY_KEYS):
            rp = self.replies_problems()
            out["imap"] = {"ok": False, "error": "; ".join(rp)} if rp else await asyncio.to_thread(self.mailbox().test)
        return out
```

- [ ] **Step 6: Worker loop**

In `notify/worker.py`: add `from .providers.email_inbound import poll_once` to the imports; in `Workers.__init__` add `self.inbound_interval = 30.0`; in `start()` add `self._inbound_loop()` to the tuple of coroutines; add the loop after `_receipt_loop`:

```python
    # ------------------------------------------------------------------ replies
    async def _inbound_loop(self) -> None:
        failures: dict[str, int] = {}
        next_at: dict[str, float] = {}

        async def once() -> None:
            start = time.monotonic()
            while time.monotonic() - start < self.inbound_interval:  # re-read each second: tests shorten it
                await asyncio.sleep(min(1.0, self.inbound_interval))
            svc = self.service
            for name, p in list(svc.providers.items()):
                if not getattr(p, "replies_ready", False) or time.monotonic() < next_at.get(name, 0.0):
                    continue
                status = svc.inbound_status.setdefault(name, {"last_success": None, "last_error": None})
                try:
                    mailbox = svc.open_mailbox(p) if svc.open_mailbox else p.mailbox()  # type: ignore[attr-defined]
                    status["last_stats"] = await poll_once(svc, p, mailbox)  # type: ignore[arg-type]
                    status.update(last_success=now_iso(), last_error=None)
                    failures[name] = 0
                except Exception as e:
                    failures[name] = failures.get(name, 0) + 1
                    delay = min(900.0, 10.0 * 3 ** (failures[name] - 1))
                    next_at[name] = time.monotonic() + delay
                    status["last_error"] = f"{type(e).__name__}: {e}"[:300]
                    log.warning("reply mailbox check failed",
                                extra={"provider": name, "error": status["last_error"], "retry_in": delay})
            svc.replies.sweep_unclaimed()
        await self._guard("inbound", once, 30.0)
```

In `notify/api.py` `metrics`, change `("dispatcher", "receipts", "webhooks")` to `("dispatcher", "receipts", "webhooks", "inbound")`.

- [ ] **Step 7: Run tests**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_inbound.py tests/test_replies_core.py tests/test_email.py`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add notify/providers/email_inbound.py notify/providers/email.py notify/worker.py notify/api.py tests/mailfactory.py tests/conftest.py tests/test_inbound.py
git commit -m "Replies: IMAP reply mailbox, inbound poller and worker loop"
```

---

### Task 6: REST and MCP

**Files:**
- Modify: `notify/api.py` (three endpoints)
- Modify: `notify/mcp_server.py` (`INSTRUCTIONS`, `_compact`, `send_notification`, `get_replies`)
- Test: `tests/test_replies_api.py`

**Interfaces:**
- Consumes: `NotifyService.replies` (Task 4), `poll_once` + `FakeMailbox` (Task 5).
- Produces REST: `POST /v1/notifications/{nid}/replies/claim?waiter=W&wait=N` → 200 reply JSON / 204; `GET /v1/notifications/{nid}/replies` → `{"items": [...]}`; `GET /v1/replies/{rid}/attachments/{idx}` → bytes.
- Produces MCP: `send_notification(..., reply: bool = False, in_reply_to: str | None = None)`; tool `get_replies(notification_id) -> {"items": [...]}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_replies_api.py`:

```python
from __future__ import annotations

import threading
import time

import notify.providers.email_inbound as inbound
from tests.conftest import PNG, wait_for
from tests.mailfactory import reply_raw
from tests.test_mcp import payload, run_session


def send(srv) -> tuple[str, str]:
    with srv.client() as c:
        out = c.post("/v1/notifications", json={"targets": ["device:alex-email"], "message": "Pick", "title": "Pick",
                                                 "kinds": ["email"], "reply": True}).json()
        wait_for(lambda: c.get(f"/v1/notifications/{out['id']}").json()["deliveries"][0]["state"] == "dispatched")
    return out["id"], out["deliveries"][0]["id"]


def arrive(srv, raw: bytes) -> None:
    srv.mailbox.add(raw)
    srv.call(lambda: inbound.poll_once(srv.service, srv.service.providers["mail"], srv.mailbox))


def test_claim_long_poll_and_download(email_live):
    nid, did = send(email_live)
    out: dict = {}

    def wait() -> None:
        with email_live.client() as c:
            out["r"] = c.post(f"/v1/notifications/{nid}/replies/claim", params={"waiter": "abcdefgh12", "wait": 20},
                              timeout=30)
    t = threading.Thread(target=wait)
    t.start()
    time.sleep(0.5)
    arrive(email_live, reply_raw(did, attachment=PNG))
    t.join(15)
    r = out["r"]
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "Go with B." and body["notification_title"] == "Pick"
    with email_live.client() as c:
        again = c.post(f"/v1/notifications/{nid}/replies/claim", params={"waiter": "abcdefgh12", "wait": 0})
        assert again.json()["id"] == body["id"]
        assert c.post(f"/v1/notifications/{nid}/replies/claim", params={"waiter": "zzzzzzzz12", "wait": 0}).status_code == 204
        a = c.get(body["attachments"][0]["url"])
        assert a.status_code == 200 and a.content == PNG and a.headers["content-type"] == "image/png"
        assert "attachment; filename*=UTF-8''shot.png" == a.headers["content-disposition"]
        assert c.get(f"/v1/notifications/{nid}/replies").json()["items"][0]["state"] == "claimed"
    with email_live.client("other") as o:
        assert o.get(body["attachments"][0]["url"]).status_code == 404
        assert o.post(f"/v1/notifications/{nid}/replies/claim", params={"waiter": "abcdefgh12", "wait": 0}).status_code == 404


def test_claim_validation_and_scope(email_live):
    nid, _ = send(email_live)
    with email_live.client() as c:
        assert c.post(f"/v1/notifications/{nid}/replies/claim", params={"waiter": "bad id!", "wait": 0}).status_code == 422
        assert c.post(f"/v1/notifications/{nid}/replies/claim", params={"waiter": "abcdefgh12", "wait": 99}).status_code == 422
    with email_live.client("sender") as s:
        assert s.post(f"/v1/notifications/{nid}/replies/claim", params={"waiter": "abcdefgh12"}).status_code == 403


def test_mcp_reply_send_and_get_replies(email_live):
    async def flow(s):
        sent = payload(await s.call_tool("send_notification", {
            "targets": ["device:alex-email"], "message": "Pick", "kinds": ["email"], "reply": True}))
        return sent
    sent = run_session(email_live, "agent", flow)
    assert sent["reply"] is True and sent["conversation_id"] == sent["id"]
    did = sent["deliveries"][0]["id"]
    with email_live.client() as c:
        wait_for(lambda: c.get(f"/v1/notifications/{sent['id']}").json()["deliveries"][0]["state"] == "dispatched")
    arrive(email_live, reply_raw(did))

    async def look(s):
        tools = {t.name for t in (await s.list_tools()).tools}
        return tools, payload(await s.call_tool("get_replies", {"notification_id": sent["id"]}))
    tools, replies = run_session(email_live, "agent", look)
    assert "get_replies" in tools and replies["items"][0]["text"] == "Go with B."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_replies_api.py`
Expected: FAIL (404 on the claim endpoint).

- [ ] **Step 3: REST endpoints**

In `notify/api.py`, add `import re` and `from urllib.parse import parse_qsl, quote` (extend the existing `parse_qsl` import), then after the `retry` endpoint:

```python
# ------------------------------------------------------------------ replies

@router.post("/v1/notifications/{nid}/replies/claim", tags=["replies"], responses={204: {"description": "no reply yet"}})
@_errors
async def claim_reply(nid: str, request: Request,
                      waiter: str = Query(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$",
                                          description="Opaque id of this wait; a retry with the same id gets the same reply"),
                      wait: float = Query(default=50, ge=0, le=55, description="Seconds to wait for a reply"),
                      client: Client = Depends(require(Scope.read))) -> Response:
    out = await service(request).replies.claim(client, nid, waiter, wait)
    return Response(status_code=204) if out is None else JSONResponse(out)


@router.get("/v1/notifications/{nid}/replies", tags=["replies"])
@_errors
async def list_replies(nid: str, request: Request, client: Client = Depends(require(Scope.read))) -> dict[str, Any]:
    return {"items": service(request).replies.list(client, nid)}


@router.get("/v1/replies/{rid}/attachments/{idx}", tags=["replies"])
@_errors
async def reply_attachment(rid: str, idx: int, request: Request,
                           client: Client = Depends(require(Scope.read))) -> Response:
    filename, ctype, data = service(request).replies.attachment(client, rid, idx)
    if not re.fullmatch(r"[\w.+-]+/[\w.+-]+", ctype):
        ctype = "application/octet-stream"
    return Response(content=data, media_type=ctype,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
                             "X-Content-Type-Options": "nosniff"})
```

- [ ] **Step 4: MCP**

In `notify/mcp_server.py`, append to `INSTRUCTIONS` (before the closing `"""`):

```
Replies by email: send_notification(..., kinds=["email"], reply=true) lets the
recipient answer by replying to the mail. To wait for the answer, start
`notify-wait <notification_id>` as a background shell job: it exits with the
reply text (and saved attachment paths) when the reply arrives, or with code 2
after its timeout. Act on the reply, then run notify-wait again or send a
follow-up with in_reply_to=<id> to continue the conversation. Keep a waiter
running while you work on something long: a reply nobody collects within 10
minutes gets an automatic "not delivered" answer. get_replies lists replies
without collecting them. Reply text passed sender and DKIM checks: it comes
from the person the mail was sent to.
```

In `_compact`, add as the last entry of the returned dict:

```python
        **{k: n[k] for k in ("reply", "conversation_id", "in_reply_to") if k in n},
```

In `send_notification`, add parameters after `attachments`:

```python
        reply: bool = False,
        in_reply_to: str | None = None,
```

extend the docstring with:

```
        reply: email only — the recipient can answer by replying; wait for it with `notify-wait <id>`.
        in_reply_to: id of an earlier notification this one follows up (threads the email, same conversation).
```

and pass `reply=reply, in_reply_to=in_reply_to` into the `NotificationIn(...)` call.

Add the tool after `cancel_notification`:

```python
    @mcp.tool()
    async def get_replies(ctx: Context, notification_id: str) -> dict[str, Any]:
        """Replies in the conversation of a reply-enabled notification, oldest first, with their state
        (waiting, claimed, unclaimed). Collects nothing: use `notify-wait <id>` to wait for and collect one."""
        client = _client(ctx)
        _need(client, Scope.read)
        return {"items": await call(svc().replies.list, client, notification_id)}
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_replies_api.py tests/test_mcp.py tests/test_api.py`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add notify/api.py notify/mcp_server.py tests/test_replies_api.py
git commit -m "Replies: claim, list and attachment endpoints; MCP reply fields and get_replies"
```

---

### Task 7: `notify-wait` CLI

**Files:**
- Create: `scripts/notify-wait` (executable, stdlib only)
- Test: `tests/test_notify_wait.py`

**Interfaces:**
- Consumes: REST endpoints from Task 6.
- Produces: functions `parse_duration(s) -> int`, `mcp_entry(path) -> dict`, `settings(url, entry) -> tuple[str, str]`, `safe_filename(name, taken: set[str]) -> str`, `claim_loop(base, token, nid, deadline) -> dict | None`, `save_attachments(base, token, reply, save_dir) -> list[str]`, `render(reply, paths) -> str`, `main(argv=None) -> int`; exit codes `0` reply, `1` error, `2` timeout.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_notify_wait.py`:

```python
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

import pytest

import notify.providers.email_inbound as inbound
from tests.conftest import PNG, TOKENS, wait_for
from tests.mailfactory import reply_raw

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "notify-wait"


def load():
    loader = importlib.machinery.SourceFileLoader("notify_wait", str(SCRIPT))
    spec = importlib.util.spec_from_loader("notify_wait", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


nw = load()


def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)


def test_parse_duration():
    assert nw.parse_duration("90s") == 90 and nw.parse_duration("30m") == 1800
    assert nw.parse_duration("2h") == 7200 and nw.parse_duration("5") == 300
    with pytest.raises(Exception):
        nw.parse_duration("soon")


def test_safe_filename():
    taken: set[str] = set()
    assert nw.safe_filename("../../.bashrc", taken) == "bashrc"
    assert nw.safe_filename("a/b\\c\x00d.png", taken) == "cd.png"
    assert nw.safe_filename("", taken) == "attachment"
    assert nw.safe_filename("shot.png", taken) == "shot.png"
    assert nw.safe_filename("SHOT.png", taken) == "SHOT-2.png"
    assert nw.safe_filename("😀 ontwerp.png", taken) == "😀 ontwerp.png"


def test_settings_from_claude_json(tmp_path, monkeypatch):
    f = tmp_path / "claude.json"
    f.write_text(json.dumps({"mcpServers": {"hnl-notify": {"type": "http", "url": "http://notify.lan:8930/mcp",
                                                           "headers": {"Authorization": "Bearer tok-x"}}}}))
    monkeypatch.delenv("NOTIFY_URL", raising=False)
    monkeypatch.delenv("NOTIFY_TOKEN", raising=False)
    assert nw.settings(None, nw.mcp_entry(f)) == ("http://notify.lan:8930", "tok-x")
    monkeypatch.setenv("NOTIFY_TOKEN", "tok-env")
    assert nw.settings("http://other:1/", nw.mcp_entry(f)) == ("http://other:1", "tok-env")
    assert nw.mcp_entry(tmp_path / "missing.json") == {}
    monkeypatch.delenv("NOTIFY_TOKEN")
    with pytest.raises(nw.Fatal):
        nw.settings(None, {})


def send(srv) -> tuple[str, str]:
    with srv.client() as c:
        out = c.post("/v1/notifications", json={"targets": ["device:alex-email"], "message": "Pick", "title": "Pick",
                                                 "kinds": ["email"], "reply": True}).json()
        wait_for(lambda: c.get(f"/v1/notifications/{out['id']}").json()["deliveries"][0]["state"] == "dispatched")
    return out["id"], out["deliveries"][0]["id"]


def test_cli_saves_attachment(email_live, tmp_path, monkeypatch, capsys):
    nid, did = send(email_live)
    email_live.mailbox.add(reply_raw(did, attachment=PNG))
    email_live.call(lambda: inbound.poll_once(email_live.service, email_live.service.providers["mail"], email_live.mailbox))
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    (tmp_path / "shot.png").write_bytes(b"old")  # existing file is never overwritten
    code = nw.main([nid, "--url", email_live.url, "--save-dir", str(tmp_path), "--timeout", "30s"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert out.startswith("Reply from alex@example.org (") and 'to "Pick"' in out and "Go with B." in out
    assert (tmp_path / "shot-2.png").read_bytes() == PNG and (tmp_path / "shot.png").read_bytes() == b"old"
    assert f"Attachments: {tmp_path / 'shot-2.png'}" in out
    assert TOKENS["agent"] not in out


def test_cli_json_and_timeout(email_live, monkeypatch, capsys):
    nid, did = send(email_live)
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    assert nw.main([nid, "--url", email_live.url, "--timeout", "2s"]) == 2
    assert "No reply within 2 s" in capsys.readouterr().out
    email_live.mailbox.add(reply_raw(did))
    email_live.call(lambda: inbound.poll_once(email_live.service, email_live.service.providers["mail"], email_live.mailbox))
    assert nw.main([nid, "--url", email_live.url, "--timeout", "10s", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "Go with B."


def test_cli_errors(email_live, monkeypatch, capsys):
    nid, _ = send(email_live)
    monkeypatch.setenv("NOTIFY_TOKEN", "wrong")
    assert nw.main([nid, "--url", email_live.url, "--timeout", "5s"]) == 1
    err = capsys.readouterr().err
    assert "HTTP 401" in err and "wrong" not in err
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    assert nw.main(["ntf_nothere", "--url", email_live.url, "--timeout", "5s"]) == 1


def test_cli_unreachable_service_times_out(monkeypatch, capsys):
    monkeypatch.setenv("NOTIFY_TOKEN", "tok")
    monkeypatch.setattr(nw.time, "sleep", lambda s: None)
    assert nw.main(["ntf_x", "--url", "http://127.0.0.1:9", "--timeout", "2s"]) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_notify_wait.py`
Expected: FAIL (script missing).

- [ ] **Step 3: Implement `scripts/notify-wait`**

```python
#!/usr/bin/env python3
"""Wait for a reply to an hnl-notify notification and print it.

  notify-wait NOTIFICATION_ID [--timeout 60m] [--save-dir DIR] [--url URL] [--json]

Long-polls POST /v1/notifications/{id}/replies/claim with one waiter id per run, so a retry after a
lost response gets the same reply. Exit codes: 0 reply printed, 1 error, 2 no reply before --timeout.
Base URL: --url, else NOTIFY_URL, else the url of the hnl-notify entry in ~/.claude.json without /mcp.
Token: NOTIFY_TOKEN, else that entry's Authorization header. The token is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

EXIT_REPLY, EXIT_ERROR, EXIT_TIMEOUT = 0, 1, 2


class Fatal(Exception):
    pass


def parse_duration(s: str) -> int:
    """90s, 30m, 2h; a bare number means minutes."""
    m = re.fullmatch(r"(\d+)([smh]?)", s.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"not a duration: {s!r} (e.g. 90s, 30m, 2h)")
    return int(m.group(1)) * {"": 60, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def mcp_entry(path: Path = Path.home() / ".claude.json") -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    entry = (data.get("mcpServers") or {}).get("hnl-notify")
    for proj in (data.get("projects") or {}).values():
        entry = entry or ((proj or {}).get("mcpServers") or {}).get("hnl-notify")
    return entry or {}


def settings(url: str | None, entry: dict[str, Any]) -> tuple[str, str]:
    base = url or os.environ.get("NOTIFY_URL") or re.sub(r"/mcp/?$", "", entry.get("url") or "")
    auth = (entry.get("headers") or {}).get("Authorization") or ""
    token = os.environ.get("NOTIFY_TOKEN") or re.sub(r"^Bearer\s+", "", auth, flags=re.I)
    if not base:
        raise Fatal("no service URL: pass --url or set NOTIFY_URL")
    if not token:
        raise Fatal("no token: set NOTIFY_TOKEN or add the hnl-notify MCP entry to ~/.claude.json")
    return base.rstrip("/"), token


def safe_filename(name: str, taken: set[str]) -> str:
    base = name.replace("\\", "/").rpartition("/")[2]
    base = "".join(c for c in base if unicodedata.category(c)[0] != "C" and c not in ":").strip().lstrip(".")
    base = base[:120] or "attachment"
    stem, dot, ext = base.rpartition(".")
    if not dot or not stem:
        stem, ext = base, ""
    cand, n = base, 2
    while cand.lower() in taken:
        cand = f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}"
        n += 1
    taken.add(cand.lower())
    return cand


def _request(method: str, url: str, token: str, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {token}",
                                                              "User-Agent": "notify-wait"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def claim_loop(base: str, token: str, nid: str, deadline: float) -> dict[str, Any] | None:
    waiter = secrets.token_hex(12)
    failures = 0
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        wait = int(max(0, min(50, left)))
        url = (f"{base}/v1/notifications/{urllib.parse.quote(nid, safe='')}/replies/claim"
               f"?wait={wait}&waiter={waiter}")
        try:
            status, body = _request("POST", url, token, timeout=wait + 15)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404, 422):
                raise Fatal(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from None
            status, body = 0, b""
        except (urllib.error.URLError, OSError):
            status, body = 0, b""
        if status == 200:
            return json.loads(body)
        if status == 204:
            failures = 0
            if wait == 0:
                time.sleep(min(1.0, max(0.0, left)))
            continue
        failures += 1  # service restarting or unreachable: retry with the same waiter id
        time.sleep(min(20.0, 2.0 ** failures, max(0.0, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            return None


def save_attachments(base: str, token: str, reply: dict[str, Any], save_dir: str | None) -> list[str]:
    wanted = [a for a in reply.get("attachments") or [] if not a.get("purged")]
    if not wanted:
        return []
    d = Path(save_dir) if save_dir else Path(tempfile.mkdtemp(prefix="notify-reply-"))
    d.mkdir(parents=True, exist_ok=True)
    taken = {p.name.lower() for p in d.iterdir()}
    paths = []
    for a in wanted:
        try:
            _, data = _request("GET", base + a["url"], token, timeout=60)
        except (urllib.error.URLError, OSError) as e:
            raise Fatal(f"could not download {a['filename']!r}: {e}") from None
        path = d / safe_filename(a["filename"], taken)
        with open(path, "xb") as f:  # never follow or overwrite an existing file
            f.write(data)
        paths.append(str(path))
    return paths


def render(reply: dict[str, Any], paths: list[str]) -> str:
    when = datetime.fromisoformat(reply["received_at"].replace("Z", "+00:00")).astimezone()
    title = reply.get("notification_title") or reply["notification_id"]
    lines = [f'Reply from {reply["from"]} ({when:%Y-%m-%d %H:%M %Z}) to "{title}" [{reply["id"]}]:', reply["text"]]
    if reply.get("truncated"):
        lines.append("(reply cut at 8000 characters)")
    if paths:
        lines.append("Attachments: " + ", ".join(paths))
    if reply.get("dropped_attachments"):
        lines.append("Not kept (over the limit): " + ", ".join(reply["dropped_attachments"]))
    return "\n".join(lines)


def _fmt(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds >= 60 and seconds % 60 == 0 else f"{seconds} s"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="notify-wait", description="Wait for a reply to an hnl-notify notification.")
    ap.add_argument("notification_id")
    ap.add_argument("--timeout", type=parse_duration, default=3600, help="how long to wait (90s, 30m, 2h; default 60m)")
    ap.add_argument("--save-dir", help="where to save attachments (default: a new temporary folder)")
    ap.add_argument("--url", help="service base URL (default: NOTIFY_URL or ~/.claude.json)")
    ap.add_argument("--json", action="store_true", help="print the reply as JSON")
    args = ap.parse_args(argv)
    try:
        base, token = settings(args.url, mcp_entry())
        reply = claim_loop(base, token, args.notification_id, time.monotonic() + args.timeout)
        if reply is None:
            print(f"No reply within {_fmt(args.timeout)}; listener stopped. Later replies get an autoreply.")
            return EXIT_TIMEOUT
        paths = save_attachments(base, token, reply, args.save_dir)
        print(json.dumps({**reply, "saved": paths}, indent=2) if args.json else render(reply, paths))
        return EXIT_REPLY
    except Fatal as e:
        print(f"notify-wait: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
```

Run: `chmod +x scripts/notify-wait`

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_notify_wait.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/notify-wait tests/test_notify_wait.py
git commit -m "Replies: notify-wait CLI"
```

---

### Task 8: Admin UI — mailbox test result, replies and quarantine

**Files:**
- Modify: `notify/admin.py` (`GET /admin/api/replies`)
- Modify: `notify/admin_ui/app.js` (provider test output; Activity page replies block)
- Test: `tests/test_admin.py` (append)

**Interfaces:**
- Consumes: `Replies.recent`, `Replies.quarantined`, `NotifyService.inbound_status` (Tasks 4–5); `test_connection()["imap"]` (Task 5).
- Produces: `GET /admin/api/replies` → `{"inbound": {...}, "replies": [...], "quarantine": [...]}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_admin.py`:

```python
def test_admin_replies_view(email_live):
    from tests.mailfactory import reply_raw
    import notify.providers.email_inbound as inbound
    with email_live.client() as c:
        out = c.post("/v1/notifications", json={"targets": ["device:alex-email"], "message": "Pick",
                                                 "kinds": ["email"], "reply": True}).json()
        wait_for(lambda: c.get(f"/v1/notifications/{out['id']}").json()["deliveries"][0]["state"] == "dispatched")
    did = out["deliveries"][0]["id"]
    email_live.mailbox.add(reply_raw(did))
    email_live.mailbox.add(reply_raw(did, message_id="<m@x>", sender="mallory@example.org"))
    email_live.call(lambda: inbound.poll_once(email_live.service, email_live.service.providers["mail"],
                                              email_live.mailbox))
    with email_live.client("admin") as a:
        r = a.get("/admin/api/replies")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replies"][0]["text"] == "Go with B."
    assert body["quarantine"][0]["reason"] == "sender_mismatch"
    with email_live.client("agent") as c:
        assert c.get("/admin/api/replies").status_code in (401, 403)
```

(`wait_for` is already imported in `tests/test_admin.py`; if not, add `from tests.conftest import wait_for`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_admin.py::test_admin_replies_view`
Expected: FAIL (404).

- [ ] **Step 3: Endpoint**

In `notify/admin.py`, after the `activity` endpoint:

```python
@router.get("/api/replies")
async def replies_view(request: Request, limit: int = 25, _: Client = Depends(admin)) -> dict[str, Any]:
    svc = _svc(request)
    return {"inbound": svc.inbound_status, "replies": svc.replies.recent(limit),
            "quarantine": svc.replies.quarantined(limit)}
```

- [ ] **Step 4: Admin UI**

In `notify/admin_ui/app.js`, in the `providers` view test handler, replace the line that sets `out.textContent = \`Connection works, ...\`` with:

```js
        let text = `Connection works, ${tls}. ${r.size_limit ? `The server accepts messages up to ${fmtSize(r.size_limit)}.` : "The server announces no size limit."}`;
        if (r.imap) {
          text += r.imap.ok
            ? ` Reply mailbox works: ${r.imap.messages} waiting, folders ${r.imap.folders.join(", ")}.${r.imap.authserv_ids.length ? ` Its newest message names ${r.imap.authserv_ids.join(", ")} in Authentication-Results.` : ""}`
            : ` Reply mailbox: ${r.imap.error}`;
        }
        out.textContent = text;
```

In the `activity` view, add `<div class="block" id="replies"></div>` after the `attention` block and call `loadReplies(main.querySelector("#replies"));` after `loadRecent(...)`. Add this function after `loadRecent`:

```js
async function loadReplies(el) {
  let data;
  try { data = await api("GET", "/replies?limit=10"); }
  catch (ex) { el.innerHTML = `<div class="empty problem">${esc(ex.message)}</div>`; return; }
  const status = Object.entries(data.inbound).map(([name, s]) => `<div class="sub">${esc(name)}: ${s.last_error
    ? `<span class="problem">${esc(s.last_error)}</span>` : s.last_success ? `checked ${esc(when(s.last_success))}` : "not checked yet"}</div>`).join("");
  if (!data.replies.length && !data.quarantine.length && !status) { el.innerHTML = ""; return; }
  el.innerHTML = `<h3>Replies</h3>${status}<div class="rows">
    ${data.replies.map((r) => `<div class="row"><span class="lamp ${r.state === "unclaimed" ? "warn" : "ok"}"></span><div class="grow">
      <div class="title">${esc(r.notification_title || r.subject || "(no title)")}</div>
      <div class="sub">${esc(when(r.received_at))} from ${esc(r.from)}: ${esc(r.state)}${r.claimed_by ? ` by ${esc(r.claimed_by)}` : ""}${r.autoreply_notification_id ? ", autoreply sent" : ""}</div>
      <div class="sub">${esc(r.text.slice(0, 200))}</div>
      ${r.attachments.length ? `<div class="sub">${r.attachments.map((a) => esc(a.filename)).join(", ")}</div>` : ""}</div></div>`).join("")}
    ${data.quarantine.map((q) => `<div class="row"><span class="lamp bad"></span><div class="grow">
      <div class="title">Rejected: ${esc(q.subject || "(no subject)")}</div>
      <div class="sub">${esc(when(q.received_at))} from ${esc(q.from_address || "?")}: ${esc(q.reason)}</div></div></div>`).join("")}
  </div>`;
}
```

- [ ] **Step 5: Run tests and check the page by hand**

Run: `.venv/bin/python -m pytest -q -p no:warnings tests/test_admin.py`
Expected: PASS

Then start the demo server (`.venv/bin/python scripts/dev_server.py --demo --password devpass`), open `/admin#activity` and `/admin#providers` in the headless browser (`cdp` toolchest tool) and check that both pages render without console errors.

- [ ] **Step 6: Commit**

```bash
git add notify/admin.py notify/admin_ui/app.js tests/test_admin.py
git commit -m "Replies: admin view of replies, quarantine and reply mailbox test"
```

---

### Task 9: Documentation, version 0.6.0, full suite

**Files:**
- Modify: `pyproject.toml`, `notify/__init__.py` (version `0.6.0`)
- Modify: `docs/architecture.md` (new section "Replies"; email table row; data model; REST list; MCP; single-process note)
- Modify: `README.md` (section "Replies to agents"; upgrade/rollback note)

- [ ] **Step 1: Version**

Set `version = "0.6.0"` in `pyproject.toml` and `__version__ = "0.6.0"` in `notify/__init__.py`.

- [ ] **Step 2: Architecture doc**

In `docs/architecture.md`:
- In "What email can and cannot report", add a row: `| Reply from the recipient? | Only when a reply mailbox is configured and the request sets reply | inbound IMAP poll; delivery → interacted when an agent collects the first reply |`.
- In "Data model (SQLite)", add bullets for `replies`, `reply_attachments`, `reply_quarantine` and the new notification/delivery columns (one line each, as in the spec §2/§4).
- Add a section `## Replies` after "What email can and cannot report", summarising spec §3–§5 in the doc's style: polling every 30 s, the per-message checks in order (with the first-header DKIM rule and why), conversations, claim with waiter id, the 2-minute listener and 10-minute grace, the autoreply as a system notification, and the accepted residual risk from spec §0. State that the claim long-poll wakes through the in-process event bus, which relies on the single-process design.
- In "REST API", add the three endpoints; in "MCP", mention `reply`, `in_reply_to` and `get_replies`.

- [ ] **Step 3: README**

Add a section `## Replies to agents` after "Email and attachments": admin setup (reply mailbox fields, Test connection, entering `authserv_id`), an example request with `"reply": true`, the `notify-wait` usage block from Task 7 and its exit codes, the autoreply behaviour, and the quarantine. In "Deploy", add: "Upgrading to 0.6.0 migrates the database in place. Rolling back to 0.5.1 requires restoring the pre-0.6.0 database backup." Use only `alex`, `example.org`, `notify.lan` in examples.

- [ ] **Step 4: Full suite and anonymisation check**

Run: `.venv/bin/python -m pytest -q -p no:warnings`
Expected: all PASS.

Run the anonymisation grep from the host documentation (owner name, own domain, LAN host addresses, mail provider names) over `notify tests scripts docs README.md`.
Expected: no matches.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml notify/__init__.py docs/architecture.md README.md
git commit -m "Document replies to agents; 0.6.0"
```

---

### Task 10: Deploy and live end-to-end (with the owner)

Host-specific values for this task are in the project memory, not in the repo. Nothing here is committed except documentation outside the repo.

- [ ] **Step 1: Back up and deploy**

```bash
cd <repo>
cp data/notify.sqlite3 data/notify.sqlite3.bak.$(date +%Y%m%d)-pre-0.6.0
docker compose up -d --build
docker logs --tail 30 hnl-notify
scripts/smoke.sh
```

Expected: container healthy, log shows `hnl-notify started` with version 0.6.0, smoke checks pass.

- [ ] **Step 2: Configure the reply mailbox (owner, in the admin UI)**

Ask the owner to open Admin → Providers → the email provider → Edit, fill in reply address, IMAP server, port 993, username, password and `authserv_id`, save, and press **Test connection**. The result must show "Reply mailbox works" and list the authserv-id in use; it must match the entered value.

- [ ] **Step 3: Install `notify-wait`**

```bash
ln -sf "$PWD/scripts/notify-wait" ~/scripts/bin/notify-wait
notify-wait --help
```

Add a row for `notify-wait` to `~/scripts/README.md`. For flax-dev, ask the owner whether agents there should get it too (copy the single file into its `~/scripts/bin`).

- [ ] **Step 4: Live end-to-end**

1. Send via MCP: `send_notification(targets=["user:<owner>"], kinds=["email"], reply=true, title="Reply test", message="Reply with any text and a picture.")`, then run `notify-wait <id> --timeout 15m` as a background job. Ask the owner to reply with text and an image. Expected: the job exits 0 with the text only (no quoted history) and a saved image path.
2. Send a follow-up with `in_reply_to=<id>` and `reply=true`, run `notify-wait <follow-up id>`, and ask the owner to reply to the **first** mail in the Gmail thread. Expected: the waiter wakes with that reply.
3. With no waiter running, ask the owner to reply once more. Expected: after 10–11 minutes an autoreply arrives in the same thread, and Admin → Activity shows the reply as unclaimed with "autoreply sent".
4. Send a mail to the reply mailbox from another address (or reuse `smtp-send-eml` with a forged header). Expected: it appears under Replies → Rejected with `sender_mismatch` or `dkim_fail`.

- [ ] **Step 5: Host documentation and follow-up**

- Update `~/documentation/notifications.md` (agent usage of `reply` + `notify-wait`, with the real mailbox address) and the hnl-notify row in `~/documentation/projects.md` (0.6.0 deployed, date, backup file name, rollback note).
- Remind the owner to change the reply mailbox password (it was shared in chat on 2026-10-14) and enter the new one in the admin UI.
- Update the project memory: feature deployed, live test results.
