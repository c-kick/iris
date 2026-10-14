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


def send(srv) -> tuple[str, str]:
    with srv.client() as c:
        out = c.post("/v1/notifications", json={"targets": ["device:alex-email"], "message": "Pick", "title": "Pick",
                                                 "kinds": ["email"], "reply": True}).json()
        wait_for(lambda: c.get(f"/v1/notifications/{out['id']}").json()["deliveries"][0]["state"] == "dispatched")
    return out["id"], out["deliveries"][0]["id"]


def test_cli_saves_attachment(email_live, tmp_path, monkeypatch, capsys):
    nid, did = send(email_live)
    email_live.mailbox.add(reply_raw(did, attachment=PNG))
    email_live.call(lambda: inbound.poll_once(email_live.service, email_live.service.providers["mail"], email_live.mailbox))
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    (tmp_path / "shot.png").write_bytes(b"old")  # existing file is never overwritten
    code = nw.main([nid, "--url", email_live.url, "--save-dir", str(tmp_path), "--timeout", "30s"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert out.startswith("Reply from alex@example.org (") and 'to "Pick"' in out and "Go with B." in out
    assert (tmp_path / "shot-2.png").read_bytes() == PNG and (tmp_path / "shot.png").read_bytes() == b"old"
    assert f"Attachments: {tmp_path / 'shot-2.png'}" in out
    assert TOKENS["agent"] not in out


def test_cli_json_and_timeout(email_live, monkeypatch, capsys):
    nid, did = send(email_live)
    monkeypatch.setenv("NOTIFY_TOKEN", TOKENS["agent"])
    assert nw.main([nid, "--url", email_live.url, "--timeout", "2s"]) == 2
    assert "No reply within 2 s" in capsys.readouterr().out
    email_live.mailbox.add(reply_raw(did))
    email_live.call(lambda: inbound.poll_once(email_live.service, email_live.service.providers["mail"], email_live.mailbox))
    assert nw.main([nid, "--url", email_live.url, "--timeout", "10s", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "Go with B."


def test_cli_errors(email_live, monkeypatch, capsys):
    nid, _ = send(email_live)
    monkeypatch.setenv("NOTIFY_TOKEN", "wrong")
    assert nw.main([nid, "--url", email_live.url, "--timeout", "5s"]) == 1
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
