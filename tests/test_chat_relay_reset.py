"""The relay's context reset. The pure checks always run; the real-tmux checks (a private tmux server with a fake
prompt program: /clear is typed and submitted only into an empty input line, text typed there is never sent and stays)
run only with IRIS_TMUX_TESTS=1, because they start and drive a tmux server."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

from tests.test_chat_relay_codex import relay

tmux_only = pytest.mark.skipif(not shutil.which("tmux") or os.environ.get("IRIS_TMUX_TESTS") != "1",
                               reason="drives a tmux server: run with IRIS_TMUX_TESTS=1")

FAKE = r'''
import sys
out = open(sys.argv[1], "a")
while True:
    sys.stdout.write("> "); sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        break
    out.write(line); out.flush()
'''


def test_input_line_check():
    box = "\u2502 > {}          \u2502"
    before = "history\n" + box.format("") + "\n"
    # Claude: the input line, then a suggestion list under it
    assert relay._input_holds_clear(before, "history\n" + box.format("/clear") + "\n  /clear  clear the context")
    assert relay._input_holds_clear("\u203a ", "\u203a /clear")  # Codex
    # text was there: the changed line is "half/clear"
    assert not relay._input_holds_clear("> half", "> half/clear")
    # an earlier /clear in the history is not the input line
    assert not relay._input_holds_clear("> /clear\n> half", "> /clear\n> half/clear")
    assert not relay._input_holds_clear("x", "x")


def test_the_only_text_the_relay_types_is_hard_coded():
    src = (relay.__file__ and open(relay.__file__).read())
    assert 'CLEAR = "/clear"' in src
    body = src[src.index("def type_clear"):src.index("def do_reset")]
    assert '"-l", CLEAR' in body and body.count('"-l"') == 1, "send-keys -l only ever with CLEAR"


def test_gone_and_unknown_kinds_never_touch_tmux(monkeypatch):
    calls = []
    monkeypatch.setattr(relay, "_tmux", lambda *a: calls.append(a))
    me = os.getpid()
    assert relay.do_reset({"kind": "claude", "pid": me, "pid_start": "999"}) == "gone"
    assert relay.do_reset({"kind": "other", "pid": me, "pid_start": relay.pid_start(me)}) == "not_found"
    assert relay.do_reset({"kind": "codex", "pid": me, "pid_start": relay.pid_start(me), "folder": "rel"}) == "not_found"
    assert calls == []


def test_a_claude_outside_tmux_is_not_tmux(monkeypatch):
    monkeypatch.setattr(relay, "_environ", lambda pid: {})
    me = os.getpid()
    assert relay.do_reset({"kind": "claude", "pid": me, "pid_start": relay.pid_start(me)}) == "not_tmux"


@pytest.fixture
def pane(tmp_path):
    sock = f"/tmp/claude-1026/tmx-{os.getpid()}"
    prog = tmp_path / "fake.py"
    prog.write_text(FAKE)
    got = tmp_path / "got.txt"
    got.write_text("")
    subprocess.run(["tmux", "-S", sock, "new-session", "-d", "-x", "80", "-y", "10", sys.executable, str(prog),
                    str(got)], check=True, timeout=10)
    pid = int(subprocess.run(["tmux", "-S", sock, "display-message", "-p", "-t", "%0", "#{pane_pid}"],
                             capture_output=True, text=True, timeout=5).stdout.strip())
    time.sleep(0.5)
    yield sock, pid, got
    subprocess.run(["tmux", "-S", sock, "kill-server"], timeout=10)


def _wake(pid):
    return {"type": "reset", "reset_id": "rst_1", "kind": "claude", "pid": pid, "pid_start": relay.pid_start(pid),
            "folder": "/srv/app", "shared_process": False}


@tmux_only
def test_an_empty_input_line_gets_clear_and_enter(pane):
    sock, pid, got = pane
    assert relay.do_reset(_wake(pid)) == "reset"
    time.sleep(0.4)
    assert got.read_text() == "/clear\n"


@tmux_only
def test_text_typed_there_is_never_sent(pane):
    sock, pid, got = pane
    subprocess.run(["tmux", "-S", sock, "send-keys", "-t", "%0", "-l", "half a thought"], timeout=5)
    time.sleep(0.3)
    assert relay.do_reset(_wake(pid)) == "typed"
    time.sleep(0.4)
    screen = subprocess.run(["tmux", "-S", sock, "capture-pane", "-p", "-t", "%0"], capture_output=True, text=True,
                            timeout=5).stdout
    assert got.read_text() == "", "nothing submitted"
    assert "> half a thought" in screen and "/clear" not in screen, "their text stays, ours is gone"
