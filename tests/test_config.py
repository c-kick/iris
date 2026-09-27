from __future__ import annotations

from pathlib import Path

import pytest

from notify.config import ConfigError, load_config
from notify.models import Severity, derive_notification_status
from notify.routing import Router, RoutingError

EXAMPLE = Path(__file__).parent.parent / "config" / "notify.example.yaml"


def test_example_config_loads_and_reports_missing_env():
    cfg = load_config(EXAMPLE, env={})
    assert cfg.missing_env == ["PUSHOVER_APP_TOKEN", "PUSHOVER_USER_KEY"]
    assert cfg.devices["alex-phone"].address["device"] == ""  # optional, defaults to all devices
    env = {"PUSHOVER_APP_TOKEN": "a" * 30, "PUSHOVER_USER_KEY": "u" * 30, "PUSHOVER_DEVICE": "iphone"}
    cfg = load_config(EXAMPLE, env=env)
    assert cfg.missing_env == [] and cfg.providers["pushover"].app_token == "a" * 30  # type: ignore[attr-defined]
    assert cfg.devices["alex-phone"].address == {"user_key": "u" * 30, "device": "iphone"}


def test_unquoted_numeric_hash_is_explained(tmp_path):
    p = tmp_path / "n.yaml"
    p.write_text("clients:\n  x:\n    token_sha256: " + "1" * 64 + "\n    scopes: [send]\n")
    with pytest.raises(ConfigError, match="quoted string"):
        load_config(p, env={})


def test_bad_reference_rejected(tmp_path):
    p = tmp_path / "n.yaml"
    p.write_text("groups:\n  g:\n    members: [user:ghost]\n")
    with pytest.raises(ConfigError, match="unknown user 'ghost'"):
        load_config(p, env={})


def test_router_bare_names_and_ambiguity(tmp_path):
    p = tmp_path / "n.yaml"
    p.write_text(
        "providers: {p: {type: pushover}}\n"
        "devices: {phone: {provider: p, address: {user_key: x}}}\n"
        "users: {sam: {devices: [phone]}}\n"
        "groups: {sam: {members: [user:sam]}}\n")
    r = Router(load_config(p, env={}))
    assert list(r.resolve(["user:sam"], Severity.normal).devices) == ["phone"]
    assert list(r.resolve(["phone"], Severity.normal).devices) == ["phone"]
    with pytest.raises(RoutingError, match="ambiguous"):
        r.resolve(["sam"], Severity.normal)


@pytest.mark.parametrize("states,expected", [
    ([], "suppressed"),
    (["queued", "dispatched"], "pending"),
    (["dispatched"], "dispatched"),
    (["delivered", "dispatched"], "dispatched"),
    (["delivered"], "delivered"),
    (["acknowledged", "dispatched"], "acknowledged"),
    (["failed", "dispatched"], "partial"),
    (["expired"], "expired"),
    (["failed", "expired"], "failed"),
    (["cancelled"], "cancelled"),
    (["cancelled", "dispatched"], "dispatched"),
])
def test_status_rollup(states, expected):
    assert derive_notification_status(states) == expected
