"""Browser push adapter: Web Push (RFC 8030) with aes128gcm encryption (RFC 8291) and VAPID (RFC 8292).

A push service accepts the message (HTTP 201) and nothing more; "shown" and "clicked" come back from the
service worker as reports to /push/report, authenticated by a per-delivery token (see `report_token`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..models import ACKNOWLEDGED, DELIVERED, Severity
from ..webpush_crypto import encrypt, load_private, unb64u, vapid_authorization
from .base import BASE_OBSERVABLE, AddressCheck, DeliveryPlan, OutboundMessage, Provider, SendResult

ALLOWED_PUSH_HOSTS = ("fcm.googleapis.com", "*.push.services.mozilla.com", "*.notify.windows.com",
                      "web.push.apple.com")
MAX_PLAINTEXT = 4096 - 86 - 16 - 1  # record size - header - GCM tag - padding delimiter
URGENCY = {
    Severity.min: "very-low",
    Severity.low: "low",
    Severity.normal: "normal",
    Severity.high: "high",
    Severity.critical: "high",
}
_BODY_LIMIT = 4000
MAX_RETRY_AFTER = 3600.0


class PayloadTooLarge(Exception):
    """The push message cannot be made small enough for one 4096-byte record."""


def push_endpoint_ok(url: str) -> bool:
    """https on a known push service host: a subscription endpoint is never fetched from anywhere else."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return False
    return any(host == h or (h.startswith("*.") and host.endswith(h[1:]) and len(host) > len(h) - 1)
               for h in ALLOWED_PUSH_HOSTS)


def _token(private_key: str, delivery_id: str) -> str:
    key = hashlib.sha256(unb64u(private_key)).digest()
    mac = hmac.new(key, delivery_id.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:16]).rstrip(b"=").decode()


class WebPushProvider(Provider):
    type = "webpush"
    kind = "push"
    label = "Browser push"
    max_concurrency = 4
    attachment_data = "none"  # a push payload carries no files
    secret_settings = frozenset({"vapid_private_key"})
    secret_address_fields = frozenset({"p256dh", "auth"})
    settings_schema = {
        "vapid_private_key": {"label": "VAPID private key", "kind": "secret",
                              "help": "Generated for you; browsers are enrolled against its public half."},
        "vapid_public_key": {"label": "VAPID public key", "kind": "text",
                             "help": "generated; changing it breaks every enrolled browser"},
        "contact": {"label": "Contact for push services", "kind": "text",
                    "help": "A mailto: address push services can reach, e.g. mailto:alex@example.org."},
        "default_ttl_seconds": {"label": "Keep undelivered messages for (seconds)", "kind": "int", "default": 86400},
    }
    address_schema = {
        "endpoint": {"label": "Push endpoint", "kind": "text", "help": "Set when a browser enrolls."},
        "p256dh": {"label": "Browser public key", "kind": "secret"},
        "auth": {"label": "Browser auth secret", "kind": "secret"},
        "user_agent": {"label": "Browser", "kind": "text"},
    }

    def __init__(self, name: str, settings: dict[str, Any], http: httpx.AsyncClient):
        super().__init__(name, settings, http)
        self.vapid_private_key: str = str(settings.get("vapid_private_key") or "")
        self.vapid_public_key: str = str(settings.get("vapid_public_key") or "")
        self.contact: str = str(settings.get("contact") or "")
        self.default_ttl_seconds = max(0, int(settings.get("default_ttl_seconds", 86400)))
        self._invalid: dict[str, str] = {}  # endpoint -> what the push service said at the last send
        unknown = set(settings) - {"type", "vapid_private_key", "vapid_public_key", "contact",
                                   "default_ttl_seconds"}
        self._unknown = sorted(unknown)

    # --- configuration ---
    def config_problems(self) -> list[str]:
        problems = []
        if not self.vapid_private_key or not self.vapid_public_key:
            problems.append("VAPID key pair is not set (vapid_private_key, vapid_public_key)")
        else:
            try:
                load_private(self.vapid_private_key)
            except ValueError:
                problems.append("vapid_private_key is not a valid P-256 private key")
        if not self.contact.startswith("mailto:"):
            problems.append("contact must start with mailto:")
        if self._unknown:
            problems.append(f"unknown settings: {', '.join(self._unknown)}")
        return problems

    def device_problems(self, address: dict[str, Any]) -> list[str]:
        missing = [f"address.{k} is not set" for k in ("endpoint", "p256dh", "auth") if not address.get(k)]
        if missing:
            return missing
        if not push_endpoint_ok(str(address["endpoint"])):
            return ["address.endpoint is not on a known browser push service (https required)"]
        return []

    # --- report authentication ---
    def report_token(self, delivery_id: str) -> str:
        """HMAC-SHA256 of the delivery id under SHA-256(VAPID private key), 22 base64url characters."""
        return _token(self.vapid_private_key, delivery_id)

    def report_ok(self, delivery_id: str, token: str) -> bool:
        if not self.vapid_private_key:
            return False
        return hmac.compare_digest(self.report_token(delivery_id).encode(), str(token).encode())

    # --- capabilities ---
    def capabilities(self) -> dict[str, Any]:
        return {
            "accept_confirmation": True,
            "delivery_receipt": {"supported": True, "note": "reported by the browser when the message is shown"},
            "read_receipt": {"supported": False},
            "acknowledgement": {"supported": True, "note": "reported when the notification is clicked"},
            "interactions": {"supported": False},
            "actions": {"max": 0, "inline_links": False},
            "attachments": {"max_count": 0},
            "links": {"max": 1},
            "cancel_after_dispatch": {"supported": False},
            "ttl": {"supported": True},
            "severities": [s.value for s in Severity],
        }

    def plan(self, msg: OutboundMessage, address: dict[str, Any]) -> DeliveryPlan:
        gaps = ["attachments: not supported on browser push"] if msg.attachments else []
        return DeliveryPlan(tracking="report", observable_states=BASE_OBSERVABLE + [DELIVERED, ACKNOWLEDGED],
                            capability_gaps=gaps)

    # --- wire format ---
    def build_payload(self, msg: OutboundMessage) -> bytes:
        url = msg.url or next((a["url"] for a in msg.actions if a.get("url")), None)
        data: dict[str, Any] = {
            "v": 1, "id": msg.delivery_id, "t": msg.title or "", "b": msg.message, "u": url,
            "s": msg.severity.value, "ri": msg.severity in (Severity.high, Severity.critical) or msg.require_ack,
            "r": f"{msg.external_url or ''}/push/report", "k": self.report_token(msg.delivery_id),
        }

        def dump() -> bytes:
            return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()

        def shrink(key: str) -> None:
            """Longest prefix of data[key] (plus an ellipsis) that makes the payload fit."""
            text = data[key]
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                data[key] = text[:mid] + "…"
                if len(dump()) <= MAX_PLAINTEXT:
                    lo = mid
                else:
                    hi = mid - 1
            data[key] = text[:lo] + "…"

        # trim the body first, then the title; a link that does not fit even without them is dropped (a cut link
        # would lead somewhere else), and then the text gets the room back
        if len(dump()) > MAX_PLAINTEXT:
            text = data["b"], data["t"]
            data["b"] = data["t"] = ""
            if len(dump()) > MAX_PLAINTEXT:
                data["u"] = None
            data["b"], data["t"] = text
        for key in ("b", "t"):
            if len(dump()) > MAX_PLAINTEXT and data[key]:
                shrink(key)
        raw = dump()
        if len(raw) > MAX_PLAINTEXT:
            raise PayloadTooLarge(f"browser push payload is {len(raw)} bytes even without text and link"
                                  f" (limit {MAX_PLAINTEXT})")
        return raw

    @staticmethod
    def _summary(address: dict[str, Any], ttl: int, urgency: str, size: int) -> dict[str, Any]:
        endpoint = str(address.get("endpoint") or "")
        host = urlsplit(endpoint).hostname or ""
        return {"endpoint": f"{host}/…{endpoint[-6:]}" if endpoint else None, "ttl": ttl, "urgency": urgency,
                "bytes": size}

    def address_problems(self, address: dict[str, Any]) -> list[str]:
        problem = self._invalid.get(str(address.get("endpoint") or ""))
        return [problem] if problem else []

    async def send(self, msg: OutboundMessage, address: dict[str, Any]) -> SendResult:
        problems = self.config_problems()
        urgency = URGENCY[msg.severity]
        ttl_left = msg.ttl_remaining(datetime.now(UTC))
        ttl = self.default_ttl_seconds if ttl_left is None else ttl_left
        summary = self._summary(address, ttl, urgency, 0)
        if problems:
            return SendResult(outcome="retryable", error="; ".join(problems), request_summary=summary)
        bad = self.device_problems(address)
        if bad:
            return SendResult(outcome="permanent", error="; ".join(bad), request_summary=summary)
        if not (msg.external_url or "").startswith("https://"):
            return SendResult(outcome="permanent", error="browser push needs an https external URL",
                              request_summary=summary)
        endpoint = str(address["endpoint"])
        try:
            payload = self.build_payload(msg)
        except PayloadTooLarge as e:
            return SendResult(outcome="permanent", error=str(e), request_summary=summary)
        try:
            body = encrypt(payload, str(address["p256dh"]), str(address["auth"]))
        except ValueError as e:
            return SendResult(outcome="permanent", request_summary=summary,
                              error=f"browser subscription keys are invalid ({e}): re-enroll this browser")
        summary["bytes"] = len(body)
        try:
            headers = {
                "TTL": str(ttl), "Urgency": urgency, "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "Authorization": vapid_authorization(endpoint, self.vapid_private_key, self.contact,
                                                     int(time.time())),
            }
            resp = await self.http.post(endpoint, content=body, headers=headers)
        except ValueError as e:  # the signing key cannot sign
            return SendResult(outcome="permanent", error=f"VAPID signing failed ({e})", request_summary=summary)
        except httpx.HTTPError as e:
            return SendResult(outcome="retryable", error=f"{type(e).__name__}: {e}", request_summary=summary)
        text = resp.text[:_BODY_LIMIT]
        code = resp.status_code
        if code in (201, 202):
            self._invalid.pop(endpoint, None)
            return SendResult(outcome="success", http_status=code, response_body=text, request_summary=summary,
                              provider_message_id=resp.headers.get("Location"), address_check=AddressCheck(True))
        error = f"HTTP {code}" + (f": {text[:200]}" if text else "")
        if code in (404, 410):
            problem = f"browser subscription is gone (HTTP {code}): re-enroll this browser"
            self._invalid[endpoint] = problem
            return SendResult(outcome="permanent", http_status=code, response_body=text, error=problem,
                              request_summary=summary, address_check=AddressCheck(False, problem))
        if code == 429:
            try:
                wait = float(resp.headers.get("Retry-After", ""))
            except ValueError:
                wait = math.nan
            retry_after = min(wait, MAX_RETRY_AFTER) if math.isfinite(wait) and wait >= 0 else None
            return SendResult(outcome="retryable", http_status=code, response_body=text, error=error,
                              retry_after=retry_after, request_summary=summary)
        if code == 413:
            error = "push service rejected the message as too large (HTTP 413)" + (f": {text[:200]}" if text else "")
        outcome = "permanent" if 400 <= code < 500 else "retryable"
        return SendResult(outcome=outcome, http_status=code, response_body=text, error=error,
                          request_summary=summary)

