"""0.14.0 group chats beta: member status (busy / waiting / free) from the session hooks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta

import pytest

from notify.chat import BUSY_QUIET_SECONDS, READ_RULES, status_view, status_words
from notify.core import Invalid, NotFound
from notify.db import iso, parse_iso
from tests.test_chat_api import chatlive  # noqa: F401  (fixture)
from tests.test_chat_core import AGENT, OTHER_HOST, join, svc  # noqa: F401
from tests.test_notify_chat import SCRIPT, Env, hook, nc


def st(svc, sid, status, kind="claude", client=AGENT):
    svc.chat.set_status(client, kind=kind, session_id=sid, status=status)


def row(svc, member_id):
    return svc.db.one("SELECT status, status_at, active_at FROM chat_members WHERE id=?", (member_id,))


def test_status_roundtrip_and_read_members(svc):
    a, b = join(svc, "s1"), join(svc, "s2", kind="codex")
    members = {m["member_id"]: m for m in svc.chat.read(a["member_handle"])["members"]}
    assert members[b["member_id"]]["status"] == "unknown" and members[b["member_id"]]["since"] is None
    st(svc, "s2", "busy", kind="codex")
    members = {m["member_id"]: m for m in svc.chat.read(a["member_handle"])["members"]}
    assert members[b["member_id"]]["status"] == "busy" and members[b["member_id"]]["since"]
    assert members[b["member_id"]]["name"] == b["name"]
    st(svc, "s2", "waiting", kind="codex")
    assert svc.chat.read(a["member_handle"])["members"][1]["status"] == "waiting for its owner"
    st(svc, "s2", "free", kind="codex")
    assert svc.chat.read(a["member_handle"])["members"][1]["status"] == "free"
    assert "free member" in READ_RULES and "waiting for its owner" in READ_RULES


def test_active_only_stamps(svc):
    a = join(svc, "s1")
    st(svc, "s1", "busy")
    before = row(svc, a["member_id"])
    svc.chat.now = lambda: parse_iso(before["status_at"]) + timedelta(seconds=10)
    st(svc, "s1", "active")
    after = row(svc, a["member_id"])
    assert after["status"] == "busy" and after["status_at"] == before["status_at"]
    assert after["active_at"] > before["active_at"]


def test_busy_question_mark_for_claude_only(svc):
    join(svc, "s1"), join(svc, "s2", kind="codex")
    st(svc, "s1", "busy")
    st(svc, "s2", "busy", kind="codex")
    t0 = svc.chat.now()
    svc.chat.now = lambda: t0 + timedelta(seconds=BUSY_QUIET_SECONDS + 130)
    other = join(svc, "s3")
    ms = {m["kind"]: m for m in svc.chat.read(other["member_handle"])["members"] if m["member_id"] != other["member_id"]}
    assert ms["claude"]["status"] == "busy (no activity for 7 min)"
    assert ms["codex"]["status"] == "busy"  # Codex reports Esc (Interrupt), so its busy is never a guess
    st(svc, "s1", "active")  # activity again: plain busy
    ms = {m["kind"]: m for m in svc.chat.read(other["member_handle"])["members"] if m["member_id"] != other["member_id"]}
    assert ms["claude"]["status"] == "busy"


def test_status_view_unknown_and_words():
    now = parse_iso("2026-11-08T12:00:00Z")
    assert status_view("claude", None, None, None, now) == {"status": "unknown", "since": None}
    v = status_view("claude", "busy", "2026-11-08T11:50:00Z", None, now)
    assert v["status"] == "busy?" and status_words(v) == "busy (no activity for 10 min)"


def test_status_errors(svc):
    join(svc, "s1")
    with pytest.raises(Invalid):
        st(svc, "s1", "sleeping")
    with pytest.raises(NotFound):
        st(svc, "nobody", "busy")
    with pytest.raises(NotFound):
        st(svc, "s1", "busy", kind="codex")  # kind is part of the key
    with pytest.raises(NotFound):
        st(svc, "s1", "busy", client=OTHER_HOST)  # another host's session is not found


def test_no_message_wake_or_event(svc):
    a, _ = join(svc, "s1"), join(svc, "s2")
    counts = [svc.db.scalar(f"SELECT COUNT(*) FROM {t}") for t in ("chat_messages", "chat_wakes", "events")]
    for s in ("busy", "active", "waiting", "free"):
        st(svc, "s1", s)
    assert [svc.db.scalar(f"SELECT COUNT(*) FROM {t}") for t in ("chat_messages", "chat_wakes", "events")] == counts


def test_rest_status(chatlive):  # noqa: F811
    c = chatlive.client("hosta")
    j = c.post("/v1/chat/join", json={"kind": "claude", "session_id": "s1", "folder": "/srv/app"}).json()
    assert c.post("/v1/chat/status", json={"kind": "claude", "session_id": "s1", "status": "busy"}).status_code == 204
    assert chatlive.service.db.scalar("SELECT status FROM chat_members WHERE id=?", (j["member_id"],)) == "busy"
    assert c.post("/v1/chat/status", json={"kind": "claude", "session_id": "zz", "status": "busy"}).status_code == 404
    assert c.post("/v1/chat/status", json={"kind": "claude", "session_id": "s1", "status": "x"}).status_code == 422
    assert chatlive.client("hostb").post(
        "/v1/chat/status", json={"kind": "claude", "session_id": "s1", "status": "free"}).status_code == 404


def run_state(e, value, agent, h, **extra):
    return subprocess.run([sys.executable, str(SCRIPT), "state", value, "--agent", agent], input=json.dumps(h),
                          capture_output=True, text=True, env=e.env(**extra), timeout=20)


def test_notify_chat_state_end_to_end(chatlive, tmp_path):  # noqa: F811
    e = Env(tmp_path, chatlive)
    h = hook(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert run_state(e, "busy", "claude", h).returncode == 0  # not joined: no request, no error
    assert e.run("start", "claude", h, CLAUDE_PID=str(os.getpid())).returncode == 0
    db = chatlive.service.db
    p = run_state(e, "busy", "claude", h, XDG_RUNTIME_DIR=str(run_dir))
    assert p.returncode == 0 and p.stderr == "" and p.stdout == ""
    assert db.scalar("SELECT status FROM chat_members") == "busy"
    note = {**h, "hook_event_name": "Notification", "notification_type": "permission_prompt"}
    run_state(e, "notification", "claude", note, XDG_RUNTIME_DIR=str(run_dir))
    assert db.scalar("SELECT status FROM chat_members") == "waiting"
    run_state(e, "notification", "claude", {**note, "notification_type": "idle_prompt"}, XDG_RUNTIME_DIR=str(run_dir))
    assert db.scalar("SELECT status FROM chat_members") == "free"
    run_state(e, "notification", "claude", {**note, "notification_type": "auth_success"}, XDG_RUNTIME_DIR=str(run_dir))
    assert db.scalar("SELECT status FROM chat_members") == "free"  # ignored
    assert run_state(e, "active", "claude", h, XDG_RUNTIME_DIR=str(run_dir)).returncode == 0
    first = db.scalar("SELECT active_at FROM chat_members")
    run_state(e, "active", "claude", h, XDG_RUNTIME_DIR=str(run_dir))  # within 30 s: not sent
    assert db.scalar("SELECT active_at FROM chat_members") == first
    assert any(p.name.startswith("active-") for p in (run_dir / "hnl-notify").iterdir())
    e.url = "http://127.0.0.1:9"
    p = run_state(e, "free", "claude", h, XDG_RUNTIME_DIR=str(run_dir))
    assert p.returncode == 0 and "notify-chat" in p.stderr  # unreachable: one line, never a broken session


def test_notification_mapping():
    assert nc.notification_state({"notification_type": "permission_prompt"}) == "waiting"
    assert nc.notification_state({"notification_type": "elicitation_dialog"}) == "waiting"
    assert nc.notification_state({"notification_type": "idle_prompt"}) == "free"
    assert nc.notification_state({"notification_type": "auth_success"}) is None
    assert nc.notification_state({"message": "Claude needs your permission to use Bash"}) == "waiting"
    assert nc.notification_state({"message": "Claude is waiting for your input"}) == "free"


def test_active_throttle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert nc.active_due("claude", "s1", now=1000.0) is True
    assert nc.active_due("claude", "s1", now=1010.0) is False
    assert nc.active_due("claude", "s2", now=1010.0) is True
    assert nc.active_due("claude", "s1", now=1031.0) is True


def test_admin_members_show_status(svc):
    from notify.admin import _group_out
    a = join(svc, "s1")
    st(svc, "s1", "waiting")
    g = svc.db.one("SELECT * FROM chat_groups WHERE id=?", (a["group_id"],))
    m = _group_out(svc, g, True)["members"][0]
    assert m["status"] == "waiting" and m["status_since"] and m["name"] == a["name"]
    assert iso(parse_iso(m["status_since"]))
