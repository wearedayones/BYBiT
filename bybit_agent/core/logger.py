"""Structured logging — ports src/core/logger.ts (pino) to structlog.

Adds a redaction processor that strips anything resembling a secret or private key,
enforcing the unconditional rule: API secrets / RSA PEM contents never appear in logs.
Keys may only ever be shown as ``first5...last4`` (see ``mask_secret``).
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any

import structlog

# Field names whose VALUES must never be logged in full.
_SECRET_KEYS = {
    "api_secret",
    "bybit_api_secret",
    "secret",
    "private_key",
    "bybit_api_private_key",
    "password",
    "app_password",
    "report_email_app_password",
    "telegram_bot_token",
    "github_token",
    "authorization",
    "x-bapi-sign",
    "sign",
}

_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)


def mask_secret(value: str | None) -> str:
    """Display rule: first 5 + last 4 chars only. Never the full secret."""
    if not value:
        return "<unset>"
    if len(value) <= 9:
        return "*" * len(value)
    return f"{value[:5]}...{value[-4:]}"


def _redact(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict.keys()):
        if key.lower() in _SECRET_KEYS:
            event_dict[key] = "<redacted>"
        else:
            val = event_dict[key]
            if isinstance(val, str) and "PRIVATE KEY-----" in val:
                event_dict[key] = _PEM_RE.sub("<redacted-pem>", val)
    return event_dict


def configure_logging() -> None:
    is_dev = os.environ.get("NODE_ENV") != "production"
    level_name = os.environ.get("LOG_LEVEL", "info").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)
    # Quiet noisy third-party request logs (httpx logs every POST at INFO).
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _redact,
    ]
    processors.append(
        structlog.dev.ConsoleRenderer() if is_dev else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def child_logger(**bindings: Any) -> structlog.stdlib.BoundLogger:
    """Equivalent of childLogger({ module }) — returns a bound logger."""
    return structlog.get_logger().bind(**bindings)


configure_logging()
logger = structlog.get_logger()
