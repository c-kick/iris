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
    top = "\u256d" + "\u2500" * 20 + "\u256e"
    box = "\u2502 > {}          \u2502"
    before = f"history\n{top}\n" + box.format("") + "\n"
    # Claude: border above, the input line, then a suggestion list under it
    assert relay._input_holds_clear(before, f"history\n{top}\n" + box.format("/clear") + "\n  /clear  clear the context")
    # Claude with a placeholder in the empty line
    assert relay._input_holds_clear(f"{top}\n" + box.format('Try "fix lint"'), f"{top}\n" + box.format("/clear"))
    assert relay._input_holds_clear("\u203a ", "\u203a /clear")  # Codex, nothing above
    # text was there: the changed line is "half/clear"
    assert not relay._input_holds_clear("> half", "> half/clear")
    # an earlier /clear in the history is not the input line
    assert not relay._input_holds_clear("> /clear\n> half", "> /clear\n> half/clear")
    # a two-line draft: /clear alone on its second line, the first line above it is the draft
    assert not relay._input_holds_clear(f"{top}\n\u2502 > fix the tests \u2502\n\u2502   \u2502",
                                        f"{top}\n\u2502 > fix the tests \u2502\n\u2502   /clear \u2502")
    assert not relay._input_holds_clear("x", "x")


def test_input_line_check_suggestions_above_the_box():
    """Claude Code 2.1.322 (captured 2026-11-12): the suggestion list opens above the input box, so the first changed
    line with /clear is the suggestion row, not the input line."""
    rule = "─" * 40
    sugg = "  ❯ /clear                     Start a new session with empty context; previous sess"
    before = "\n".join(["history", "", rule, "❯ Try \"how do I log an error?\"", rule,
                        "  ⏵⏵ auto mode on (shift+tab to cycle) · ← 2 agents"])
    after = "\n".join(["history", sugg, rule, "❯ /clear", rule,
                       "  ⏵⏵ auto mode on (shift+tab to cycle)"])
    assert relay._input_holds_clear(before, after)
    # the same screen with a draft in the input: never sent
    assert not relay._input_holds_clear(before.replace("Try \"how do I log an error?\"", "half"),
                                        after.replace(" /clear", " half/clear"))
    # a two-line draft under the suggestions: the line above the input's /clear is the draft
    assert not relay._input_holds_clear("\n".join(["", rule, "❯ fix", rule]),
                                        "\n".join([sugg, rule, "❯ fix", "  /clear", rule]))


def test_codex_refuses_when_it_cannot_tell_which_window(monkeypatch):
    me = os.getpid()
    base = {"kind": "codex", "pid": me, "pid_start": relay.pid_start(me), "folder": "/srv/app", "shared_process": True}
    monkeypatch.setattr(relay, "_codex_tuis", lambda folder: [111])
    monkeypatch.setattr(relay, "_environ", lambda pid: {"TMUX_PANE": "%1", "TMUX": "/tmp/x,1,0"})
    monkeypatch.setattr(relay, "_pane_of", lambda pid: None)
    assert relay.do_reset({**base, "codex_members": 2}) == "not_found", "two Codex members on one app-server"
    monkeypatch.setattr(relay, "_codex_tuis", lambda folder: [111, 222])
    assert relay.do_reset({**base, "codex_members": 1}) == "not_found", "two windows in the folder"
    monkeypatch.setattr(relay, "_codex_tuis", lambda folder: [111])
    monkeypatch.setattr(relay, "_environ", lambda pid: {})
    assert relay.do_reset({**base, "codex_members": 1}) == "not_tmux"


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
