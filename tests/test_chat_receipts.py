"""Agent group chats: read receipts (messenger-style ticks) on the admin Chats page."""

from __future__ import annotations

from tests.test_admin import H, fresh  # noqa: F401  (fixture re-export)
from tests.test_chat_admin import _join, ctx  # noqa: F401  (fixture re-export)
from tests.test_chat_core import AGENT, join
from tests.test_chat_wakes import run, svc  # noqa: F401  (fixture re-export: fake clock)


def deliver_all(svc):  # noqa: F811
    while (g := run(svc.chat.claim_host(AGENT, "w", 0))) is not None:
        svc.chat.confirm(AGENT, g["wake_id"], "w")


def rc(svc, mid):  # noqa: F811
    m = svc.db.one("SELECT group_id FROM chat_messages WHERE id=?", (mid,))
    return svc.chat.receipts(m["group_id"])[mid]


def test_owner_message_sent_reached_read(svc):  # noqa: F811
    a, b = join(svc, "s1"), join(svc, "s2", kind="codex")
    svc.chat.now.advance(1)
    mid = svc.chat.post_owner(a["group_id"], "please look at X")["message_id"]
    assert rc(svc, mid) == {"recipients": 2, "reached": 0, "read": 0, "claimed": False, "state": "sent"}
    deliver_all(svc)
    assert rc(svc, mid)["state"] == "reached"  # in both sessions, nobody active since
    svc.chat.post(a["member_handle"], "on it")
    assert (rc(svc, mid)["read"], rc(svc, mid)["state"]) == (1, "reached")
    svc.chat.read(b["member_handle"])
    assert rc(svc, mid)["state"] == "read"


def test_claim_turns_it_green(svc):  # noqa: F811
    a, b = join(svc, "s1"), join(svc, "s2", kind="codex")
    mid = svc.chat.post_owner(a["group_id"], "fix it")["message_id"]
    assert rc(svc, mid)["claimed"] is False
    deliver_all(svc)
    svc.chat.claim(b["member_handle"], mid)
    r = rc(svc, mid)
    assert r["claimed"] is True and r["read"] == 1  # claiming is a group call: it read what reached it


def test_queued_wake_is_not_read_by_an_unrelated_call(svc):  # noqa: F811
    a, b = join(svc, "s1"), join(svc, "s2", kind="codex")
    mid = svc.chat.post(a["member_handle"], "for you")["message_id"]
    svc.chat.post(b["member_handle"], "unrelated, before the wake arrived")
    assert rc(svc, mid) == {"recipients": 1, "reached": 0, "read": 0, "claimed": False, "state": "sent"}


def test_recipients_are_those_present_minus_author(svc):  # noqa: F811
    a, b = join(svc, "s1"), join(svc, "s2", kind="codex")
    svc.chat.now.advance(1)
    mid = svc.chat.post(a["member_handle"], "hello")["message_id"]
    svc.chat.now.advance(1)
    join(svc, "s3")  # came later: owes nothing, does not hold the ticks back
    assert rc(svc, mid)["recipients"] == 1
    svc.chat.read(b["member_handle"])
    assert rc(svc, mid)["state"] == "read"


def test_member_who_left_unread_holds_ticks(svc):  # noqa: F811
    a, b = join(svc, "s1"), join(svc, "s2", kind="codex")
    svc.chat.now.advance(1)
    mid = svc.chat.post(a["member_handle"], "hello")["message_id"]
    svc.chat.now.advance(1)
    svc.chat.leave(b["member_handle"])
    r = rc(svc, mid)
    assert r["recipients"] == 1 and r["state"] == "sent"


def test_system_lines_have_no_receipt_and_lone_agent_message_none(svc):  # noqa: F811
    a = join(svc, "s1")
    own = svc.chat.post(a["member_handle"], "anyone here?")["message_id"]
    mid = svc.chat.post_owner(a["group_id"], "hi")["message_id"]
    rec = svc.chat.receipts(a["group_id"])
    assert set(rec) == {own, mid}  # the join notice is a system line: no receipt
    assert rec[own]["state"] == "none" and rec[own]["recipients"] == 0  # nobody else was there
    assert rec[mid]["recipients"] == 1 and rec[mid]["state"] == "sent"


def test_admin_messages_carry_receipts(ctx):  # noqa: F811
    srv, b, bearer = ctx
    m1 = _join(srv, "host-a", "/srv/one", "s1")
    _join(srv, "host-a", "/srv/one", "s2")
    mid = srv.service.chat.post_owner(m1["group_id"], "hello")["message_id"]
    for who in (b, None):
        r = (who.get(f"/admin/api/chats/{m1['group_id']}/messages", headers=H) if who
             else b.get(f"/admin/api/chats/{m1['group_id']}/messages", headers=bearer))
        data = r.json()
        msg = next(x for x in data["messages"] if x["id"] == mid)
        assert msg["receipt"]["state"] == "sent" and msg["receipt"]["recipients"] == 2
        assert data["receipts"][mid] == msg["receipt"]


def test_automatic_assignment_is_not_an_acknowledged_claim(svc):
    a = join(svc, "s1")
    mid = svc.chat.post_owner(a["group_id"], f"@{a['name']} please look")["message_id"]
    assert svc.db.scalar("SELECT claimed_by_member_id FROM chat_messages WHERE id=?", (mid,)) == a["member_id"]
    assert rc(svc, mid)["claimed"] is False
    assert rc(svc, mid)["read"] == 0
    svc.chat.claim(a["member_handle"], mid)
    assert rc(svc, mid)["claimed"] is True
    svc.chat.unclaim(a["member_handle"], mid)
    assert svc.db.scalar("SELECT claim_accepted_at FROM chat_messages WHERE id=?", (mid,)) is None


def test_retrying_and_exhausted_delivery_are_visible_and_recover(svc):
    a = join(svc, "s1")
    mid = svc.chat.post_owner(a["group_id"], f"@{a['name']} please look")["message_id"]
    for attempt in range(5):
        w = run(svc.chat.claim_host(AGENT, "w", 0))
        assert w is not None
        svc.chat.release(AGENT, w["wake_id"], "w")
        r = rc(svc, mid)
        assert not r["claimed"]
        assert r.get("retrying", 0) == (0 if attempt == 4 else 1)
        assert r.get("failed", 0) == (1 if attempt == 4 else 0)
        svc.chat.now.advance(301)
    svc.chat.read(a["member_handle"])
    assert not rc(svc, mid).get("failed"), "manually reading a missed message resolves the failed wake"


def test_admin_distinguishes_assignment_from_acceptance(ctx):
    srv, b, _ = ctx
    a = _join(srv, "host-a", "/srv/one", "s1")
    mid = srv.service.chat.post_owner(a["group_id"], f"@{a['name']} please look")["message_id"]
    path = f"/admin/api/chats/{a['group_id']}/messages"
    msg = next(m for m in b.get(path).json()["messages"] if m["id"] == mid)
    assert msg["claimed_by"] and msg["claim_accepted"] is False
    srv.service.chat.claim(a["member_handle"], mid)
    msg = next(m for m in b.get(path).json()["messages"] if m["id"] == mid)
    assert msg["claim_accepted"] is True and msg["receipt"]["claimed"] is True


def test_status_labels_do_not_turn_unread_assignments_or_failures_green():
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    source = (Path(__file__).parents[1] / "notify/admin_ui/app.js").read_text()
    fn = source[source.index("function chatTicksHtml"):source.index("// join and leave lines")]
    script = 'const esc = String;\n' + fn + r'''
const assert = require('node:assert/strict');
const r = {state:'sent',claimed:false,read:0,recipients:1};
assert.match(chatTicksHtml('m',r,'owner'), /ticks unseen/);
assert.match(chatTicksHtml('m',{...r,claimed:true},'owner'), /ticks seen/);
assert.match(chatTicksHtml('m',{...r,claimed:true,retrying:1},'owner'), /ticks unseen/);
assert.match(chatTicksHtml('m',{...r,retrying:1},'owner'), /delivery failed to 1; retrying/);
assert.match(chatTicksHtml('m',{...r,failed:1},'codex'), /delivery failed to 1/);
'''
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
