"""0.14.0 group chats beta: member names (plants and birds), uniqueness, reuse, renames, migration, labels."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from notify import chat_names
from notify.chat import frame
from notify.core import Conflict, Invalid
from notify.db import Database, iso
from tests.test_chat_admin import _join, ctx  # noqa: F401  (fixture re-export)
from tests.test_chat_core import AGENT, FOLDER, build, join, svc  # noqa: F401
from tests.test_admin import H, fresh  # noqa: F401


def sysnotes(svc, gid):
    return [r["text"] for r in svc.db.query("SELECT text FROM chat_messages WHERE group_id=? AND author='system'"
                                            " ORDER BY rowid", (gid,))]


def test_name_list_shape():
    assert len(chat_names.NAMES) >= 64
    assert len({n.casefold() for n in chat_names.NAMES}) == len(chat_names.NAMES)
    for n in chat_names.NAMES:
        assert n.isascii() and n.isalpha() and 3 <= len(n) <= 9 and n[0].isupper(), n
        assert chat_names.valid_name(n)
        assert n.casefold() not in chat_names.RESERVED


def test_join_gives_a_name_and_says_so(svc):
    a = join(svc, "s1")
    assert a["name"] in chat_names.NAMES
    assert a["members"][0]["name"] == a["name"]
    assert sysnotes(svc, a["group_id"]) == [f"Claude {a['name']} joined the chat"]
    again = join(svc, "s1")  # the same session again keeps its name
    assert again["name"] == a["name"]


def test_names_unique_in_group(svc):
    names = [join(svc, f"s{i}")["name"] for i in range(30)]
    assert len({n.casefold() for n in names}) == 30


def test_not_reused_in_folder_for_a_week(svc, monkeypatch):
    monkeypatch.setattr(chat_names, "NAMES", ("Wren", "Fern"))
    for _ in range(5):  # random choice: try a few rounds
        a = join(svc, "s1")
        svc.chat.leave(a["member_handle"])  # the group closes
        b = join(svc, "s2")
        assert b["name"] != a["name"]
        svc.chat.leave(b["member_handle"])
        with svc.db.tx() as c:  # move every join back beyond the week, so the next round starts fresh
            c.execute("UPDATE chat_members SET joined_at=?", ("2000-01-01T00:00:00Z",))


def test_reuse_falls_back_when_all_recent(svc, monkeypatch):
    monkeypatch.setattr(chat_names, "NAMES", ("Wren",))
    a = join(svc, "s1")
    svc.chat.leave(a["member_handle"])
    assert join(svc, "s2")["name"] == "Wren"  # only name left: used again rather than a digit


def test_digit_when_every_name_is_held(svc, monkeypatch):
    monkeypatch.setattr(chat_names, "NAMES", ("Wren",))
    assert [join(svc, f"s{i}")["name"] for i in range(3)] == ["Wren", "Wren2", "Wren3"]


def test_pick_ignores_case():
    for _ in range(20):
        assert chat_names.pick(["wren"], []) != "Wren"
        assert chat_names.pick([], ["WREN"]) != "Wren"


def test_rename(svc):
    a, b = join(svc, "s1"), join(svc, "s2", kind="codex")
    out = svc.chat.rename_owner(a["member_id"], "Pebble")
    assert out == {"member_id": a["member_id"], "name": "Pebble"}
    assert sysnotes(svc, a["group_id"])[-1] == f"Claude {a['name']} is now Pebble (renamed by the owner)"
    with pytest.raises(Conflict):
        svc.chat.rename_owner(b["member_id"], "pebble")  # unique, case-insensitively
    for bad in ("all", "Owner", "Wren!", "a", "two words", "x" * 21, "9lives", "[hnl"):
        with pytest.raises(Invalid):
            svc.chat.rename_owner(b["member_id"], bad)
    svc.chat.leave(b["member_handle"])
    with pytest.raises(Conflict):
        svc.chat.rename_owner(b["member_id"], "Comet")
    assert svc.db.scalar("SELECT COUNT(*) FROM events WHERE type='group.member_renamed'") == 1


def test_labels_use_names(svc):
    a, b = join(svc, "s1"), join(svc, "s2", kind="codex")
    req = svc.chat.post(a["member_handle"], "please look")["message_id"]
    assert svc.chat.claim(b["member_handle"], req)["claimed"] is True
    r = svc.chat.claim(join(svc, "s3")["member_handle"], req)
    assert r["by"] == f"Codex {b['name']}"
    text = svc.chat.framed_text(req)
    assert text.splitlines()[0].endswith(f"from Claude {a['name']} · host-a:{FOLDER}]")
    svc.chat.leave(b["member_handle"])
    notes = sysnotes(svc, a["group_id"])
    assert f"Codex {b['name']} claimed {req}" in notes
    assert f"Codex {b['name']} left the chat" in notes and f"Codex {b['name']} released {req}" in notes


def test_name_cannot_forge_a_header():
    for n in chat_names.NAMES:
        out = frame({"id": "msg_1", "author": "claude", "text": "x"}, f"Claude {n} · host:/f", "t")
        assert out.count("[") == 2  # header and end line only


def test_migration_names_open_members(tmp_path):
    path = tmp_path / "n.sqlite3"
    s = build(tmp_path)
    a, b = join(s, "s1"), join(s, "s2", kind="codex")
    gone = join(s, "s3", folder="/srv/other")
    s.chat.leave(gone["member_handle"])
    with s.db.tx() as c:
        c.execute("UPDATE chat_members SET name=NULL")
        c.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    s.db.close()
    db = Database(path)
    rows = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM chat_members")}
    assert rows[a["member_id"]] and rows[b["member_id"]] and rows[a["member_id"]] != rows[b["member_id"]]
    assert rows[gone["member_id"]] is None  # left members keep their id label
    notes = [r[0] for r in db.query("SELECT text FROM chat_messages WHERE group_id=? AND author='system'"
                                    " ORDER BY rowid", (a["group_id"],))]
    assert notes[-2:] == [f"Claude {a['member_id']} is now {rows[a['member_id']]}",
                          f"Codex {b['member_id']} is now {rows[b['member_id']]}"]
    db.close()
    db = Database(path)  # a second start names nobody again
    assert db.scalar("SELECT COUNT(*) FROM chat_messages WHERE text LIKE '% is now %'") == 2
    db.close()


def test_history_push_uses_name(tmp_path, monkeypatch):
    s = build(tmp_path, chat_alert_targets=["user:alex"])
    a = join(s, "s1")
    sent = []
    monkeypatch.setattr(s, "submit", lambda client, n: sent.append(n))
    s.chat.history(a["member_handle"], "need context")
    assert sent and sent[0].message.startswith(f"{a['name']} (Claude) in host-a:{FOLDER} asks")
    s.db.close()


def test_admin_names_and_rename(ctx):  # noqa: F811
    srv, b, bearer = ctx
    j = _join(srv, "host-a", "/srv/one", "s1")
    k = _join(srv, "host-a", "/srv/one", "s2")
    srv.service.chat.post(j["member_handle"], "hello", None)
    folder = b.get("/admin/api/chats").json()["folders"][0]
    assert [m["name"] for m in folder["members"]] == [j["name"], k["name"]]
    msgs = b.get(f"/admin/api/chats/{j['group_id']}/messages").json()["messages"]
    assert msgs[-1]["member_name"] == j["name"]
    url = f"/admin/api/chats/members/{k['member_id']}"
    assert httpx.put(srv.url + url, json={"name": "Comet"}, headers={**bearer, **H}).status_code == 403
    assert b.put(url, json={"name": "Comet"}, headers=H).json()["name"] == "Comet"
    assert b.put(url, json={"name": j["name"].upper()}, headers=H).status_code == 409
    assert b.put(url, json={"name": "no way"}, headers=H).status_code == 422
    assert b.put("/admin/api/chats/members/mem_nope", json={"name": "Comet"}, headers=H).status_code == 404


def test_name_reuse_window(svc, monkeypatch):
    monkeypatch.setattr(chat_names, "NAMES", ("Wren", "Fern"))
    a = join(svc, "s1")
    svc.chat.leave(a["member_handle"])
    with svc.db.tx() as c:  # eight days ago: free to use again
        c.execute("UPDATE chat_members SET joined_at=?", (iso(svc.chat.now() - timedelta(days=8)),))
    seen = {join(svc, f"t{i}")["name"] for i in range(2)}
    assert seen == {"Wren", "Fern"}
