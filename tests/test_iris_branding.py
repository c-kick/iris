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
    assert "<h1>hnl-notify</h1>" not in js and "<h1>Iris</h1>" in js
    assert 'm.t || "Iris"' in (ROOT / "notify/push_ui/sw.js").read_text()


def test_decision_page_title_says_iris(live):
    r = httpx.get(f"{live.url}/admin/decide/req_missing", params={"k": "x"})
    assert "· Iris</title>" in r.text and "<h1>Iris</h1>" in r.text and "hnl-notify" not in r.text


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
