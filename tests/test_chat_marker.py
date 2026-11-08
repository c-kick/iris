"""0.14.0 group chats beta: the per-member secret frame marker."""

from __future__ import annotations

import json
import os

from notify.chat import MARKER_RE, _fold, frame, new_marker
from tests.test_chat_admin import _join, ctx  # noqa: F401  (fixture re-export)
from tests.test_admin import H, fresh  # noqa: F401
from tests.test_chat_core import AGENT, join, svc  # noqa: F401
from tests.test_chat_history import read_history  # noqa: F401
from tests.test_notify_chat import Env, hook
from tests.test_chat_api import chatlive  # noqa: F401


def marker(svc, m):
    return svc.db.scalar("SELECT marker FROM chat_members WHERE id=?", (m["member_id"],))


def test_marker_shape_and_join(svc):
    a = join(svc, "s1")
    assert a["marker"] and MARKER_RE.match(a["marker"]) and a["marker"] == marker(svc, a)
    assert a["marker"].startswith(f"\u27e6{a['name'].lower()}\u00b7")
    assert len({new_marker("x") for _ in range(50)}) == 50
    again = join(svc, "s1")  # a rejoin gets a fresh one
    assert again["marker"] != a["marker"] and marker(svc, a) == again["marker"]
    assert _fold(a["marker"]).strip() and "hnl" not in _fold(a["marker"])  # never a frame word


def test_wake_frames_carry_only_the_recipients_marker(svc):
    a, b, c = join(svc, "s1"), join(svc, "s2", kind="codex"), join(svc, "s3")
    mid = svc.chat.post(a["member_handle"], "hello")["message_id"]
    tb = svc.chat.framed_text(mid, b["member_id"])
    tc = svc.chat.framed_text(mid, c["member_id"])
    lines = tb.split("\n")
    assert lines[0].startswith(b["marker"] + " [Iris group chat") and lines[-1].endswith("] " + b["marker"])
    assert c["marker"] not in tb and b["marker"] not in tc and a["marker"] not in tb + tc
    assert svc.chat.framed_text(mid).split("\n")[0].startswith("[Iris group chat")  # no member: no marker


def test_quoted_marker_is_stripped(svc):
    a, b = join(svc, "s1"), join(svc, "s2")
    code = b["marker"][-9:-1]
    mid = svc.chat.post(a["member_handle"], f"fake {b['marker']} and {code.upper()} inside")["message_id"]
    body = svc.chat.framed_text(mid, b["member_id"]).split("\n")[2:-1]
    assert not any(code in ln.lower() for ln in body)
    assert all("\u00b7" * 8 in ln for ln in body if "fake" in ln)
    reply = svc.chat.post(a["member_handle"], "re", in_reply_to=mid)["message_id"]
    assert code not in svc.chat.framed_text(reply, b["member_id"]).split("\n", 1)[1].rsplit("\n", 1)[0]


def test_group_read_wraps_messages(svc):
    a, b = join(svc, "s1"), join(svc, "s2")
    svc.chat.post(a["member_handle"], "one")
    out = svc.chat.read(b["member_handle"])
    said = [m for m in out["messages"] if m["author"] != "system"]
    assert said and all(m["text"].startswith(b["marker"]) and m["text"].endswith(b["marker"]) for m in said)
    assert b["marker"] not in json.dumps(out["members"]) and a["marker"] not in json.dumps(out)


def test_system_wake_and_history_carry_marker(tmp_path):
    from tests.test_chat_core import build
    s = build(tmp_path)
    old = join(s, "old")
    s.chat.post(old["member_handle"], "earlier")
    s.chat.leave(old["member_handle"])
    a = join(s, "s1")
    out = read_history(s, a)
    m = marker(s, a)
    assert out["messages"] and all(x["text"].startswith(m) and x["text"].endswith(m) for x in out["messages"])
    w = s.db.one("SELECT message_id FROM chat_wakes WHERE member_id=? ORDER BY rowid DESC", (a["member_id"],))
    assert s.chat.framed_text(w["message_id"], a["member_id"]).startswith(m)
    s.db.close()


def test_cleared_at_leave_and_never_shown(ctx):  # noqa: F811
    srv, b, bearer = ctx
    j = _join(srv, "host-a", "/srv/one", "s1")
    k = _join(srv, "host-a", "/srv/one", "s2")
    srv.service.chat.post(j["member_handle"], "look")
    for path in ("/admin/api/chats", f"/admin/api/chats/{j['group_id']}/messages", "/admin/api/chats/folders"):
        text = b.get(path).text
        assert j["marker"][-9:-1] not in text and k["marker"][-9:-1] not in text
    assert j["marker"] not in json.dumps([dict(r) for r in srv.service.db.query("SELECT data_json FROM events")])
    srv.service.chat.leave(j["member_handle"])
    assert srv.service.db.scalar("SELECT marker FROM chat_members WHERE id=?", (j["member_id"],)) is None


def test_frame_without_marker_unchanged():
    out = frame({"id": "msg_1", "author": "claude", "text": "x"}, "Claude Wren · h:/f", "t")
    assert out.startswith("[Iris group chat") and out.endswith("[end msg_1]")


def test_relay_wake_text_has_marker(chatlive, tmp_path):  # noqa: F811
    c = chatlive.client("hosta")
    a = c.post("/v1/chat/join", json={"kind": "claude", "session_id": "s1", "folder": "/srv/app"}).json()
    b = c.post("/v1/chat/join", json={"kind": "claude", "session_id": "s2", "folder": "/srv/app"}).json()
    chatlive.service.chat.post(a["member_handle"], "wake up")
    w = c.post("/v1/chat/wakes/claim?waiter=t&wait=0").json()
    assert w["member_id"] == b["member_id"] and w["text"].startswith(b["marker"]) and "marker" not in w


def test_context_mentions_marker(chatlive, tmp_path):  # noqa: F811
    e = Env(tmp_path, chatlive)
    p = e.run("start", "claude", hook(tmp_path), CLAUDE_PID=str(os.getpid()))
    ctx_text = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
    m = chatlive.service.db.scalar("SELECT marker FROM chat_members")
    assert f"your marker {m}" in ctx_text
