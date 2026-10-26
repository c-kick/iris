from __future__ import annotations

from pathlib import Path

import pytest

from notify.config import ConfigError, load_config
from notify.models import Severity, derive_notification_status
from notify.routing import Router, RoutingError

EXAMPLE = Path(__file__).parent.parent / "config" / "notify.example.yaml"


def test_example_config_loads_and_reports_missing_env():
    cfg = load_config(EXAMPLE, env={})
    assert cfg.missing_env == ["PUSHOVER_APP_TOKEN", "PUSHOVER_USER_KEY", "SMTP_PASSWORD"]
    assert cfg.devices["alex-phone"].address["device"] == ""  # optional, defaults to all devices
    env = {"PUSHOVER_APP_TOKEN": "a" * 30, "PUSHOVER_USER_KEY": "u" * 30, "PUSHOVER_DEVICE": "iphone", "SMTP_PASSWORD": "s3cret"}
    cfg = load_config(EXAMPLE, env=env)
    assert cfg.missing_env == [] and cfg.providers["pushover"].app_token == "a" * 30  # type: ignore[attr-defined]
    assert cfg.devices["alex-phone"].address == {"user_key": "u" * 30, "device": "iphone"}


def test_unquoted_numeric_hash_is_explained(tmp_path):
    p = tmp_path / "n.yaml"
    p.write_text("clients:\n  x:\n    token_sha256: " + "1" * 64 + "\n    scopes: [send]\n")
    with pytest.raises(ConfigError, match="quoted string"):
        load_config(p, env={})


@pytest.mark.parametrize("name", ["admin-ui", "hnl-notify"])
def test_reserved_client_names(tmp_path, name):
    p = tmp_path / "n.yaml"
    p.write_text(f"clients:\n  {name}:\n    token_sha256: \"{'a' * 64}\"\n    scopes: [send]\n")
    with pytest.raises(ConfigError, match="reserved"):
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


def test_attachment_hosts_trailing_dot_stripped():
    from notify.config import ServerConfig
    assert ServerConfig(attachment_url_hosts=["HA.lan.", "ha.lan.:8123"]).attachment_url_hosts == ["ha.lan", "ha.lan:8123"]


def _cfg(**extra):
    from notify.config import NotifyConfig
    base = {"providers": {"mail": {"type": "email"}}, "users": {"alex": {"display_name": "Alex"}},
            "channels": {"infra": {"targets": ["user:alex"]}}}
    return NotifyConfig.model_validate({**base, **extra})


def test_cleanup_defaults():
    c = _cfg().cleanup_for("x")
    assert (c.enabled, c.processed_days, c.quarantine_days) == (True, 30, 90)
    assert (c.processed_max_mib, c.quarantine_max_mib, c.alert_targets) == (1024, 500, [])
    assert _cfg(mailbox_cleanup={"mail": {}}).cleanup_for("mail").processed_days == 30
    assert _cfg(mailbox_cleanup={"mail": {"processed_days": 0}}).cleanup_for("mail").processed_days == 0


@pytest.mark.parametrize("section", [
    {"nope": {}},
    {"mail": {"processed_days": -1}}, {"mail": {"quarantine_days": 3651}},
    {"mail": {"processed_max_mib": 1048577}}, {"mail": {"quarantine_max_mib": -5}},
    {"mail": {"surprise": 1}},
    {"mail": {"alert_targets": ["user:ghost"]}}, {"mail": {"alert_targets": ["channel:ghost"]}},
    {"mail": {"alert_targets": ["nonsense"]}},
])
def test_cleanup_section_rejected(section):
    with pytest.raises(ValueError):
        _cfg(mailbox_cleanup=section)


def test_cleanup_alert_targets_accept_channels():
    c = _cfg(mailbox_cleanup={"mail": {"alert_targets": ["channel:infra", "user:alex"]}})
    assert c.cleanup_for("mail").alert_targets == ["channel:infra", "user:alex"]
