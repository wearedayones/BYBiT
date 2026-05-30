"""Phase 1: signing parity — ports test/signer.test.ts.

HMAC parity is locked by a golden hex value produced by Node's ``crypto.createHmac``
on the same inputs — if the Python signer drifts, it fails.

RSA uses PKCS#1 v1.5 + SHA-256, which is *deterministic*: for the same key and message
Node's ``createSign('RSA-SHA256')`` and ``cryptography``'s ``sign(..., PKCS1v15(),
SHA256())`` produce byte-identical signatures (verified once out-of-band during the
port). We don't commit a private key (security rule), so here we generate a keypair at
runtime, sign, and verify with the matching public key — proving padding/hash are right.
"""

from __future__ import annotations

import base64
import re

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from bybit_agent.exchange.signer import (
    BybitAuth,
    build_get_param_str,
    build_headers,
    build_post_param_str,
    hmac_sign,
    rsa_sign,
)

API_KEY = "testkey123"
SECRET = "supersecret"  # test literal, not a credential
TS = 1700000000000

# Golden HMAC-SHA256 hex from Node on param_str = "{ts}{key}5000category=linear&symbol=BTCUSDT".
NODE_HMAC_GET = "d6d9c83cf1b7c479995be7a08640c1d48148c2d4fb3c8fbaa75c26e1a4ca2989"


def _fresh_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def test_build_get_param_str_follows_skill_spec():
    qs = "category=linear&symbol=BTCUSDT"
    assert build_get_param_str(TS, API_KEY, qs) == f"{TS}{API_KEY}5000{qs}"


def test_build_post_param_str_follows_skill_spec():
    body = '{"symbol":"BTCUSDT","side":"Buy"}'
    assert build_post_param_str(TS, API_KEY, body) == f"{TS}{API_KEY}5000{body}"


def test_hmac_matches_node_golden_value():
    param = build_get_param_str(TS, API_KEY, "category=linear&symbol=BTCUSDT")
    assert hmac_sign(SECRET, param) == NODE_HMAC_GET


def test_build_headers_hmac_omits_sign_type():
    param = build_get_param_str(TS, API_KEY, "")
    h = build_headers(BybitAuth(API_KEY, 1, secret=SECRET), TS, param, "bybit-skill/1.4.1", "bybit-skill")
    assert h["X-BAPI-API-KEY"] == API_KEY
    assert h["X-BAPI-TIMESTAMP"] == str(TS)
    assert h["X-BAPI-RECV-WINDOW"] == "5000"
    assert h["X-BAPI-SIGN"] == hmac_sign(SECRET, param)
    assert "X-BAPI-SIGN-TYPE" not in h
    assert h["User-Agent"] == "bybit-skill/1.4.1"
    assert h["X-Referer"] == "bybit-skill"


def test_different_bodies_produce_different_signatures():
    s1 = hmac_sign(SECRET, build_post_param_str(TS, API_KEY, '{"side":"Buy"}'))
    s2 = hmac_sign(SECRET, build_post_param_str(TS, API_KEY, '{"side":"Sell"}'))
    assert s1 != s2


def test_rsa_sign_is_base64_and_verifies_with_public_key():
    pem = _fresh_key_pem()
    param = build_post_param_str(TS, API_KEY, '{"symbol":"BTCUSDT"}')
    sig_b64 = rsa_sign(pem, param)
    assert re.match(r"^[A-Za-z0-9+/]+=*$", sig_b64)  # base64, not hex
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    key.public_key().verify(  # raises InvalidSignature on mismatch
        base64.b64decode(sig_b64), param.encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_rsa_sign_is_deterministic():
    # PKCS#1 v1.5 is deterministic — same key+message → identical signature.
    # This is what makes cross-language (Node↔Python) parity hold.
    pem = _fresh_key_pem()
    param = build_post_param_str(TS, API_KEY, '{"symbol":"BTCUSDT"}')
    assert rsa_sign(pem, param) == rsa_sign(pem, param)


def test_build_headers_rsa_sets_sign_type_2():
    pem = _fresh_key_pem()
    param = build_get_param_str(TS, API_KEY, "")
    h = build_headers(BybitAuth(API_KEY, 2, private_key=pem), TS, param, "bybit-skill/1.4.1", "bybit-skill")
    assert h["X-BAPI-SIGN-TYPE"] == "2"
    assert h["X-BAPI-SIGN"] == rsa_sign(pem, param)
