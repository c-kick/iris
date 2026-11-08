"""Outbound HTTP that checks the address it actually connects to (webhooks, attachment URLs).

A check of the host name before the request leaves a window: the name can resolve to an allowed address for the
check and to a denied one (loopback, link-local, ...) for the connect (DNS rebinding). Here the name is resolved
once, inside the connect, every address is checked against the same deny list as before, and the socket is opened
to one of those checked addresses. TLS still verifies the certificate for the host name in the URL (httpcore passes
the URL's host as server_hostname), so only where the socket goes changes. A refusal never sends a byte.
"""

from __future__ import annotations

import asyncio
import socket
import typing
from typing import Any, Awaitable, Callable

import httpcore
import httpx

from . import attachments

Resolver = Callable[[str, int], Awaitable[list[str]]]


class AddressNotAllowed(httpcore.ConnectError):
    """The host resolved to (or is) a denied address; nothing was sent."""


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(i[4][0]) for i in infos))


class GuardedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, resolve: Resolver | None = None, inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self.resolve = resolve or _resolve
        self.inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                          socket_options: typing.Iterable[Any] | None = None) -> httpcore.AsyncNetworkStream:
        host = host.strip("[]")
        if attachments._ip(host) is not None:
            ips = [host]
        else:
            try:
                ips = await self.resolve(host, port)
            except (OSError, UnicodeError) as e:
                raise httpcore.ConnectError(f"cannot resolve {host!r}: {type(e).__name__}") from None
        if not ips or any(attachments._denied(attachments._ip(x)) for x in ips):
            raise AddressNotAllowed("address not allowed")
        last: Exception | None = None
        for ip in ips:  # each one checked above; the first that answers wins
            try:
                return await self.inner.connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                                     socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        assert last is not None
        raise last

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: typing.Iterable[Any] | None = None) -> httpcore.AsyncNetworkStream:
        raise AddressNotAllowed("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose connections go through GuardedBackend. Never via a proxy."""

    def __init__(self, resolve: Resolver | None = None, inner: httpcore.AsyncNetworkBackend | None = None,
                 max_connections: int = 10) -> None:
        super().__init__()
        self._pool = httpcore.AsyncConnectionPool(ssl_context=httpx.create_ssl_context(),
                                                  max_connections=max_connections,
                                                  network_backend=GuardedBackend(resolve, inner))


def refused(e: BaseException) -> bool:
    """True if an httpx error came from a GuardedBackend refusal."""
    seen: BaseException | None = e
    while seen is not None:
        if isinstance(seen, AddressNotAllowed) or "address not allowed" in str(seen):
            return True
        seen = seen.__cause__ or seen.__context__
    return False
