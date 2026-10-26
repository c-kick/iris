from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

import pytest

import notify.providers.email_inbound as inbound
from tests.conftest import PNG, TOKENS, wait_for
from tests.mailfactory import reply_raw
from tests.test_replies_core import HANDLES, send

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "notify-wait"


def load():
    loader = importlib.machinery.SourceFileLoader("notify_wait", str(SCRIPT))
    spec = importlib.util.spec_from_loader("notify_wait", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


nw = load()


def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)


def test_parse_duration():
    assert nw.parse_duration("90s") == 90 and nw.parse_duration("30m") == 1800
    assert nw.parse_duration("2h") == 7200 and nw.parse_duration("5") == 300
    with pytest.raises(Exception):
        nw.parse_duration("soon")


def test_safe_filename():
    taken: set[str] = set()
    assert nw.safe_filename("../../.bashrc", taken) == "bashrc"
    assert nw.safe_filename("a/b\\c\x00d.png", taken) == "cd.png"
    assert nw.safe_filename("", taken) == "attachment"
    assert nw.safe_filename("shot.png", taken) == "shot.png"
    assert nw.safe_filename("SHOT.png", taken) == "SHOT-2.png"
    assert nw.safe_filename("😀 ontwerp.png", taken) == "😀 ontwerp.png"


def test_settings_from_claude_json(tmp_path, monkeypatch):
    f = tmp_path / "claude.json"
    f.write_text(json.dumps({"mcpServers": {"hnl-notify": {"type": "http", "url": "http://notify.lan:8930/mcp",
                                                           "headers": {"Authorization": "Bearer tok-x"}}}}))
    monkeypatch.delenv("NOTIFY_URL", raising=False)
    monkeypatch.delenv("NOTIFY_TOKEN", raising=False)
    assert nw.settings(None, nw.mcp_entry(f)) == ("http://notify.lan:8930", "tok-x")
    monkeypatch.setenv("NOTIFY_TOKEN", "tok-env")
    assert nw.settings("http://other:1/", nw.mcp_entry(f)) == ("http://other:1", "tok-env")
    assert nw.mcp_entry(tmp_path / "missing.json") == {}
    monkeypatch.delenv("NOTIFY_TOKEN")
    with pytest.raises(nw.Fatal):
        nw.settings(None, {})


def test_token_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFY_TOKEN", "tok-env")
    tf = tmp_path / "codex-token"
    tf.write_text("tok-file\n")
    assert nw.settings("http://h:1", {}, tf) == ("http://h:1", "tok-file")  # beats NOTIFY_TOKEN
    with pytest.raises(nw.Fatal) as e:
        nw.settings("http://h:1", {}, tmp_path / "missing")
    assert "cannot read token file" in str(e.value)
    (tmp_path / "empty").write_text("")
    with pytest.raises(nw.Fatal):
        nw.settings("http://h:1", {}, tmp_path / "empty")


def test_cli_token_file(email_live, tmp_path, monkeypatch, capsys):
    nid, _ = send(email_live)
    monkeypatch.delenv("NOTIFY_TOKEN", raising=False)
    tf = tmp_path / "tok"
    tf.write_text(TOKENS["agent"])
    assert nw.main([HANDLES[nid], "--url", email_live.url, "--token-file", str(tf), "--timeout", "1s"]) == 2
    assert TOKENS["agent"] not in capsys.readouterr().out


def test_cli_saves_attachment(email_live, tmp_path, monkeypatch, capsys):
    nid, did = send(email_live)
    email_live.mailbox.add(reply_raw(did, attachment=PNG))
    email_live.call(lambda: inbound.poll_once(email_live.service, email_live.service.providers["mail"], email_live.mailbox))
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    (tmp_path / "shot.png").write_bytes(b"old")  # existing file is never overwritten
    code = nw.main([HANDLES[nid], "--url", email_live.url, "--save-dir", str(tmp_path), "--timeout", "30s"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert out.startswith("Reply from alex@example.org (") and 'to "Pick"' in out and "Go with B." in out
    assert (tmp_path / "shot-2.png").read_bytes() == PNG and (tmp_path / "shot.png").read_bytes() == b"old"
    assert f"Attachments: {tmp_path / 'shot-2.png'}" in out
    assert TOKENS["agent"] not in out


def test_cli_json_and_timeout(email_live, monkeypatch, capsys):
    nid, did = send(email_live)
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    assert nw.main([HANDLES[nid], "--url", email_live.url, "--timeout", "2s"]) == 2
    assert "No reply within 2 s" in capsys.readouterr().out
    email_live.mailbox.add(reply_raw(did))
    email_live.call(lambda: inbound.poll_once(email_live.service, email_live.service.providers["mail"], email_live.mailbox))
    assert nw.main([HANDLES[nid], "--url", email_live.url, "--timeout", "10s", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "Go with B."


def test_cli_errors(email_live, monkeypatch, capsys):
    nid, _ = send(email_live)
    monkeypatch.setenv("NOTIFY_TOKEN", "wrong")
    assert nw.main([HANDLES[nid], "--url", email_live.url, "--timeout", "5s"]) == 1
    err = capsys.readouterr().err
    assert "HTTP 401" in err and "wrong" not in err
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    assert nw.main(["ntf_nothere", "--url", email_live.url, "--timeout", "5s"]) == 1


def test_cli_unreachable_service_times_out(monkeypatch, capsys):
    monkeypatch.setenv("NOTIFY_TOKEN", "tok")
    monkeypatch.setattr(nw.time, "sleep", lambda s: None)
    assert nw.main(["ntf_x", "--url", "http://127.0.0.1:9", "--timeout", "2s"]) == 2


def test_safe_filename_long_and_dots():
    taken: set[str] = set()
    n = nw.safe_filename("😀" * 120 + ".png", taken)
    assert len(n.encode()) <= 200 and n.endswith(".png")
    n2 = nw.safe_filename("😀" * 120 + ".png", taken)
    assert n2 != n and len(n2.encode()) <= 200 and n2.endswith("-2.png")
    assert [nw.safe_filename(x, set()) for x in ("..", ".", "a/..")] == ["attachment"] * 3


def test_long_name_saves(tmp_path):
    nw_req = nw._request
    nw._request = lambda *a, **k: (200, b"x")
    try:
        paths = nw.save_attachments("http://h", "t", {"attachments": [{"filename": "😀" * 120 + ".png", "url": "/v1/a"}]},
                                    str(tmp_path))
    finally:
        nw._request = nw_req
    assert len(paths) == 1 and Path(paths[0]).read_bytes() == b"x"


@pytest.mark.parametrize("url", ["@evil.com/x", "//evil.com/x", "http://evil.com/x"])
def test_hostile_attachment_url(url, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(nw, "_request", lambda *a, **k: calls.append(a) or (200, b"x"))
    with pytest.raises(nw.Fatal):
        nw.save_attachments("http://h", "t", {"attachments": [{"filename": "a", "url": url}]}, str(tmp_path))
    assert calls == []


def test_no_redirect_follow():
    import urllib.request
    h = nw._NoRedirect()
    req = urllib.request.Request("http://h/x")
    assert h.redirect_request(req, None, 302, "Found", {}, "http://evil.com/") is None


def test_retries_incomplete_read_and_bad_json(monkeypatch, capsys):
    import http.client
    monkeypatch.setattr(nw.time, "sleep", lambda s: None)
    reply = {"id": "rpl_1", "notification_id": "n", "from": "a@example.org", "received_at": "2026-01-01T00:00:00Z",
             "text": "hi", "attachments": []}
    seq = [http.client.IncompleteRead(b""), (200, b"{nope"), (200, json.dumps(reply).encode())]

    def fake(*a, **k):
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(nw, "_request", fake)
    monkeypatch.setenv("NOTIFY_TOKEN", "tok")
    assert nw.main(["n", "--url", "http://h", "--timeout", "30s"]) == 0
    assert "hi" in capsys.readouterr().out


def test_bad_reply_shape_is_error(monkeypatch, capsys):
    monkeypatch.setattr(nw, "_request", lambda *a, **k: (200, b"{}"))
    monkeypatch.setenv("NOTIFY_TOKEN", "tok")
    assert nw.main(["n", "--url", "http://h", "--timeout", "30s"]) == 1
    assert "unexpected reply format" in capsys.readouterr().err


def test_render_strips_escapes():
    out = nw.render({"id": "r", "notification_id": "n", "from": "a\x1b[31m@example.org", "received_at": "2026-01-01T00:00:00Z",
                     "text": "x\x1b]0;pwned\x07y\x1b[2J\nline\ttab"}, ["/tmp/\x1b[1mf"])
    assert "\x1b" not in out and "\x07" not in out and "line\ttab" in out


@pytest.mark.parametrize("as_json", [False, True])
def test_attachment_failure_still_prints_reply(email_live, tmp_path, monkeypatch, capsys, as_json):
    nid, did = send(email_live)
    email_live.mailbox.add(reply_raw(did, attachment=PNG))
    email_live.call(lambda: inbound.poll_once(email_live.service, email_live.service.providers["mail"], email_live.mailbox))
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    real = nw._request

    def flaky(method, url, token, timeout, handle=None):
        if method == "GET":
            raise nw.urllib.error.URLError("boom")
        return real(method, url, token, timeout, handle)
    monkeypatch.setattr(nw, "_request", flaky)
    argv = [HANDLES[nid], "--url", email_live.url, "--save-dir", str(tmp_path), "--timeout", "30s"] + (["--json"] if as_json else [])
    assert nw.main(argv) == 0
    cap = capsys.readouterr()
    assert "Go with B." in cap.out
    if as_json:
        assert json.loads(cap.out)["saved"] == []
    assert "notify-wait: could not save attachments:" in cap.err and "boom" in cap.err
    assert TOKENS["agent"] not in cap.out + cap.err


def poll(srv):
    srv.call(lambda: inbound.poll_once(srv.service, srv.service.providers["mail"], srv.mailbox))


def test_wait_with_handle_env(email_live, monkeypatch, capsys):
    nid, did = send(email_live)
    email_live.mailbox.add(reply_raw(did))
    poll(email_live)
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    monkeypatch.setenv("NOTIFY_REPLY_HANDLE", HANDLES[nid] + "\n")
    assert nw.main([nid, "--url", email_live.url, "--timeout", "10s"]) == 0
    assert "Go with B." in capsys.readouterr().out


def test_wait_with_handle_file(email_live, tmp_path, monkeypatch, capsys):
    nid, did = send(email_live)
    email_live.mailbox.add(reply_raw(did))
    poll(email_live)
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    monkeypatch.delenv("NOTIFY_REPLY_HANDLE", raising=False)
    f = tmp_path / "h"
    f.write_text(HANDLES[nid] + "\n")
    assert nw.main(["--handle-file", str(f), "--url", email_live.url, "--timeout", "10s"]) == 0
    assert "Go with B." in capsys.readouterr().out


def test_positional_handle_beats_env(email_live, monkeypatch, capsys):
    nid, did = send(email_live)
    email_live.mailbox.add(reply_raw(did))
    poll(email_live)
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    monkeypatch.setenv("NOTIFY_REPLY_HANDLE", "ntf_x:" + "z" * 32)
    assert nw.main([HANDLES[nid], "--url", email_live.url, "--timeout", "10s"]) == 0
    assert "Go with B." in capsys.readouterr().out


def test_conflicting_id_and_env_handle_exit_1(email_live, monkeypatch, capsys):
    nid, _ = send(email_live)
    other, _ = send(email_live)
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    monkeypatch.setenv("NOTIFY_REPLY_HANDLE", HANDLES[other])
    assert nw.main([nid, "--url", email_live.url, "--timeout", "5s"]) == 1
    err = capsys.readouterr().err
    assert "the reply handle belongs to another conversation" in err
    assert HANDLES[other].split(":", 1)[1] not in err


def test_handle_never_printed(email_live, monkeypatch, capsys):
    nid, _ = send(email_live)
    wrong = nid + ":" + "Q" * 32
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    assert nw.main([wrong, "--url", email_live.url, "--timeout", "5s"]) == 1
    cap = capsys.readouterr()
    assert "Q" * 32 not in cap.out + cap.err
    assert nw.main([nid, "--url", email_live.url, "--timeout", "5s"]) == 1  # no handle at all
    assert HANDLES[nid].split(":", 1)[1] not in capsys.readouterr().out


def test_attachment_download_sends_handle(email_live, tmp_path, monkeypatch, capsys):
    nid, did = send(email_live)
    email_live.mailbox.add(reply_raw(did, attachment=PNG))
    poll(email_live)
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    seen = []
    real = nw._request

    def spy(method, url, token, timeout, handle=None):
        seen.append((method, handle))
        return real(method, url, token, timeout, handle)
    monkeypatch.setattr(nw, "_request", spy)
    assert nw.main([HANDLES[nid], "--url", email_live.url, "--save-dir", str(tmp_path), "--timeout", "10s"]) == 0
    assert ("POST", HANDLES[nid]) in seen and ("GET", HANDLES[nid]) in seen
    assert (tmp_path / "shot.png").read_bytes() == PNG
