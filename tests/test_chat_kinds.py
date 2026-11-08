"""0.14.0 group chats beta: @mentions, heads-up (FYI) and @all messages, and who may claim them."""

from __future__ import annotations

import pytest

from notify.chat import FYI_WITH_MENTIONS, find_mentions, kind_line
from notify.core import Invalid
from tests.test_chat_admin import _join, ctx  # noqa: F401  (fixture re-export)
from tests.test_admin import H, fresh  # noqa: F401
from tests.test_chat_core import join, svc, wakes  # noqa: F401


def trio(svc):
    return join(svc, "s1"), join(svc, "s2", kind="codex"), join(svc, "s3")


def sysnotes(svc, gid):
    return [r["text"] for r in svc.db.query("SELECT text FROM chat_messages WHERE group_id=? AND author='system'"
                                            " ORDER BY rowid", (gid,))]


def test_find_mentions_edges():
    assert find_mentions("@Wren please") == ["Wren"]
    assert find_mentions("hi @wren, and (@Fennel) [@Sage] \"@Moss\"") == ["wren", "Fennel", "Sage", "Moss"]
    assert find_mentions("mail me at alex@Wren or x@all") == []
    assert find_mentions("@Wren @WREN @wren") == ["Wren"]
    assert find_mentions("> @Wren said\n| @Sage quoted\n  > @Moss\nok @Fern") == ["Fern"]
    assert find_mentions("@all hands") == ["all"]
    assert find_mentions("@Wren2 @9x @_x") == ["Wren2"]


def test_mention_one_member(svc):
    a, b, c = trio(svc)
    r = svc.chat.post(a["member_handle"], f"@{b['name'].lower()}, please run the tests")
    assert r["kind"] == "request" and r["mentions"] == [b["name"]] and r["unknown_mentions"] == []
    assert set(r["woken"]) == {b["member_id"], c["member_id"]}  # everyone still reads it
    mid = r["message_id"]
    assert sysnotes(svc, a["group_id"])[-1] == f"{mid} is for {b['name']}"
    text = svc.chat.framed_text(mid).split("\n")
    assert text[1] == f"→ for {b['name']} only: {b['name']} acts; everyone else reads and does not act"
    assert svc.chat.claim(b["member_handle"], mid) == {"claimed": True, "message_id": mid}
    no = svc.chat.claim(c["member_handle"], mid)
    assert no["claimed"] is False and no["reason"] == f"this message is for {b['name']}"
    read = svc.chat.read(c["member_handle"])["messages"]
    said = next(m for m in read if m["id"] == mid)
    assert f"→ for {b['name']} only" in said["text"]


def test_mention_several_first_mentioned_wins(svc):
    a, b, c = trio(svc)
    d = join(svc, "s4", kind="codex")
    r = svc.chat.post(a["member_handle"], f"@{b['name']} or @{c['name']}: one of you, please")
    mid = r["message_id"]
    assert r["mentions"] == [b["name"], c["name"]]
    assert svc.db.scalar("SELECT claimed_by_member_id FROM chat_messages WHERE id=?", (mid,)) is None
    assert svc.chat.framed_text(mid).split("\n")[1] == (
        f"→ for {b['name']} and {c['name']}: they act; everyone else reads and does not act")
    no = svc.chat.claim(d["member_handle"], mid)
    assert no["claimed"] is False and no["reason"] == f"this message is for {b['name']} and {c['name']}"
    assert svc.chat.claim(c["member_handle"], mid)["claimed"] is True
    late = svc.chat.claim(b["member_handle"], mid)
    assert late["claimed"] is False and late["by"].endswith(c["name"])


def test_unknown_and_self_mentions(svc):
    a, b, _ = trio(svc)
    r = svc.chat.post(a["member_handle"], f"@{a['name']} note to self, cc @Nobody")
    assert r["mentions"] == [] and r["unknown_mentions"] == ["Nobody"] and r["kind"] == "request"
    assert svc.chat.framed_text(r["message_id"]).split("\n")[1] != ""  # no for-line: rules follow the header
    assert not svc.chat.framed_text(r["message_id"]).split("\n")[1].startswith("→")
    assert svc.chat.claim(b["member_handle"], r["message_id"])["claimed"] is True  # open request


def test_fyi_and_all(svc):
    a, b, c = trio(svc)
    f = svc.chat.post(a["member_handle"], "heads-up: I am rebasing main", fyi=True)
    assert f["kind"] == "fyi" and set(f["woken"]) == {b["member_id"], c["member_id"]}
    assert svc.chat.framed_text(f["message_id"]).split("\n")[1] == (
        "→ heads-up for everyone: nobody acts on this; nothing to claim")
    with pytest.raises(Invalid, match="nothing to claim: heads-up"):
        svc.chat.claim(b["member_handle"], f["message_id"])
    al = svc.chat.post(a["member_handle"], f"@all pull, and @{b['name']} too")
    assert al["kind"] == "all" and al["mentions"] == []
    assert svc.chat.framed_text(al["message_id"]).split("\n")[1] == (
        "→ for everyone: each of you acts on this; nothing to claim")
    with pytest.raises(Invalid, match="nothing to claim: for everyone"):
        svc.chat.claim(c["member_handle"], al["message_id"])
    for text in (f"@{b['name']} fyi", "@all fyi"):
        with pytest.raises(Invalid) as e:
            svc.chat.post(a["member_handle"], text, fyi=True)
        assert str(e.value) == FYI_WITH_MENTIONS


def test_owner_mentions_and_release(svc):
    a, b, c = trio(svc)
    r = svc.chat.post_owner(a["group_id"], f"@{c['name']} take this")
    mid = r["message_id"]
    assert r["mentions"] == [c["name"]]
    assert svc.db.scalar("SELECT claimed_by_member_id FROM chat_messages WHERE id=?", (mid,)) == c["member_id"]
    assert svc.chat.release_claim_owner(mid) == {"released": True}
    no = svc.chat.claim(b["member_handle"], mid)  # still for c only
    assert no["claimed"] is False and "is for" in no["reason"]
    assert svc.chat.claim(c["member_handle"], mid)["claimed"] is True


def test_mentioned_member_left_opens_it(svc):
    a, b, c = trio(svc)
    mid = svc.chat.post(a["member_handle"], f"@{b['name']} please")["message_id"]
    svc.chat.leave(b["member_handle"])  # its claim is released as on any leave
    assert svc.chat.claim(c["member_handle"], mid)["claimed"] is True


def test_rename_then_mention_new_name(svc):
    a, b, _ = trio(svc)
    svc.chat.rename_owner(b["member_id"], "Pebble")
    r = svc.chat.post(a["member_handle"], "@pebble hi")
    assert r["mentions"] == ["Pebble"]


def test_kind_line_plain():
    assert kind_line("request", []) is None


def test_admin_shows_kind_and_mentions_and_owner_fyi(ctx):  # noqa: F811
    srv, b, bearer = ctx
    j = _join(srv, "host-a", "/srv/one", "s1")
    k = _join(srv, "host-a", "/srv/one", "s2")
    srv.service.chat.post(j["member_handle"], f"@{k['name']} look")
    r = b.post(f"/admin/api/chats/{j['group_id']}/messages", json={"text": "fyi all", "fyi": True}, headers=H)
    assert r.status_code == 201 and r.json()["kind"] == "fyi"
    r = b.post(f"/admin/api/chats/{j['group_id']}/messages", json={"text": f"@{k['name']} x", "fyi": True}, headers=H)
    assert r.status_code == 422
    msgs = b.get(f"/admin/api/chats/{j['group_id']}/messages").json()["messages"]
    said = [m for m in msgs if m["author"] != "system"]
    assert said[0]["mentions"] == [k["name"]] and said[0]["kind"] == "request"
    assert said[1]["kind"] == "fyi" and "mentions" not in said[1]


def test_retention_drops_mentions(svc):
    from datetime import timedelta
    from notify.db import iso, utcnow
    a, b, _ = trio(svc)
    svc.chat.post(a["member_handle"], f"@{b['name']} x")
    for m in (a, b, _):
        svc.chat.leave(m["member_handle"])
    with svc.db.tx() as c:
        c.execute("UPDATE chat_groups SET closed_at=?", (iso(utcnow() - timedelta(days=400)),))
    svc.retention_sweep()
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_mentions") == 0
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_groups") == 0
