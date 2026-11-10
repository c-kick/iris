"""The relay asks a shared `codex app-server` which sessions are still open (JSON-RPC `thread/loaded/list` over its
unix control socket, WebSocket framing). A session the server no longer has loaded is not reported alive, so the
stale sweep removes it; a server that cannot be asked leaves the pid check as the answer.

The fake server runs in the test process, so the relay finds its socket through /proc like the real one."""

from __future__ import annotations

import base64
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import socket
import struct
import tempfile
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "chat-relay"


def _load():
    loader = importlib.machinery.SourceFileLoader("chat_relay_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


relay = _load()


def _frame(text: str, op: int = 0x1) -> bytes:  # server frames are not masked
    data = text.encode()
    n = len(data)
    head = bytes([0x80 | op]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536
                                 else bytes([127]) + struct.pack(">Q", n))
    return head + data


def _read_frame(conn: socket.socket) -> tuple[int, bytes]:
    def exact(n):
        buf = b""
        while len(buf) < n:
            c = conn.recv(n - len(buf))
            if not c:
                raise EOFError
            buf += c
        return buf
    b1, b2 = exact(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", exact(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", exact(8))[0]
    mask = exact(4)
    assert b2 & 0x80, "client frames must be masked"
    return b1 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(exact(n)))


class FakeAppServer:
    """pages: the loaded thread ids, page by page. mode: 'ok', 'error' (an error answer), 'plain' (no WebSocket)."""

    def __init__(self, pages: list[list[str]], mode: str = "ok"):
        self.pages, self.mode, self.methods = pages, mode, []
        self.dir = tempfile.mkdtemp(prefix="cx-")
        self.path = os.path.join(self.dir, "control.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            try:
                self._handle(conn)
            except (EOFError, OSError):
                pass
            finally:
                conn.close()

    def _handle(self, conn):
        conn.settimeout(5)
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(1)
        if self.mode == "plain":
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        key = [ln.split(b":", 1)[1].strip() for ln in head.split(b"\r\n") if ln.lower().startswith(b"sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        if self.mode == "trickle":  # a frame that never completes, one byte at a time
            conn.sendall(b"\x81\x7e\xff\xff")
            while not self.stop.wait(0.3):
                conn.sendall(b"x")
            return
        conn.sendall(_frame(json.dumps({"method": "remoteControl/status/changed", "params": {}})))  # unasked
        conn.sendall(_frame("", op=0x9))  # a ping the client must survive
        while True:
            op, data = _read_frame(conn)
            if op != 0x1:
                continue
            m = json.loads(data)
            self.methods.append(m.get("method"))
            if "id" not in m:
                continue
            if m["method"] == "initialize":
                conn.sendall(_frame(json.dumps({"id": m["id"], "result": {"userAgent": "fake"}})))
            elif m["method"] == "thread/loaded/list":
                if self.mode == "nested":
                    conn.sendall(_frame("[" * 200000))
                    continue
                if self.mode == "error":
                    conn.sendall(_frame(json.dumps({"id": m["id"], "error": {"code": -1, "message": "no"}})))
                    continue
                i = int((m.get("params") or {}).get("cursor") or 0)
                nxt = str(i + 1) if i + 1 < len(self.pages) else None
                conn.sendall(_frame(json.dumps({"id": m["id"], "result": {"data": self.pages[i], "nextCursor": nxt}})))

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self.sock.close()
        try:
            os.unlink(self.path)
            os.rmdir(self.dir)
        except OSError:
            pass


@pytest.fixture
def server(request):
    servers = []

    def make(pages, mode="ok"):
        s = FakeAppServer(pages, mode)
        servers.append(s)
        return s
    yield make
    for s in servers:
        s.close()


ME = os.getpid()


def test_finds_the_listening_socket_of_a_process(server):
    s = server([[]])
    assert s.path in relay.listening_unix_sockets(ME)
    assert relay.listening_unix_sockets(2 ** 22 + 12345) == []  # no such process


def test_lists_loaded_threads_across_pages(server):
    s = server([["t-1", "t-2"], ["t-3"]])
    assert relay.codex_loaded_threads_at(s.path) == {"t-1", "t-2", "t-3"}
    assert s.methods[:2] == ["initialize", "initialized"]
    assert relay.codex_loaded_threads(ME) == {h("t-1"), h("t-2"), h("t-3")}  # hashed like the server's


def test_an_error_answer_or_a_non_websocket_socket_is_unknown(server):
    with pytest.raises(relay._RpcError):
        relay.codex_loaded_threads_at(server([["t"]], mode="error").path)
    with pytest.raises(relay._RpcError):
        relay.codex_loaded_threads_at(server([["t"]], mode="plain").path)


def _relay_with(members):
    r = relay.Relay("http://127.0.0.1:9", "tok", "codex", "/nonexistent", "relay-test")
    r.members = members
    return r


def h(sid):
    return hashlib.sha256(sid.encode()).hexdigest()


def _m(mid, kind="codex", shared=True, session="t-open", pid=ME):  # as the server's heartbeat answer has it
    m = {"member_id": mid, "kind": kind, "pid": pid, "pid_start": relay.pid_start(pid)}
    if kind == "codex" and shared:
        m["codex_session_sha256"] = h(session)
    return m


def test_a_closed_codex_session_is_not_reported_alive(server):
    server([["t-open"]])
    r = _relay_with([_m("open"), _m("closed", session="t-gone"), _m("unshared", shared=False, session="t-gone"),
                     _m("claude", kind="claude", shared=False, session="c-1")])
    alive = {a["member_id"] for a in r.alive_members()}
    assert alive == {"open", "unshared", "claude"}


def test_a_server_that_cannot_be_asked_keeps_the_pid_answer(server):
    server([["t-open"]], mode="error")
    r = _relay_with([_m("a", session="t-gone")])
    assert [a["member_id"] for a in r.alive_members()] == ["a"]


def test_no_control_socket_keeps_the_pid_answer(monkeypatch):
    monkeypatch.setattr(relay, "listening_unix_sockets", lambda pid: [])
    r = _relay_with([_m("a", session="t-gone")])
    assert [a["member_id"] for a in r.alive_members()] == ["a"]


def test_the_server_is_asked_once_per_round(monkeypatch):
    calls = []
    monkeypatch.setattr(relay, "codex_loaded_threads", lambda pid, *_: calls.append(pid) or {h("t-open")})
    r = _relay_with([_m("a"), _m("b"), _m("c", session="t-gone")])
    assert {a["member_id"] for a in r.alive_members()} == {"a", "b"}
    assert calls == [ME]


def test_a_dead_pid_is_never_asked(monkeypatch):
    monkeypatch.setattr(relay, "codex_loaded_threads", lambda pid, *_: pytest.fail("asked a dead server"))
    m = _m("a")
    m["pid_start"] = "not-its-start"
    assert _relay_with([m]).alive_members() == []


def test_a_trickling_server_cannot_hold_the_heartbeat(server, monkeypatch):
    import time
    server([["t"]], mode="trickle")
    monkeypatch.setattr(relay, "CODEX_RPC_TIMEOUT", 1.0)
    t0 = time.monotonic()
    r = _relay_with([_m("a", session="t-gone")])
    assert [a["member_id"] for a in r.alive_members()] == ["a"], "unknown: the pid decides"
    assert time.monotonic() - t0 < 3


def test_deeply_nested_json_is_unknown_not_a_crash(server):
    server([["t"]], mode="nested")
    r = _relay_with([_m("a", session="t-gone")])
    assert [a["member_id"] for a in r.alive_members()] == ["a"]


def test_the_round_budget_bounds_every_server(monkeypatch):
    monkeypatch.setattr(relay, "CODEX_ROUND_BUDGET", 0.0)
    monkeypatch.setattr(relay, "listening_unix_sockets", lambda pid: ["/nonexistent.sock"])
    monkeypatch.setattr(relay, "codex_loaded_threads_at", lambda *a: pytest.fail("asked past the budget"))
    assert [a["member_id"] for a in _relay_with([_m("a", session="t-gone")]).alive_members()] == ["a"]
