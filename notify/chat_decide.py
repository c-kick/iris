"""The phone decision page for a group chat history request.

Same pattern as the access-request page (registrations.py): the push links carry a single-use code for one request
(only its hash is stored, cleared on any decision or expiry), opening the page never decides, a button press (POST)
does, and wrong codes are throttled.
"""

from __future__ import annotations

import hmac
import html
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .auth import hash_token
from .clientip import client_ip
from .chat import _clean_desc
from .core import Conflict
from .registrations import _page, _when

router = APIRouter(tags=["chat"])


def _gone() -> HTMLResponse:
    return _page("Link no longer valid", """
<h2>This link no longer works</h2>
<p>The request was already decided, has expired, or the link is incomplete.</p>
<p><a class="btn" href="/admin#chats">Open chats</a></p>""", status=410)


def _check_code(request: Request, rid: str, code: str | None) -> Any:
    """The pending request row if `code` is its current decision code, else None (throttled)."""
    from .admin import Throttle  # admin imports registrations; import lazily to avoid a cycle

    app = request.app
    if not hasattr(app.state, "decide_throttle"):
        app.state.decide_throttle = Throttle()
    throttle = app.state.decide_throttle
    key = f"decide:{client_ip(request)}"
    svc = app.state.service
    svc.chat.expire_history()
    r = svc.db.one("SELECT * FROM chat_history_requests WHERE id=?", (rid,))
    ok = (r is not None and code and r["state"] == "pending" and r["decision_sha256"]
          and hmac.compare_digest(hash_token(code), r["decision_sha256"]))
    if not ok:  # only misses are throttled: behind a proxy junk probes must not lock out the owner's valid link
        throttle.check(key)
        throttle.fail(key)
        return None
    throttle.ok(key)
    return r


def _who(request: Request, r: Any) -> tuple[str, str, str]:
    row = request.app.state.service.db.one(
        "SELECT m.kind, g.host, g.folder FROM chat_members m JOIN chat_groups g ON g.id=m.group_id WHERE m.id=?",
        (r["member_id"],))
    return row["kind"].capitalize(), _clean_desc(f"{row['host']}:{row['folder']}"), _clean_desc(" ".join(r["reason"].split()))


@router.get("/admin/decide-history/{rid}", include_in_schema=False)
async def history_page(rid: str, request: Request, k: str | None = None, action: str | None = None) -> HTMLResponse:
    r = _check_code(request, rid, k)
    if r is None:
        return _gone()
    e = html.escape
    kind, where, reason = _who(request, r)
    approve = '<button class="btn primary" name="decision" value="approve"{af}>Approve</button>'
    deny = '<button class="btn danger" name="decision" value="deny"{af}>Deny</button>'
    first, second = (deny, approve) if action == "deny" else (approve, deny)
    buttons = first.format(af=" autofocus") + second.format(af="")
    return _page("Group chat history request", f"""
<h2>{e(kind)} wants to read earlier chats</h2>
<p class="hint">In {e(where)}, asked {e(_when(r['created_at']))}</p>
<p>Reason it gave:</p>
<p><q>{e(reason)}</q></p>
<p>If you approve, it can read the last closed chats of this folder once, as history.</p>
<form method="post" action="/admin/decide-history/{e(rid)}">
  <input type="hidden" name="k" value="{e(k or '')}">
  <div class="decide-buttons">{buttons}</div>
</form>""")


@router.post("/admin/decide-history/{rid}", include_in_schema=False)
async def history_decide(rid: str, request: Request) -> HTMLResponse:
    form = dict(parse_qsl((await request.body()).decode(errors="replace")))
    decision = form.get("decision")
    if decision not in ("approve", "deny"):
        return _page("Choose Approve or Deny", "<h2>Choose Approve or Deny</h2>", status=400)
    r = _check_code(request, rid, form.get("k"))
    if r is None:
        return _gone()
    try:
        request.app.state.service.chat.decide_history(rid, decision == "approve", by="phone link")
    except Conflict:
        return _gone()
    if decision == "approve":
        return _page("Approved", "<h2>Approved</h2><p>The agent can read the earlier chats once. "
                                 "The approval lapses after 30 minutes.</p>")
    return _page("Denied", "<h2>Denied</h2><p>The agent was told. It can ask again later.</p>")
