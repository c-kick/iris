"""Provider registry. Add an adapter here to make a new `type:` available."""

from __future__ import annotations

from typing import Any

import httpx

from .base import Provider
from .email import EmailProvider
from .pushover import PushoverProvider
from .webpush import WebPushProvider

REGISTRY: dict[str, type[Provider]] = {
    PushoverProvider.type: PushoverProvider,
    EmailProvider.type: EmailProvider,
    WebPushProvider.type: WebPushProvider,
}


def build_provider(name: str, settings: dict[str, Any], http: httpx.AsyncClient) -> Provider:
    ptype = settings.get("type")
    if ptype not in REGISTRY:
        raise ValueError(f"provider {name}: unknown type {ptype!r} (known: {', '.join(sorted(REGISTRY))})")
    return REGISTRY[ptype](name, settings, http)
