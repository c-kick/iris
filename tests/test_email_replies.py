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
