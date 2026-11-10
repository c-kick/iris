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
    start = CSS.index("@media (max-width: 760px), (max-height: 500px) and (hover: none) {\n  /* edge to edge")
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
    assert "chatMembersHtml(" in fill, "the same list as on desktop"
    thread = _fn("async function chatOpenThread")
    assert "roster.onclick" in thread and "chatMention(el" in thread and "chatRename(el" in thread, "and the same actions"
    assert "chatBindMembers(list, el)" in _fn("function chatsRenderList")
    assert "Escape" in toggle


def test_roster_survives_the_poll():
    # the 5 s poll must not rebuild an open roster: that resets its scroll and ends an iOS swipe mid-gesture
    fill = _fn("function chatRosterFill")
    assert "roster.dataset.sig" in fill and "scrollTop" in fill
    assert "chatBindMembers" not in fill, "handlers are delegated once, so a refill needs no rebinding"
    assert "chatRosterSize(" in _fn("function chatRosterToggle"), "sized to the space actually left under the header"


def test_roster_cleans_up_wherever_the_pane_goes():
    toggle = _fn("function chatRosterToggle")
    assert toggle.index("removeEventListener") < toggle.index("return"), "listeners go before any early return"
    opener = _fn("async function chatOpen(")
    assert "chatRosterToggle(el, false)" in opener, "closed before the pane is replaced"
    assert "chatRosterToggle(el, false)" in _fn("async function chatsPage")
    assert 'matchMedia("(max-width: 760px), (max-height: 500px) and (hover: none)")' in JS


def test_refresh_does_not_race_opening_a_thread_and_reports_failures():
    assert "chats.opening" in _fn("async function chatOpen(") and "chats.opening" in _fn("async function chatTickOnce")
    assert "console.error" in _fn("async function chatTickOnce")
    assert "chats.syncing" in _fn("async function chatCatchUp"), "overlapping catch-ups: the last one hides the pill"


def test_owner_presence_counts_real_input_only():
    line = JS[JS.index('["mousemove"'):JS.index("chatLastInput = Date.now(); }")]
    assert '"scroll"' not in line, "code scrolling the thread is not the owner looking"
    assert "chatLastInput = Date.now()" in JS[JS.index("const chatOnReturn"):JS.index("document.addEventListener(\"visibilitychange\"")]


def test_pause_button_keeps_its_width():
    # a fixed width, not a minimum: "Pause now" with its icon outgrew the minimum and moved the title on every toggle
    assert re.search(r"(^|[; ])width: [\d.]+rem", _rule("#chat-pausebtn"))
    assert re.search(r"(^|[; ])width: [\d.]+rem", _rule(".thread-head #chat-pausebtn", _mobile()))


def test_chats_page_never_pans_or_bounces():
    # landscape iPhone: a swipe on the header or composer dragged the whole document (and the shell, pinned to
    # visualViewport.offsetTop, chased it down), and a pull at the top refreshed the page
    rule = _rule("html:has(.chats), body:has(.chats)")
    assert "overflow: hidden" in rule and "overscroll-behavior: none" in rule
    for sel in (".chat-msgs", ".chat-list", ".chat-roster"):
        assert "overscroll-behavior: contain" in _rule(sel), sel
    fit = JS[JS.index("if (window.visualViewport) {"):JS.index("const chatUnread")]
    assert "keyboardOpen" in fit, "the offset is followed only while the keyboard is open"


def test_paused_banner_floats_over_the_thread():
    # the last child of the header (position: relative), so showing it moves nothing under it
    assert re.search(r'<div class="chat-paused" id="chat-paused" role="status" hidden></div></div>\s*<div class="chat-msgs"', JS)
    rule = _rule(".chat-paused")
    assert "position: absolute" in rule and "top: 100%" in rule


def test_a_phone_on_its_side_gets_the_phone_layout():
    # iPhone landscape is 844 wide: by width alone it got the desktop layout with a thread a few lines tall
    assert "@media (max-width: 760px) {" not in CSS and "(max-width: 760px) {" not in CSS
    assert CSS.count("@media (max-width: 760px), (max-height: 500px) and (hover: none) {") >= 3


def test_paused_banner_review_fixes():
    sync = int(re.search(r"z-index: (\d+)", _rule(".chat-sync")).group(1))
    banner = int(re.search(r"z-index: (\d+)", _rule(".chat-paused")).group(1))
    assert sync > banner, "the Updating pill shows over the banner"
    assert "var(--paused-h" in CSS, "the thread's top room is the banner's measured height, not a guess"
    thread = _fn("async function chatOpenThread")
    assert "--paused-h" in thread and "if (stick) box.scrollTop = box.scrollHeight" in thread[thread.index("--paused-h") - 300:]
    assert ".chat-head:has(.chat-roster:not([hidden])) .chat-paused" in CSS
    # sign-out in the top bar: hidden by default, shown only by the phone query (no gap between two queries)
    assert "(min-width: 761px)" not in CSS


def test_phone_thread_header_is_one_aligned_grid():
    head = JS[JS.index('<div class="chat-head thread-head">'):JS.index('<div class="chat-msgs"')]
    back = re.search(r'<button[^>]*id="chat-back"[^>]*>(.*?)</button>', head, re.S)
    assert 'aria-label="All folders"' in back.group(0) and "<svg" in back.group(1), "a chevron with a spoken label"
    mob = _mobile()
    grid = _rule(".thread-head", mob)
    assert "display: grid" in grid and "grid-template-areas" in grid
    for area in ('"back title title pause"', '"back host watch pause"', '"mini mini mini mini"'):
        assert area in grid, area
    assert "display: contents" in _rule(".thread-head .grow", mob)
    assert "mask-image" in _rule(".chat-mini .mini-list", mob), "fades out instead of ending in an ellipsis"
    hdr = _fn("function chatHeader")
    assert 'class="mini-count"' in hdr, "a fixed member count closes the rail"
    assert "chatPauseLabel(" in hdr and "<svg" in _fn("function chatPauseLabel")
    assert "--warn" in _rule('#chat-pausebtn[data-act="resume"]'), "a paused group shows it in the header"


def test_viewport_debug_overlay_only_on_request():
    # ?vvdebug=1 shows the visual-viewport numbers on the page, to diagnose iOS keyboard behaviour from a screenshot
    block = JS[JS.index("if (window.visualViewport) {"):JS.index("const chatUnread")]
    assert 'new URLSearchParams(location.search).has("vvdebug")' in block
    assert "vv.offsetTop" in block and "innerHeight" in block and "keyboardOpen" in block


def test_keyboard_correction_does_not_guess_from_window_sizes():
    # iPhone (2026-11-09, ?vvdebug): with the keyboard up iOS reported inner 402x384 = visual 402x384, offsetTop 330,
    # scrollY 330. innerHeight shrinks with the keyboard there, so "vv.height < innerHeight - 80" said no keyboard and
    # the page stayed 330 px above the view. While a field has focus: undo iOS's scroll, else follow its offset.
    fit = JS[JS.index("const fit = () => {"):JS.index("// ?vvdebug=1")]
    assert "innerHeight" not in fit
    assert "window.scrollTo(0, 0)" in fit
    assert "keyboardOpen = !!typing" in fit


def test_scroll_reset_only_on_the_chats_page():
    # elsewhere (a long Settings form) the page scrolls normally, and typing in a field must not jump to the top
    fit = JS[JS.index("const fit = () => {"):JS.index("// ?vvdebug=1")]
    assert re.search(r'const onChats = !!document\.getElementById\("chats"\);', fit)
    assert "keyboardOpen = !!typing && onChats;" in fit


def test_leftover_scroll_on_chats_is_reset_even_without_focus():
    # the field lost focus while iOS still had the page scrolled: the pinned page would sit shifted up again
    fit = JS[JS.index("const fit = () => {"):JS.index("// ?vvdebug=1")]
    assert "if (onChats && (window.scrollY > 0 || (keyboardOpen && vv.offsetTop > 0))) window.scrollTo(0, 0);" in fit


def test_a_tap_on_the_chat_box_focuses_it_without_scrolling_the_page():
    fit = JS[JS.index("if (window.visualViewport) {"):JS.index("const chatUnread")]
    guard = fit[fit.index('addEventListener("touchend"'):]
    assert "e.preventDefault()" in guard and "focus({ preventScroll: true })" in guard
    assert "document.activeElement !== ta" in guard, "a box that has focus keeps iOS caret placement and selection"
    assert "passive: false" in guard
    assert "new Event(\"click\")" in guard, "the @ picker still sees the tap"
    assert 'addEventListener("touchmove"' in fit, "a drag or scroll is not a tap"
    # every scripted focus of the box (reply, mention, after send) leaves the page where it is
    assert not re.search(r'(?:ta|#chat-text"\))\.focus\(\)', JS)
