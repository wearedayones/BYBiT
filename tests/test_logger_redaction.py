"""Phase 0: the logger must never emit secrets or RSA PEM contents."""

from __future__ import annotations

from bybit_agent.core.logger import _redact, mask_secret


def test_mask_secret_shows_first5_last4():
    assert mask_secret("Fs7vvAbCdEfNl6W") == "Fs7vv...Nl6W"


def test_mask_secret_hides_short_values():
    assert mask_secret("short") == "*" * 5
    assert mask_secret("") == "<unset>"
    assert mask_secret(None) == "<unset>"


def test_redact_secret_keys():
    out = _redact(None, "info", {"api_secret": "supersecret", "msg": "ok"})
    assert out["api_secret"] == "<redacted>"
    assert out["msg"] == "ok"


def test_redact_telegram_and_github_tokens():
    out = _redact(None, "info", {"telegram_bot_token": "123:abc", "github_token": "ghp_x"})
    assert out["telegram_bot_token"] == "<redacted>"
    assert out["github_token"] == "<redacted>"


def test_redact_pem_in_freetext():
    pem = "-----BEGIN PRIVATE KEY-----\nABCDEF\n-----END PRIVATE KEY-----"
    out = _redact(None, "info", {"detail": f"loaded key {pem} done"})
    assert "ABCDEF" not in out["detail"]
    assert "<redacted-pem>" in out["detail"]
