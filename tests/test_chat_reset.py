"""Context reset from the chat (spec 2026-11-11-chat-reset-design.md), and mentions inside code are text."""

from __future__ import annotations

from notify.chat import find_mentions


def test_mentions_inside_code_are_text():
    assert find_mentions("ask `@all /clear` like this") == []
    assert find_mentions("`code @Wren` but @Fern acts") == ["Fern"]
    assert find_mentions("```\n@all\n```\n@Wren please") == ["Wren"]
    assert find_mentions("~~~\n@all\n~~~") == []
    assert find_mentions("@Wren and `x` and @Fern") == ["Wren", "Fern"]
