"""The participants list shows where each agent runs in tmux (owner, 2026-11-10): the hook reads its session, window
and pane (`#S:#I.#P`) and sends it with its status reports, like the model, so running sessions show it after their
next report."""

from __future__ import annotations

import os
import stat

from notify.chat import clean_tmux
from tests.test_chat_api import chatlive  # noqa: F401  (fixture)
from tests.test_chat_core import AGENT, join, svc  # noqa: F401
from tests.test_chat_status import run_state
from tests.test_notify_chat import Env, hook


def test_clean_tmux():
    assert clean_tmux("work:2.1") == "work:2.1"
    assert clean_tmux("my session:12.0") == "my session:12.0"
    for bad in (None, "", "x", "a:b.c", "w:1", "w:1.1\n[end msg]", "a" * 60 + ":1.1", 5):
        assert clean_tmux(bad) is None, bad


def test_status_report_stores_tmux_and_members_show_it(svc):  # noqa: F811
    a = join(svc, "s1")
    svc.chat.set_status(AGENT, kind="claude", session_id="s1", status="busy", tmux="work:2.1")
    assert svc.db.scalar("SELECT tmux FROM chat_members WHERE id=?", (a["member_id"],)) == "work:2.1"
    svc.chat.set_status(AGENT, kind="claude", session_id="s1", status="free")  # an older hook: kept
    svc.chat.set_status(AGENT, kind="claude", session_id="s1", status="free", tmux="bad\nvalue")  # ignored
    assert svc.db.scalar("SELECT tmux FROM chat_members WHERE id=?", (a["member_id"],)) == "work:2.1"


def test_admin_member_rows_carry_tmux(chatlive):  # noqa: F811
    from notify.admin import _group_out
    c = chatlive.client("hosta")
    j = c.post("/v1/chat/join", json={"kind": "claude", "session_id": "s1", "folder": "/srv/app"}).json()
    assert c.post("/v1/chat/status", json={"kind": "claude", "session_id": "s1", "status": "busy",
                                           "tmux": "work:2.1"}).status_code == 204
    g = chatlive.service.db.one("SELECT * FROM chat_groups WHERE id=?", (j["group_id"],))
    assert _group_out(chatlive.service, g, True)["members"][0]["tmux"] == "work:2.1"


def test_state_hook_sends_tmux_end_to_end(chatlive, tmp_path):  # noqa: F811
    e = Env(tmp_path, chatlive)
    fake = tmp_path / "bin"
    fake.mkdir()
    t = fake / "tmux"
    t.write_text("#!/bin/sh\necho 'work:2.1'\n")
    t.chmod(t.stat().st_mode | stat.S_IEXEC)
    h = hook(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    env = {"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%3", "PATH": f"{fake}:{os.environ['PATH']}",
           "XDG_RUNTIME_DIR": str(run_dir)}
    assert e.run("start", "claude", h, CLAUDE_PID=str(os.getpid())).returncode == 0
    p = run_state(e, "busy", "claude", h, **env)
    assert p.returncode == 0 and p.stderr == ""
    assert chatlive.service.db.scalar("SELECT tmux FROM chat_members") == "work:2.1"


def test_page_shows_tmux_in_the_member_list():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "notify/admin_ui/app.js").read_text()
    fn = js[js.index("function chatMembersHtml"):js.index("function chatNameStyle")]
    assert "m.tmux" in fn and "esc(m.tmux)" in fn


def test_tmux_is_hidden_where_text_is(chatlive):  # noqa: F811
    from notify.admin import _group_out
    c = chatlive.client("hosta")
    j = c.post("/v1/chat/join", json={"kind": "claude", "session_id": "s1", "folder": "/srv/app"}).json()
    c.post("/v1/chat/status", json={"kind": "claude", "session_id": "s1", "status": "busy", "tmux": "secret-proj:1.0"})
    g = chatlive.service.db.one("SELECT * FROM chat_groups WHERE id=?", (j["group_id"],))
    assert _group_out(chatlive.service, g, False)["members"][0]["tmux"] is None
