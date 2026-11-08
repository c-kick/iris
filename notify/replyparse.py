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
from typing import Any

MAX_TEXT = 8000
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 10485760
MAX_PARTS = 200  # MIME parts; each costs a Python object tree, so a part flood could exhaust memory
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


class TooComplex(ValueError):
    """The message has more than MAX_PARTS MIME parts."""


def parse_message(raw: bytes, max_parts: int | None = None) -> EmailMessage:
    """Full parse. With `max_parts` the parser stops (TooComplex) as soon as it creates one part more, so a
    flood of tiny parts never becomes an object tree in memory."""
    if max_parts is None:
        return BytesParser(policy=policy.default).parsebytes(raw)  # type: ignore[return-value]
    made = -1  # the parser calls the factory once up front to probe its signature

    def factory(policy: Any = None) -> EmailMessage:  # the parser passes its policy by this name
        nonlocal made
        made += 1
        if made > max_parts + 1:  # the message itself plus its parts
            raise TooComplex(f"more than {max_parts} MIME parts")
        return EmailMessage(policy)
    return BytesParser(factory, policy=policy.default).parsebytes(raw)  # type: ignore[return-value]


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


_ADDR = r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
# One quantifier per stretch of input and no overlapping character classes, so matching stays linear
# (an earlier version backtracked quadratically on long runs of spaces).
_FROM_BARE = re.compile(rf"({_ADDR})")
_FROM_NAMED = re.compile(rf'(?:"(?:[^"\\@\x00-\x1f\x7f]|\\[^@\x00-\x1f\x7f])*"[ \t]*|[^"<>@,;:()\\\x00-\x1f\x7f]*)<({_ADDR})>')
_FROM_MAX = 998  # RFC 5322 line limit; longer From values are not genuine replies


def from_address(msg: EmailMessage) -> str:
    """The sender, read strictly from the raw header: `addr` or `Name <addr>` with a dot-atom address.
    Anything a lenient parser could read differently from the receiving server (address lists, comments,
    source routes, encoded words or @ outside the address, CVE-2023-27043 shapes) yields ""."""
    try:
        values = [str(v) for k, v in msg.raw_items() if k.lower() == "from"]
    except Exception:
        return ""
    if len(values) != 1:
        return ""
    raw = re.sub(r"\r?\n[ \t]+", " ", values[0]).strip(" \t")
    if len(raw) > _FROM_MAX:
        return ""
    m = _FROM_BARE.fullmatch(raw) or _FROM_NAMED.fullmatch(raw)
    return m.group(1).lower() if m else ""


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


def _strip_comments_quotes(v: str) -> str:
    """Drop RFC 8601 (nested) comments and quoted strings so ';' and tokens inside them cannot forge a verdict."""
    out: list[str] = []
    depth = 0
    quoted = False
    i = 0
    while i < len(v):
        c = v[i]
        if c == "\\" and (quoted or depth) and i + 1 < len(v):
            i += 2
            continue
        if quoted:
            quoted = c != '"'
        elif c == "(":
            depth += 1
        elif depth:
            depth -= c == ")"
        elif c == '"':
            quoted = True
            out.append(" ")
        else:
            out.append(c)
        i += 1
    return re.sub(r"\s+", " ", "".join(out))


_HEADER_LINE = re.compile(rb"[!-9;-~]+:")
_FROM_LINE = re.compile(rb"from[ \t]*:", re.I)


def header_block_problem(raw_headers: bytes) -> str | None:
    """Check the header block ourselves, so Python's lenient parser (which silently ends the headers at
    the first line it does not recognise, and treats a bare CR as a line break) can never see a different
    set of headers than the mail server and its DKIM/DMARC checks did. Returns a reason, or None."""
    end = raw_headers.find(b"\r\n\r\n")
    block = raw_headers if end < 0 else raw_headers[:end]
    if re.search(rb"\r(?!\n)|(?<!\r)\n", block):
        return "bare CR or LF in the headers"
    if re.search(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", block):
        return "control character in the headers"
    lines = block.split(b"\r\n")
    if not lines or lines[0][:1] in (b" ", b"\t"):
        return "headers start with a continuation line"
    froms = 0
    for line in lines:
        if line[:1] in (b" ", b"\t"):
            continue
        if not _HEADER_LINE.match(line):
            return "a header line has no valid name"
        froms += bool(_FROM_LINE.match(line))
    return None if froms == 1 else "not exactly one From header"


def dkim_passes(msg: EmailMessage, authserv_id: str, from_domain: str) -> bool:
    """Read the Authentication-Results headers from the top: the receiving server prepends its own, and
    anything below them may have been written by the sender. Only the leading run of headers carrying the
    trusted authserv-id counts (the first header with another id ends it), so a split OpenDKIM + OpenDMARC
    pair is combined. Per method the topmost header in that run that reports it decides; a lower header
    can never override it. It must show dkim=pass for the From domain (or a parent) AND dmarc=pass for
    exactly that From domain: DMARC is the server's own reading of the From header, which binds the
    signature to the address we compare."""
    if not authserv_id or not from_domain:
        return False
    try:
        values = [str(v) for k, v in msg.raw_items() if k.lower() == "authentication-results"]
    except Exception:
        return False
    from_domain = from_domain.lower().rstrip(".")
    trusted = authserv_id.strip().lower()
    dkim: bool | None = None
    dmarc: bool | None = None
    for value in values:
        parts = [p.strip() for p in _strip_comments_quotes(value).split(";")]
        if not parts[0] or parts[0].split()[0].lower() != trusted:
            break  # the end of the server's own headers
        has_dkim = has_dmarc = False
        dkim_ok = dmarc_ok = False
        for p in parts[1:]:
            if re.match(r"dkim\s*=", p, re.I):
                has_dkim = True
                if re.match(r"dkim\s*=\s*pass(?:\s|$)", p, re.I):
                    m = re.search(r"(?:^|\s)header\.d\s*=\s*([a-z0-9.-]+)", p, re.I)
                    d = m.group(1).lower().rstrip(".") if m else ""
                    dkim_ok = dkim_ok or ("." in d and (from_domain == d or from_domain.endswith("." + d)))
            elif re.match(r"dmarc\s*=", p, re.I):
                has_dmarc = True
                if re.match(r"dmarc\s*=\s*pass(?:\s|$)", p, re.I):
                    m = re.search(r"(?:^|\s)header\.from\s*=\s*([a-z0-9.-]+)", p, re.I)
                    dmarc_ok = dmarc_ok or bool(m and m.group(1).lower().rstrip(".") == from_domain)
        if dkim is None and has_dkim:
            dkim = dkim_ok
        if dmarc is None and has_dmarc:
            dmarc = dmarc_ok
        if dkim is not None and dmarc is not None:
            break
    return bool(dkim and dmarc)


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
        if len(kept) >= MAX_ATTACHMENTS:  # past the cap: listed by name, never decoded
            dropped.append(name[:255])
            continue
        data = part.get_payload(decode=True) or b""
        if total + len(data) > MAX_ATTACHMENT_BYTES:
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
    try:
        msg = parse_message(raw, MAX_PARTS)
        text, stripped = strip_quotes(body_text(msg))
        atts, dropped = extract_attachments(msg)
    except RecursionError:
        raise ValueError("message structure too deeply nested") from None
    return ParsedReply(from_address(msg), header(msg, "Subject")[:300], text[:MAX_TEXT], stripped,
                       len(text) > MAX_TEXT, atts, dropped)
