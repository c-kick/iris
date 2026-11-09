"""The Chats page reads well and fits a phone: these pin the layout decisions that a browser check confirmed."""
import re
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "notify" / "admin_ui"
JS = (UI / "app.js").read_text()
CSS = (UI / "app.css").read_text()
HTML = (UI / "index.html").read_text()


def _rule(selector: str, css: str = CSS) -> str:
    m = re.search(r"(?:^|\n)\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"no rule for {selector}"
    return m.group(1)


def _mobile() -> str:
    start = CSS.index("@media (max-width: 760px) {\n  /* edge to edge")
    return CSS[start:CSS.index("\n}\n", start)]


def test_fyi_and_send_share_one_column_fyi_on_top():
    form = JS[JS.index('<form class="chat-compose"'):JS.index("</form>", JS.index('<form class="chat-compose"'))]
    side = re.search(r'<div class="compose-side">(.*?)</div>', form, re.S)
    assert side, "FYI and Send sit together in .compose-side"
    inner = side.group(1)
    assert inner.index('id="chat-fyi"') < inner.index('type="submit"'), "FYI above Send"
    assert "minmax(0, 1fr) auto;" in _rule(".chat-compose"), "two columns: the field and the FYI/Send column"
    assert "flex-direction: column" in _rule(".compose-side")


def test_placeholder_fits_a_phone_field():
    ph = re.search(r'id="chat-text"[^>]*placeholder="([^"]*)"', JS).group(1)
    assert len(ph) <= 64, ph


def test_message_text_uses_the_reading_face_at_a_readable_size():
    assert "Atkinson+Hyperlegible+Next" in HTML
    assert '--read: "Atkinson Hyperlegible Next"' in CSS
    body = _rule(".cm .body")
    assert "var(--read)" in body and re.search(r"px/1\.5 |line-height: 1\.5", body)
    assert "font-size: 16px" in _rule(".cm .body", _mobile())
    assert "var(--read)" in _rule(".chat-compose textarea")


def test_time_sits_in_the_footer_not_the_name_line():
    fn = JS[JS.index("function chatMessageHtml"):JS.index("function chatEarlierHtml")]
    who = re.search(r'<div class="who">(.*?)</div>', fn).group(1)
    assert "when(m.created_at)" not in who
    assert 'class="cm-time"' in fn


def test_phone_thread_hides_the_app_bar_and_follows_the_keyboard():
    assert ".shell:has(.chats.thread) .side { display: none; }" in _mobile()
    assert "interactive-widget=resizes-content" in HTML
    fit = JS[JS.index("if (window.visualViewport) {"):JS.index("const chatUnread")]
    assert 'addEventListener("scroll"' in fit and "offsetTop" in fit


def _fn(name: str) -> str:
    start = JS.index(name)
    return JS[start:JS.index("\n}\n", start)]


def test_watch_line_keeps_its_space_and_sits_under_the_limit_line():
    head = JS[JS.index('<div class="chat-head">'):JS.index('<div class="chat-paused"')]
    grow = re.search(r'<div class="grow">(.*?)</div>', head, re.S).group(1)
    assert "chat-watch" not in grow, "not in the title column, where it pushes the header taller"
    note = re.search(r'<div class="chat-head-note">(.*?)</div>', head, re.S).group(1)
    assert note.index('id="chat-limitbtn"') < note.index('id="chat-watch"'), "under the 'While paused' line"
    assert "visibility: hidden" in _rule(".chat-watch.off")
    assert "watch.hidden" not in JS and '#chat-watch").hidden' not in JS, "toggled with .off (keeps its space), never hidden"


def test_returning_to_the_tab_catches_up_at_once_and_says_so():
    assert 'id="chat-sync"' in JS and 'role="status"' in JS[JS.index('id="chat-sync"') - 80:JS.index('id="chat-sync"') + 80]
    assert 'addEventListener("visibilitychange", chatOnReturn)' in JS
    assert 'addEventListener("focus", chatOnReturn)' in JS
    catch = _fn("async function chatCatchUp")
    assert "chatSync(el, true)" in catch and "chatSync(el, false)" in catch
    tick = _fn("function chatTick(")
    assert "chats.ticking" in tick, "one refresh at a time; a request during one runs once more after it"
    pill = _rule(".chat-sync")
    assert "position: absolute" in pill, "floats: showing it moves nothing"


def test_chat_field_spellchecks_and_is_not_taken_for_a_contact_field():
    tag = re.search(r'<textarea id="chat-text"[^>]*>', JS).group(0)
    for attr in ('spellcheck="true"', 'autocorrect="on"', 'autocapitalize="sentences"', 'autocomplete="off"'):
        assert attr in tag, attr
    ph = re.search(r'placeholder="([^"]*)"', tag).group(1)
    assert "name" not in ph.lower(), "Safari offers contact autofill on fields that mention a name"


def test_phone_strip_opens_every_member_with_status():
    head = JS[JS.index('<div class="chat-head">'):JS.index('<div class="chat-paused"')]
    assert re.search(r'<button type="button" class="chat-mini" id="chat-mini" aria-expanded="false" aria-controls="chat-roster"', head)
    assert 'id="chat-roster"' in head, "the roster floats under the header"
    assert "position: absolute" in _rule(".chat-roster"), "opening it moves nothing"
    toggle = _fn("function chatRosterToggle")
    fill = _fn("function chatRosterFill")
    assert "chatRosterFill(" in toggle
    assert "chatMembersHtml(" in fill and "chatBindMembers(" in fill, "the same list and actions as on desktop"
    assert "chatBindMembers(list, el)" in _fn("function chatsRenderList")
    assert "Escape" in toggle
