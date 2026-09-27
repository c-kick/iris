"""Provider adapter contract.

An adapter translates a provider-agnostic OutboundMessage into one provider's
API, declares what that provider can *observably* report, and parses whatever
feedback (receipts, callbacks) the provider offers. Nothing outside
notify/providers/ knows provider identifiers or wire formats.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import httpx

from ..models import CANCELLED, DISPATCHED, EXPIRED, FAILED, QUEUED, SENDING, Severity

Outcome = Literal["success", "retryable", "permanent"]

BASE_OBSERVABLE = [QUEUED, SENDING, DISPATCHED, FAILED, EXPIRED, CANCELLED]


@dataclass
class OutboundMessage:
    notification_id: str
    delivery_id: str
    title: str | None
    message: str
    severity: Severity
    url: str | None
    url_title: str | None
    actions: list[dict[str, Any]]
    tags: list[str]
    require_ack: bool
    created_at: datetime
    expires_at: datetime | None
    callback_url: str | None = None

    def ttl_remaining(self, now: datetime) -> int | None:
        if self.expires_at is None:
            return None
        return max(0, int((self.expires_at - now).total_seconds()))


@dataclass
class DeliveryPlan:
    """What this device can do for this particular message."""
    tracking: Literal["none", "receipt"]
    observable_states: list[str]
    capability_gaps: list[str] = field(default_factory=list)


@dataclass
class SendResult:
    outcome: Outcome
    http_status: int | None = None
    provider_message_id: str | None = None
    receipt: str | None = None
    response_body: str | None = None
    error: str | None = None
    retry_after: float | None = None
    request_summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReceiptStatus:
    delivered_at: datetime | None = None
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    expired: bool = False
    expires_at: datetime | None = None
    interaction: dict[str, Any] | None = None
    raw: str | None = None


class ProviderError(Exception):
    def __init__(self, message: str, http_status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.body = body


class Provider(ABC):
    type: str = ""
    label: str = ""
    max_concurrency: int = 2
    receipt_poll_seconds: float = 30
    # settings/address keys that are secrets: encrypted at rest, never returned by the admin API
    secret_settings: frozenset[str] = frozenset()
    secret_address_fields: frozenset[str] = frozenset()
    # editable fields for the admin UI: key -> {label, kind (text|secret|int), help}
    settings_schema: dict[str, dict[str, Any]] = {}
    address_schema: dict[str, dict[str, Any]] = {}

    def __init__(self, name: str, settings: dict[str, Any], http: httpx.AsyncClient):
        self.name = name
        self.settings = settings
        self.http = http

    # --- configuration ---
    @abstractmethod
    def config_problems(self) -> list[str]:
        """Human-readable reasons this provider cannot send (empty = ready)."""

    def device_problems(self, address: dict[str, Any]) -> list[str]:
        return []

    @property
    def configured(self) -> bool:
        return not self.config_problems()

    # --- capabilities ---
    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        """Declared, provider-wide capability document."""

    @abstractmethod
    def plan(self, msg: OutboundMessage, address: dict[str, Any]) -> DeliveryPlan:
        """Which states are observable and which requested features are unsupported."""

    # --- delivery ---
    @abstractmethod
    async def send(self, msg: OutboundMessage, address: dict[str, Any]) -> SendResult:
        ...

    async def poll_receipt(self, receipt: str) -> ReceiptStatus:
        raise ProviderError(f"{self.type} has no receipts")

    async def cancel(self, receipt: str) -> SendResult:
        return SendResult(outcome="permanent", error=f"{self.type} cannot cancel after dispatch")

    def can_cancel_after_dispatch(self, severity: Severity) -> bool:
        return False

    def parse_callback(self, form: dict[str, str]) -> tuple[str, ReceiptStatus] | None:
        return None

    async def validate_address(self, address: dict[str, Any]) -> dict[str, Any]:
        """Ask the provider whether an address is valid; may list its sub-devices."""
        return {"supported": False}

    def health(self) -> dict[str, Any]:
        return {"configured": self.configured, "problems": self.config_problems()}
