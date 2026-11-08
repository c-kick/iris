"""Names for group chat members: plants and birds that read as names (Juniper, Wren, Fennel, ...).

A member keeps its name for as long as it is a member; the member id stays the key everywhere. Names are unique
within a group (case-insensitively) and, where possible, not reused in the same host + folder for a week, so
"Wren" in today's transcript is not yesterday's Wren. The list avoids common English verbs (an agent might write
"@rush" by accident) and the frame words (iris, hnl, end, history).
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable

NAMES: tuple[str, ...] = (
    "Alder", "Aster", "Basil", "Birch", "Bracken", "Bramble", "Briar", "Cedar", "Clover", "Cress", "Curlew",
    "Daisy", "Dunlin", "Egret", "Elder", "Fennel", "Fern", "Finch", "Hawthorn", "Hazel", "Heather", "Heron",
    "Holly", "Ivy", "Juniper", "Kestrel", "Larch", "Lark", "Laurel", "Linden", "Linnet", "Lupin", "Magpie",
    "Maple", "Marigold", "Merlin", "Moss", "Myrtle", "Nettle", "Oriole", "Osier", "Plover", "Poppy", "Puffin",
    "Quail", "Raven", "Reed", "Robin", "Rowan", "Saffron", "Sage", "Sedge", "Siskin", "Sorrel", "Sparrow",
    "Starling", "Tamarisk", "Tansy", "Teal", "Thistle", "Thyme", "Tulip", "Vetch", "Violet", "Wagtail", "Willow",
    "Wren", "Yarrow", "Yew", "Zinnia",
)

# owner renames: one word of letters (and digits after the first), so a name can never forge a frame header
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,19}$")
RESERVED = frozenset({"all", "owner", "system", "everyone", "iris", "hnl", "end", "history", "notify", "claude", "codex"})


def valid_name(name: str) -> bool:
    return isinstance(name, str) and bool(NAME_RE.match(name)) and name.casefold() not in RESERVED


def pick(taken: Iterable[str], recent: Iterable[str]) -> str:
    """A random name not held in the group (taken) and, if any is left, not used in the folder lately (recent).
    Comparisons ignore case. With every name taken, a digit is appended (Wren2, Wren3, ...)."""
    held = {n.casefold() for n in taken if n}
    old = {n.casefold() for n in recent if n}
    free = [n for n in NAMES if n.casefold() not in held]
    fresh = [n for n in free if n.casefold() not in old]
    if fresh or free:
        return secrets.choice(fresh or free)
    for i in range(2, 10_000):
        for n in NAMES:
            if f"{n}{i}".casefold() not in held:
                return f"{n}{i}"
    raise RuntimeError("no member name left")  # unreachable below the per-group cap
