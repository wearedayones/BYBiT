"""Phase 1: auth resolution — ports the logic of src/exchange/credentials.ts."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from bybit_agent.exchange.credentials import resolve_bybit_auth


@dataclass
class FakeEnv:
    BYBIT_API_KEY: str = "key123"
    BYBIT_API_SECRET: str | None = None
    BYBIT_API_PRIVATE_KEY_PATH: str | None = None
    BYBIT_API_PRIVATE_KEY: str | None = None


def test_hmac_when_only_secret_set():
    auth = resolve_bybit_auth(FakeEnv(BYBIT_API_SECRET="sec"))
    assert auth.sign_type == 1
    assert auth.secret == "sec"
    assert auth.api_key == "key123"


def test_rsa_inline_pem_with_escaped_newlines():
    pem = "-----BEGIN PRIVATE KEY-----\\nABC\\n-----END PRIVATE KEY-----"
    auth = resolve_bybit_auth(FakeEnv(BYBIT_API_PRIVATE_KEY=pem))
    assert auth.sign_type == 2
    assert "\n" in auth.private_key  # escaped \n turned into real newlines
    assert "\\n" not in auth.private_key


def test_rsa_preferred_when_both_set():
    pem = "-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----"
    auth = resolve_bybit_auth(FakeEnv(BYBIT_API_SECRET="sec", BYBIT_API_PRIVATE_KEY=pem))
    assert auth.sign_type == 2


def test_rejects_inline_value_without_pem_marker():
    with pytest.raises(ValueError, match="PEM private key"):
        resolve_bybit_auth(FakeEnv(BYBIT_API_PRIVATE_KEY="not-a-key"))


def test_raises_when_no_credentials():
    with pytest.raises(ValueError, match="No Bybit credentials"):
        resolve_bybit_auth(FakeEnv())
