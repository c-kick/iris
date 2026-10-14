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
