"""Web Push crypto: RFC 8291 test vector, VAPID JWT verification, key shape."""

from __future__ import annotations

import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from notify.webpush_crypto import b64u, encrypt, generate_vapid_keys, unb64u, vapid_authorization

# RFC 8291 section 5 / appendix A
UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AUTH_SECRET = "BTBZMqHH6r4Tts7J_aSIgg"
AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
SALT = "DGv6ra1nlYgDCS1FRnbzlw"
BODY = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
    "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
    "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)


def test_rfc8291_appendix_a_vector():
    body = encrypt(
        b"When I grow up, I want to be a watermelon",
        UA_PUBLIC,
        AUTH_SECRET,
        salt=unb64u(SALT),
        sender_private=AS_PRIVATE,
    )
    assert b64u(body) == BODY


def test_vapid_jwt_verifies():
    priv, pub = generate_vapid_keys()
    header = vapid_authorization("https://fcm.googleapis.com/fcm/send/abc", priv, "mailto:alex@example.org", 1_000_000)
    assert header.startswith("vapid t=")
    token_part, key_part = header[len("vapid t="):].split(", k=")
    assert key_part == pub
    h, c, s = token_part.split(".")
    claims = json.loads(unb64u(c))
    assert json.loads(unb64u(h)) == {"typ": "JWT", "alg": "ES256"}
    assert claims == {"aud": "https://fcm.googleapis.com", "exp": 1_000_000 + 43200, "sub": "mailto:alex@example.org"}
    raw = unb64u(s)
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(pub))
    public.verify(der, f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))  # raises on failure


def test_generated_keys_shape():
    priv, pub = generate_vapid_keys()
    assert len(unb64u(priv)) == 32
    assert len(unb64u(pub)) == 65 and unb64u(pub)[0] == 0x04
    assert "=" not in priv + pub
