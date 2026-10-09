from __future__ import annotations

import base64

from tests.conftest import PNG, wait_for
from tests.test_attachments_api import get, inline, submit




def deliver(live, fake, atts):
    r = submit(live, attachments=atts)
    assert r.status_code == 202, r.text
    nid = r.json()["id"]
    wait_for(lambda: len(fake.messages) >= 1)
    d = wait_for(lambda: (x := get(live, nid)["deliveries"][0])["state"] == "dispatched" and x)
    return nid, fake.messages[-1], d


def test_inline_png_sent_as_attachment(live, fake):
    nid, form, d = deliver(live, fake, [inline()])
    assert base64.b64decode(form["attachment_base64"]) == PNG
    assert form["attachment_type"] == "image/png"
    assert not d["capability_gaps"]
    req = d["attempt_log"][0]["request"]
    assert "attachment_base64" not in req and req["attachment_bytes"] == len(PNG)


def test_pdf_only_not_sent_with_gap(live, fake):
    pdf = base64.b64encode(b"%PDF-1.4\n%%EOF\n").decode()
    nid, form, d = deliver(live, fake, [inline("a.pdf", pdf, "application/pdf")])
    assert "attachment_base64" not in form and "attachment_type" not in form
    assert any("1 attachment(s) not sent" in g for g in d["capability_gaps"])


def test_oversize_png_not_sent_with_gap(live, fake):
    big = base64.b64encode(PNG + b"\x00" * (5 * 1024 * 1024 + 1 - len(PNG))).decode()
    nid, form, d = deliver(live, fake, [inline("big.png", big)])
    assert "attachment_base64" not in form
    assert any("1 attachment(s) not sent" in g for g in d["capability_gaps"])


def test_capabilities_advertise_one_image(live):
    p = live.client().get("/v1/providers").json()
    items = p["items"] if isinstance(p, dict) and "items" in p else p
    push = next(x for x in items if x.get("type") == "pushover" or x.get("name") == "pushover")
    caps = push.get("capabilities", push)
    assert caps["attachments"]["max_count"] == 1
