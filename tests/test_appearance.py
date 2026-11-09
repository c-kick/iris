"""Appearance: sun-based dark mode for the admin account (off by default, no default place)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from tests.test_admin import H, fresh, setup_admin  # noqa: F401  (fixture re-export)

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "notify" / "admin_ui"
NEUTRAL = {"sun_dark": False, "latitude": None, "longitude": None}


def test_appearance_defaults_off_without_a_place(fresh):
    b = setup_admin(fresh())
    assert b.get("/admin/api/state").json()["appearance"] == NEUTRAL
    assert b.get("/admin/api/appearance").json() == NEUTRAL


def test_appearance_off_may_have_no_place_and_keeps_one_it_has(fresh):
    b = setup_admin(fresh())
    assert b.put("/admin/api/appearance", json=NEUTRAL, headers=H).json() == NEUTRAL
    kept = {"sun_dark": False, "latitude": 40.71, "longitude": -74.01}
    assert b.put("/admin/api/appearance", json=kept, headers=H).json() == kept


def test_appearance_saved_for_the_account_and_survives_restart(fresh):
    srv = fresh()
    b = setup_admin(srv)
    want = {"sun_dark": True, "latitude": -33.87, "longitude": 151.21}
    r = b.put("/admin/api/appearance", json=want, headers=H)
    assert r.status_code == 200 and r.json() == want
    srv.stop()
    srv2 = fresh()  # same database file
    b2 = httpx.Client(base_url=srv2.url, timeout=10)
    assert b2.post("/admin/api/login", json={"password": "correct horse battery"}, headers=H).status_code == 200
    assert b2.get("/admin/api/state").json()["appearance"] == want


@pytest.mark.parametrize("body", [
    {"sun_dark": True, "latitude": 91, "longitude": 5},
    {"sun_dark": True, "latitude": 52, "longitude": -180.5},
    {"sun_dark": "yes", "latitude": 52, "longitude": 5},
    {"sun_dark": True, "latitude": "52,03", "longitude": 5},
    {"sun_dark": True, "latitude": 52},
    {"sun_dark": True, "latitude": 52, "longitude": 5, "extra": 1},
    {"sun_dark": True, "latitude": None, "longitude": None},   # on needs a place
    {"sun_dark": False, "latitude": 52, "longitude": None},    # both or neither
])
def test_appearance_rejects_bad_places(fresh, body):
    b = setup_admin(fresh())
    assert b.put("/admin/api/appearance", json=body, headers=H).status_code == 422
    assert b.get("/admin/api/appearance").json() == NEUTRAL


def test_appearance_rejects_nan(fresh):
    b = setup_admin(fresh())
    raw = '{"sun_dark": true, "latitude": NaN, "longitude": 5}'
    r = b.put("/admin/api/appearance", content=raw, headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 422


def test_appearance_needs_admin_and_csrf(fresh):
    srv = fresh()
    b = setup_admin(srv)
    assert httpx.get(f"{srv.url}/admin/api/appearance").status_code == 401
    assert b.put("/admin/api/appearance", json={"sun_dark": True, "latitude": 52, "longitude": 5}).status_code in (400, 403)
    send_tok = b.post("/admin/api/clients/sender", json={"scopes": ["send"]}, headers=H).json()["token"]
    no = httpx.get(f"{srv.url}/admin/api/appearance", headers={"Authorization": f"Bearer {send_tok}"})
    assert no.status_code == 403


# ------------------------------------------------------------------ front end

def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block))


def _block(css: str, selector: str) -> str:
    i = css.index(selector)
    start = css.index("{", i + len(selector) - 1)
    depth, j = 0, start
    while True:
        depth += {"{": 1, "}": -1}.get(css[j], 0)
        if depth == 0:
            return css[start:j]
        j += 1


def test_css_forced_dark_matches_system_dark_and_light_can_be_forced():
    css = (UI / "app.css").read_text()
    system = _block(css, "@media (prefers-color-scheme: dark)")
    assert ':root:not([data-theme="light"])' in system  # a forced light theme wins over a dark system
    forced = _block(css, ':root[data-theme="dark"]')
    assert _tokens(system) and _tokens(system) == _tokens(forced)
    assert "color-scheme: dark" in forced


def test_page_loads_the_boot_script_before_the_stylesheet_and_app_imports_sun():
    html = (UI / "index.html").read_text()
    assert html.index('src="/admin/static/theme-boot.js"') < html.index("app.css")
    assert 'type="module"' not in html.split("theme-boot.js")[0].rsplit("<script", 1)[1]  # classic: runs before paint
    assert re.search(r'import \{[^}]*watchSun[^}]*\} from "\./sun\.js"', (UI / "app.js").read_text())


def test_sun_module_has_no_imports_so_it_can_be_copied():
    src = (UI / "sun.js").read_text()
    assert not re.search(r"^\s*import\b", src, re.M)


NODE = shutil.which("node")


def _node(script: str):
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True,
                         timeout=30, cwd=UI)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_sun_times_greenwich_match_reference_values():
    # London/Greenwich solstice times (rounded to the minute, Europe/London); NOAA equations: within 3 minutes.
    # At the March equinox the day is a little over 12 hours (refraction and the sun's disc).
    res = _node("""
        import { sunTimes, GREENWICH as G } from "./sun.js";
        const f = (d) => d.toLocaleTimeString("en-GB", { timeZone: "Europe/London", hour: "2-digit", minute: "2-digit" });
        const out = {};
        for (const day of ["2026-06-21", "2026-12-21"]) {
          const t = sunTimes(new Date(day + "T12:00:00Z"), G.latitude, G.longitude);
          out[day] = [f(t.sunrise), f(t.sunset)];
        }
        const eq = sunTimes(new Date("2026-03-20T12:00:00Z"), G.latitude, G.longitude);
        console.log(JSON.stringify({ out, G, equinoxMinutes: (eq.sunset - eq.sunrise) / 60000 }));""")
    assert res["G"] == {"latitude": 51.4779, "longitude": -0.0015}
    want = {"2026-06-21": ("04:43", "21:21"), "2026-12-21": ("08:04", "15:53")}

    def minutes(hhmm):
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    for day, (rise, sset) in want.items():
        got = res["out"][day]
        assert abs(minutes(got[0]) - minutes(rise)) <= 3 and abs(minutes(got[1]) - minutes(sset)) <= 3, (day, got)
    assert 720 <= res["equinoxMinutes"] <= 740


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_is_dark_and_next_change_around_the_clock_and_at_the_poles():
    res = _node("""
        import { isDarkAt, nextChange, sunTimes, validPlace } from "./sun.js";
        const G = [51.4779, -0.0015], S = [-33.87, 151.21], N = [78.22, 15.65];
        const at = (s) => new Date(s);
        console.log(JSON.stringify({
          greenwichNight: isDarkAt(at("2026-11-09T22:00:00Z"), ...G),
          greenwichNoon: isDarkAt(at("2026-11-09T12:00:00Z"), ...G),
          greenwichBeforeDawn: isDarkAt(at("2026-11-09T05:00:00Z"), ...G),
          next: nextChange(at("2026-11-09T22:00:00Z"), ...G).toISOString(),
          sydneyLocalNight: isDarkAt(at("2026-06-21T12:00:00Z"), ...S),
          polarDay: [sunTimes(at("2026-06-21T12:00:00Z"), ...N).polar, isDarkAt(at("2026-06-21T23:00:00Z"), ...N),
                     nextChange(at("2026-06-21T12:00:00Z"), ...N)],
          polarNight: [sunTimes(at("2026-12-21T12:00:00Z"), ...N).polar, isDarkAt(at("2026-12-21T12:00:00Z"), ...N)],
          valid: [validPlace(52, 5), validPlace(90, -180), validPlace(91, 0), validPlace(0, 181), validPlace(NaN, 0)],
        }));""")
    assert res["greenwichNight"] is True and res["greenwichNoon"] is False and res["greenwichBeforeDawn"] is True
    assert res["next"].startswith("2026-11-10T07:")  # the next sunrise, a little after 07:00 UTC
    assert res["sydneyLocalNight"] is True
    assert res["polarDay"] == ["day", False, None]
    assert res["polarNight"] == ["night", True]
    assert res["valid"] == [True, True, False, False, False]


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_boot_script_applies_a_cached_theme_only_while_it_is_valid():
    boot = (UI / "theme-boot.js").read_text()
    res = _node("""
        import { readFileSync } from "node:fs";
        const src = readFileSync("theme-boot.js", "utf8");
        const run = (cached, now) => {
          const store = new Map(cached ? [["iris.theme", JSON.stringify(cached)]] : []);
          const el = { dataset: {} };
          const ctx = { document: { documentElement: el }, Date: { now: () => now },
                        localStorage: { getItem: (k) => store.get(k) ?? null } };
          new Function("document", "Date", "localStorage", src)(ctx.document, ctx.Date, ctx.localStorage);
          return el.dataset.theme ?? null;
        };
        console.log(JSON.stringify([
          run({ theme: "dark", until: 2000 }, 1000), run({ theme: "dark", until: 2000 }, 3000),
          run({ theme: "light", until: 2000 }, 1000), run({ theme: "purple", until: 2000 }, 1000),
          run(null, 1000), run("garbage", 1000),
        ]));""")
    assert res == ["dark", None, "light", None, None, None]
    assert "try" in boot  # storage can throw (private mode, blocked site data)


# ------------------------------------------------------------------ agent colours vs status colours

def _rgb(hexv: str) -> tuple[float, float, float]:
    h = hexv.strip().lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _hue(hexv: str) -> float:
    import colorsys
    return colorsys.rgb_to_hls(*_rgb(hexv))[0] * 360


def _oklab(hexv: str) -> tuple[float, float, float]:
    r, g, b = (c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in _rgb(hexv))
    lms = [(.4122214708 * r + .5363325363 * g + .0514459929 * b) ** (1 / 3),
           (.2119034982 * r + .6806995451 * g + .1073969566 * b) ** (1 / 3),
           (.0883024619 * r + .2817188376 * g + .6299787005 * b) ** (1 / 3)]
    l, m, s_ = lms
    return (.2104542553 * l + .7936177850 * m - .0040720468 * s_,
            1.9779984951 * l - 2.4285922050 * m + .4505937099 * s_,
            .0259040371 * l + .7827717662 * m - .8086757660 * s_)


def _ok_hue(hexv: str) -> float:
    import math
    _, a, b = _oklab(hexv)
    return math.degrees(math.atan2(b, a)) % 360


def _lum(hexv: str) -> float:
    lin = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in _rgb(hexv)]
    return .2126 * lin[0] + .7152 * lin[1] + .0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + .05) / (lb + .05)


def _gap(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


STATUS = ("--ok", "--warn", "--bad", "--waiting", "--action", "--claude", "--codex")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_agent_colours_never_look_like_a_status_colour(theme):
    css = (UI / "app.css").read_text()
    tokens = _tokens(css[:css.index("@media (prefers-color-scheme: dark)")]) if theme == "light" \
        else _tokens(_block(css, ':root[data-theme="dark"]'))
    agents = [v.strip() for k, v in sorted(tokens.items()) if re.fullmatch(r"--a\d", k)]
    assert len(agents) >= 4
    js = (UI / "app.js").read_text()
    assert f"const CHAT_COLORS = {len(agents)};" in js
    import math
    for a in agents:
        for s in STATUS:  # perceptual (OKLCH) hue: a name never reads as a state
            assert _gap(_ok_hue(a), _ok_hue(tokens[s])) >= 30, (theme, a, s, tokens[s])
        assert _contrast(a, tokens["--surface"]) >= 4.5, (theme, a)  # names are text
    # and the agents look clearly different from each other (OKLab distance; the old violet-to-pink set was 0.08)
    labs = [_oklab(a) for a in agents]
    closest = min(math.dist(x, y) for i, x in enumerate(labs) for y in labs[i + 1:])
    assert closest >= .15, (theme, round(closest, 3))


def test_appearance_form_has_no_place_preset_button():
    # an open-source project: the form offers no button for one particular town; the place is typed in
    js = (UI / "app.js").read_text()
    form = js[js.index('<form class="stack" id="appearance">'):js.index("</form>", js.index('id="appearance"'))]
    assert form.count("<button") == 1 and 'type="submit"' in form


def test_no_personal_place_in_the_shipped_code_or_docs():
    # an open-source project: no default town, no preset for one
    for path in [*(ROOT / "notify").rglob("*.*"), *ROOT.glob("docs/*.md"), ROOT / "README.md"]:
        if path.suffix in (".py", ".js", ".css", ".html", ".md", ".yaml"):
            text = path.read_text(errors="replace").lower()
            assert "houten" not in text and "52.0281" not in text, path


def test_every_member_name_rendering_uses_the_agent_colour():
    # the narrow-screen header strip (chatMembersMini) showed names without their colour; desktop list and messages
    # had it. Both member renderings go through one helper.
    js = (UI / "app.js").read_text()

    def body(fn):
        i = js.index(f"function {fn}(")
        return js[i:js.index("\n}\n", i)]
    for fn in ("chatMembersMini", "chatMembersHtml"):
        assert "chatNameStyle(m)" in body(fn), fn
    assert "var(--a${" in body("chatNameStyle")
