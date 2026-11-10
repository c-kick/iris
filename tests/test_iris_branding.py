"""The product is called Iris (formerly hnl-notify): visible names say Iris, technical identifiers stay."""
from __future__ import annotations

from pathlib import Path

import httpx
from mcp import ClientSession

from notify import __version__
from notify.config import RESERVED_CLIENT_NAMES
from notify.core import SYSTEM_CLIENT
from tests.test_email import prov
from tests.test_mcp import run_session

ROOT = Path(__file__).resolve().parent.parent


def test_openapi_title_is_iris(live):
    spec = httpx.get(f"{live.url}/openapi.json").json()
    assert spec["info"]["title"] == "Iris" and spec["info"]["version"] == __version__


def test_mcp_server_identity_is_iris(live):
    async def flow(s: ClientSession):
        init = await s.initialize()
        return init.serverInfo.name, init.instructions

    name, instructions = run_session(live, "agent", flow)
    assert name == "Iris"
    assert instructions.startswith("Iris is the household notification service.")


def test_pages_and_ui_say_iris():
    assert "<title>Iris</title>" in (ROOT / "notify/admin_ui/index.html").read_text()
    js = (ROOT / "notify/admin_ui/app.js").read_text()
    assert "claude mcp add --transport http iris " in js and "docker logs iris " in js
    assert "<h1>hnl-notify</h1>" not in js and js.count('<h1 class="wordmark" aria-label="Iris">${wordmark}</h1>') == 2
    assert 'm.t || "Iris"' in (ROOT / "notify/push_ui/sw.js").read_text()


def test_decision_page_title_says_iris(live):
    r = httpx.get(f"{live.url}/admin/decide/req_missing", params={"k": "x"})
    assert "· Iris</title>" in r.text and '<h1 class="wordmark" aria-label="Iris"><svg' in r.text and "hnl-notify" not in r.text


def test_email_sender_default_is_iris_but_configured_names_stay():
    assert prov().from_name == "Iris"
    assert prov(from_name="hnl-notify").from_name == "hnl-notify"  # an explicitly configured name is kept
    assert prov(from_name="").from_name == ""


def test_technical_identifiers_are_kept():
    assert SYSTEM_CLIENT.id == "hnl-notify"  # persisted attribution of the service's own notifications
    assert RESERVED_CLIENT_NAMES == {"admin-ui", "hnl-notify"}
    code = [ln for ln in (ROOT / "notify/store.py").read_text().splitlines() if not ln.lstrip().startswith("#")]
    assert sum(ln.count('b"hnl-notify"') for ln in code) == 2  # encryption AAD: a format identifier, never changed
    compose = (ROOT / "compose.yaml").read_text()
    assert "container_name: iris" in compose and "image: iris:" in compose
    # no project name in the tracked file: a worktree or second clone must never become the production project
    assert not any(ln.startswith("name:") for ln in compose.splitlines())
    up = (ROOT / "docs/upgrading.md").read_text()
    assert "COMPOSE_PROJECT_NAME=hnl-notify" in up and "worktree" in up
    assert "\n  notify:\n" in compose  # service key kept


# ------------------------------------------------------------------ the Compass Hub identity
BRAND = ROOT / "notify/admin_ui/brand"


def test_brand_assets_are_shipped_and_linked():
    head = (ROOT / "notify/admin_ui/index.html").read_text()
    for name in ("iris-favicon-dark.svg", "favicon.ico", "favicon-32x32.png", "favicon-16x16.png",
                 "apple-touch-icon.png", "site.webmanifest"):
        assert (BRAND / name).stat().st_size > 0
        assert f'href="/admin/static/brand/{name}"' in head
    assert "data:image/svg+xml" not in head  # the old inline icon is gone
    import json
    manifest = json.loads((BRAND / "site.webmanifest").read_text())
    assert manifest["name"] == "Iris" and manifest["start_url"] == "/admin"
    for icon in manifest["icons"]:
        assert icon["src"].startswith("/admin/static/brand/") and (BRAND / icon["src"].rsplit("/", 1)[1]).exists()


def test_brand_files_are_served_without_signing_in(live):
    for name, ctype in (("iris-favicon-dark.svg", "image/svg+xml"), ("site.webmanifest", None), ("favicon.ico", None)):
        r = httpx.get(f"{live.url}/admin/static/brand/{name}")
        assert r.status_code == 200 and len(r.content) > 0
        if ctype:
            assert r.headers["content-type"].startswith(ctype)


def test_mark_and_wordmark_are_the_same_everywhere():
    from notify import registrations
    js = (ROOT / "notify/admin_ui/app.js").read_text()
    assert f"const logo = `{registrations.LOGO}`;" in js
    assert f"const wordmark = `{registrations.WORDMARK}`;" in js
    path = (BRAND / "iris-wordmark.svg").read_text().split(' d="', 1)[1].split('"', 1)[0]
    assert len(path) > 1000 and f'd="{path}"' in registrations.WORDMARK
    assert "<text" not in js.split("const wordmark")[1].split("\n")[0]  # outlines: no font is needed for it


def test_brand_colours_are_the_palette():
    css = (ROOT / "notify/admin_ui/app.css").read_text()
    light = css.split("@media (prefers-color-scheme: dark)")[0]
    assert "--ink: #0F1316;" in light and "--action: #087F79;" in light
    assert css.count("--paper: #0F1316;") == 2  # both dark blocks
    assert ".brand .mark .hub { fill: var(--action); }" in css
    enroll = (ROOT / "notify/push_ui/enroll.html").read_text()
    assert "--action: #087F79;" in enroll and "#0D6A71" not in enroll + css


def test_no_font_files_or_board_are_shipped():
    shipped = {p.suffix.lower() for p in (ROOT / "notify").rglob("*") if p.is_file()}
    assert not shipped & {".ttf", ".otf", ".woff", ".woff2"}
    assert not list(BRAND.glob("*Board*"))
