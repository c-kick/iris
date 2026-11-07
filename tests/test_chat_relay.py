from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from tests.test_chat_api import CHAT_TOKENS, FOLDER, chatlive  # noqa: F401  (fixture)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "chat-relay"

FAKE_CODEX = """#!{py}
import json, os, sys
with open({log!r}, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(1 if os.path.exists({fail!r}) else 0)
"""


class Env:
    def __init__(self, tmp_path, srv):
        self.home = tmp_path / "home"
        (self.home / ".config" / "hnl-notify").mkdir(parents=True)
        (self.home / ".config" / "hnl-notify" / "chat-token").write_text(CHAT_TOKENS["hosta"] + "\n")
        self.srv = srv
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.codex_log = tmp_path / "codex.log"
        self.fail = tmp_path / "codex.fail"
        codex = self.bin / "codex"
        codex.write_text(FAKE_CODEX.format(py=sys.executable, log=str(self.codex_log), fail=str(self.fail)))
        codex.chmod(0o755)
        self.socks = Path(tempfile.mkdtemp(prefix="cr-"))

    def fake_claude(self, version):
        c = self.bin / "claude"
        c.write_text(f"#!/bin/sh\necho '{version}'\n")
        c.chmod(0o755)

    def relay(self):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "NOTIFY"))}
        e.update(HOME=str(self.home), NOTIFY_URL=self.srv.url, PATH=f"{self.bin}:{e.get('PATH', '')}")
        return subprocess.run([sys.executable, str(SCRIPT), "--once", "--sock-dir", str(self.socks)],
                              capture_output=True, text=True, env=e, timeout=30)

    def codex_calls(self):
        if not self.codex_log.exists():
            return []
        return [json.loads(x) for x in self.codex_log.read_text().splitlines()]

    def close(self):
        shutil.rmtree(self.socks, ignore_errors=True)


@pytest.fixture
def env(tmp_path, chatlive):
    e = Env(tmp_path, chatlive)
    yield e
    e.close()


def pid_start_of(pid):
    return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]


def join(srv, kind, sid, pid, start):
    r = srv.client("hosta").post("/v1/chat/join", json={"kind": kind, "session_id": sid, "folder": FOLDER,
                                                        "pid": pid, "pid_start": start})
    assert r.status_code == 201, r.text
    return r.json()


def wake_row(srv, member_id):
    return srv.service.db.one("SELECT * FROM chat_wakes WHERE member_id=?", (member_id,))


def spawn():
    return subprocess.Popen(["sleep", "30"])


def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)


def test_relay_delivers_codex_wake(env, chatlive):
    me = spawn()
    try:
        a = join(chatlive, "claude", "s-a", me.pid, pid_start_of(me.pid))
        b = join(chatlive, "codex", "thread-b", me.pid, pid_start_of(me.pid))
        chatlive.service.chat.post(a["member_handle"], "hello codex")
        p = env.relay()
        assert p.returncode == 0, p.stderr
        calls = env.codex_calls()
        assert len(calls) == 1 and calls[0][:2] == ["queue", "--thread=thread-b"] and len(calls[0]) == 3
        assert calls[0][2].startswith("--message=") and "hello codex" in calls[0][2]
        assert wake_row(chatlive, b["member_id"])["state"] == "delivered"
        assert CHAT_TOKENS["hosta"] not in p.stderr and "hello codex" not in p.stderr
    finally:
        me.kill()
        me.wait()


def test_relay_failure_releases_wake(env, chatlive):
    me = spawn()
    try:
        a = join(chatlive, "claude", "s-a", me.pid, pid_start_of(me.pid))
        b = join(chatlive, "codex", "thread-b", me.pid, pid_start_of(me.pid))
        chatlive.service.chat.post(a["member_handle"], "hello")
        env.fail.write_text("x")
        p = env.relay()
        assert p.returncode == 0 and "exited 1" in p.stderr
        assert len(env.codex_calls()) == 1
        w = wake_row(chatlive, b["member_id"])
        assert w["state"] == "pending" and w["attempts"] == 1 and w["retry_after"]
    finally:
        me.kill()
        me.wait()


def last_seen(srv, member_id):
    return srv.service.db.scalar("SELECT last_seen_at FROM chat_members WHERE id=?", (member_id,))


def test_relay_heartbeat_alive_and_dead_pid(env, chatlive):
    live, dead, reused = spawn(), spawn(), spawn()
    try:
        ls, ds = pid_start_of(live.pid), pid_start_of(dead.pid)
        a = join(chatlive, "claude", "s-live", live.pid, ls)
        b = join(chatlive, "claude", "s-dead", dead.pid, ds)
        c = join(chatlive, "claude", "s-reuse", reused.pid, "1")  # same pid number, other start time
        dead.kill()
        dead.wait()
        old = "2000-01-01T00:00:00.000Z"
        with chatlive.service.db.tx() as cx:
            cx.execute("UPDATE chat_members SET last_seen_at=?", (old,))
        p = env.relay()
        assert p.returncode == 0, p.stderr
        assert last_seen(chatlive, a["member_id"]) != old
        assert last_seen(chatlive, b["member_id"]) == old
        assert last_seen(chatlive, c["member_id"]) == old
    finally:
        for x in (live, dead, reused):
            x.kill()
            x.wait()


class SockServer:
    def __init__(self, path):
        self.lines = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(path))
        self.srv.listen(5)
        self.srv.settimeout(20)
        self.t = threading.Thread(target=self.serve, daemon=True)
        self.t.start()

    def serve(self):
        try:
            conn, _ = self.srv.accept()
            with conn:
                buf = b""
                while True:
                    d = conn.recv(4096)
                    if not d:
                        break
                    buf += d
            self.lines = buf.split(b"\n")
        except OSError:
            pass

    def close(self):
        self.t.join(timeout=5)
        self.srv.close()


def test_relay_delivers_claude_wake(env, chatlive):
    me = spawn()
    try:
        a = join(chatlive, "codex", "t-a", me.pid, pid_start_of(me.pid))
        b = join(chatlive, "claude", "s-b", me.pid, pid_start_of(me.pid))
        s = SockServer(env.socks / f"{me.pid}.sock")
        chatlive.service.chat.post(a["member_handle"], "hello claude")
        p = env.relay()
        s.close()
        assert p.returncode == 0, p.stderr
        assert len(s.lines) == 2 and s.lines[1] == b""  # exactly one newline-terminated line
        msg = json.loads(s.lines[0])
        assert msg["type"] == "user" and msg["message"]["role"] == "user" and set(msg) == {"type", "message"}
        assert "hello claude" in msg["message"]["content"]
        assert wake_row(chatlive, b["member_id"])["state"] == "delivered"
        assert env.codex_calls() == []
    finally:
        me.kill()
        me.wait()


def test_relay_claude_socket_missing(env, chatlive):
    me = spawn()
    try:
        a = join(chatlive, "codex", "t-a", me.pid, pid_start_of(me.pid))
        b = join(chatlive, "claude", "s-b", me.pid, pid_start_of(me.pid))
        chatlive.service.chat.post(a["member_handle"], "hello")
        p = env.relay()
        assert p.returncode == 0 and "socket delivery" in p.stderr
        w = wake_row(chatlive, b["member_id"])
        assert w["state"] == "pending" and w["attempts"] == 1 and w["retry_after"]
    finally:
        me.kill()
        me.wait()


def test_relay_version_warning(env, chatlive):
    env.fake_claude("3.0.0 (Claude Code)")
    me = spawn()
    try:
        a = join(chatlive, "claude", "s-a", me.pid, pid_start_of(me.pid))
        b = join(chatlive, "codex", "thread-b", me.pid, pid_start_of(me.pid))
        chatlive.service.chat.post(a["member_handle"], "hello")
        p = env.relay()
        assert p.returncode == 0 and p.stderr.count("warning") == 1 and "untested" in p.stderr
        assert wake_row(chatlive, b["member_id"])["state"] == "delivered"
        env.fake_claude("2.1.150 (Claude Code)")
        assert "warning" not in env.relay().stderr
    finally:
        me.kill()
        me.wait()


def test_relay_failed_wake_does_not_block_others(env, chatlive):
    me = spawn()
    try:
        st = pid_start_of(me.pid)
        sender = join(chatlive, "codex", "t-sender", me.pid, st)
        a = join(chatlive, "claude", "s-a", me.pid, st)  # no socket: undeliverable
        b = join(chatlive, "codex", "t-b", me.pid, st)
        chatlive.service.chat.post(sender["member_handle"], "hello")
        assert env.relay().returncode == 0 and env.relay().returncode == 0
        wa, wb = wake_row(chatlive, a["member_id"]), wake_row(chatlive, b["member_id"])
        assert wa["state"] == "pending" and wa["attempts"] == 1
        assert wb["state"] == "delivered"
    finally:
        me.kill()
        me.wait()


def test_relay_claude_pid_reused(env, chatlive):
    me = spawn()
    try:
        a = join(chatlive, "codex", "t-a", me.pid, pid_start_of(me.pid))
        b = join(chatlive, "claude", "s-b", me.pid, "1")  # recorded start time differs: the pid was reused
        s = SockServer(env.socks / f"{me.pid}.sock")
        chatlive.service.chat.post(a["member_handle"], "hello")
        p = env.relay()
        assert p.returncode == 0 and "reused" in p.stderr
        w = wake_row(chatlive, b["member_id"])
        assert w["state"] == "pending" and w["attempts"] == 1
        s.srv.close()
        s.t.join(timeout=5)
        assert s.lines == []
    finally:
        me.kill()
        me.wait()


def test_relay_rejects_bad_session_id(env, chatlive):
    me = spawn()
    try:
        a = join(chatlive, "claude", "s-a", me.pid, pid_start_of(me.pid))
        b = join(chatlive, "codex", "t-b", me.pid, pid_start_of(me.pid))
        with chatlive.service.db.tx() as cx:  # a row from before the join check
            cx.execute("UPDATE chat_members SET session_id='--help' WHERE id=?", (b["member_id"],))
        chatlive.service.chat.post(a["member_handle"], "hello")
        p = env.relay()
        assert p.returncode == 0 and "invalid session id" in p.stderr
        assert env.codex_calls() == [] and wake_row(chatlive, b["member_id"])["attempts"] == 1
    finally:
        me.kill()
        me.wait()
