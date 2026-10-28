"""Scheduled notifications: time rules (pure functions, no I/O)."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

IMMEDIATE_AHEAD = 5          # a send time this close (or already slightly past) means "now"
PAST_GRACE = 60              # further in the past than this is rejected
HORIZON_DAYS = 366
MAX_LATE_DEFAULT = 3600
MAX_SCHEDULED = 100          # per client
MAX_SCHEDULED_BYTES = 50 * 1024 * 1024
OVERDUE_SECONDS = 60
# the moment a notification actually went out, wherever a list or a window needs it
EFFECTIVE_SQL = "COALESCE(released_at, send_at, created_at)"

_DURATION = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", re.IGNORECASE)


def local_zone() -> ZoneInfo:
    """The household time zone: $TZ when set and valid, else UTC."""
    name = os.environ.get("TZ")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return ZoneInfo("UTC")


def parse_duration(v: int | str) -> int:
    """Seconds from a bare positive integer or a d/h/m/s string such as '2h30m'."""
    if isinstance(v, bool):
        raise ValueError("duration must be a positive number of seconds or like 2h30m")
    if isinstance(v, int):
        seconds = v
    else:
        s = str(v).strip() if isinstance(v, str) else ""
        if s.isascii() and s.isdigit():
            seconds = int(s)
        else:
            m = _DURATION.match(s)
            if not s or not m or not any(m.groups()):
                raise ValueError(f"invalid duration {v!r}: use seconds or a form like 90s, 45m, 2h30m, 1d")
            d, h, mi, sec = (int(g or 0) for g in m.groups())
            seconds = ((d * 24 + h) * 60 + mi) * 60 + sec
    if seconds <= 0:
        raise ValueError(f"duration must be greater than zero, got {v!r}")
    return seconds


def to_utc(dt: datetime, zone: ZoneInfo) -> datetime:
    """Aware datetimes are converted; naive ones are read as wall-clock time in `zone`."""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC)
    local = dt.replace(tzinfo=zone, fold=0)
    utc = local.astimezone(UTC)
    if utc.astimezone(zone).replace(tzinfo=None) != dt:  # round trip moved it: it fell in a DST gap
        raise ValueError(f"{dt.isoformat()} does not exist in {zone.key} (DST change)")
    return utc


def resolve_send_at(send_at: datetime | None, delay: int | str | None, now: datetime,
                    zone: ZoneInfo) -> datetime | None:
    """The UTC send time, or None when the notification should go out immediately."""
    if send_at is None and delay is None:
        return None
    if delay is not None:
        seconds = parse_duration(delay)
        if seconds > HORIZON_DAYS * 86400:
            raise ValueError(f"the send time may be at most {HORIZON_DAYS} days ahead")
        when = now + timedelta(seconds=seconds)
    else:
        when = to_utc(send_at, zone)
    ahead = (when - now).total_seconds()
    if ahead < -PAST_GRACE:
        raise ValueError("the send time is in the past")
    if ahead > HORIZON_DAYS * 86400:
        raise ValueError(f"the send time may be at most {HORIZON_DAYS} days ahead")
    if ahead <= IMMEDIATE_AHEAD:
        return None
    return when
