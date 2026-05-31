"""Bybit V5 request signing — ports src/exchange/signer.ts.

Two methods, per the skill spec:
  HMAC-SHA256 — Bybit-generated key (you hold a Secret string). SIGN-TYPE 1, hex output.
  RSA-SHA256  — self-generated key (you uploaded a public key). SIGN-TYPE 2, base64 output.
``param_str`` (timestamp+apiKey+recvWindow+body) is identical for both; only the SIGN
computation and the X-BAPI-SIGN-TYPE header differ.

Parity target: byte-identical to Node's ``createHmac('sha256', …).digest('hex')`` and
``createSign('RSA-SHA256').sign(key, 'base64')`` (PKCS#1 v1.5 + SHA-256).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from ..config.constants import RECV_WINDOW


@dataclass(frozen=True)
class BybitAuth:
    api_key: str
    sign_type: int  # 1 = HMAC, 2 = RSA
    secret: str | None = None       # HMAC secret string (sign_type 1)
    private_key: str | None = None  # RSA private key PEM contents (sign_type 2)


def build_get_param_str(timestamp: int, api_key: str, query_string: str) -> str:
    return f"{timestamp}{api_key}{RECV_WINDOW}{query_string}"


def build_post_param_str(timestamp: int, api_key: str, json_body: str) -> str:
    return f"{timestamp}{api_key}{RECV_WINDOW}{json_body}"


def hmac_sign(secret: str, param_str: str) -> str:
    return hmac.new(secret.encode("utf-8"), param_str.encode("utf-8"), hashlib.sha256).hexdigest()


def rsa_sign(private_key_pem: str, param_str: str) -> str:
    """RSA-SHA256, PKCS#1 v1.5 padding (Node's default), base64-encoded."""
    key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    signature = key.sign(param_str.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
    return base64.b64encode(signature).decode("ascii")


def _sign(auth: BybitAuth, param_str: str) -> str:
    if auth.sign_type == 2:
        if not auth.private_key:
            raise ValueError("RSA signing selected but no private key provided")
        return rsa_sign(auth.private_key, param_str)
    if not auth.secret:
        raise ValueError("HMAC signing selected but no secret provided")
    return hmac_sign(auth.secret, param_str)


def build_headers(
    auth: BybitAuth,
    timestamp: int,
    param_str: str,
    user_agent: str,
    referer: str,
) -> dict[str, str]:
    headers = {
        "X-BAPI-API-KEY": auth.api_key,
        "X-BAPI-TIMESTAMP": str(timestamp),
        "X-BAPI-SIGN": _sign(auth, param_str),
        "X-BAPI-RECV-WINDOW": str(RECV_WINDOW),
        "User-Agent": user_agent,
        "X-Referer": referer,
        "Content-Type": "application/json",
    }
    if auth.sign_type == 2:
        headers["X-BAPI-SIGN-TYPE"] = "2"
    return headers
