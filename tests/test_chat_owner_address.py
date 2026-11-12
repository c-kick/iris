"""0.23.0: agents address the owner with @<name> (setting chat_owner_name, default Klaas; @owner as an alias). The
message is marked for the owner (a chip, and an @ on the folder's row until they look); nobody claims it. The start-up
context tells agents not to sign their messages or prefix them with "Owner:"."""

from __future__ import annotations

import pytest

from notify.chat import kind_line
from notify.core import Conflict, Invalid
from tests.test_chat_admin import _join, ctx  # noqa: F401  (fixture re-export)
from tests.test_admin import H, fresh  # noqa: F401
from tests.test_chat_core import build, join, svc  # noqa: F401


def pair(svc):
    return join(svc, "s1"), join(svc, "s2")


def for_owner(svc, mid):
    return svc.db.scalar("SELECT for_owner FROM chat_messages WHERE id=?", (mid,))


def test_at_owner_name_marks_the_message(svc):
    a, b = pair(svc)
    r = svc.chat.post(a["member_handle"], "@Klaas the build is ready to test")
    assert r["unknown_mentions"] == [] and r["mentions"] == [] and r["for_owner"] is True
    assert for_owner(svc, r["message_id"]) == 1
    assert svc.chat.framed_text(r["message_id"]).split("\n")[1] == kind_line("request", [], owner="Klaas")
    assert kind_line("request", [], owner="Klaas") == "→ for Klaas (the owner): nobody else acts; nothing to claim"


@pytest.mark.parametrize("text", ["@klaas look", "@owner look", "@OWNER look"])
def test_case_and_alias(svc, text):
    a, _ = pair(svc)
    r = svc.chat.post(a["member_handle"], text)
    assert r["for_owner"] is True and r["unknown_mentions"] == []


def test_nothing_to_claim(svc):
    a, b = pair(svc)
    r = svc.chat.post(a["member_handle"], "@Klaas please decide")
    with pytest.raises(Invalid, match="nothing to claim: for Klaas"):
        svc.chat.claim(b["member_handle"], r["message_id"])


def test_owner_and_a_member(svc):
    a, b = pair(svc)
    r = svc.chat.post(a["member_handle"], f"@{b['name']} run the tests, @Klaas fyi")
    assert r["for_owner"] is True and r["mentions"] == [b["name"]]
    line = svc.chat.framed_text(r["message_id"]).split("\n")[1]
    assert line == f"→ for {b['name']} only: {b['name']} acts; everyone else reads and does not act (also for Klaas)"


def test_plain_and_quoted_are_not_for_owner(svc):
    a, _ = pair(svc)
    assert svc.chat.post(a["member_handle"], "Klaas said so")["for_owner"] is False
    assert svc.chat.post(a["member_handle"], "> @Klaas quoted")["for_owner"] is False
    assert svc.chat.post(a["member_handle"], "mail klaas@example.org")["for_owner"] is False


def test_owner_own_message_is_not_for_owner(svc):
    a, _ = pair(svc)
    r = svc.chat.post_owner(a["group_id"], "@Klaas note to self")
    assert r["for_owner"] is False


def test_fyi_with_owner_mention_is_allowed(svc):
    a, _ = pair(svc)
    r = svc.chat.post(a["member_handle"], "@Klaas heads-up: rebasing", fyi=True)
    assert r["kind"] == "fyi" and r["for_owner"] is True


def test_group_marker_set_and_cleared(svc):
    a, _ = pair(svc)
    r = svc.chat.post(a["member_handle"], "@Klaas look")
    at = svc.db.scalar("SELECT owner_mention_at FROM chat_groups WHERE id=?", (a["group_id"],))
    assert at is not None
    svc.chat.post(a["member_handle"], "no mention")
    assert svc.db.scalar("SELECT owner_mention_at FROM chat_groups WHERE id=?", (a["group_id"],)) == at
    svc.chat.owner_mention_seen(a["group_id"], at)
    assert svc.db.scalar("SELECT owner_mention_at FROM chat_groups WHERE id=?", (a["group_id"],)) is None


def test_seen_keeps_a_newer_mention(svc):
    a, _ = pair(svc)
    svc.chat.post(a["member_handle"], "@Klaas one")
    old = svc.db.scalar("SELECT owner_mention_at FROM chat_groups WHERE id=?", (a["group_id"],))
    svc.chat.now = lambda: __import__("datetime").datetime.fromisoformat("2099-01-01T00:00:00+00:00")
    svc.chat.post(a["member_handle"], "@Klaas two")
    svc.chat.owner_mention_seen(a["group_id"], old)
    assert svc.db.scalar("SELECT owner_mention_at FROM chat_groups WHERE id=?", (a["group_id"],)) is not None


def test_configured_name(tmp_path):
    s = build(tmp_path, chat_owner_name="Sam")
    try:
        a, _ = join(s, "s1"), join(s, "s2")
        assert s.chat.post(a["member_handle"], "@Sam look")["for_owner"] is True
        r = s.chat.post(a["member_handle"], "@Klaas look")
        assert r["for_owner"] is False and r["unknown_mentions"] == ["Klaas"]
        assert s.chat.post(a["member_handle"], "@owner look")["for_owner"] is True
    finally:
        s.db.close()


@pytest.mark.parametrize("bad", ["", "all", "Two words", "1abc", "x" * 21])
def test_bad_owner_name_refused(tmp_path, bad):
    with pytest.raises(Exception):
        build(tmp_path, chat_owner_name=bad)


def test_rename_to_owner_name_refused(svc):
    a, _ = pair(svc)
    with pytest.raises(Conflict, match="the owner"):
        svc.chat.rename_owner(a["member_id"], "klaas")
    with pytest.raises((Conflict, Invalid)):
        svc.chat.rename_owner(a["member_id"], "owner")


def test_startup_context_rules(svc):
    text = svc.chat.join_context("/srv/x", join(svc, "s1"), report_in=True)
    assert "your name and what" not in text
    assert "@Klaas" in text
    assert "Don't sign messages" in text
    assert "Owner:" in text  # named as the thing not to do


def test_admin_message_and_folder_carry_owner_mark(ctx):  # noqa: F811
    srv, b, _ = ctx
    a = _join(srv, "host-a", "/srv/one", "s1")
    _join(srv, "host-a", "/srv/one", "s2")
    srv.service.chat.post(a["member_handle"], "@Klaas see this")
    msgs = b.get(f"/admin/api/chats/{a['group_id']}/messages", headers=H).json()["messages"]
    assert msgs[-1]["for_owner"] is True
    g = [g for g in b.get("/admin/api/chats", headers=H).json()["groups"] if g["id"] == a["group_id"]][0]
    assert g["owner_mention"] is True and g["owner_mention_at"]


def test_setting_owner_name(ctx):  # noqa: F811
    import httpx
    srv, b, bearer = ctx
    st = b.put("/admin/api/server", json={"chat_owner_name": " Sam "}, headers=H).json()
    assert st["config"]["server"]["chat_owner_name"] == "Sam"
    for bad in ("all", "two words", ""):
        assert b.put("/admin/api/server", json={"chat_owner_name": bad}, headers=H).status_code == 422
    assert httpx.put(srv.url + "/admin/api/server", json={"chat_owner_name": "X"},
                     headers={**bearer, **H}).status_code == 403
    a = _join(srv, "host-a", "/srv/one", "s1")
    _join(srv, "host-a", "/srv/one", "s2")
    assert srv.service.chat.post(a["member_handle"], "@Sam look")["for_owner"] is True


def test_seen_clears_the_at(ctx):  # noqa: F811
    srv, b, _ = ctx
    a = _join(srv, "host-a", "/srv/one", "s1")
    _join(srv, "host-a", "/srv/one", "s2")
    srv.service.chat.post(a["member_handle"], "@Klaas look")
    g = [g for g in b.get("/admin/api/chats", headers=H).json()["groups"] if g["id"] == a["group_id"]][0]
    assert b.post(f"/admin/api/chats/{a['group_id']}/seen?mention_upto={g['owner_mention_at']}",
                  headers=H).status_code == 200
    g = [g for g in b.get("/admin/api/chats", headers=H).json()["groups"] if g["id"] == a["group_id"]][0]
    assert g["owner_mention"] is False


def test_owner_name_taken_by_a_member(ctx):  # noqa: F811
    srv, b, _ = ctx
    a = _join(srv, "host-a", "/srv/one", "s1")
    r = b.put("/admin/api/server", json={"chat_owner_name": a["name"].upper()}, headers=H)
    assert r.status_code == 422 and "member" in r.json()["detail"]
