from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.test_chat_api import CHAT_TOKENS, chatlive  # noqa: F401  (fixture)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "notify-chat"


def load():
    loader = importlib.machinery.SourceFileLoader("notify_chat", str(SCRIPT))
    spec = importlib.util.spec_from_loader("notify_chat", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


nc = load()


def git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@x"})


@pytest.fixture
def plain(tmp_path):
    """A folder outside any git checkout (pytest's basetemp lives inside this repo)."""
    with tempfile.TemporaryDirectory(prefix="nc-") as d:
        yield Path(d).resolve()


def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)


def test_group_folder_repo_root(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    git("init", "-q", cwd=repo)
    assert nc.group_folder(str(repo / "src")) == str(repo.resolve())


def test_group_folder_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    git("commit", "-q", "--allow-empty", "-m", "x", cwd=repo)
    wt = tmp_path / "wt"
    git("worktree", "add", "-q", str(wt), cwd=repo)
    (wt / "sub").mkdir()
    assert nc.group_folder(str(wt / "sub")) == str(wt.resolve())


def test_group_folder_no_repo(plain):
    assert nc.group_folder(str(plain)) == str(plain)


def test_group_folder_symlink(tmp_path):
    d = tmp_path / "real"
    d.mkdir()
    (tmp_path / "link").symlink_to(d)
    assert nc.group_folder(str(tmp_path / "link")) == nc.group_folder(str(d))


def test_proc_stat_comm_with_parens_and_spaces(monkeypatch):
    fake = "42 (a) b (c d) S 7 1 1 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 98765 1 1 0"
    monkeypatch.setattr(nc.Path, "read_text", lambda self, **k: fake)
    assert nc.proc_stat(42) == ("a) b (c d", 7, "98765")


class Env:
    def __init__(self, tmp_path, srv, who="hosta"):
        self.home = tmp_path / "home"
        (self.home / ".config" / "hnl-notify").mkdir(parents=True)
        self.token = CHAT_TOKENS[who]
        self.tf = self.home / ".config" / "hnl-notify" / "chat-token"
        self.tf.write_text(self.token + "\n")
        self.url = srv.url
        self.state = self.home / ".cache" / "hnl-notify-chat"

    def env(self, **extra):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "NOTIFY"))}
        e.update(HOME=str(self.home), NOTIFY_URL=self.url, **extra)
        return e

    def run(self, cmd, agent, hook, exe=None, **extra):
        t = time.monotonic()
        p = subprocess.run([exe or sys.executable, str(SCRIPT), cmd, "--agent", agent], input=json.dumps(hook),
                           capture_output=True, text=True, env=self.env(**extra), timeout=20)
        p.elapsed = time.monotonic() - t
        return p


def hook(tmp_path, sid="sess-1", **kw):
    return {"session_id": sid, "cwd": str(tmp_path), "transcript_path": str(tmp_path / "none.jsonl"), **kw}


def live_count(srv):
    return srv.service.db.scalar("SELECT COUNT(*) FROM chat_members WHERE left_at IS NULL")


def test_start_writes_state_0600_and_context(chatlive, tmp_path, plain):
    e = Env(tmp_path, chatlive)
    me = subprocess.Popen(["sleep", "30"])
    try:
        p = e.run("start", "claude", hook(plain), CLAUDE_PID=str(me.pid), CLAUDE_CODE_SESSION_ATTENDED="1")
        assert p.returncode == 0 and p.stderr == "", p.stderr
        out = json.loads(p.stdout)["hookSpecificOutput"]
        assert out["hookEventName"] == "SessionStart"
        f = e.state / "claude-sess-1.json"
        st = json.loads(f.read_text())
        assert st["member_handle"] in out["additionalContext"]
        for tool in ("group_post", "group_read", "group_leave"):
            assert tool in out["additionalContext"]
        assert len(out["additionalContext"]) < 600 and e.token not in p.stdout
        assert oct(f.stat().st_mode & 0o777) == "0o600" and oct(e.state.stat().st_mode & 0o777) == "0o700"
        row = chatlive.service.db.one("SELECT pid, pid_start FROM chat_members")
        assert row["pid"] == me.pid and row["pid_start"] == nc.pid_start(me.pid)
        assert chatlive.service.db.one("SELECT folder FROM chat_groups")["folder"] == str(plain)
    finally:
        me.kill()
        me.wait()


def test_end_leaves_and_removes_state(chatlive, tmp_path):
    e = Env(tmp_path, chatlive)
    h = hook(tmp_path)
    assert e.run("start", "claude", h).returncode == 0
    assert live_count(chatlive) == 1
    p = e.run("end", "claude", h)
    assert p.returncode == 0 and p.stderr == ""
    assert live_count(chatlive) == 0 and not (e.state / "claude-sess-1.json").exists()
    assert e.run("end", "claude", h).returncode == 0  # nothing to leave


def test_end_fast(chatlive, tmp_path):
    e = Env(tmp_path, chatlive)
    h = hook(tmp_path)
    e.run("start", "claude", h)
    e.url = "http://127.0.0.1:9"
    p = e.run("end", "claude", h)
    assert p.returncode == 0 and p.elapsed < 1.5 and "notify-chat" in p.stderr
    assert not (e.state / "claude-sess-1.json").exists()


def test_start_unreachable_exits_0(chatlive, tmp_path):
    e = Env(tmp_path, chatlive)
    e.url = "http://127.0.0.1:9"
    p = e.run("start", "claude", hook(tmp_path))
    assert p.returncode == 0 and p.stdout == "" and p.stderr.count("\n") == 1


def test_start_codex_pid_from_ancestors(chatlive, tmp_path):
    e = Env(tmp_path, chatlive)
    codex = tmp_path / "codex"
    codex.symlink_to(os.path.realpath(shutil.which("python3") or sys.executable))
    # the "codex" process runs the script as its child; the script's parent is then that process
    code = ("import subprocess,sys,os;"
            "p=subprocess.run([sys.executable,sys.argv[1],'start','--agent','codex'],stdin=sys.stdin);"
            "sys.exit(p.returncode)")
    h = hook(tmp_path, sid="cx-1")
    proc = subprocess.Popen([str(codex), "-c", code, str(SCRIPT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=e.env())
    out, err = proc.communicate(json.dumps(h), timeout=20)
    assert proc.returncode == 0 and err == "", err
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"]
    row = chatlive.service.db.one("SELECT kind, pid FROM chat_members")
    assert row["kind"] == "codex" and row["pid"] == proc.pid


def test_skip_claude_unattended(chatlive, tmp_path):
    e = Env(tmp_path, chatlive)
    p = e.run("start", "claude", hook(tmp_path), CLAUDE_CODE_SESSION_ATTENDED="0")
    assert p.returncode == 0 and p.stdout == "" and live_count(chatlive) == 0
    assert not e.state.exists()


def test_skip_codex_exec(chatlive, tmp_path):
    e = Env(tmp_path, chatlive)
    tp = tmp_path / "rollout.jsonl"
    tp.write_text(json.dumps({"payload": {"originator": "codex_exec", "source": "exec"}}) + "\n")
    p = e.run("start", "codex", hook(tmp_path, transcript_path=str(tp)))
    assert p.returncode == 0 and p.stdout == "" and live_count(chatlive) == 0


def test_token_never_printed(chatlive, tmp_path):
    e = Env(tmp_path, chatlive)
    h = hook(tmp_path)
    outs = [e.run("start", "claude", h), e.run("end", "claude", h)]
    e.tf.write_text("wrong-token-xyz\n")
    outs += [e.run("start", "claude", h, ), ]
    assert "wrong-token-xyz" not in outs[-1].stdout + outs[-1].stderr and outs[-1].stderr
    for p in outs[:2]:
        assert CHAT_TOKENS["hosta"] not in p.stdout + p.stderr
