"""Reply handles: the per-conversation secret that lets an agent collect replies and send follow-ups.

Form `<conversation id>:<secret>`. Only the SHA-256 of the secret is stored; the handle itself is never
written to a column, event, log record, error text or URL.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


def _sha(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_handle(conversation_id: str) -> tuple[str, str]:
    """(handle, sha256 hex of its secret)."""
    secret = secrets.token_urlsafe(24)
    return f"{conversation_id}:{secret}", _sha(secret)


def parse_handle(handle: str | None) -> tuple[str, str] | None:
    """(conversation id, sha256 hex of the secret) after stripping whitespace; None when malformed."""
    if not handle:
        return None
    conv, sep, secret = handle.strip().partition(":")
    if not sep or not conv or not secret or ":" in secret:
        return None
    return conv, _sha(secret)


def handle_matches(handle: str | None, conversation_id: str, stored_sha256: str | None) -> bool:
    parsed = parse_handle(handle)
    if parsed is None or not stored_sha256:
        return False
    conv, digest = parsed
    return conv == conversation_id and hmac.compare_digest(digest, stored_sha256)
