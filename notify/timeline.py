"""The admin Activity list: everything sent and received, newest first, in one paged list.

Three tables feed it: notifications (at the moment they went out), replies and rejected mail. A page
is cut with a cursor on (time, id), so arrivals never shift the page being read and a row that was
purged meanwhile does not break paging.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .core import Client, Invalid, NotFound
from .schedule import EFFECTIVE_SQL, IN_HISTORY_SQL

if TYPE_CHECKING:
    from .core import NotifyService

_CURSOR = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)\|([A-Za-z0-9_-]+)$")
_REJECTED_KEY = "printf('rej_%012d', {})"


def _parse_cursor(cursor: str) -> tuple[str, str]:
    match = _CURSOR.match(cursor)
    if match is None:
        raise Invalid("not a valid page cursor")
    return match.group(1), match.group(2)


def _cursor(item: dict[str, Any]) -> str:
    return f"{item['at']}|{item['id']}"


def _keys(svc: NotifyService, direction: str, source: str | None, op: str | None, cursor: tuple[str, str] | None,
          limit: int) -> list[tuple[str, str, str]]:
    """Up to `limit` (time, id, kind) closest to the cursor, in paging order: `<` walks back in time, else forward."""
    selects: list[tuple[str, str, list[Any]]] = []
    if direction != "received":
        where, params = IN_HISTORY_SQL, []
        if source:
            where, params = f"{where} AND source=?", [source]
        selects.append(("sent", f"SELECT {EFFECTIVE_SQL} AS at, id AS key FROM notifications WHERE {where}", params))
    if direction != "sent":
        if source:  # a reply counts under the service whose message it answers
            selects.append(("reply", "SELECT r.received_at AS at, r.id AS key FROM replies r"
                                     " JOIN notifications n ON n.id = r.notification_id WHERE n.source=?", [source]))
        else:
            selects.append(("reply", "SELECT received_at AS at, id AS key FROM replies", []))
            # rejected mail names a notification only by what the unverified mail claims: never tied to a service
            selects.append(("rejected", f"SELECT received_at AS at, {_REJECTED_KEY.format('id')} AS key"
                                        " FROM reply_quarantine", []))
    order = "ASC" if op == ">" else "DESC"
    found: list[tuple[str, str, str]] = []
    for kind, select, params in selects:
        sql, args = f"SELECT at, key FROM ({select})", list(params)
        if cursor is not None:
            sql += f" WHERE (at, key) {op} (?, ?)"
            args += list(cursor)
        rows = svc.db.query(f"{sql} ORDER BY at {order}, key {order} LIMIT ?", [*args, limit])
        found += [(r["at"], r["key"], kind) for r in rows]
    return sorted(found, reverse=order == "DESC")[:limit]


def _sent(svc: NotifyService, client: Client, nid: str, at: str | None = None) -> dict[str, Any] | None:
    try:
        n = svc.get(client, nid, detail=False)
    except NotFound:
        return None
    waiting = n["status"] == "scheduled" or (n["status"] == "cancelled" and bool(n.get("send_at")) and not n["deliveries"])
    return {**n, "kind": "sent", "at": at or n["sent_at"] or n.get("send_at") or n["created_at"], "unsent": waiting}


def _reply(svc: NotifyService, rid: str) -> dict[str, Any] | None:
    row = svc.db.one("SELECT * FROM replies WHERE id=?", (rid,))
    return None if row is None else {**svc.replies._out(row), "kind": "reply", "at": row["received_at"]}


def _rejected(svc: NotifyService, key: str) -> dict[str, Any] | None:
    row = svc.db.one("SELECT * FROM reply_quarantine WHERE id=?", (int(key.removeprefix("rej_")),))
    return None if row is None else {**dict(row), "id": key, "kind": "rejected", "at": row["received_at"]}


def find(svc: NotifyService, client: Client, needle: str) -> dict[str, Any]:
    """One entry by its exact id, whatever its state: the only way to see something that was never sent."""
    needle = needle.strip()
    item = None
    if needle.startswith("ntf_"):
        item = _sent(svc, client, needle)
    elif needle.startswith("rpl_"):
        item = _reply(svc, needle)
    return {"items": [item] if item else [], "older": None, "newer": None}


def page(svc: NotifyService, client: Client, *, limit: int = 25, before: str | None = None, after: str | None = None,
         direction: str = "all", source: str | None = None) -> dict[str, Any]:
    if before and after:
        raise Invalid("give before or after, not both")
    limit = max(1, min(limit, 100))
    keys: list[tuple[str, str, str]] | None = None
    if after:
        keys = _keys(svc, direction, source, ">", _parse_cursor(after), limit + 1)
        # fewer than a page is newer: show the newest page, never a short one at the top
        keys = list(reversed(keys[:limit])) if len(keys) > limit else None
    if keys is None:
        cursor = _parse_cursor(before) if before else None
        keys = _keys(svc, direction, source, "<" if cursor else None, cursor, limit)
    items = []
    for at, key, kind in keys:
        item = (_sent(svc, client, key, at) if kind == "sent" else
                _reply(svc, key) if kind == "reply" else _rejected(svc, key))
        if item is not None:  # purged since the keys were read
            items.append(item)

    def beyond(op: str, item: dict[str, Any]) -> str | None:
        more = _keys(svc, direction, source, op, (item["at"], item["id"]), 1)
        return _cursor(item) if more else None

    return {"items": items, "older": beyond("<", items[-1]) if items else None,
            "newer": beyond(">", items[0]) if items else None}


def sources(svc: NotifyService) -> list[str]:
    rows = svc.db.query(f"SELECT DISTINCT source FROM notifications WHERE {IN_HISTORY_SQL}"
                        " AND source IS NOT NULL ORDER BY source")
    return [r["source"] for r in rows]
