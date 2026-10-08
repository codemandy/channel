"""
Check the innercity-life login token (copied from art_documentation_app/engine/artdoc/session_token.py,
a Python port of inner-city.life/lib/session-token.ts). Only api/index.py uses it.

The hub signs a session token (ES256 JWT) after a passkey login and stores it in the
`icl_auth` cookie on .innercity-life.com. This verifies the signature with the hub's
public key (AUTH_PUBLIC_JWK) and checks issuer, audience and expiry.
"""

from __future__ import annotations

import base64
import json
import time
from functools import lru_cache

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

TOKEN_ISSUER = "innercity-life"
TOKEN_AUDIENCE = "innercity-life"


def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


@lru_cache(maxsize=4)
def _public_key(jwk_json: str) -> ec.EllipticCurvePublicKey:
    jwk = json.loads(jwk_json)
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise ValueError("Expected an EC P-256 JWK")
    x = int.from_bytes(_b64url(jwk["x"]), "big")
    y = int.from_bytes(_b64url(jwk["y"]), "big")
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()


def verify_session_token(token: str | None, public_jwk_json: str | None, now: float | None = None) -> bool:
    """True only for an unexpired token signed by the hub's key. Never raises."""
    if not token or not public_jwk_json:
        return False
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        if json.loads(_b64url(header_b64)).get("alg") != "ES256":
            return False
        signature = _b64url(signature_b64)
        if len(signature) != 64:
            return False
        der = encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
        _public_key(public_jwk_json).verify(der, f"{header_b64}.{payload_b64}".encode(), ec.ECDSA(hashes.SHA256()))
        claims = json.loads(_b64url(payload_b64))
        exp = claims.get("exp")
        return (
            claims.get("iss") == TOKEN_ISSUER
            and claims.get("aud") == TOKEN_AUDIENCE
            and isinstance(exp, (int, float))
            and exp > (time.time() if now is None else now)
        )
    except (InvalidSignature, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
