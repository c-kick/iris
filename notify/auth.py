"""Token authentication. Tokens are configured as SHA-256 hashes.

A token is normally sent as ``Authorization: Bearer <token>``. For senders that
can only do HTTP Basic auth (the *arr "Webhook" connection), the token may be
the Basic password; the username is ignored (or holds the token if the password is empty).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

from .config import NotifyConfig
from .core import Client


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return "ntk_" + secrets.token_urlsafe(32)


class Authenticator:
    def __init__(self, config: NotifyConfig):
        self._clients = [(name, c.token_sha256, frozenset(c.scopes)) for name, c in config.clients.items()]

    def from_header(self, header: str | None) -> Client | None:
        if not header:
            return None
        scheme, _, token = header.partition(" ")
        if scheme.lower() == "basic":
            try:
                user, _, password = base64.b64decode(token.strip(), validate=True).decode().partition(":")
            except (binascii.Error, UnicodeDecodeError):
                return None
            token = password or user
        elif scheme.lower() != "bearer":
            return None
        if not token.strip():
            return None
        digest = hash_token(token.strip())
        found = None
        for name, expected, scopes in self._clients:  # constant-time compare against every client
            if hmac.compare_digest(digest, expected):
                found = Client(name, scopes)
        return found
