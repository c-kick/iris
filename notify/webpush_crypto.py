"""Web Push cryptography: RFC 8291 message encryption (aes128gcm) and VAPID (RFC 8292) signing."""

from __future__ import annotations

import base64
import json
import os
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

RECORD_SIZE = 4096
VAPID_LIFETIME = 12 * 3600


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _point(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def _load_private(private: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(unb64u(private), "big"), ec.SECP256R1())


def generate_vapid_keys() -> tuple[str, str]:
    """(private scalar, uncompressed public point), both base64url."""
    key = ec.generate_private_key(ec.SECP256R1())
    return b64u(key.private_numbers().private_value.to_bytes(32, "big")), b64u(_point(key))


def _hkdf(key: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(key)


def encrypt(
    plaintext: bytes,
    p256dh: str,
    auth: str,
    *,
    salt: bytes | None = None,
    sender_private: str | None = None,
) -> bytes:
    """RFC 8291 aes128gcm body for one record. `salt` and `sender_private` are test seams."""
    ua_public = unb64u(p256dh)
    sender = _load_private(sender_private) if sender_private else ec.generate_private_key(ec.SECP256R1())
    salt = salt if salt is not None else os.urandom(16)
    sender_public = _point(sender)
    shared = sender.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = _hkdf(shared, unb64u(auth), b"WebPush: info\x00" + ua_public + sender_public, 32)
    cek = _hkdf(ikm, salt, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(ikm, salt, b"Content-Encoding: nonce\x00", 12)
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(sender_public)]) + sender_public
    return header + AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)


def vapid_authorization(endpoint: str, private_key: str, contact: str, now: int) -> str:
    """`Authorization` header value: `vapid t=<ES256 JWT>, k=<public key>`."""
    parts = urlsplit(endpoint)
    segments = [
        b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()),
        b64u(
            json.dumps(
                {"aud": f"{parts.scheme}://{parts.netloc}", "exp": now + VAPID_LIFETIME, "sub": contact},
                separators=(",", ":"),
            ).encode()
        ),
    ]
    key = _load_private(private_key)
    r, s = decode_dss_signature(key.sign(".".join(segments).encode(), ec.ECDSA(hashes.SHA256())))
    segments.append(b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big")))
    return f"vapid t={'.'.join(segments)}, k={b64u(_point(key))}"
