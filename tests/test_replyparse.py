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
                            auth_results=["mx.example.org; dkim=pass header.d=example.org;"
                                          " dmarc=pass header.from=mail.example.org"]))
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


def _dk(ar: str, dom: str = "example.org") -> bool:
    return rp.dkim_passes(head(reply_raw(DLV, auth_results=[f"{ar}; dmarc=pass header.from={dom}"])),
                          "mx.example.org", dom)


def test_dkim_rejects_injected_verdicts():
    assert _dk("mx.example.org; dkim=pass header.d=example.org")
    assert not _dk("mx.example.org; dkim=fail (x; dkim=pass header.d=example.org)")
    assert not _dk("mx.example.org; dkim=fail (a (b) ; dkim=pass header.d=example.org)")
    assert not _dk('mx.example.org; dkim=fail reason="a; dkim=pass header.d=example.org"')
    assert not _dk("mx.example.org; dkim=pass x-header.d=example.org")
    assert not _dk("mx.example.org; dkim=pass-ish header.d=example.org")
    assert _dk("mx.example.org; dkim=pass (good) header.d=example.org")


def test_multiple_from_headers_rejected():
    m = rp.parse_headers(b"From: a@example.org\r\nFrom: b@example.org\r\n\r\n")
    assert rp.from_address(m) == ""


def test_deeply_nested_multipart_raises_value_error():
    import pytest
    n = 3000
    raw = b"".join(b"Content-Type: multipart/mixed; boundary=b%d\r\n\r\n--b%d\r\n" % (i, i) for i in range(n))
    raw = b"From: a@example.org\r\nMIME-Version: 1.0\r\n" + raw + b"Content-Type: text/plain\r\n\r\nhi\r\n"
    with pytest.raises(ValueError, match="too deeply nested|more than 200 MIME parts"):  # the part cap now stops it first
        rp.parse_reply(raw)


def from_of(value: bytes):
    return rp.from_address(rp.parse_headers(b"From: " + value + b"\r\n\r\n"))


def test_from_accepts_only_plain_forms():
    assert from_of(b"alex@example.org") == "alex@example.org"
    assert from_of(b"Alex <Alex@Example.org>") == "alex@example.org"
    assert from_of(b'"Alex, Home" <alex@example.org>') == "alex@example.org"
    assert from_of(b"=?utf-8?q?Al=C3=A9x?= <alex@example.org>") == "alex@example.org"  # encoded display name is fine


def test_from_rejects_ambiguous_forms():
    for v in (b"alex@example.org)<mallory@example.net>",          # CVE-2023-27043 shape
              b"mallory@example.net <alex@example.org>",           # address-looking display name
              b'"alex@example.org" <mallory@example.net>',
              b"alex@example.org, mallory@example.net",            # address list
              b"Alex <alex@example.org>, Bob <bob@example.org>",
              b"=?utf-8?q?alex=40example.org?=",                   # encoded word as the address
              b"Alex <=?utf-8?q?alex=40example.org?=>",
              b"alex@example.org (mallory@example.net)",           # comment
              b"<@route.example:alex@example.org>",                # source route
              b"Alex <alex@example.org> trailing",
              b"Alex <\"alex\"@example.org>",
              b"alex@@example.org", b"", b"Alex"):
        assert from_of(v) == "", v


def test_from_parsing_is_linear_on_hostile_input():
    import time
    for v in (b" " * 50000, b"a " * 25000, b'"' + b"\\x" * 25000, b"a" * 50000 + b"@", b"x" * 30000 + b" <",
              b"a@" + b"b." * 20000, b"a" + b" " * 60000 + b"b", b'"a' + b" " * 60000):
        t = time.perf_counter()
        assert from_of(v) == ""
        assert time.perf_counter() - t < 0.5, v[:20]


def test_dkim_requires_dmarc_for_the_from_domain():
    def ok(ar: str) -> bool:
        return rp.dkim_passes(head(reply_raw(DLV, auth_results=[ar])), "mx.example.org", "example.org")
    assert ok("mx.example.org; dkim=pass header.d=example.org; dmarc=pass (p=none) header.from=example.org")
    assert not ok("mx.example.org; dkim=pass header.d=example.org")                      # no DMARC verdict
    assert not ok("mx.example.org; dkim=pass header.d=example.org; dmarc=fail header.from=example.org")
    assert not ok("mx.example.org; dkim=pass header.d=example.org; dmarc=pass header.from=evil.org")
    assert not ok("mx.example.org; dkim=pass header.d=org; dmarc=pass header.from=example.org")  # d without a dot
    assert not ok("mx.example.org; dmarc=pass header.from=example.org")                  # no DKIM pass


GOOD_BLOCK = (b"Authentication-Results: mx.example.org; dkim=pass header.d=example.org\r\n"
              b"DKIM-Signature: v=1; a=rsa-sha256;\r\n\tb=abc\r\n"
              b"From: Alex <alex@example.org>\r\nSubject: Re: x\r\n\r\n")


def test_header_block_problem():
    assert rp.header_block_problem(GOOD_BLOCK) is None
    hidden = (b"From: victim@example.org\r\nX-Pad : 1\r\nFrom: mallory@example.org\r\n\r\n")  # Python stops at X-Pad
    assert rp.header_block_problem(hidden) == "a header line has no valid name"
    assert rp.header_block_problem(b"X-A: 1\rFrom: v@example.org\r\n\r\n") == "bare CR or LF in the headers"
    assert rp.header_block_problem(b"X-A: 1\nFrom: v@example.org\r\n\r\n") == "bare CR or LF in the headers"
    assert rp.header_block_problem(b"From: a\x01b@example.org\r\n\r\n") == "control character in the headers"
    assert rp.header_block_problem(b"X-P\xe9: 1\r\nFrom: v@example.org\r\n\r\n") == "a header line has no valid name"
    assert rp.header_block_problem(b" X: 1\r\nFrom: v@example.org\r\n\r\n") == "headers start with a continuation line"
    assert rp.header_block_problem(b"Subject: x\r\n\r\n") == "not exactly one From header"
    assert rp.header_block_problem(b"From: a@example.org\r\nfrom: b@example.org\r\n\r\n") == "not exactly one From header"


def test_from_rejects_control_characters():
    for v in (b"\x1fv@example.org\x1f", b"a\x00b <v@example.org>", b'"a\x01" <v@example.org>'):
        assert from_of(v) == "", v
