"""Reply handle: issued once per conversation, required for follow-ups, never stored or logged in plain text."""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from notify.db import utcnow
from notify.handles import handle_matches, new_handle, parse_handle
from notify.replies import GRACE_SECONDS
from tests.mailfactory import reply_raw
import pytest

from notify.core import Client, NotFound
from notify.models import Scope
from tests.test_replies_core import AGENT, HANDLES, OTHER, claim, deliver, hdl, send

BASE = {"targets": ["device:alex-email"], "message": "Pick", "kinds": ["email"]}


def post(srv, who="agent", **body):
    with srv.client(who) as c:
        return c.post("/v1/notifications", json={**BASE, **body})


def root(srv, **body) -> dict:
    r = post(srv, reply=True, **body)
    assert r.status_code == 202, r.text
    return r.json()


def conv_row(srv, cid):
    return srv.service.db.one("SELECT * FROM conversations WHERE id=?", (cid,))


def test_handle_helpers():
    h, digest = new_handle("ntf_a")
    assert h.startswith("ntf_a:") and parse_handle(f" {h}\n") == ("ntf_a", digest)
    assert handle_matches(h + "\n", "ntf_a", digest) and not handle_matches(h, "ntf_b", digest)
    assert not handle_matches(None, "ntf_a", digest) and not handle_matches(h, "ntf_a", None)
    assert parse_handle("nocolon") is None and parse_handle("a:b:c") is None and parse_handle(None) is None


def test_root_reply_send_returns_handle_once(email_live, caplog):
    caplog.set_level(logging.DEBUG)
    out = root(email_live)
    h = out["reply_handle"]
    assert h.startswith(out["id"] + ":")
    secret = h.split(":", 1)[1]
    with email_live.client() as c:
        got = c.get(f"/v1/notifications/{out['id']}").json()
    assert "reply_handle" not in got
    assert conv_row(email_live, out["id"])["handle_sha256"] == hashlib.sha256(secret.encode()).hexdigest()
    dump = "\n".join(email_live.service.db.conn.iterdump())
    assert secret not in dump
    for rec in caplog.records:
        assert secret not in rec.getMessage() and secret not in str(rec.__dict__)


def test_replay_returns_no_handle_and_keeps_old(email_live):
    first = root(email_live, idempotency_key="k1")
    again = post(email_live, reply=True, idempotency_key="k1").json()
    assert again["id"] == first["id"]
    assert again["reply_handle"] is None and again["reply_handle_issued"] is True
    row = conv_row(email_live, first["id"])
    assert handle_matches(first["reply_handle"], first["id"], row["handle_sha256"])


def test_idempotency_key_hidden_in_handle_conversation(email_live):
    out = root(email_live, idempotency_key="k2")
    plain = post(email_live, idempotency_key="k3").json()
    with email_live.client() as c:
        assert c.get(f"/v1/notifications/{out['id']}").json()["idempotency_key"] is None
        assert c.get(f"/v1/notifications/{plain['id']}").json()["idempotency_key"] == "k3"


def test_followup_needs_handle(email_live):
    out = root(email_live)
    other = root(email_live)
    r = post(email_live, in_reply_to=out["id"])
    assert r.status_code == 422 and "not found" in r.text
    r = post(email_live, in_reply_to=out["id"], reply_handle=other["reply_handle"])
    assert r.status_code == 422
    r = post(email_live, in_reply_to=out["id"], reply_handle=" " + out["reply_handle"] + "\n")
    assert r.status_code == 202, r.text
    assert "reply_handle" not in r.json()


def test_first_reply_followup_creates_handle(email_live):
    r0 = post(email_live)
    nid = r0.json()["id"]
    r = post(email_live, in_reply_to=nid, reply=True)
    assert r.status_code == 202, r.text
    h = r.json()["reply_handle"]
    assert h.startswith(nid + ":")
    assert post(email_live, in_reply_to=nid, reply=True).status_code == 422
    assert post(email_live, in_reply_to=nid, reply=True, reply_handle=h).status_code == 202


def test_legacy_conversation_followup_by_token(email_live):
    out = root(email_live)
    with email_live.service.db.tx() as c:
        c.execute("UPDATE conversations SET legacy=1, handle_sha256=NULL WHERE id=?", (out["id"],))
    r = post(email_live, in_reply_to=out["id"], reply=True)
    assert r.status_code == 202, r.text
    assert "reply_handle" not in r.json()
    assert post(email_live, "other", in_reply_to=out["id"], reply=True).status_code == 422


def test_missing_row_fails_closed(email_live):
    out = root(email_live)
    with email_live.service.db.tx() as c:
        c.execute("DELETE FROM conversations WHERE id=?", (out["id"],))
    r = post(email_live, in_reply_to=out["id"], reply_handle=out["reply_handle"])
    assert r.status_code == 422 and "not found" in r.text
    assert conv_row(email_live, out["id"]) is None


def test_handle_as_in_reply_to_not_echoed(email_live):
    out = root(email_live)
    r = post(email_live, in_reply_to=out["reply_handle"])
    assert r.status_code == 422
    assert "in_reply_to takes a notification id; pass the handle as reply_handle" in r.text
    assert out["reply_handle"].split(":", 1)[1] not in r.text


def test_handle_checked_again_at_commit(email_live):
    out = root(email_live)
    svc = email_live.service
    with svc.db.tx() as c:  # a conversation row without a handle yet
        c.execute("UPDATE conversations SET handle_sha256=NULL WHERE id=?", (out["id"],))
    real = svc.commit

    def commit(*a, **k):
        with svc.db.tx() as c:  # someone else's reply:true follow-up sets the handle in between
            c.execute("UPDATE conversations SET handle_sha256=? WHERE id=?", ("0" * 64, out["id"]))
        return real(*a, **k)

    svc.commit = commit
    try:
        r = post(email_live, in_reply_to=out["id"])
    finally:
        svc.commit = real
    assert r.status_code == 422 and "not found" in r.text


def test_autoreply_passes_in_handle_conversation(email_live):
    nid, did = send(email_live)
    assert deliver(email_live, reply_raw(did)) is not None
    replies = email_live.service.replies
    replies.now = lambda: utcnow() + timedelta(seconds=GRACE_SECONDS + 60)
    assert email_live.call(replies.sweep_unclaimed) == 1
    assert email_live.service.db.scalar(
        "SELECT COUNT(*) FROM notifications WHERE tags_json LIKE '%reply-autoreply%'") == 1


ADMIN = Client("admin", frozenset({Scope.admin}))


def test_other_agent_same_token_cannot_claim(email_live):
    nid, did = send(email_live)
    rid = deliver(email_live, reply_raw(did))
    with pytest.raises(NotFound):
        claim(email_live, nid, handle=None)
    with pytest.raises(NotFound):
        claim(email_live, nid, handle=nid + ":" + "x" * 32)
    assert claim(email_live, nid, handle=" " + HANDLES[nid] + "\n")["id"] == rid


def test_admin_token_no_bypass(email_live):
    from tests.mailfactory import reply_raw as rr
    nid, did = send(email_live)
    rid = deliver(email_live, rr(did, attachment=b"\x89PNG\r\n\x1a\n" + b"0" * 20))
    replies = email_live.service.replies
    with pytest.raises(NotFound):
        claim(email_live, nid, client=ADMIN, handle=None)
    with pytest.raises(NotFound):
        replies.list(ADMIN, nid)
    claim(email_live, nid)
    with pytest.raises(NotFound):
        replies.attachment(ADMIN, rid, 0)


def test_wrong_handle_does_not_refresh_listener(email_live, sent_mail):
    nid, did = send(email_live)
    rid = deliver(email_live, reply_raw(did))
    for i in range(3):
        with pytest.raises(NotFound):
            claim(email_live, nid, waiter=f"wrong-wait-{i}", handle=nid + ":" + "y" * 32)
    db = email_live.service.db
    assert db.scalar("SELECT listener_seen_at FROM notifications WHERE id=?", (nid,)) is None
    replies = email_live.service.replies
    replies.now = lambda: utcnow() + timedelta(seconds=GRACE_SECONDS + 60)
    assert email_live.call(replies.sweep_unclaimed) == 1
    assert db.scalar("SELECT autoreply_notification_id FROM replies WHERE id=?", (rid,))
    got = claim(email_live, nid)
    assert got["id"] == rid and got["late"] is True


def test_claim_order_waiting_before_late(email_live):
    nid, did = send(email_live)
    old = deliver(email_live, reply_raw(did, message_id="<old@x>"))
    replies = email_live.service.replies
    replies.now = lambda: utcnow() + timedelta(seconds=GRACE_SECONDS + 60)
    email_live.call(replies.sweep_unclaimed)
    replies.now = utcnow
    new = deliver(email_live, reply_raw(did, message_id="<new@x>"))
    first = claim(email_live, nid, waiter="first-wait-1")
    second = claim(email_live, nid, waiter="second-wait2")
    assert (first["id"], first["late"]) == (new, False)
    assert (second["id"], second["late"]) == (old, True)


def test_list_shows_claimed_text_with_handle_only(email_live):
    nid, did = send(email_live)
    deliver(email_live, reply_raw(did))
    claim(email_live, nid)
    replies = email_live.service.replies
    assert replies.list(AGENT, nid, hdl(email_live, nid))[0]["text"] == "Go with B."
    for bad in (None, nid + ":" + "x" * 32):
        with pytest.raises(NotFound):
            replies.list(AGENT, nid, bad)


def test_root_retained_away_still_needs_handle(email_live):
    nid, _ = send(email_live)
    n2, d2 = send(email_live, in_reply_to=nid)
    rid = deliver(email_live, reply_raw(d2))
    with email_live.service.db.tx() as c:
        c.execute("DELETE FROM notifications WHERE id=?", (nid,))
    h = HANDLES[nid]
    assert claim(email_live, n2, handle=h)["id"] == rid
    with pytest.raises(NotFound):
        claim(email_live, n2, handle=None)
    with email_live.service.db.tx() as c:
        c.execute("DELETE FROM conversations WHERE id=?", (nid,))
    with pytest.raises(NotFound):
        claim(email_live, n2, handle=h)


def test_handle_works_after_root_retained(email_live):
    nid, _ = send(email_live)
    n2, d2 = send(email_live, in_reply_to=nid)
    rid = deliver(email_live, reply_raw(d2))
    with email_live.service.db.tx() as c:
        c.execute("DELETE FROM notifications WHERE id=?", (nid,))
    got = claim(email_live, nid, handle=HANDLES[nid])  # the conversation id from the handle
    assert got["id"] == rid


def test_legacy_admin_bearer_refused(email_live):
    nid, did = send(email_live)
    with email_live.service.db.tx() as c:
        c.execute("UPDATE conversations SET legacy=1, handle_sha256=NULL WHERE id=?", (nid,))
    rid = deliver(email_live, reply_raw(did))
    replies = email_live.service.replies
    with pytest.raises(NotFound):
        claim(email_live, nid, client=ADMIN, handle=None)
    with pytest.raises(NotFound):
        replies.list(ADMIN, nid)
    assert claim(email_live, nid, handle=None)["id"] == rid  # the sender's token still works
