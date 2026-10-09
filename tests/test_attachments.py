from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from pydantic import ValidationError

from notify import attachments
from notify.attachments import AttachmentError, check_url_allowed, clean_content_type, clean_filename, decode_inline, metadata, prepare
from notify.config import ServerConfig
from notify.models import AttachmentIn, NotificationIn


def b64(data: bytes, wrap: bool = False) -> str:
    s = base64.b64encode(data).decode()
    return "\r\n".join(s[i:i + 8] for i in range(0, len(s), 8)) if wrap else s


def inline(data: bytes = b"hi", name: str = "a.txt", **kw) -> AttachmentIn:
    return AttachmentIn(filename=name, data_base64=b64(data), **kw)


async def lan(host: str) -> list[str]:
    return ["192.168.1.10"]


def run(items, allow, handler=None, resolve=lan):
    t = httpx.MockTransport(handler or (lambda r: httpx.Response(200, content=b"x")))
    return asyncio.run(prepare(items, allow, t, resolve))


def fail(items, allow, handler=None, resolve=lan) -> str:
    with pytest.raises(AttachmentError) as e:
        run(items, allow, handler, resolve)
    return str(e.value)


def url(u: str, **kw) -> AttachmentIn:
    return AttachmentIn(url=u, **kw)


# ------------------------------------------------------------------ cleaning

def test_clean_filename():
    assert clean_filename("../../etc/passwd", "f") == "passwd"
    assert clean_filename("C:\\x\\y.txt", "f") == "y.txt"
    assert clean_filename("a\r\nb.txt", "f") == "a__b.txt"
    assert clean_filename("", "f") == "f"
    assert clean_filename("..", "f") == "f"
    assert clean_filename("dir/", "f") == "f"
    assert len(clean_filename("x" * 500, "f")) == 200


def test_clean_content_type():
    assert clean_content_type("Image/PNG; charset=x", "a.bin") == "image/png"
    assert clean_content_type("bad type", "x.pdf") == "application/pdf"
    assert clean_content_type(None, "x.unknownext") == "application/octet-stream"


# ------------------------------------------------------------------ model

def test_model_validation():
    with pytest.raises(ValidationError):
        AttachmentIn(filename="a", data_base64="aGk=", url="http://x/")
    with pytest.raises(ValidationError):
        AttachmentIn(data_base64="aGk=")
    with pytest.raises(ValidationError):
        AttachmentIn(url="ftp://x")
    with pytest.raises(ValidationError):
        AttachmentIn()
    with pytest.raises(ValidationError):
        AttachmentIn(url="http://x/", bogus=1)
    assert AttachmentIn(url="http://x/").filename is None
    with pytest.raises(ValidationError):
        NotificationIn(targets=["user:a"], message="m", attachments=[{"url": "http://x/"}] * 11)


# ------------------------------------------------------------------ inline

def test_inline_decode():
    items = [AttachmentIn(filename="a.txt", data_base64=b64(b"hello world", wrap=True)), url("http://x/")]
    out = decode_inline(items)
    assert out[1] is None and out[0].data == b"hello world" and out[0].source == "inline"
    assert out[0].content_type == "text/plain" and out[0].size == 11 and len(out[0].sha256) == 64


def test_inline_invalid_base64_names_index():
    with pytest.raises(AttachmentError, match="attachment 1: data_base64 is not valid base64"):
        decode_inline([inline(), AttachmentIn(filename="b", data_base64="!!notbase64")])


def test_inline_total_limit():
    big = b"\0" * (6 * 1024 * 1024)
    with pytest.raises(AttachmentError, match="10 MiB in total"):
        decode_inline([inline(big), inline(big)])


def test_inline_count_limit():
    items = [inline()] * 11
    with pytest.raises(AttachmentError, match="at most 10"):
        decode_inline(items)


def test_metadata():
    m = metadata([AttachmentIn(filename="../a.txt", data_base64=b64(b"hello", wrap=True)),
                  inline(b"ab", "b.png"), url("http://x/y?token=s", filename="c.jpg"), url("http://x/")])
    assert m[0] == {"filename": "a.txt", "content_type": "text/plain", "size": 5, "source": "inline"}
    assert m[1]["size"] == 2 and m[1]["content_type"] == "image/png"
    assert m[2] == {"filename": "c.jpg", "content_type": None, "size": None, "source": "url"}
    assert m[3]["filename"] is None and m[3]["size"] is None


# ------------------------------------------------------------------ allowlist

def test_allowlist():
    with pytest.raises(AttachmentError, match="not enabled"):
        check_url_allowed("http://ha.lan/x", [])
    with pytest.raises(AttachmentError, match="not in the allowed list"):
        check_url_allowed("http://other.lan/x", ["ha.lan"])
    check_url_allowed("http://ha.lan:8123/x", ["ha.lan"])
    check_url_allowed("http://HA.lan:8123/x", ["ha.lan:8123"])
    with pytest.raises(AttachmentError):
        check_url_allowed("http://ha.lan/x", ["ha.lan:8123"])
    check_url_allowed("https://ha.lan/x", ["ha.lan:443"])
    check_url_allowed("http://192.168.1.10/x", ["192.168.1.0/24"])
    with pytest.raises(AttachmentError):
        check_url_allowed("http://192.168.2.10/x", ["192.168.1.0/24"])


def test_cidr_never_matches_hostnames():
    assert "not in the allowed list" in fail([url("http://cam.lan/x")], ["192.168.1.0/24"])
    with pytest.raises(AttachmentError, match="not in the allowed list"):
        check_url_allowed("http://cam.lan/x", ["192.168.1.0/24"])


def test_disabled_by_default():
    assert "not enabled" in fail([url("http://ha.lan/x")], [])


# ------------------------------------------------------------------ deny ranges

@pytest.mark.parametrize("ip", ["127.0.0.1", "::ffff:127.0.0.1", "::7f00:1", "2002:7f00:1::", "64:ff9b::7f00:1", "::a9fe:a9fe", "169.254.169.254", "::1", "0.0.0.0", "::", "fe80::1", "0.1.2.3"])
def test_denied_addresses(ip):
    async def res(h):
        return [ip]
    assert "attachment 0: address not allowed" == fail([url("http://ha.lan/x")], ["ha.lan"], resolve=res)


def test_denied_ip_literal_skips_dns():
    async def boom(h):
        raise AssertionError("must not resolve")
    assert "address not allowed" in fail([url("http://127.0.0.1/x")], ["127.0.0.0/8"], resolve=boom)
    assert "address not allowed" in fail([url("http://[::ffff:127.0.0.1]/x")], ["127.0.0.0/8"], resolve=boom)


def test_literal_ip_fetches_without_dns():
    async def boom(h):
        raise AssertionError("must not resolve")
    assert run([url("http://192.168.1.10/a.bin")], ["192.168.1.0/24"], resolve=boom)[0].filename == "a.bin"


def test_resolution_failure():
    async def res(h):
        raise OSError("nope")
    assert "attachment 0: could not be fetched" == fail([url("http://ha.lan/x")], ["ha.lan"], resolve=res)


# ------------------------------------------------------------------ peer address

class FakeStream:
    def __init__(self, addr):
        self.addr = addr

    def get_extra_info(self, name):
        return self.addr if name == "server_addr" else None


@pytest.mark.parametrize("addr,denied", [(("127.0.0.1", 80), True), (("::ffff:127.0.0.1", 80, 0, 0), True),
                                         (("169.254.169.254", 80), True), (("192.168.1.10", 80), False),
                                         (None, False)])
def test_peer_address_check(addr, denied):
    r = httpx.Response(200, extensions={"network_stream": FakeStream(addr)})
    assert attachments._peer_denied(r) is denied
    assert attachments._peer_denied(httpx.Response(200)) is False


def test_connected_peer_in_deny_range_refused():
    h = lambda r: httpx.Response(200, content=b"x", extensions={"network_stream": FakeStream(("127.0.0.1", 80))})
    assert "attachment 0: address not allowed" == fail([url("http://ha.lan/x")], ["ha.lan"], h)


# ------------------------------------------------------------------ fetching

def test_fetch_success():
    def h(r):
        return httpx.Response(200, content=b"JPEG", headers={"content-type": "Image/JPEG; x=y"})
    p = run([inline(), url("http://ha.lan:8123/api/camera?token=s", filename=None)], ["ha.lan"], h)
    assert p[1].data == b"JPEG" and p[1].content_type == "image/jpeg" and p[1].filename == "camera"
    assert p[1].source == "url" and p[1].source_host == "ha.lan"


def test_fetch_given_name_and_type_win():
    h = lambda r: httpx.Response(200, content=b"x", headers={"content-type": "text/plain"})
    p = run([url("http://ha.lan/x", filename="door.jpg", content_type="image/jpeg")], ["ha.lan"], h)[0]
    assert (p.filename, p.content_type) == ("door.jpg", "image/jpeg")


def test_fetch_filename_fallback():
    p = run([inline(), url("http://ha.lan/")], ["ha.lan"], lambda r: httpx.Response(200, content=b"x"))[1]
    assert p.filename == "attachment-2" and p.content_type == "application/octet-stream"


def test_redirect_to_disallowed_host_refused():
    h = lambda r: httpx.Response(302, headers={"location": "http://evil.example/x"})
    assert "not in the allowed list" in fail([url("http://ha.lan/x")], ["ha.lan"], h)


def test_redirect_to_denied_address_refused():
    async def res(host):
        return ["169.254.169.254"] if host == "meta.lan" else ["192.168.1.10"]
    h = lambda r: httpx.Response(302, headers={"location": "http://meta.lan/x"})
    assert "address not allowed" in fail([url("http://ha.lan/x")], ["ha.lan", "meta.lan"], h, res)


def test_too_many_redirects():
    n = {"c": 0}

    def h(r):
        n["c"] += 1
        return httpx.Response(302, headers={"location": "/next"})
    assert "attachment 0: too many redirects" == fail([url("http://ha.lan/x")], ["ha.lan"], h)
    assert n["c"] == 4


def test_redirect_within_allowlist():
    def h(r):
        if r.url.path == "/start":
            return httpx.Response(302, headers={"location": "http://cam.lan/img/final.png"})
        return httpx.Response(200, content=b"png")
    p = run([url("http://ha.lan/start")], ["ha.lan", "cam.lan"], h)[0]
    assert p.filename == "final.png" and p.content_type == "image/png" and p.source_host == "cam.lan"


def test_redirect_to_non_http_scheme_refused():
    h = lambda r: httpx.Response(302, headers={"location": "file:///etc/passwd"})
    assert "attachment URL is not valid" in fail([url("http://ha.lan/x")], ["ha.lan"], h)


def test_http_error_status():
    assert "attachment 0: fetch failed with HTTP 404" == fail([url("http://ha.lan/x")], ["ha.lan"], lambda r: httpx.Response(404, content=b"secret body"))


def test_transport_error():
    def h(r):
        raise httpx.ConnectError("boom http://ha.lan/x?token=secret")
    msg = fail([url("http://ha.lan/x?token=secret")], ["ha.lan"], h)
    assert msg == "attachment 0: could not be fetched"


def test_body_over_remaining_budget():
    big = b"\0" * (6 * 1024 * 1024)
    h = lambda r: httpx.Response(200, content=big)
    assert "10 MiB in total" in fail([inline(big), url("http://ha.lan/x")], ["ha.lan"], h)
    assert "10 MiB in total" in fail([url("http://ha.lan/a"), url("http://ha.lan/b")], ["ha.lan"], h)


def test_time_budget(monkeypatch):
    monkeypatch.setattr(attachments, "FETCH_BUDGET_SECONDS", 0.2)

    async def h(r):
        await asyncio.sleep(1)
        return httpx.Response(200, content=b"x")
    assert "took longer than" in fail([url("http://ha.lan/x")], ["ha.lan"], h)


def test_errors_never_contain_the_url():
    u = "http://ha.lan/x?token=secret"
    cases = [(lambda r: httpx.Response(500), ["ha.lan"]),
             (lambda r: httpx.Response(302, headers={"location": "http://evil/x?token=secret"}), ["ha.lan"]),
             (lambda r: httpx.Response(200), ["other.lan"])]
    for h, allow in cases:
        msg = fail([url(u)], allow, h)
        assert "secret" not in msg and "token" not in msg


# ------------------------------------------------------------------ config

def test_config_hosts_normalised():
    c = ServerConfig(attachment_url_hosts=["HA.lan:8123", "10.0.0.0/8", " cam.lan "])
    assert c.attachment_url_hosts == ["ha.lan:8123", "10.0.0.0/8", "cam.lan"]
    assert c.attachment_retention_days == 7
    assert ServerConfig().attachment_url_hosts == []


@pytest.mark.parametrize("bad", ["bad host!", "ha.lan:99999", "http://ha.lan", ""])
def test_config_bad_host(bad):
    with pytest.raises(ValidationError):
        ServerConfig(attachment_url_hosts=[bad])


def test_config_retention_bounds():
    for d in (0, 91):
        with pytest.raises(ValidationError):
            ServerConfig(attachment_retention_days=d)


def test_environment_proxies_are_ignored(monkeypatch):
    for k in ("HTTP_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.setenv(k, "http://192.168.1.99:3128")
    seen = []

    def h(r):
        seen.append((r.url.host, r.url.path))
        return httpx.Response(200, content=b"x")
    run([url("http://ha.lan/x")], ["ha.lan"], h)
    assert seen == [("ha.lan", "/x")]


def test_real_client_is_built_without_env_or_redirects(monkeypatch):
    made = {}
    real = httpx.AsyncClient

    def spy(**kw):
        made.update(kw)
        return real(**kw)
    monkeypatch.setattr(attachments.httpx, "AsyncClient", spy)
    run([url("http://ha.lan/x")], ["ha.lan"])
    assert made["trust_env"] is False and made["follow_redirects"] is False


def test_identity_encoding_requested():
    seen = []

    def h(r):
        seen.append(r.headers.get("accept-encoding"))
        return httpx.Response(200, content=b"x")
    run([url("http://ha.lan/x")], ["ha.lan"], h)
    assert seen == ["identity"]


def test_malformed_allowlist_entries_fail_closed():
    for bad in ("h:x", "ha.lan:", ":80", "ha.lan:1:2"):
        with pytest.raises(AttachmentError, match="not in the allowed list"):
            check_url_allowed("http://ha.lan/x", [bad])
    check_url_allowed("http://ha.lan/x", ["h:x", "ha.lan"])
