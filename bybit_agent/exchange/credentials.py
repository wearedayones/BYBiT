"""Resolve Bybit auth from env — ports src/exchange/credentials.ts.

Auto-selects the signing method exactly as the skill specifies:
  - RSA key (path or inline PEM) set → RSA-SHA256 (sign type 2)
  - BYBIT_API_SECRET set            → HMAC-SHA256 (sign type 1)
  - both set                        → prefer RSA, warn once
  - neither                         → raise (caller halts before any signed call)

Security: the private-key PEM is read into memory and never logged; only the file
basename is ever surfaced.
"""

from __future__ import annotations

import os

from ..core.logger import child_logger
from .signer import BybitAuth

log = child_logger(module="credentials")


class _EnvLike:
    BYBIT_API_KEY: str
    BYBIT_API_SECRET: str | None
    BYBIT_API_PRIVATE_KEY_PATH: str | None
    BYBIT_API_PRIVATE_KEY: str | None


def resolve_bybit_auth(env: _EnvLike) -> BybitAuth:
    has_rsa = bool(env.BYBIT_API_PRIVATE_KEY_PATH) or bool(env.BYBIT_API_PRIVATE_KEY)
    has_hmac = bool(env.BYBIT_API_SECRET)

    if has_rsa and has_hmac:
        log.warning(
            "Both BYBIT_API_SECRET and an RSA key are set. Using RSA. "
            "To force HMAC, unset BYBIT_API_PRIVATE_KEY_PATH / BYBIT_API_PRIVATE_KEY."
        )

    if has_rsa:
        if env.BYBIT_API_PRIVATE_KEY:
            # Inline PEM — turn literal \n into real newlines (common via env var).
            private_key = env.BYBIT_API_PRIVATE_KEY.replace("\\n", "\n")
            log.info("Bybit auth: RSA (inline key)", signing="RSA-SHA256", key_source="env-var")
        else:
            path = env.BYBIT_API_PRIVATE_KEY_PATH or ""
            try:
                with open(path, encoding="utf-8") as fh:
                    private_key = fh.read()
            except OSError as exc:
                raise ValueError(
                    f"Could not read RSA private key at BYBIT_API_PRIVATE_KEY_PATH ({os.path.basename(path)})"
                ) from exc
            log.info("Bybit auth: RSA (file)", signing="RSA-SHA256", key_file=os.path.basename(path))

        if "PRIVATE KEY" not in private_key:
            raise ValueError(
                "BYBIT_API_PRIVATE_KEY / BYBIT_API_PRIVATE_KEY_PATH does not contain a PEM private key"
            )
        return BybitAuth(api_key=env.BYBIT_API_KEY, sign_type=2, private_key=private_key)

    if has_hmac:
        log.info("Bybit auth: HMAC", signing="HMAC-SHA256")
        return BybitAuth(api_key=env.BYBIT_API_KEY, sign_type=1, secret=env.BYBIT_API_SECRET)

    raise ValueError(
        "No Bybit credentials found. Set BYBIT_API_SECRET (HMAC) or BYBIT_API_PRIVATE_KEY_PATH (RSA)."
    )
