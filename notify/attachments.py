"""Attachment intake: validate, decode inline data and fetch allowlisted URLs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import mimetypes
import re
import socket
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from .models import AttachmentIn

MAX_COUNT = 10
MAX_TOTAL = 10 * 1024 * 1024
MCP_INLINE_MAX = 256 * 1024
FETCH_BUDGET_SECONDS = 15.0
MAX_REDIRECTS = 3

_CT_RE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_DENY = [ipaddress.ip_network(n) for n in
         ("127.0.0.0/8", "::1/128", "0.0.0.0/8", "::/128", "169.254.0.0/16", "fe80::/10")]
_TOTAL_MSG = "attachments exceed 10 MiB in total"


class AttachmentError(ValueError):
    """Message is safe to return to the sender (never contains a URL)."""


@dataclass
class PreparedAttachment:
    filename: str
    content_type: str
    data: bytes
    source: str  # 'inline' | 'url'
    source_host: str | None = None

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def clean_filename(name: str | None, fallback: str) -> str:
    base = re.split(r"[/\\]", name or "")[-1]
    base = "".join("_" if unicodedata.category(c).startswith("C") else c for c in base).strip()[:200].strip()
    return fallback if base in ("", ".", "..") else base


def clean_content_type(ct: str | None, filename: str) -> str:
    c = (ct or "").split(";", 1)[0].strip().lower()
    if _CT_RE.match(c):
        return c
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _b64(item: AttachmentIn) -> str:
    return "".join((item.data_base64 or "").split())


def decode_inline(items: list[AttachmentIn]) -> list[PreparedAttachment | None]:
    if len(items) > MAX_COUNT:
        raise AttachmentError(f"at most {MAX_COUNT} attachments are allowed")
    out: list[PreparedAttachment | None] = []
    total = 0
    for i, it in enumerate(items):
        if it.data_base64 is None:
            out.append(None)
            continue
        try:
            data = base64.b64decode(_b64(it), validate=True)
        except (binascii.Error, ValueError):
            raise AttachmentError(f"attachment {i}: data_base64 is not valid base64") from None
        total += len(data)
        if total > MAX_TOTAL:
            raise AttachmentError(_TOTAL_MSG)
        name = clean_filename(it.filename, f"attachment-{i + 1}")
        out.append(PreparedAttachment(name, clean_content_type(it.content_type, name), data, "inline", None))
    return out


async def default_resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(i[4][0]) for i in infos))


def _ip(s: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        a = ipaddress.ip_address(s.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        return a.ipv4_mapped
    return a


def _denied(a: ipaddress.IPv4Address | ipaddress.IPv6Address | None) -> bool:
    return a is None or any(a.version == n.version and a in n for n in _DENY)


def _host_port(url: str) -> tuple[str, int]:
    try:
        u = urlsplit(url)
        host = (u.hostname or "").lower().rstrip(".")
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        raise AttachmentError("attachment URL is not valid") from None
    if u.scheme not in ("http", "https") or not host:
        raise AttachmentError("attachment URL is not valid")
    return host, port


def _match(host: str, port: int, allow: list[str], ips: list[str] | None) -> str:
    """'yes', 'no', or 'maybe' (a CIDR entry could still match once the name resolves)."""
    lit = _ip(host)
    maybe = False
    for e in allow:
        e = e.strip().lower()
        try:
            net = ipaddress.ip_network(e, strict=False)
        except ValueError:
            h, _, p = e.partition(":")
            if h == host and (not p or int(p) == port):
                return "yes"
            continue
        if lit is not None:
            if lit.version == net.version and lit in net:
                return "yes"
        elif ips is None:
            maybe = True
        elif ips:
            addrs = [_ip(x) for x in ips]
            if all(a is not None and a.version == net.version and a in net for a in addrs):
                return "yes"
    return "maybe" if maybe else "no"


def check_url_allowed(url: str, allow: list[str], index: int | None = None) -> None:
    if not allow:
        raise AttachmentError("URL attachments are not enabled; an admin can allow hosts under Settings")
    host, port = _host_port(url)
    if _match(host, port, allow, None) == "no":
        raise AttachmentError(f"{'' if index is None else f'attachment {index}: '}host {host} is not in the allowed list")


async def _fetch(i: int, it: AttachmentIn, allow: list[str], http: httpx.AsyncClient,
                 resolve: Callable[[str], Awaitable[list[str]]], used: int) -> PreparedAttachment:
    url = it.url or ""
    for hop in range(MAX_REDIRECTS + 1):
        check_url_allowed(url, allow, i)
        host, port = _host_port(url)
        try:
            ips = [host] if _ip(host) is not None else list(await resolve(host))
        except OSError:
            raise AttachmentError(f"attachment {i}: could not be fetched") from None
        if not ips or any(_denied(_ip(x)) for x in ips):
            raise AttachmentError(f"attachment {i}: address not allowed")
        if _match(host, port, allow, ips) != "yes":
            raise AttachmentError(f"attachment {i}: host {host} is not in the allowed list")
        try:
            async with http.stream("GET", url) as r:
                if 300 <= r.status_code < 400 and r.headers.get("location"):
                    if hop == MAX_REDIRECTS:
                        raise AttachmentError(f"attachment {i}: too many redirects")
                    url = urljoin(url, r.headers["location"])
                    continue
                if not 200 <= r.status_code < 300:
                    raise AttachmentError(f"attachment {i}: fetch failed with HTTP {r.status_code}")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if used + len(buf) > MAX_TOTAL:
                        raise AttachmentError(_TOTAL_MSG)
                ctype = r.headers.get("content-type")
        except (httpx.HTTPError, httpx.InvalidURL):
            raise AttachmentError(f"attachment {i}: could not be fetched") from None
        fallback = f"attachment-{i + 1}"
        name = clean_filename(it.filename or unquote(urlsplit(url).path), fallback)
        return PreparedAttachment(name, clean_content_type(it.content_type or ctype, name),
                                  bytes(buf), "url", host)
    raise AttachmentError(f"attachment {i}: too many redirects")  # unreachable


async def prepare(items: list[AttachmentIn], allow: list[str], http: httpx.AsyncClient,
                  resolve: Callable[[str], Awaitable[list[str]]] = default_resolve) -> list[PreparedAttachment]:
    decoded = decode_inline(items)
    used = sum(p.size for p in decoded if p)
    out: list[PreparedAttachment] = []
    try:
        async with asyncio.timeout(FETCH_BUDGET_SECONDS):
            for i, (it, p) in enumerate(zip(items, decoded)):
                if p is None:
                    p = await _fetch(i, it, allow, http, resolve, used)
                    used += p.size
                out.append(p)
    except TimeoutError:
        raise AttachmentError(f"attachment URLs took longer than {FETCH_BUDGET_SECONDS:g} s to fetch") from None
    return out


def metadata(items: list[AttachmentIn]) -> list[dict]:
    out = []
    for i, it in enumerate(items):
        if it.data_base64 is not None:
            s = _b64(it)
            name = clean_filename(it.filename, f"attachment-{i + 1}")
            size: int | None = len(s) * 3 // 4 - (len(s) - len(s.rstrip("=")))
            out.append({"filename": name, "content_type": clean_content_type(it.content_type, name),
                        "size": size, "source": "inline"})
        else:
            name = clean_filename(it.filename, "") or None
            ct = clean_content_type(it.content_type, name or "") if it.content_type else None
            out.append({"filename": name, "content_type": ct, "size": None, "source": "url"})
    return out
