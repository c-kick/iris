"""Pushover adapter (https://pushover.net/api, checked 2026-09-27).

What Pushover reports back, and nothing more:
- every priority: synchronous acceptance (HTTP 200, `request` id);
- priority 2 (emergency) only: a receipt with last_delivered_at,
  acknowledged(_at/_by/_by_device), expired; optionally a callback POST to a
  publicly reachable URL on acknowledgement.
No read receipts, no action buttons, no interaction reports, no recall of
non-emergency messages.
"""

from __future__ import annotations

import base64
import html
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from ..models import ACKNOWLEDGED, DELIVERED, Severity
from .base import (BASE_OBSERVABLE, AttachmentData, DeliveryPlan, OutboundMessage, Provider, ProviderError,
                   ReceiptStatus, SendResult)

PRIORITY = {
    Severity.min: -2,
    Severity.low: -1,
    Severity.normal: 0,
    Severity.high: 1,
    Severity.critical: 2,
}

MAX_TITLE = 250
MAX_MESSAGE = 1024
MAX_URL = 512
MAX_URL_TITLE = 100
MAX_EXPIRE = 10800
MAX_ATTACHMENT = 5242880
MIN_RETRY = 30
_KEY_RE = re.compile(r"^[A-Za-z0-9]{30}$")
_BODY_LIMIT = 4000


def _ts(v: Any) -> datetime | None:
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(i, UTC) if i > 0 else None


class PushoverProvider(Provider):
    type = "pushover"
    kind = "push"
    label = "Pushover"
    max_concurrency = 2  # Pushover: no more than 2 concurrent connections
    attachment_data = "first_image"
    image_max_bytes = MAX_ATTACHMENT
    secret_settings = frozenset({"app_token"})
    secret_address_fields = frozenset({"user_key"})
    settings_schema = {
        "app_token": {"label": "Application API token", "kind": "secret",
                      "help": "Create an application at pushover.net/apps/build and paste its API token."},
        "emergency_retry_seconds": {"label": "Critical alert repeats every (seconds)", "kind": "int", "default": 60},
        "emergency_expire_seconds": {"label": "Critical alert stops repeating after (seconds)", "kind": "int",
                                     "default": 3600},
    }
    address_schema = {
        "user_key": {"label": "User key", "kind": "secret",
                     "help": "Shown at the top of your Pushover dashboard after logging in."},
        "device": {"label": "Pushover device", "kind": "text",
                   "help": "Leave empty to reach every device on this Pushover account."},
    }

    def __init__(self, name: str, settings: dict[str, Any], http: httpx.AsyncClient):
        super().__init__(name, settings, http)
        self.app_token: str = str(settings.get("app_token") or "")
        self.api_base: str = str(settings.get("api_base") or "https://api.pushover.net/1").rstrip("/")
        self.emergency_retry = max(MIN_RETRY, int(settings.get("emergency_retry_seconds", 60)))
        self.emergency_expire = min(MAX_EXPIRE, int(settings.get("emergency_expire_seconds", 3600)))
        self.receipt_poll_seconds = max(5.0, float(settings.get("receipt_poll_seconds", 30)))
        self.sounds: dict[str, str] = dict(settings.get("sounds") or {})
        self.quota: dict[str, Any] = {}
        unknown = set(settings) - {"type", "app_token", "api_base", "emergency_retry_seconds",
                                   "emergency_expire_seconds", "receipt_poll_seconds", "sounds"}
        self._unknown = sorted(unknown)

    # --- configuration ---
    def config_problems(self) -> list[str]:
        problems = []
        if not self.app_token:
            problems.append("app_token is not set (PUSHOVER_APP_TOKEN)")
        elif not _KEY_RE.match(self.app_token):
            problems.append("app_token does not look like a Pushover application token (30 alphanumerics)")
        if self._unknown:
            problems.append(f"unknown settings: {', '.join(self._unknown)}")
        return problems

    def device_problems(self, address: dict[str, Any]) -> list[str]:
        key = str(address.get("user_key") or "")
        if not key:
            return ["address.user_key is not set"]
        if not _KEY_RE.match(key):
            return ["address.user_key does not look like a Pushover user/group key"]
        return []

    # --- capabilities ---
    def capabilities(self) -> dict[str, Any]:
        return {
            "accept_confirmation": True,
            "delivery_receipt": {"supported": True, "when": "severity=critical",
                                 "note": "Pushover reports delivery only for emergency-priority messages"},
            "read_receipt": {"supported": False},
            "acknowledgement": {"supported": True, "when": "severity=critical",
                                "note": "emergency alerts repeat until acknowledged or expired"},
            "interactions": {"supported": False},
            "actions": {"max": 0, "inline_links": True,
                        "note": "no buttons; extra actions become tappable links in the message text "
                                "(Pushover app only, not the lock-screen banner); taps are not reported"},
            "attachments": {"max_count": 1, "max_bytes": MAX_ATTACHMENT, "types": ["image/*"]},
            "links": {"max": 1, "note": "one supplementary URL, opened on tap, not reported back"},
            "cancel_after_dispatch": {"supported": True, "when": "severity=critical",
                                      "note": "stops the repeating emergency alert only"},
            "ttl": {"supported": True, "when": "severity!=critical",
                    "note": "message auto-deleted from device after TTL"},
            "severities": [s.value for s in Severity],
            "max_title_length": MAX_TITLE,
            "max_message_length": MAX_MESSAGE,
            "receipt_poll_seconds": self.receipt_poll_seconds,
        }

    def plan(self, msg: OutboundMessage, address: dict[str, Any]) -> DeliveryPlan:
        critical = msg.severity == Severity.critical
        observable = list(BASE_OBSERVABLE)
        if critical:
            observable += [DELIVERED, ACKNOWLEDGED]
        gaps: list[str] = []
        if msg.require_ack and not critical:
            gaps.append("acknowledgement: pushover reports acknowledgement only at severity=critical; not tracked")
        if msg.actions:
            linked = self._link(msg)[0]
            parts = []
            for a in msg.actions:
                if a.get("url") and a["url"] == linked:
                    parts.append(f"action {a['id']!r} sent as the tap link")
                elif a.get("url"):
                    parts.append(f"action {a['id']!r} shown as a link in the message text")
                else:
                    parts.append(f"action {a['id']!r} dropped (no url)")
            gaps.append("actions: pushover has no buttons and does not report taps; " + "; ".join(parts))
        if msg.url and len(msg.url) > MAX_URL:
            gaps.append(f"links: url longer than {MAX_URL} characters; link dropped")
        if msg.title and len(msg.title) > MAX_TITLE:
            gaps.append(f"title truncated to {MAX_TITLE} characters")
        if len(msg.message) > MAX_MESSAGE:
            gaps.append(f"message truncated to {MAX_MESSAGE} characters")
        if critical and msg.expires_at is not None:
            gaps.append("ttl: not honoured by pushover at severity=critical; used as the alert's expire window instead")
        if msg.attachments:
            if any(a.size is None for a in msg.attachments):
                gaps.append("attachments: type and size unknown until sent; pushover carries one image ≤ 5 MB")
            else:
                n = len(msg.attachments) - (1 if self._image(msg) else 0)
                if n:
                    gaps.append(f"attachments: pushover carries one image ≤ 5 MB; {n} attachment(s) not sent")
        return DeliveryPlan(tracking="receipt" if critical else "none",
                            observable_states=observable, capability_gaps=gaps)

    def can_cancel_after_dispatch(self, severity: Severity) -> bool:
        return severity == Severity.critical

    # --- wire format ---
    @staticmethod
    def _link(msg: OutboundMessage) -> tuple[str | None, str | None]:
        if msg.url:
            return msg.url, msg.url_title
        for a in msg.actions:
            if a.get("url"):
                return a["url"], a.get("label")
        return None, None

    def _image(self, msg: OutboundMessage) -> AttachmentData | None:
        i = self.first_image(msg.attachments)
        return None if i is None else msg.attachments[i]

    def _body(self, msg: OutboundMessage) -> tuple[str, bool]:
        """Message text; URL actions other than the tap link become an HTML link line (Pushover app only)."""
        linked = self._link(msg)[0]
        inline = [a for a in msg.actions if a.get("url") and a["url"] != linked and len(a["url"]) <= MAX_URL]
        if not inline:
            return msg.message[:MAX_MESSAGE], False
        links = "   ".join(f'<a href="{html.escape(a["url"], quote=True)}">{html.escape(a.get("label") or a["id"])}</a>'
                           for a in inline)
        suffix = "\n\n" + links
        budget = MAX_MESSAGE - len(suffix)
        if budget < 40:
            return msg.message[:MAX_MESSAGE], False
        body = html.escape(msg.message, quote=False)
        if len(body) > budget:
            body = body[:budget]
            amp = body.rfind("&")
            if amp != -1 and ";" not in body[amp:]:
                body = body[:amp]  # never cut an entity in half
        return body + suffix, True

    def build_form(self, msg: OutboundMessage, address: dict[str, Any], now: datetime) -> dict[str, str]:
        message, as_html = self._body(msg)
        form: dict[str, str] = {
            "token": self.app_token,
            "user": str(address.get("user_key") or ""),
            "message": message,
            "priority": str(PRIORITY[msg.severity]),
            "timestamp": str(int(msg.created_at.timestamp())),
        }
        if as_html:
            form["html"] = "1"
        if address.get("device"):
            form["device"] = str(address["device"])
        if msg.title:
            form["title"] = msg.title[:MAX_TITLE]
        url, url_title = self._link(msg)
        if url and len(url) <= MAX_URL:
            form["url"] = url
            if url_title:
                form["url_title"] = url_title[:MAX_URL_TITLE]
        sound = self.sounds.get(msg.severity.value) or address.get("sound")
        if sound:
            form["sound"] = str(sound)
        ttl = msg.ttl_remaining(now)
        if msg.severity == Severity.critical:
            expire = self.emergency_expire if ttl is None else min(self.emergency_expire, ttl)
            form["retry"] = str(self.emergency_retry)
            form["expire"] = str(max(self.emergency_retry, min(MAX_EXPIRE, expire)))
            # tag with our notification id so cancel_by_tag remains possible out of band
            form["tags"] = msg.notification_id
            if msg.callback_url:
                form["callback"] = msg.callback_url
        elif ttl is not None and ttl > 0:
            form["ttl"] = str(ttl)
        image = self._image(msg)
        if image is not None and image.data is not None:
            form["attachment_base64"] = base64.b64encode(image.data).decode()
            form["attachment_type"] = image.content_type
        return form

    @staticmethod
    def _redact(form: dict[str, str]) -> dict[str, Any]:
        out: dict[str, Any] = {k: v for k, v in form.items()
                               if k not in ("token", "user", "message", "attachment_base64")}
        if "attachment_base64" in form:
            b64 = form["attachment_base64"]
            out["attachment_bytes"] = len(b64) * 3 // 4 - b64.count("=", -2)
        out["user"] = f"…{form.get('user', '')[-4:]}" if form.get("user") else None
        out["message_chars"] = len(form.get("message", ""))
        return out

    def _record_quota(self, resp: httpx.Response) -> None:
        h = resp.headers
        if "X-Limit-App-Remaining" in h:
            self.quota = {
                "limit": int(h.get("X-Limit-App-Limit", 0) or 0),
                "remaining": int(h.get("X-Limit-App-Remaining", 0) or 0),
                "reset_at": _ts(h.get("X-Limit-App-Reset")),
                "observed_at": datetime.now(UTC),
            }

    async def send(self, msg: OutboundMessage, address: dict[str, Any]) -> SendResult:
        now = datetime.now(UTC)
        problems = self.config_problems() + self.device_problems(address)
        form = self.build_form(msg, address, now)
        summary = self._redact(form)
        if problems:
            # configuration can be fixed and the service restarted; keep the message queued
            return SendResult(outcome="retryable", error="; ".join(problems), request_summary=summary)
        try:
            resp = await self.http.post(f"{self.api_base}/messages.json", data=form)
        except httpx.HTTPError as e:
            return SendResult(outcome="retryable", error=f"{type(e).__name__}: {e}", request_summary=summary)
        self._record_quota(resp)
        body = resp.text[:_BODY_LIMIT]
        data: dict[str, Any] = {}
        try:
            data = resp.json()
        except ValueError:
            pass
        if resp.status_code == 200 and data.get("status") == 1:
            return SendResult(outcome="success", http_status=200, provider_message_id=data.get("request"),
                              receipt=data.get("receipt"), response_body=body, request_summary=summary)
        errors = "; ".join(data.get("errors") or []) or f"HTTP {resp.status_code}"
        if resp.status_code == 429:
            reset = _ts(resp.headers.get("X-Limit-App-Reset"))
            retry_after = max(60.0, (reset - now).total_seconds()) if reset else None
            return SendResult(outcome="retryable", http_status=429, response_body=body,
                              error=f"quota exhausted: {errors}", retry_after=retry_after, request_summary=summary)
        if 400 <= resp.status_code < 500:
            return SendResult(outcome="permanent", http_status=resp.status_code, response_body=body,
                              error=errors, request_summary=summary)
        return SendResult(outcome="retryable", http_status=resp.status_code, response_body=body,
                          error=errors, request_summary=summary)

    async def poll_receipt(self, receipt: str) -> ReceiptStatus:
        try:
            resp = await self.http.get(f"{self.api_base}/receipts/{receipt}.json", params={"token": self.app_token})
        except httpx.HTTPError as e:
            raise ProviderError(f"{type(e).__name__}: {e}") from e
        body = resp.text[:_BODY_LIMIT]
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200 or data.get("status") != 1:
            raise ProviderError("; ".join(data.get("errors") or []) or f"HTTP {resp.status_code}",
                                http_status=resp.status_code, body=body)
        return ReceiptStatus(
            delivered_at=_ts(data.get("last_delivered_at")),
            acknowledged_at=_ts(data.get("acknowledged_at")) if data.get("acknowledged") else None,
            acknowledged_by=(data.get("acknowledged_by_device") or data.get("acknowledged_by") or None)
            if data.get("acknowledged") else None,
            expired=bool(data.get("expired")),
            expires_at=_ts(data.get("expires_at")),
            raw=body,
        )

    async def cancel(self, receipt: str) -> SendResult:
        try:
            resp = await self.http.post(f"{self.api_base}/receipts/{receipt}/cancel.json",
                                        data={"token": self.app_token})
        except httpx.HTTPError as e:
            return SendResult(outcome="retryable", error=f"{type(e).__name__}: {e}")
        body = resp.text[:_BODY_LIMIT]
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 200 and data.get("status") == 1:
            return SendResult(outcome="success", http_status=200, response_body=body)
        return SendResult(outcome="permanent" if 400 <= resp.status_code < 500 else "retryable",
                          http_status=resp.status_code, response_body=body,
                          error="; ".join(data.get("errors") or []) or f"HTTP {resp.status_code}")

    def parse_callback(self, form: dict[str, str]) -> tuple[str, ReceiptStatus] | None:
        receipt = form.get("receipt")
        if not receipt or str(form.get("acknowledged")) != "1":
            return None
        return receipt, ReceiptStatus(
            acknowledged_at=_ts(form.get("acknowledged_at")) or datetime.now(UTC),
            acknowledged_by=form.get("acknowledged_by_device") or form.get("acknowledged_by"),
            raw=str({k: v for k, v in form.items() if k != "acknowledged_by"}),
        )

    async def validate_address(self, address: dict[str, Any]) -> dict[str, Any]:
        problems = self.config_problems() + self.device_problems(address)
        if problems:
            return {"supported": True, "valid": False, "error": "; ".join(problems)}
        form = {"token": self.app_token, "user": str(address.get("user_key"))}
        if address.get("device"):
            form["device"] = str(address["device"])
        try:
            resp = await self.http.post(f"{self.api_base}/users/validate.json", data=form)
        except httpx.HTTPError as e:
            return {"supported": True, "valid": False, "error": f"could not reach Pushover: {type(e).__name__}"}
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 200 and data.get("status") == 1:
            return {"supported": True, "valid": True, "devices": data.get("devices") or [],
                    "is_group": bool(data.get("group"))}
        return {"supported": True, "valid": False,
                "error": "; ".join(data.get("errors") or []) or f"Pushover answered HTTP {resp.status_code}"}

    def health(self) -> dict[str, Any]:
        h = super().health()
        if self.quota:
            q = dict(self.quota)
            q["reset_at"] = q["reset_at"].isoformat() if q.get("reset_at") else None
            q["observed_at"] = q["observed_at"].isoformat()
            h["quota"] = q
        return h
