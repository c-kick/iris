from __future__ import annotations

import base64
from datetime import UTC, datetime

from notify.models import Severity
from notify.providers.base import AttachmentData, OutboundMessage
from notify.providers.pushover import PushoverProvider
from tests.conftest import PNG, wait_for
from tests.test_attachments_api import get, inline, submit

UNKNOWN_GAP = "type and size unknown"


def _plan(attachments):
    p = PushoverProvider("pushover", {"app_token": "a" * 30}, None)  # type: ignore[arg-type]
    now = datetime.now(UTC)
    msg = OutboundMessage("ntf_1", "dlv_1", "T", "hi", Severity.normal, None, None, [], [], False, now, None,
                          attachments=attachments)
    return p, msg, p.plan(msg, {}), p.build_form(msg, {"user_key": "u" * 30}, now)


def test_unknown_attachment_gap_and_nothing_sent():
    p, msg, plan, form = _plan([AttachmentData(filename="x", content_type="", size=None)])
    assert any(UNKNOWN_GAP in g for g in plan.capability_gaps)
    assert p._image(msg) is None
    assert not [k for k in form if k.startswith("attachment_")]


def test_unknown_mixed_with_known_image_only_unknown_gap():
    atts = [AttachmentData("x", "", None), AttachmentData("a.png", "image/png", len(PNG), PNG)]
    p, msg, plan, form = _plan(atts)
    gaps = [g for g in plan.capability_gaps if g.startswith("attachments")]
    assert len(gaps) == 1 and UNKNOWN_GAP in gaps[0]
    assert p._image(msg) is atts[1]


def test_planned_image_without_data_does_not_crash():
    p, msg, plan, form = _plan([AttachmentData("a.png", "image/png", len(PNG))])
    assert not plan.capability_gaps and "attachment_base64" not in form


def deliver(live, fake, atts):
    r = submit(live, attachments=atts)
    assert r.status_code == 202, r.text
    nid = r.json()["id"]
    wait_for(lambda: len(fake.messages) >= 1)
    d = wait_for(lambda: (x := get(live, nid)["deliveries"][0])["state"] == "dispatched" and x)
    return nid, fake.messages[-1], d


def test_inline_png_sent_as_attachment(live, fake):
    _, form, d = deliver(live, fake, [inline()])
    assert base64.b64decode(form["attachment_base64"]) == PNG
    assert form["attachment_type"] == "image/png"
    assert not d["capability_gaps"]
    req = d["attempt_log"][0]["request"]
    assert "attachment_base64" not in req and req["attachment_bytes"] == len(PNG)


def test_pdf_only_not_sent_with_gap(live, fake):
    pdf = base64.b64encode(b"%PDF-1.4\n%%EOF\n").decode()
    _, form, d = deliver(live, fake, [inline("a.pdf", pdf, "application/pdf")])
    assert "attachment_base64" not in form and "attachment_type" not in form
    assert any("1 attachment(s) not sent" in g for g in d["capability_gaps"])


def test_oversize_png_not_sent_with_gap(live, fake):
    big = base64.b64encode(PNG + b"\x00" * (5 * 1024 * 1024 + 1 - len(PNG))).decode()
    _, form, d = deliver(live, fake, [inline("big.png", big)])
    assert "attachment_base64" not in form
    assert any("1 attachment(s) not sent" in g for g in d["capability_gaps"])


def test_capabilities_advertise_one_image(live):
    items = live.client().get("/v1/providers").json()["items"]
    push = next(x for x in items if x["type"] == "pushover")
    assert push["capabilities"]["attachments"]["max_count"] == 1


def test_only_the_sent_image_is_loaded(live):
    pdf = base64.b64encode(b"%PDF-1.4\n%%EOF\n").decode()
    r = submit(live, attachments=[inline("a.pdf", pdf, "application/pdf"), inline()])
    nid = r.json()["id"]
    n = live.service.db.one("SELECT * FROM notifications WHERE id=?", (nid,))
    did = live.service.db.scalar("SELECT id FROM deliveries WHERE notification_id=?", (nid,))
    msg = live.service.message_for(n, did, "pushover", with_data=True)
    assert [(a.filename, a.data) for a in msg.attachments] == [("a.pdf", None), ("door.png", PNG)]
    live.service.providers["pushover"].attachment_data = "all"  # providers that take everything load everything
    msg = live.service.message_for(n, did, "pushover", with_data=True)
    assert all(a.data is not None for a in msg.attachments)
