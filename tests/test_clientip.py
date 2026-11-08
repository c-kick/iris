"""0.13.2: server.trusted_proxies decides whose forwarding headers count."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from notify import clientip
from notify.config import ServerConfig


def req(peer: str, headers: dict[str, str] | None = None, proxies: list[str] | None = None,
        scheme: str = "http") -> Request:
    cfg = SimpleNamespace(server=ServerConfig(trusted_proxies=proxies or []))
    app = SimpleNamespace(state=SimpleNamespace(config=cfg))
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw, "client": (peer, 1234),
                    "scheme": scheme, "server": ("svc", 80), "query_string": b"", "app": app})


def test_headers_ignored_without_trusted_proxies():
    r = req("10.0.0.9", {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"})
    assert clientip.client_ip(r) == "10.0.0.9" and clientip.scheme(r) == "http"
    assert not clientip.via_trusted_proxy(r)


def test_headers_ignored_from_an_untrusted_peer():
    r = req("10.0.0.9", {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"}, ["10.0.0.2"])
    assert clientip.client_ip(r) == "10.0.0.9" and clientip.scheme(r) == "http"


def test_trusted_proxy_passes_the_client():
    r = req("10.0.0.2", {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"}, ["10.0.0.2"])
    assert clientip.client_ip(r) == "203.0.113.7" and clientip.scheme(r) == "https"
    assert clientip.via_trusted_proxy(r)


def test_rightmost_untrusted_hop_wins_over_a_spoofed_left_entry():
    r = req("10.0.0.2", {"X-Forwarded-For": "198.51.100.1, 203.0.113.7, 10.0.0.3"}, ["10.0.0.0/30"])
    assert clientip.client_ip(r) == "203.0.113.7"


def test_garbage_or_only_proxies_falls_back_to_the_peer():
    assert clientip.client_ip(req("10.0.0.2", {"X-Forwarded-For": "nonsense"}, ["10.0.0.2"])) == "10.0.0.2"
    assert clientip.client_ip(req("10.0.0.2", {}, ["10.0.0.2"])) == "10.0.0.2"
    assert clientip.scheme(req("10.0.0.2", {"X-Forwarded-Proto": "gopher"}, ["10.0.0.2"])) == "http"


def test_ipv4_mapped_peer_matches():
    r = req("::ffff:10.0.0.2", {"X-Forwarded-For": "203.0.113.7"}, ["10.0.0.2/32"])
    assert clientip.client_ip(r) == "203.0.113.7"


def test_config_validates():
    assert ServerConfig(trusted_proxies=["10.0.0.2", " 10.1.0.0/16 "]).trusted_proxies == ["10.0.0.2/32", "10.1.0.0/16"]
    with pytest.raises(ValueError):
        ServerConfig(trusted_proxies=["proxy.lan"])


def _push_req(peer: str, proxies: list[str]) -> Request:
    cfg = SimpleNamespace(server=ServerConfig(trusted_proxies=proxies, external_url="https://notify.example.org:8931"))
    app = SimpleNamespace(state=SimpleNamespace(config=cfg))
    raw = [(b"host", b"notify.example.org:8931"), (b"x-forwarded-proto", b"https")]
    return Request({"type": "http", "method": "GET", "path": "/push/enroll", "headers": raw, "client": (peer, 1),
                    "scheme": "http", "server": ("svc", 8930), "query_string": b"", "app": app})


def test_push_origin_check_believes_proto_only_from_a_trusted_proxy():
    from fastapi import HTTPException

    from notify.webpush_routes import host_guard
    host_guard(_push_req("10.0.0.2", ["10.0.0.2"]))  # through the proxy: allowed
    with pytest.raises(HTTPException):
        host_guard(_push_req("10.0.0.9", ["10.0.0.2"]))  # a LAN client claiming https
