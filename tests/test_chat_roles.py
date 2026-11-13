"""Owner-managed membership and lead roles survive context refreshes and end on departure."""
import httpx
import pytest

from notify.chat import MemberGone
from notify.core import Conflict, NotFound
from notify.db import Database
from tests.test_admin import H, fresh  # noqa: F401
from tests.test_chat_admin import _join, ctx  # noqa: F401
from tests.test_chat_core import AGENT, FOLDER, build, join, svc  # noqa: F401


def test_lead_reaches_current_future_and_cleared_contexts(svc):
    a, b = join(svc, "s1"), join(svc, "s2")
    svc.chat.lead_owner(a["member_id"])
    notice = svc.db.one("SELECT * FROM chat_messages WHERE kind='role'")
    wakes = svc.db.query("SELECT member_id, state FROM chat_wakes WHERE message_id=?", (notice["id"],))
    assert {r["member_id"] for r in wakes} == {a["member_id"], b["member_id"]}
    assert all(r["state"] == "pending" for r in wakes)
    for member in (a, b):
        text = svc.chat.framed_text(notice["id"], member["member_id"])
        assert f"{a['name']} ({a['member_id']})" in text
        assert "no additional permissions" in text and "group_claim still apply" in text
        assert text.startswith(member["marker"]) and text.endswith(member["marker"])
    c = join(svc, "s3")
    assert f"{a['name']} ({a['member_id']})" in svc.chat.join_context(FOLDER, c)
    assert [m["member_id"] for m in c["members"] if m["is_lead"]] == [a["member_id"]]
    # Re-joining the same session (a refreshed startup context) retains the appointment.
    again = join(svc, "s1")
    assert next(m for m in again["members"] if m["member_id"] == a["member_id"])["is_lead"]
    assert "lead developer" in svc.chat.join_context(FOLDER, again)
    read = svc.chat.read(c["member_handle"])
    assert read["messages"] == []
    assert f"{a['name']} ({a['member_id']})" in read["rules"]


def test_transfer_is_unique_idempotent_and_supersedes_old_wakes(svc):
    a, b = join(svc, "s1"), join(svc, "s2")
    svc.chat.lead_owner(a["member_id"])
    old = svc.db.scalar("SELECT id FROM chat_messages WHERE kind='role'")
    svc.chat.lead_owner(a["member_id"])
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_messages WHERE kind='role'") == 1
    svc.chat.lead_owner(b["member_id"])
    assert svc.db.scalar("SELECT id FROM chat_members WHERE is_lead=1") == b["member_id"]
    assert {r["reason"] for r in svc.db.query("SELECT reason FROM chat_wakes WHERE message_id=?", (old,))} == {"role superseded"}
    # Even a stale in-flight role notice carries the current authoritative role.
    text = svc.chat.framed_text(old, a["member_id"])
    assert f"{b['name']} ({b['member_id']}) as this chat's lead developer" in text


def test_cleared_lead_keeps_role_when_new_session_takes_over(svc):
    a = join(svc, "s1")
    svc.chat.lead_owner(a["member_id"])
    from notify.db import iso
    with svc.db.tx() as c:
        c.execute("UPDATE chat_members SET reset_at=? WHERE id=?", (iso(svc.chat.now()), a["member_id"]))
    again = svc.chat.join(AGENT, kind="claude", session_id="after-clear", folder=FOLDER,
                          pid=100, pid_start="x-s1")
    assert again["member_id"] == a["member_id"]
    assert again["members"][0]["is_lead"]
    assert a["member_id"] in svc.chat.join_context(FOLDER, again)


def test_role_notice_is_deliverable_while_paused(svc):
    a, b = join(svc, "s1"), join(svc, "s2")
    svc.chat.pause_owner(a["group_id"])
    svc.chat.lead_owner(a["member_id"])
    got, mid = svc.chat._claim_once("host-a", "relay")
    assert got["member_id"] in (a["member_id"], b["member_id"])
    assert svc.db.scalar("SELECT kind FROM chat_messages WHERE id=?", (mid,)) == "role"


@pytest.mark.parametrize("reason", ["ended", "left", "stale", "replaced", "removed"])
def test_every_departure_clears_the_lead(svc, reason):
    a, b = join(svc, "s1"), join(svc, "s2")
    svc.chat.lead_owner(a["member_id"])
    svc.chat.remove_member(a["member_id"], reason)
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_members WHERE is_lead=1") == 0
    assert "no lead developer" in svc.chat.read(b["member_handle"])["rules"]
    notice = svc.db.one("SELECT id FROM chat_messages WHERE kind='role' ORDER BY rowid DESC LIMIT 1")
    assert svc.db.scalar("SELECT member_id FROM chat_wakes WHERE message_id=?", (notice["id"],)) == b["member_id"]


def test_remove_releases_claims_invalidates_handles_and_blocks_same_session(svc):
    a, b = join(svc, "s1"), join(svc, "s2")
    mid = svc.chat.post_owner(a["group_id"], "Please check this")["message_id"]
    svc.chat.claim(a["member_handle"], mid)
    svc.chat.remove_owner(a["member_id"])
    assert svc.db.scalar("SELECT claimed_by_member_id FROM chat_messages WHERE id=?", (mid,)) is None
    assert not svc.db.one("SELECT 1 FROM chat_wakes WHERE member_id=? AND state IN ('pending','claimed')", (a["member_id"],))
    with pytest.raises(MemberGone):
        svc.chat.post(a["member_handle"], "still here")
    with pytest.raises(Conflict, match="new session"):
        join(svc, "s1")
    with pytest.raises(Conflict, match="new session"):
        svc.chat.presence(AGENT, kind="claude", session_id="s1", folder=FOLDER, pid=100, pid_start="x-s1")
    # The relay cannot resurrect an owner-removed member.
    svc.chat.heartbeat(AGENT, [{"member_id": a["member_id"], "pid": 100, "pid_start": "x-s1"}])
    assert svc.db.scalar("SELECT leave_reason FROM chat_members WHERE id=?", (a["member_id"],)) == "removed"
    assert join(svc, "new-session")["member_id"] != a["member_id"]


def test_removing_last_member_closes_group_and_errors_are_clear(svc):
    a = join(svc, "s1")
    svc.chat.lead_owner(a["member_id"])
    svc.chat.remove_owner(a["member_id"])
    assert svc.db.scalar("SELECT state FROM chat_groups") == "closed"
    with pytest.raises(Conflict, match="new session"):
        svc.chat.presence(AGENT, kind="claude", session_id="s1", folder=FOLDER, pid=100, pid_start="x-s1")
    with pytest.raises(Conflict):
        svc.chat.lead_owner(a["member_id"])
    with pytest.raises(Conflict):
        svc.chat.remove_owner(a["member_id"])
    with pytest.raises(NotFound):
        svc.chat.lead_owner("missing")
    with pytest.raises(NotFound):
        svc.chat.remove_owner("missing")


def test_roles_persist_across_database_reopen(svc):
    a = join(svc, "s1")
    svc.chat.lead_owner(a["member_id"])
    db = Database(svc.db.path)
    try:
        assert db.scalar("SELECT is_lead FROM chat_members WHERE id=?", (a["member_id"],)) == 1
    finally:
        db.close()


def test_existing_database_gets_unassigned_roles(tmp_path):
    s = build(tmp_path)
    a = join(s, "s1")
    path = s.db.path
    with s.db.tx() as c:
        c.execute("DROP INDEX ix_chat_one_lead")
        c.execute("ALTER TABLE chat_members DROP COLUMN is_lead")
    s.db.close()
    db = Database(path)
    try:
        assert db.scalar("SELECT is_lead FROM chat_members WHERE id=?", (a["member_id"],)) == 0
    finally:
        db.close()


def test_owner_endpoints_require_browser_session_and_csrf(ctx):
    srv, browser, bearer = ctx
    a = _join(srv, "host-a", "/srv/one", "s1")
    base = f"/admin/api/chats/members/{a['member_id']}"
    with httpx.Client(base_url=srv.url) as anonymous:
        assert anonymous.put(base + "/lead", headers=H).status_code == 401
    assert browser.put(base + "/lead").status_code == 403
    with httpx.Client(base_url=srv.url, headers=bearer) as token:
        assert token.put(base + "/lead", headers=H).status_code == 403
        assert token.delete(base, headers=H).status_code == 403
    assert browser.put(base + "/lead", headers=H).status_code == 200
    members = browser.get("/admin/api/chats").json()["groups"][0]["members"]
    assert members[0]["is_lead"] is True
    assert browser.delete(base).status_code == 403
    assert browser.delete(base, headers=H).status_code == 200
    assert browser.put(base + "/lead", headers=H).status_code == 409
    assert browser.delete(base, headers=H).status_code == 409
