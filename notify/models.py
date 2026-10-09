"""Core enums, state machine and API schemas (provider-agnostic)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Severity(str, Enum):
    min = "min"
    low = "low"
    normal = "normal"
    high = "high"
    critical = "critical"

    @property
    def rank(self) -> int:
        return SEVERITY_ORDER.index(self)


SEVERITY_ORDER = [Severity.min, Severity.low, Severity.normal, Severity.high, Severity.critical]


KINDS = ("push", "email")
KIND_OPT_IN_DEFAULT = {"push": False, "email": True}


class Scope(str, Enum):
    send = "send"
    read = "read"
    manage = "manage"
    admin = "admin"


# --- delivery state machine -------------------------------------------------

QUEUED = "queued"
SENDING = "sending"
DISPATCHED = "dispatched"
DELIVERED = "delivered"
ACKNOWLEDGED = "acknowledged"
INTERACTED = "interacted"
FAILED = "failed"
EXPIRED = "expired"
CANCELLED = "cancelled"

DELIVERY_STATES = [QUEUED, SENDING, DISPATCHED, DELIVERED, ACKNOWLEDGED, INTERACTED,
                   FAILED, EXPIRED, CANCELLED]

TRANSITIONS: dict[str, set[str]] = {
    QUEUED: {SENDING, CANCELLED, EXPIRED, FAILED},
    SENDING: {DISPATCHED, QUEUED, FAILED, EXPIRED, CANCELLED},
    DISPATCHED: {DELIVERED, ACKNOWLEDGED, INTERACTED, EXPIRED, CANCELLED},
    DELIVERED: {ACKNOWLEDGED, INTERACTED, EXPIRED, CANCELLED},
    ACKNOWLEDGED: {INTERACTED},
    INTERACTED: set(),
    FAILED: {QUEUED},  # manual retry
    EXPIRED: set(),
    CANCELLED: set(),
}

ACTIVE_STATES = {QUEUED, SENDING}
SUCCESS_STATES = {DISPATCHED, DELIVERED, ACKNOWLEDGED, INTERACTED}
FAILURE_STATES = {FAILED, EXPIRED}

EVENT_TYPES = [
    "notification.created",
    "notification.status_changed",
    "delivery.queued",
    "delivery.attempt_failed",
    "delivery.dispatched",
    "delivery.delivered",
    "delivery.acknowledged",
    "delivery.interacted",
    "delivery.failed",
    "delivery.expired",
    "delivery.cancelled",
]


def derive_notification_status(states: list[str], expired_before_dispatch: bool = False) -> str:
    """Roll delivery states up into one notification status."""
    if not states:
        return "suppressed"
    if all(s == CANCELLED for s in states):
        return "cancelled"
    live = [s for s in states if s != CANCELLED]
    if any(s in ACTIVE_STATES for s in live):
        return "pending"
    successes = [s for s in live if s in SUCCESS_STATES]
    failures = [s for s in live if s in FAILURE_STATES]
    if successes and failures:
        # A critical alert that reached the phone but expired unacknowledged is
        # still a "failure" of the acknowledgement it asked for; report partial.
        return "partial"
    if failures:
        return "expired" if all(s == EXPIRED for s in failures) else "failed"
    if any(s in (ACKNOWLEDGED, INTERACTED) for s in successes):
        return "acknowledged"
    if all(s == DELIVERED for s in successes):
        return "delivered"
    return "dispatched"


def event_matches(pattern: str, event_type: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return event_type.startswith(pattern[:-1])
    return pattern == event_type


def check_http_url(v: str) -> str:
    if not v.startswith(("http://", "https://")):
        raise ValueError("url must be http(s)")
    return v


def check_event_patterns(v: list[str]) -> list[str]:
    for e in v:
        if not any(event_matches(e, t) for t in EVENT_TYPES):
            raise ValueError(f"unknown event type {e!r}")
    return v or ["*"]


# --- API schemas ------------------------------------------------------------

class ActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=100)
    url: str | None = Field(default=None, max_length=2048)


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=2048)
    events: list[str] = Field(default_factory=lambda: ["*"])
    secret: str | None = Field(default=None, min_length=16, max_length=256)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return check_http_url(v)

    @field_validator("events")
    @classmethod
    def _events(cls, v: list[str]) -> list[str]:
        return check_event_patterns(v)


class NotificationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: list[str] = Field(min_length=1, max_length=50,
                               description="Logical targets: user:NAME, device:NAME, group:NAME, channel:NAME (bare names allowed when unambiguous)")
    message: str = Field(min_length=1, max_length=10000)
    title: str | None = Field(default=None, max_length=1000)
    severity: Severity = Severity.normal
    url: str | None = Field(default=None, max_length=2048)
    url_title: str | None = Field(default=None, max_length=200)
    actions: list[ActionIn] = Field(default_factory=list, max_length=10)
    data: dict[str, Any] | None = Field(default=None, description="Opaque metadata echoed back in status and events")
    tags: list[str] = Field(default_factory=list, max_length=20)
    source: str | None = Field(default=None, max_length=200)
    ttl_seconds: int | None = Field(default=None, ge=1, le=60 * 60 * 24 * 30)
    expires_at: datetime | None = None
    require_ack: bool = False
    idempotency_key: str | None = Field(default=None, max_length=200)
    webhook: WebhookIn | None = None
    dry_run: bool = False
    kinds: list[Literal["push", "email"]] | None = Field(
        default=None, description="Delivery kinds to use; omitted = default devices (push). email devices only receive when 'email' is listed")

    @field_validator("kinds")
    @classmethod
    def _kinds(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        v = list(dict.fromkeys(v))
        if not v:
            raise ValueError("kinds must name at least one of push, email")
        return v

    @field_validator("targets", mode="before")
    @classmethod
    def _coerce_targets(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [v]
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        for t in v:
            if not t or len(t) > 64:
                raise ValueError("tags must be 1-64 characters")
        return v


class SubscriptionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=2048)
    events: list[str] = Field(default_factory=lambda: ["*"])
    secret: str | None = Field(default=None, min_length=16, max_length=256)
    notification_id: str | None = None
    source: str | None = None
    tag: str | None = None
    description: str | None = Field(default=None, max_length=200)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return check_http_url(v)

    @field_validator("events")
    @classmethod
    def _events(cls, v: list[str]) -> list[str]:
        return check_event_patterns(v)
