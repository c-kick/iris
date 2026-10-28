"""Scheduling: time rules, request fields and schema (pure, no server)."""

import importlib.machinery
import importlib.util
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from notify import schedule
from notify.db import SCHEMA, Database
from notify.models import EVENT_TYPES, NotificationIn, ScheduleUpdate

AMS = ZoneInfo("Europe/Amsterdam")
NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


# One table for the server and the notify-wait script: forms with a unit mean the same in both
DURATIONS = [("90s", 90), ("45m", 2700), ("2h30m", 9000), ("1d", 86400), ("1H30M", 5400)]


def _notify_wait():
    path = Path(__file__).resolve().parent.parent / "scripts" / "notify-wait"
    loader = importlib.machinery.SourceFileLoader("notify_wait_rules", str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("notify_wait_rules", loader))
    loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("v,expected", DURATIONS + [(90, 90), ("90", 90)])
def test_parse_duration_accepts(v, expected):
    assert schedule.parse_duration(v) == expected


@pytest.mark.parametrize("v,expected", DURATIONS)
def test_notify_wait_parses_the_same_grammar(v, expected):
    assert _notify_wait().parse_duration(v) == expected


def test_bare_number_differs_by_design():
    assert _notify_wait().parse_duration("90") == 5400  # notify-wait: minutes (existing callers)
    assert schedule.parse_duration("90") == 90  # server delay: seconds


@pytest.mark.parametrize("v", [0, -5, "0", "-5m", "1.5h", "", "5 m", "1h1h", "m"])
def test_parse_duration_rejects(v):
    with pytest.raises(ValueError):
        schedule.parse_duration(v)


def test_to_utc_dst():
    # the autumn overlap resolves to the first (summer-time) occurrence
    assert schedule.to_utc(datetime(2026, 10, 25, 2, 30), AMS) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    with pytest.raises(ValueError, match="does not exist in Europe/Amsterdam"):
        schedule.to_utc(datetime(2026, 3, 29, 2, 30), AMS)
    aware = datetime(2026, 6, 1, 12, 0, tzinfo=datetime.fromisoformat("2026-01-01T00:00:00+05:00").tzinfo)
    assert schedule.to_utc(aware, AMS) == datetime(2026, 6, 1, 7, 0, tzinfo=UTC)


def test_resolve_send_at():
    assert schedule.resolve_send_at(None, None, NOW, AMS) is None
    assert schedule.resolve_send_at(NOW + timedelta(seconds=3), None, NOW, AMS) is None
    assert schedule.resolve_send_at(NOW - timedelta(seconds=30), None, NOW, AMS) is None
    with pytest.raises(ValueError, match="in the past"):
        schedule.resolve_send_at(NOW - timedelta(seconds=61), None, NOW, AMS)
    with pytest.raises(ValueError, match="at most 366 days"):
        schedule.resolve_send_at(NOW + timedelta(days=367), None, NOW, AMS)
    assert schedule.resolve_send_at(None, "2h", NOW, AMS) == NOW + timedelta(hours=2)
    with pytest.raises(ValueError):
        schedule.resolve_send_at(None, "0", NOW, AMS)


def test_send_at_and_delay_exclusive():
    base = {"targets": ["alex"], "message": "x"}
    with pytest.raises(ValidationError):
        NotificationIn(**base, send_at=NOW, delay=60)
    assert NotificationIn(**base, delay="2h").delay == "2h"
    with pytest.raises(ValidationError):
        NotificationIn(**base, max_late_seconds=59)


def test_schedule_update_rules():
    with pytest.raises(ValidationError):
        ScheduleUpdate(send_now=True, delay=60)
    with pytest.raises(ValidationError):
        ScheduleUpdate(send_at=NOW, delay=60)
    with pytest.raises(ValidationError):
        ScheduleUpdate(bogus=1)
    u = ScheduleUpdate(send_at=None, title="t")
    assert u.model_fields_set == {"send_at", "title"}


def test_event_types():
    for t in ("notification.scheduled", "notification.updated", "notification.missed"):
        assert t in EVENT_TYPES


def test_local_zone(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    assert schedule.local_zone().key == "Europe/Amsterdam"
    monkeypatch.setenv("TZ", "Nowhere/Bogus")
    assert schedule.local_zone().key == "UTC"
    monkeypatch.delenv("TZ")
    assert schedule.local_zone().key == "UTC"


def test_columns_migrate(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)  # the 0.7.0 schema: none of the scheduling columns
    conn.close()
    db = Database(path)
    have = {r[1] for r in db.query("PRAGMA table_info(notifications)")}
    assert {"send_at", "max_late_seconds", "deliver_by", "ttl_seconds", "explicit_expires_at",
            "released_at", "dismissed_at", "release_problem"} <= have
    assert db.scalar("SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_notifications_scheduled'") == 1
    db.close()
