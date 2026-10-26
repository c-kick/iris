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


HANDLES: dict[str, str] = {}  # conversation id -> reply handle, as the sender of that conversation would keep it


def send(srv, **body) -> tuple[str, str]:
    """Reply-enabled email notification; returns (notification id, delivery id) once dispatched.
    A follow-up passes the conversation's handle unless the test gives one."""
    if body.get("in_reply_to") and "reply_handle" not in body:
        conv = srv.service.db.scalar("SELECT conversation_id FROM notifications WHERE id=?", (body["in_reply_to"],))
        if conv in HANDLES:
            body["reply_handle"] = HANDLES[conv]
    with srv.client() as c:
        out = c.post("/v1/notifications", json={"targets": ["device:alex-email"], "message": "Pick", "title": "Pick",
                                                 "kinds": ["email"], "reply": True, **body}).json()
        wait_for(lambda: c.get(f"/v1/notifications/{out['id']}").json()["deliveries"][0]["state"] == "dispatched")
    if out.get("reply_handle"):
        HANDLES[out["reply_handle"].split(":", 1)[0]] = out["reply_handle"]
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


def _hidden_from(did: str) -> bytes:
    """Replay attack: a validly signed mail from mallory with an extra `From: alex` put in front of a line
    Python's parser does not recognise, so Python would see only the extra From."""
    raw = reply_raw(did, sender="mallory@example.org")
    first, rest = raw.split(b"\r\n", 1)
    return first + b"\r\nFrom: alex@example.org\r\nX-Pad : 1\r\n" + rest


@pytest.mark.parametrize("make,reason", [
    (lambda did: reply_raw(did, extra=[("Auto-Submitted", "auto-replied")]), "automatic"),
    (lambda did: reply_raw("dlv_" + "f" * 22), "no_thread"),
    (lambda did: reply_raw(did, sender="mallory@example.org"), "sender_mismatch"),
    (lambda did: reply_raw(did, auth_results=["mx.example.org; dkim=none",
                                              "mx.example.org; dkim=pass header.d=example.org"]), "dkim_fail"),
    (lambda did: reply_raw(did, auth_results=[]), "dkim_fail"),
    (lambda did: _hidden_from(did), "malformed_headers"),
])
def test_quarantine_reasons(email_live, make, reason):
    nid, did = send(email_live)
    assert deliver(email_live, make(did)) is None
    row = email_live.service.db.one("SELECT reason, subject FROM reply_quarantine ORDER BY id DESC LIMIT 1")
    assert row["reason"] == reason
    if reason != "malformed_headers":  # Python's parser never reaches the subject of a malformed block
        assert row["subject"] == "Re: [home] Pick a design"
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
    claim(email_live, nid)
    assert email_live.service.replies.attachment(AGENT, rid, 0) == ("shot.png", "image/png", PNG)
    with pytest.raises(NotFound):
        email_live.service.replies.attachment(OTHER, rid, 0)
    with email_live.service.db.tx() as c:
        c.execute("UPDATE replies SET received_at='2000-01-01T00:00:00.000000Z'")
    email_live.call(email_live.service.retention_sweep)
    with pytest.raises(NotFound):
        email_live.service.replies.attachment(AGENT, rid, 0)


ADMIN = Client("admin", frozenset({Scope.admin}))


def test_unclaimed_content_not_listed_or_downloadable(email_live):
    nid, did = send(email_live)
    rid = deliver(email_live, reply_raw(did, attachment=PNG))
    replies = email_live.service.replies
    r = replies.list(AGENT, nid)[0]
    assert r["state"] == "waiting" and r["text"] is None and r["attachments"] == [] and r["from"]
    assert r["subject"] is None
    assert replies.list(ADMIN, nid)[0]["text"] == "Go with B."
    assert replies.list(ADMIN, nid)[0]["subject"]
    with pytest.raises(NotFound):
        replies.attachment(AGENT, rid, 0)
    assert replies.attachment(ADMIN, rid, 0)[0] == "shot.png"
    claim(email_live, nid)
    assert replies.list(AGENT, nid)[0]["text"] == "Go with B."
    assert replies.attachment(AGENT, rid, 0)[0] == "shot.png"
    with email_live.service.db.tx() as c:
        c.execute("UPDATE replies SET state='unclaimed' WHERE id=?", (rid,))
    with pytest.raises(NotFound):
        replies.attachment(AGENT, rid, 0)
    assert replies.list(AGENT, nid)[0]["text"] is None


def test_sweep_survives_autoreply_failure(email_live, sent_mail):
    n1, d1 = send(email_live)
    n2, d2 = send(email_live)
    r1 = deliver(email_live, reply_raw(d1, message_id="<a@x>"))
    r2 = deliver(email_live, reply_raw(d2, message_id="<b@x>"))
    svc, replies = email_live.service, email_live.service.replies
    real, calls = svc.submit, []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(*a, **kw)

    svc.submit = flaky
    later = utcnow() + timedelta(seconds=GRACE_SECONDS + 60)
    replies.now = lambda: later
    assert email_live.call(replies.sweep_unclaimed) == 2
    assert len(calls) == 2
    rows = {r["id"]: r for r in svc.db.query("SELECT id, state, autoreply_notification_id FROM replies")}
    assert rows[r1]["state"] == rows[r2]["state"] == "unclaimed"
    assert sum(1 for r in rows.values() if r["autoreply_notification_id"]) == 1


def test_claimed_reply_never_swept(email_live):
    nid, did = send(email_live)
    rid = deliver(email_live, reply_raw(did))
    claim(email_live, nid)
    replies = email_live.service.replies
    replies.now = lambda: utcnow() + timedelta(seconds=GRACE_SECONDS * 3)
    assert email_live.call(replies.sweep_unclaimed) == 0
    assert email_live.service.db.scalar("SELECT state FROM replies WHERE id=?", (rid,)) == "claimed"


def test_listener_seen_survives_deleted_root(email_live):
    nid, did = send(email_live)
    n2, d2 = send(email_live, in_reply_to=nid)
    deliver(email_live, reply_raw(d2))
    claim(email_live, n2, waiter="a" * 12)  # stamps the whole conversation; the reply is now claimed
    deliver(email_live, reply_raw(d2, message_id="<second@x>"))
    with email_live.service.db.tx() as c:
        c.execute("DELETE FROM notifications WHERE id=?", (nid,))
    replies = email_live.service.replies
    replies.now = lambda: utcnow() + timedelta(seconds=GRACE_SECONDS + 60)
    # follow-up was stamped at real now; "now" is GRACE+60 later, so move the stamp inside the listen window
    with email_live.service.db.tx() as c:
        c.execute("UPDATE notifications SET listener_seen_at=? WHERE id=?",
                  (iso(replies.now() - timedelta(seconds=LISTEN_SECONDS - 10)), n2))
    assert email_live.call(replies.sweep_unclaimed) == 0
    with email_live.service.db.tx() as c:
        c.execute("UPDATE notifications SET listener_seen_at=NULL WHERE id=?", (n2,))
    assert email_live.call(replies.sweep_unclaimed) == 1
