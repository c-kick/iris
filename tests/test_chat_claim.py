"""Agent group chats: group_claim for requests."""

from __future__ import annotations

import threading

import pytest

from notify.chat import AGENT_RULES, CLAIM_INVALID, OWNER_RULES
from notify.core import Invalid
from tests.test_admin import H, fresh  # noqa: F401  (fixture re-export)
from tests.test_chat_admin import _join, ctx  # noqa: F401  (fixture re-export)
from tests.test_chat_api import chatlive, join as api_join  # noqa: F401  (fixture re-export)
from tests.test_chat_core import build, join
from tests.test_mcp import payload, run_session


@pytest.fixture
def svc(tmp_path):
    s = build(tmp_path, chat_cap=3)
    yield s
    s.db.close()


def setup(svc):
    a, b, c = join(svc, "s1"), join(svc, "s2", kind="codex"), join(svc, "s3")
    req = svc.chat.post_owner(a["group_id"], "please fix X")["message_id"]
    return a, b, c, req


def sysnotes(svc, gid):
    return [r["text"] for r in svc.db.query("SELECT text FROM chat_messages WHERE group_id=? AND author='system'"
                                            " ORDER BY rowid", (gid,))]


def test_first_claim_wins_second_is_told(svc):
    a, b, c, req = setup(svc)
    assert svc.chat.claim(b["member_handle"], req) == {"claimed": True, "message_id": req}
    r = svc.chat.claim(c["member_handle"], req)
    assert r["claimed"] is False and r["by"] == f"Codex {b['member_id']}" and r["at"]
    assert sysnotes(svc, a["group_id"])[-1] == f"Codex {b['member_id']} claimed {req}"
    n = len(sysnotes(svc, a["group_id"]))
    assert svc.chat.claim(b["member_handle"], req)["claimed"] is True  # again: still held, no new line
    assert len(sysnotes(svc, a["group_id"])) == n


def test_concurrent_claims_exactly_one_wins(svc):
    a, b, c, req = setup(svc)
    out, bar = [], threading.Barrier(3)

    def go(m):
        bar.wait()
        out.append(svc.chat.claim(m["member_handle"], req)["claimed"])

    ts = [threading.Thread(target=go, args=(m,)) for m in (a, b, c)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out) == [False, False, True]


def test_invalid_targets(svc):
    a, b, c, req = setup(svc)
    own = svc.chat.post(a["member_handle"], "mine")["message_id"]
    sysid = svc.db.scalar("SELECT id FROM chat_messages WHERE author='system' AND group_id=?", (a["group_id"],))
    other = join(svc, "o1", folder="/srv/other")
    foreign = svc.chat.post(other["member_handle"], "elsewhere")["message_id"]
    for bad in (own, sysid, foreign, "msg_nope", ""):
        with pytest.raises(Invalid, match="only a request from the owner or another agent"):
            svc.chat.claim(a["member_handle"], bad)
    assert CLAIM_INVALID.startswith("only a request")


def test_unclaim_rules(svc):
    a, b, c, req = setup(svc)
    svc.chat.claim(b["member_handle"], req)
    with pytest.raises(Invalid):
        svc.chat.unclaim(c["member_handle"], req)
    assert svc.chat.unclaim(b["member_handle"], req) == {"released": True, "message_id": req}
    assert sysnotes(svc, a["group_id"])[-1] == f"Codex {b['member_id']} released {req}"
    assert svc.chat.claim(c["member_handle"], req)["claimed"] is True


def test_leaving_releases_claims(svc):
    a, b, c, req = setup(svc)
    second = svc.chat.post_owner(a["group_id"], "and Y")["message_id"]
    svc.chat.claim(b["member_handle"], req)
    svc.chat.claim(b["member_handle"], second)
    svc.chat.leave(b["member_handle"], "ended")
    notes = sysnotes(svc, a["group_id"])
    assert notes[-2:] == [f"Codex {b['member_id']} released {req}", f"Codex {b['member_id']} released {second}"]
    assert svc.chat.claim(c["member_handle"], req)["claimed"] is True


def test_system_lines_are_wakeless_and_streak_neutral(svc):
    a, b, c, req = setup(svc)
    before = svc.db.scalar("SELECT COUNT(*) FROM chat_wakes")
    streak = svc.db.scalar("SELECT agent_streak FROM chat_groups WHERE id=?", (a["group_id"],))
    svc.chat.claim(b["member_handle"], req)
    svc.chat.unclaim(b["member_handle"], req)
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_wakes") == before
    assert svc.db.scalar("SELECT agent_streak FROM chat_groups WHERE id=?", (a["group_id"],)) == streak


def test_frames_carry_the_claim_rule(svc):
    a, b, c, req = setup(svc)
    q = svc.chat.post(a["member_handle"], "hello")["message_id"]
    sentence = "Before acting on a request, call group_claim with its message id; act only if it returns claimed=true"
    assert sentence in AGENT_RULES and sentence in OWNER_RULES and "I'll take it" not in AGENT_RULES
    assert sentence in svc.chat.framed_text(q) and sentence in svc.chat.framed_text(req)


def test_mcp_tools(chatlive):  # noqa: F811
    j, j2, j3 = (api_join(chatlive, sid="s1"), api_join(chatlive, sid="s2"), api_join(chatlive, sid="s3"))
    chat = chatlive.service.chat
    req = chat.post(j["member_handle"], "do it")["message_id"]

    async def flow(s):
        first = payload(await s.call_tool("group_claim", {"member_handle": j2["member_handle"], "message_id": req}))
        again = payload(await s.call_tool("group_claim", {"member_handle": j3["member_handle"], "message_id": req}))
        bad = await s.call_tool("group_claim", {"member_handle": j2["member_handle"], "message_id": "msg_nope"})
        dead = await s.call_tool("group_unclaim", {"member_handle": "garbage", "message_id": req})
        rel = payload(await s.call_tool("group_unclaim", {"member_handle": j2["member_handle"], "message_id": req}))
        return first, again, bad, dead, rel

    first, again, bad, dead, rel = run_session(chatlive, "bot", flow)
    assert first["claimed"] is True and again["claimed"] is False and rel["released"] is True
    assert bad.isError and "claimed" in bad.content[0].text and dead.isError


def test_admin_shows_claimed_by_to_session_and_bearer(ctx):  # noqa: F811
    import httpx
    srv, b, bearer = ctx
    j = _join(srv, "host-a", "/srv/one", "s1")
    j2 = _join(srv, "host-a", "/srv/one", "s2")
    req = srv.service.chat.post_owner(j["group_id"], "task")["message_id"]
    srv.service.chat.claim(j2["member_handle"], req)
    url = f"/admin/api/chats/{j['group_id']}/messages"
    by = f"Claude {j2['member_id']}"
    sess = {m["id"]: m for m in b.get(url).json()["messages"]}[req]
    bear = {m["id"]: m for m in httpx.get(f"{srv.url}{url}", headers=bearer).json()["messages"]}[req]
    assert sess["claimed_by"] == by and bear["claimed_by"] == by and "text" not in bear
