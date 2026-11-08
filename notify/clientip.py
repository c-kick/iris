"""Who sent a request, when a reverse proxy may sit in front (server.trusted_proxies).

Forwarding headers are client-controlled. They count only when the TCP peer is one of the configured proxies:
then the client address is the rightmost X-Forwarded-For entry that is not itself a trusted proxy, and the scheme is
X-Forwarded-Proto. From any other peer both headers are ignored. With no trusted proxies configured nothing changes
for the address; the throttles and limits key on the TCP peer as before.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from starlette.requests import Request


def _nets(request: Request) -> list[Any]:
    cfg = getattr(request.app.state, "config", None)
    raw = getattr(getattr(cfg, "server", None), "trusted_proxies", None) or []
    return [ipaddress.ip_network(n, strict=False) for n in raw]


def _addr(s: str) -> Any:
    try:
        a = ipaddress.ip_address(s.strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return None
    return a.ipv4_mapped if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped else a


def _trusted(addr: Any, nets: list[Any]) -> bool:
    return addr is not None and any(addr.version == n.version and addr in n for n in nets)


def peer(request: Request) -> str:
    return request.client.host if request.client else "?"


def via_trusted_proxy(request: Request) -> bool:
    """The TCP peer is a configured reverse proxy."""
    return _trusted(_addr(peer(request)), _nets(request))


def client_ip(request: Request) -> str:
    """The client's address: the TCP peer, or behind a trusted proxy the nearest untrusted X-Forwarded-For hop."""
    nets = _nets(request)
    p = peer(request)
    if not nets or not _trusted(_addr(p), nets):
        return p
    hops = [h.strip() for h in ",".join(request.headers.getlist("x-forwarded-for")).split(",") if h.strip()]
    for hop in reversed(hops):
        a = _addr(hop)
        if a is None:
            return p  # garbage in the chain: fall back to the proxy itself (shared throttle, never a spoofed one)
        if not _trusted(a, nets):
            return str(a)
    return p


def scheme(request: Request) -> str:
    """http or https as the client used it: X-Forwarded-Proto only from a trusted proxy."""
    if via_trusted_proxy(request):
        fwd = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if fwd in ("http", "https"):
            return fwd
    return request.url.scheme
