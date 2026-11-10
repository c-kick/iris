"""0.18.0: chat messages render a small, safe Markdown subset (chatmd.js), and the rail counts a chat's agents."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "notify" / "admin_ui"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node not installed")


def md(*texts: str) -> list[str]:
    script = ('import { chatMarkdown } from "./chatmd.js";'
              f"console.log(JSON.stringify({json.dumps(list(texts))}.map(chatMarkdown)));")
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30, cwd=UI)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_module_is_self_contained_and_used_for_message_bodies():
    src = (UI / "chatmd.js").read_text()
    assert not re.search(r"^\s*import\b", src, re.M)
    js = (UI / "app.js").read_text()
    assert re.search(r'import \{[^}]*\bchatMarkdown\b[^}]*\} from "\./chatmd\.js"', js)
    fn = js[js.index("function chatMessageHtml"):js.index("function chatEarlierHtml")]
    assert "chatMarkdown(m.text)" in fn


@needs_node
def test_html_is_always_escaped():
    out = md('<script>alert(1)</script>', '<img src=x onerror=alert(1)>', '**<b>x</b>**', '`<i>`')
    for o in out:
        assert "<script" not in o and "<img" not in o and "<b>" not in o and "<i>" not in o, o
    assert "&lt;script&gt;" in out[0]


@needs_node
def test_links_only_http_and_never_break_out_of_the_attribute():
    ok, js, data, quote = md("[docs](https://example.com/a?b=1&c=2)", "[x](javascript:alert(1))",
                             "[x](data:text/html,hi)", '[x](https://e.com" onmouseover="alert(1))')
    assert '<a href="https://example.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">docs</a>' in ok
    assert "<a" not in js and "<a" not in data
    assert "onmouseover=\"" not in quote


@needs_node
def test_blocks_paragraphs_lists_code():
    para, br, ul, ol, code, head = md("one\n\ntwo", "one\ntwo", "- a\n- b", "1. a\n2. b", "```\nx = 1\n<y>\n```",
                                      "## Plan")
    assert para == "<p>one</p><p>two</p>"
    assert br == "<p>one<br>two</p>"
    assert ul == "<ul><li>a</li><li>b</li></ul>"
    assert ol == "<ol><li>a</li><li>b</li></ol>"
    assert code == "<pre><code>x = 1\n&lt;y&gt;</code></pre>"
    assert head == '<p class="md-h">Plan</p>'


@needs_node
def test_inline_marks_and_what_must_stay_literal():
    marks, codeword, snake, star, unclosed = md("**bold** and *it* and `code`", "`**not bold**`",
                                                "reply_handle to thread_key", "2 * 3 * 4", "a **b")
    assert marks == "<p><strong>bold</strong> and <em>it</em> and <code>code</code></p>"
    assert codeword == "<p><code>**not bold**</code></p>"
    assert "<em>" not in snake and "reply_handle" in snake
    assert "<em>" not in star
    assert unclosed == "<p>a **b</p>"


@needs_node
def test_list_then_paragraph_and_nested_text():
    out, = md("Steps:\n- run `make`\n- check **logs**\n\nDone.")
    assert out == "<p>Steps:</p><ul><li>run <code>make</code></li><li>check <strong>logs</strong></li></ul><p>Done.</p>"


def test_rendered_body_does_not_keep_raw_whitespace_but_code_does():
    css = (UI / "app.css").read_text()
    body = re.search(r"\n\.cm \.body \{([^}]*)\}", css).group(1)
    assert "white-space: normal" in body
    assert "white-space: pre-wrap" in re.search(r"\n\.cm \.body pre \{([^}]*)\}", css).group(1)


def test_rail_rows_count_the_agents_of_an_open_chat():
    js = (UI / "app.js").read_text()
    row = js[js.index("function chatGroupRow"):js.index("const CHAT_EXCERPT")]
    assert "chatAgentCount(g)" in row
    fn = js[js.index("const chatAgentCount"):]
    fn = fn[:fn.index(";\n") + 1]
    assert "left_at" in fn and "agent" in fn


HOSTILE = {  # one line each, built to make a backtracking regex blow up
    "heading-spaces": '"# a" + " ".repeat(N) + "x"',
    "heading-hashes": '"# a" + " #".repeat(N / 2) + "x"',
    "brackets": '"[".repeat(N)',
    "link-opens": '"[a](".repeat(N / 4)',
    "link-long-url": '"[a](" + "h".repeat(N)',
    "stars": '"*".repeat(N)',
    "star-words": '"*a ".repeat(N / 3)',
    "bold-open": '"**a".repeat(N / 3)',
    "unders": '"_a ".repeat(N / 3)',
    "backticks": '"`".repeat(N)',
    "list-spaces": '"-" + " ".repeat(N) + "x"',
    "numbers": '"1".repeat(N) + ". x"',
    "urls": '"https://a.b/" + "x.".repeat(N / 2)',
    "url-dots": '"http://" + ".".repeat(N)',
}


@needs_node
@pytest.mark.parametrize("size", [8192, 100_000])
def test_hostile_lines_render_fast(size):
    # an agent message renders on the owner's page; no input may stall it (messages are at most 8 KB)
    cases = ",".join(f'["{k}", {v}]' for k, v in HOSTILE.items())
    script = ('import { chatMarkdown } from "./chatmd.js"; const N = ' + str(size) + ';'
              f"const out = {{}}; for (const [k, s] of [{cases}]) {{ const t = performance.now(); chatMarkdown(s);"
              " out[k] = Math.round(performance.now() - t); } console.log(JSON.stringify(out));")
    res = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=120, cwd=UI)
    assert res.returncode == 0, res.stderr
    times = json.loads(res.stdout)
    slow = {k: ms for k, ms in times.items() if ms > 300}
    assert not slow, slow


@needs_node
def test_marks_inside_a_link_never_leak_placeholders():
    label, url, heading = md("[`x` and **y**](https://a.com)", "[a](https://x.com/`y`)", "# Title ##")
    assert "\u0000" not in label + url
    assert label == '<p><a href="https://a.com" target="_blank" rel="noopener noreferrer">`x` and **y**</a></p>'
    assert heading == '<p class="md-h">Title</p>'


@needs_node
def test_underscores_in_paths_and_urls_stay_literal():
    url, path, ok = md("see https://x.com/_foo_/bar", "in /tmp/_build_/out", "this is _really_ it")
    assert "<em>" not in url and "<em>" not in path
    assert ok == "<p>this is <em>really</em> it</p>"


@needs_node
def test_previews_drop_the_marks():
    script = ('import { chatPlain } from "./chatmd.js";'
              'console.log(JSON.stringify(["Plan:\\n\\n1. **Migration** first, `make test`, see [docs](https://e.com)",'
              ' "- a\\n- b", "# Title", "```\\ncode\\n```", "snake_case stays", "2 * 3"].map(chatPlain)));')
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30, cwd=UI)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["Plan: 1. Migration first, make test, see docs", "a b", "Title", "code",
                                      "snake_case stays", "2 * 3"]
    js = (UI / "app.js").read_text()
    assert "chatPlain(" in js[js.index("function chatGroupRow"):js.index("const chatMemberLabel")]


A = 'target="_blank" rel="noopener noreferrer"'


@needs_node
def test_bare_urls_are_links_without_trailing_punctuation():
    out = md("open http://192.0.2.7:8930/admin?vvdebug=1#chats now",
             "see https://x.com/a_b_c.",
             "(https://example.org/path)",
             "`https://x.com/in?code=1`",
             "javascript:alert(1) and ftp://x.com stay text",
             'x https://a.com/"onmouseover="alert(1) y',
             "[label](https://a.com) and https://b.com")
    assert out[0] == f'<p>open <a href="http://192.0.2.7:8930/admin?vvdebug=1#chats" {A}>http://192.0.2.7:8930/admin?vvdebug=1#chats</a> now</p>'
    assert out[1] == f'<p>see <a href="https://x.com/a_b_c" {A}>https://x.com/a_b_c</a>.</p>', "the full stop is not part of it"
    assert out[2] == f'<p>(<a href="https://example.org/path" {A}>https://example.org/path</a>)</p>'
    assert out[3] == f'<p><code><a href="https://x.com/in?code=1" {A}>https://x.com/in?code=1</a></code></p>'
    assert "<a" not in out[4]
    assert 'onmouseover="' not in out[5] and "&quot;" not in out[5].split('href="')[1].split('"')[0]
    assert out[6].count("<a ") == 2 and "\u0000" not in out[6]


@needs_node
def test_urls_inside_emphasis_and_parentheses():
    bold, bold_dot, em, under, wiki = md("**https://a.com**", "**see https://a.com.**", "*https://a.com*",
                                         "_https://a.com_", "(https://en.wikipedia.org/wiki/Foo_(bar)).")
    link = f'<a href="https://a.com" {A}>https://a.com</a>'
    assert bold == f"<p><strong>{link}</strong></p>"
    assert bold_dot == f"<p><strong>see {link}.</strong></p>"
    assert em == f"<p><em>{link}</em></p>"
    assert under == f"<p><em>{link}</em></p>"
    assert 'href="https://en.wikipedia.org/wiki/Foo_(bar)"' in wiki and wiki.endswith("</a>).</p>")
