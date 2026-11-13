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
    assert "auto minmax(0, 1fr) auto;" in _rule(".chat-compose"), "paperclip, the field, and the FYI/Send column"
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
    assert "var(--read)" in _rule(".chat-compose textarea, .chat-compose .chat-marks")  # shared with the mention layer


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
    assert "roster.onclick" in thread and "chatAgentMenu(el" in thread, "the same action menu as on desktop"
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
    ret = JS.index("const chatOnReturn")
    assert "chatLastInput = Date.now()" in JS[ret:JS.index("document.addEventListener(\"visibilitychange\"", ret)]


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
    assert re.search(r'<div class="chat-paused" id="chat-paused" role="status" hidden></div></div>\s*<section class="pinned-confirmations"', JS)
    rule = _rule(".chat-paused")
    assert "position: absolute" in rule and "top: 100%" in rule
    assert ".chat-pane:has(.chat-paused:not([hidden])) .pinned-confirmations:not([hidden])" in CSS


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
    # window sizes never decide whether the keyboard is up (focus does); 0.20.1 uses them only once the keyboard is
    # known to be up, to tell the floating iOS bar (window unchanged) from the docked one (window shrinks)
    uses = [ln.strip() for ln in fit.splitlines() if "innerHeight" in ln and not ln.strip().startswith("//")]
    assert uses == ["const floatingBar = keyboardOpen && IOS && innerHeight - vv.height > 120;"], uses
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


def test_attaching_files_never_moves_the_thread():
    assert "position: absolute" in _rule(".chat-pending"), "pending files float over the thread"
    assert "position: absolute" in _rule(".chat-drop") and "pointer-events: none" in _rule(".chat-drop")
    fn = JS[JS.index("function chatFilesHtml"):JS.index("function chatMessageHtml")]
    assert "aspect-ratio:" in fn and 'width="${f.width}" height="${f.height}"' in fn, "an image's box is known before it loads"
    assert 'loading="lazy"' in fn
    assert "max-height: 280px" in _rule(".cm-img")


def test_attach_paste_and_drop_are_wired():
    fn = JS[JS.index("function chatAttach"):JS.index("function chatDiscard")]
    assert 'addEventListener("paste"' in fn and "clipboardData" in fn
    assert "ondrop" in fn and "ondragover" in fn
    assert 'method: "PUT"' in fn and '"X-Notify-Admin": "1"' in fn
    assert 'id="chat-file" multiple' in JS and 'id="chat-attach"' in JS


def test_a_file_only_message_has_no_empty_body_and_quotes_its_files():
    fn = JS[JS.index("function chatMessageHtml"):JS.index("function chatEarlierHtml")]
    assert 'm.text === "" && m.files?.length ? ""' in fn, "no empty text bubble above the files"
    assert "chatFilesSummary(m.files)" in fn, "the reply bar names the files"


def test_lines_under_the_pending_strip_stay_reachable():
    assert "height: var(--pending-h, 0px)" in _rule(".chats.has-pending .chat-msgs::after")
    fn = JS[JS.index("function chatAttach"):JS.index("function chatDiscard")]
    assert '"--pending-h"' in fn and '"has-pending"' in fn


def test_pending_files_review_fixes():
    fn = JS[JS.index("function chatAttach"):JS.index("function chatDropRoom")]
    assert "chats.pendingRender[gid] = render" in fn and "chats.pendingRender[gid]?.()" in fn, "an upload updates the strip on screen"
    assert '"over 10 MB in total"' in fn, "the server keeps 10 MiB pending: refuse before it drops one"
    assert "chatDropRoom" in JS[JS.index("async function chatTickOnce"):], "the room goes with new content, not on its own"
    assert 'getData("text/plain")' in fn, "pasted text wins over a picture of it"


def test_unsent_text_survives_leaving_and_stays_with_its_chat():
    assert "composeDraft" not in JS, "one draft per chat, never a shared one"
    fn = JS[JS.index("const DRAFT_KEY"):JS.index("const chatUnread")]
    assert "localStorage.setItem(DRAFT_KEY + gid" in fn and "catch { chatDrafts.stored = false; }" in fn
    assert 'addEventListener("beforeunload"' in fn, "where storage is blocked, or files are pending, leaving asks first"
    assert "chatDraftsForget(); boot();" in JS, "sign-out removes the drafts"
    assert "chatPendingLoad(gid)" in JS and "chatPendingSave(gid, list)" in JS, "uploaded but unsent files come back after a reload"
    # kept in memory on every key, written to storage within 400 ms and at once on blur, hide or leave
    assert "chatDrafts.mem[draftFor] = ta.value;" in JS and "setTimeout(saveNow, 400)" in JS
    assert 'ta.addEventListener("blur", saveNow)' in JS
    # one page-wide flush for the open composer only, cancelled by sign-out (no listener per thread)
    assert JS.count('addEventListener("pagehide"') == 1 and 'addEventListener("pagehide", () => chats.flushDraft?.())' in JS
    assert "if (chats.flushDraft === saveNow && ta.isConnected)" in JS
    assert "chats.flushDraft = null;" in JS[JS.index("function chatDraftsForget"):]


def test_composer_sits_above_the_floating_ios_keyboard_bar():
    fit = JS[JS.index("if (window.visualViewport) {"):JS.index("const chatUnread")]
    assert "const floatingBar = keyboardOpen && IOS && innerHeight - vv.height > 120;" in fit, \
        "only while typing on an iPhone with a window that did not shrink (the bar floats inside the viewport)"
    assert "const IOS = /iPhone|iPod/.test(navigator.userAgent);" in fit, "iPad has no floating bar over its keyboard"
    assert '`${Math.round(vv.height) - (floatingBar ? IOS_BAR : 0)}px`' in fit
    assert "const IOS_BAR = 60;" in fit, "measured: the bar covers the viewport's last ~57 px"


def test_agents_present_together_never_share_a_colour():
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not installed")
    fn = JS[JS.index("const CHAT_COLORS"):JS.index("function chatLearnColors")]
    # a real case: 7 joined, 3 left; of the 4 present ones the 3rd and the 7th to join got the same colour
    members = [
        {"id": "raven", "joined_at": "2026-11-08T20:36:01Z", "left_at": "2026-11-09T17:53:55Z"},
        {"id": "zinnia", "joined_at": "2026-11-08T20:37:10Z", "left_at": "2026-11-09T22:38:08Z"},
        {"id": "quail", "joined_at": "2026-11-09T17:21:20Z", "left_at": None},
        {"id": "bracken", "joined_at": "2026-11-09T17:43:17Z", "left_at": "2026-11-09T17:43:21Z"},
        {"id": "sorrel", "joined_at": "2026-11-09T17:53:55Z", "left_at": None},
        {"id": "larch", "joined_at": "2026-11-09T20:23:05Z", "left_at": None},
        {"id": "fern", "joined_at": "2026-11-09T20:41:11Z", "left_at": None},
    ]
    script = fn + f"; console.log(JSON.stringify(chatColorIndex({json.dumps(members)})));"
    out = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30).stdout)
    present = [out[m] for m in ("quail", "sorrel", "larch", "fern")]
    assert len(set(present)) == 4, out
    assert all(0 <= v < 6 for v in out.values())


def test_agents_are_told_to_notify_only_when_the_owner_is_needed():
    # the owner's standing rule (2026-11-10): no ping after every edit or commit
    root = UI.parent
    for f in ("chat.py", "mcp_server.py"):
        src = (root / f).read_text()
        assert "after any edit or commit" not in src.replace("\n", " ").lower()
        assert "only when you need them or there is something for" in src.replace("\n", " ")


def test_six_agents_present_at_once_all_differ():
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not installed")
    fn = JS[JS.index("const CHAT_COLORS"):JS.index("function chatLearnColors")]
    members = [{"id": f"m{i}", "joined_at": f"2026-11-10T10:0{i}:00Z", "left_at": None} for i in range(6)]
    out = json.loads(subprocess.run([node, "-e", fn + f"; console.log(JSON.stringify(chatColorIndex({json.dumps(members)})));"],
                                    capture_output=True, text=True, timeout=30).stdout)
    assert sorted(out.values()) == [0, 1, 2, 3, 4, 5]


def test_typing_stays_cheap_with_a_long_thread():
    fn = JS[JS.index("let shrink = true;"):JS.index("const at = chatAtPicker(el, ta")]
    assert "requestAnimationFrame(grow)" in fn, "the box and the mention layer once per frame, not per key"
    assert 'e.inputType.startsWith("delete") || ta.value.length < lastLen' in fn, "reset only when the text got shorter"
    marks = JS[JS.index("function chatMarksSync"):JS.index("function chatMention(")]
    assert 'text.includes("@")' in marks and "marks.dataset.size !== size" in marks
    assert "contain: layout paint style" in _rule(".chat-msgs")
    # the box is a layout boundary (an edit inside never reaches the page), its height measured on a hidden copy
    assert "contain: strict" in _rule(".chat-compose textarea#chat-text")
    assert "mirror.scrollHeight" in fn and 'if (ta.style.height !== h) ta.style.height = h;' in fn
