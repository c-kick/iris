"""Group chats: which model a member runs (reported by the session's hook from its own transcript)."""

from __future__ import annotations

import json
import os

import pytest

from notify.chat import clean_model, model_label
from tests.test_admin import H, fresh, setup_admin  # noqa: F401  (fixture re-export)
from tests.test_chat_api import chatlive  # noqa: F401  (fixture)
from tests.test_chat_core import AGENT, join, svc  # noqa: F401
from tests.test_chat_status import run_state
from tests.test_notify_chat import Env, hook, load


def st(svc, sid, status, model=None, kind="claude"):
    svc.chat.set_status(AGENT, kind=kind, session_id=sid, status=status, model=model)


@pytest.mark.parametrize("raw, label", [
    ("claude-fable-5-2", "Fable 5.2"),
    ("claude-opus-5-5", "Opus 5.5"),
    ("claude-opus-5-5[1m]", "Opus 5.5"),
    ("claude-haiku-5-5-20260301", "Haiku 5.5"),
    ("claude-3-5-sonnet-20241022", "Sonnet 3.5"),
    ("gpt-6.1-sol", "Sol 6.1"),
    ("gpt-6.1-terra-mini", "Terra Mini 6.1"),
    ("gpt-5", "gpt-5"),  # no name to show: the id as it is
    ("o3", "o3"),
    ("gpt-5[1m]", "gpt-5"),  # brackets never reach a reader, also when the id has no name in it
    ("0[x]", "0"),
    ("astra", "Astra"),
])
def test_model_label(raw, label):
    assert model_label(raw) == label


def test_model_label_of_nothing():
    assert model_label(None) is None and model_label("") is None


@pytest.mark.parametrize("bad", ["", " ", "x" * 81, "has space", "<script>", "a\nb", "<synthetic>", 5, None, "|pipe"])
def test_clean_model_refuses(bad):
    assert clean_model(bad) is None


def test_clean_model_accepts_ids():
    for ok in ("claude-fable-5-2", "gpt-6.1-sol", "claude-opus-5-5[1m]", "vendor/model:tag", "o3"):
        assert clean_model(ok) == ok


def test_status_report_stores_the_model_and_members_show_it(svc):  # noqa: F811
    a, b = join(svc, "s1"), join(svc, "s2", kind="codex")
    st(svc, "s1", "busy", "claude-fable-5-2")
    st(svc, "s2", "free", "gpt-6.1-sol", kind="codex")
    assert svc.db.scalar("SELECT model FROM chat_members WHERE id=?", (a["member_id"],)) == "claude-fable-5-2"
    members = {m["member_id"]: m for m in svc.chat.read(b["member_handle"])["members"]}
    assert members[a["member_id"]]["model"] == "Fable 5.2" and members[b["member_id"]]["model"] == "Sol 6.1"
    st(svc, "s1", "active", "claude-opus-5-5")  # switched with /model: the next report carries the new one
    assert svc.db.scalar("SELECT model FROM chat_members WHERE id=?", (a["member_id"],)) == "claude-opus-5-5"
    st(svc, "s1", "free")  # a report without a model (an older hook script) keeps what is known
    st(svc, "s1", "free", "not a model id")  # nonsense is ignored, never stored
    assert svc.db.scalar("SELECT model FROM chat_members WHERE id=?", (a["member_id"],)) == "claude-opus-5-5"


def test_a_member_without_a_report_has_no_model(svc):  # noqa: F811
    a, b = join(svc, "s1"), join(svc, "s2")
    assert [m["model"] for m in svc.chat.read(b["member_handle"])["members"]] == [None, None]
    assert a["member_id"]


def test_a_message_keeps_the_model_it_was_written_with(svc):  # noqa: F811
    a = join(svc, "s1")
    st(svc, "s1", "busy", "claude-fable-5-2")
    first = svc.chat.post(a["member_handle"], "one")["message_id"]
    st(svc, "s1", "busy", "claude-opus-5-5")
    second = svc.chat.post(a["member_handle"], "two")["message_id"]
    owner = svc.chat.post_owner(a["group_id"], "mine")["message_id"]
    got = {r["id"]: r["model"] for r in svc.db.query("SELECT id, model FROM chat_messages")}
    assert (got[first], got[second], got[owner]) == ("claude-fable-5-2", "claude-opus-5-5", None)


def test_admin_shows_model_on_members_and_messages(chatlive):  # noqa: F811
    from notify.admin import _group_out
    s = chatlive.service
    c = chatlive.client("hosta")
    j = c.post("/v1/chat/join", json={"kind": "claude", "session_id": "s1", "folder": "/srv/app"}).json()
    assert c.post("/v1/chat/status", json={"kind": "claude", "session_id": "s1", "status": "busy",
                                           "model": "claude-fable-5-2"}).status_code == 204
    g = s.db.one("SELECT * FROM chat_groups WHERE id=?", (j["group_id"],))
    m = _group_out(s, g, True)["members"][0]
    assert (m["model"], m["model_id"]) == ("Fable 5.2", "claude-fable-5-2")
    assert c.post("/v1/chat/status", json={"kind": "claude", "session_id": "s1", "status": "busy",
                                           "model": "x" * 300}).status_code == 422  # absurdly long: refused
    assert c.post("/v1/chat/status", json={"kind": "claude", "session_id": "s1", "status": "busy",
                                           "model": "bad model"}).status_code == 204  # odd: ignored, status still set


def test_admin_message_list_carries_the_authors_model(fresh):  # noqa: F811
    from notify.core import Client
    from notify.models import Scope
    srv = fresh()
    b = setup_admin(srv)
    b.post("/admin/api/clients/agent-a", json={"scopes": ["chat"], "chat_host": "host-a"}, headers=H)
    agent = Client("agent-a", frozenset({Scope.chat}))
    chat = srv.service.chat
    j = chat.join(agent, kind="claude", session_id="s1", folder="/srv/app", pid=None, pid_start=None)
    chat.post(j["member_handle"], "before any report")
    chat.set_status(agent, kind="claude", session_id="s1", status="busy", model="claude-fable-5-2")
    chat.post(j["member_handle"], "with fable")
    msgs = [m for m in b.get(f"/admin/api/chats/{j['group_id']}/messages").json()["messages"] if m["author"] == "claude"]
    assert [m.get("member_model") for m in msgs] == [None, "Fable 5.2"]


# ------------------------------------------------------------------ the hook script
def _transcript(path, lines):
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return str(path)


def test_script_reads_the_newest_model_from_a_claude_transcript(tmp_path):
    nc = load()
    t = _transcript(tmp_path / "t.jsonl", [
        {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": []}},
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"model": "claude-fable-5-2", "content": []}},
        {"type": "assistant", "isSidechain": True, "message": {"model": "claude-haiku-5-5", "content": []}},
        {"type": "assistant", "message": {"model": "<synthetic>", "content": []}},
    ])
    assert nc.session_model({"transcript_path": t}) == "claude-fable-5-2"


def test_script_reads_the_model_from_a_codex_rollout(tmp_path):
    nc = load()
    t = _transcript(tmp_path / "r.jsonl", [
        {"type": "session_meta", "payload": {"model_provider": "openai"}},
        {"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "cwd": "/x"}},
        {"type": "response_item", "payload": {"type": "message"}},
    ])
    assert nc.session_model({"transcript_path": t}) == "gpt-6.1-sol"


def test_script_model_edge_cases(tmp_path):
    nc = load()
    assert nc.session_model({}) is None
    assert nc.session_model({"transcript_path": str(tmp_path / "missing.jsonl")}) is None
    assert nc.session_model({"transcript_path": _transcript(tmp_path / "e.jsonl", [{"type": "user"}])}) is None
    # the hook input names it itself (a session that has not answered yet)
    assert nc.session_model({"model": "claude-fable-5-2", "transcript_path": str(tmp_path / "missing.jsonl")}) == "claude-fable-5-2"
    # what a user typed or a tool printed is not the session's model: only a line's own model field counts
    t = _transcript(tmp_path / "u.jsonl", [
        {"type": "assistant", "message": {"model": "claude-fable-5-2", "content": []}},
        {"type": "user", "message": {"content": 'look: "model":"evil-model" and {"model": "other"}'}},
        {"type": "user", "toolUseResult": {"model": "tool-said-so"}, "message": {"content": [{"type": "tool_result", "content": "x"}]}},
    ])
    assert nc.session_model({"transcript_path": t}) == "claude-fable-5-2"
    big = tmp_path / "big.jsonl"  # only the tail is read: an old model far up the file is not found, and that is fine
    with big.open("w") as f:
        f.write(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5"}}) + "\n")
        for _ in range(3000):
            f.write(json.dumps({"type": "user", "message": {"content": "x" * 200}}) + "\n")
    assert nc.session_model({"transcript_path": str(big)}) is None


def test_state_hook_sends_the_model_end_to_end(chatlive, tmp_path):  # noqa: F811
    e = Env(tmp_path, chatlive)
    t = _transcript(tmp_path / "t.jsonl", [{"type": "assistant", "message": {"model": "claude-fable-5-2"}}])
    h = {**hook(tmp_path), "transcript_path": t}
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert e.run("start", "claude", h, CLAUDE_PID=str(os.getpid())).returncode == 0
    p = run_state(e, "busy", "claude", h, XDG_RUNTIME_DIR=str(run_dir))
    assert p.returncode == 0 and p.stderr == ""
    assert chatlive.service.db.scalar("SELECT model FROM chat_members") == "claude-fable-5-2"


def test_state_hook_still_works_against_a_server_that_does_not_know_model(tmp_path, monkeypatch):
    """A host's script can be newer than the server: a 422 for the unknown field is retried without it."""
    nc = load()
    sent = []

    def fake_post(base, token, path, body, timeout, handle=None):
        sent.append(dict(body))
        return (422 if "model" in body else 204), None
    monkeypatch.setattr(nc, "post", fake_post)
    assert nc.post_status("http://x", "tok", {"kind": "claude", "session_id": "s", "status": "busy"}, "claude-fable-5-2") == 204
    assert [("model" in b) for b in sent] == [True, False]


# ------------------------------------------------------------------ the page
def test_page_shows_the_model_next_to_the_agent_kind():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "notify/admin_ui/app.js").read_text()
    assert 'const chatKindModel = (kind, model) => (CHAT_WHO[kind] || kind) + (model ? ` \\u00b7 ${model}` : "");' in js
    assert "chatAuthorHtml(m.author, m.member_name, m.member_model)" in js  # on a message: the model it was written with
    assert "${esc(chatKindModel(m.kind, m.model))}</small>" in js  # in the member list: the model it runs now
    assert "esc(chatKindModel(author, model))" in js  # escaped: the id comes from a host's hook
