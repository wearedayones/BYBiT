"""Telegram delivery — ports src/reports/telegram.ts.

Telegram Bot API runs on api.telegram.org:443 (HTTPS), so it works in cloud
environments where SMTP is blocked. No proxy required. Both token and chat_id
come from env; nothing is hardcoded. HTML parse mode only (supports <b>, <i>,
<code>, <pre>, <a>).
"""
from __future__ import annotations

import os

import httpx

from bybit_agent.core.logger import get_logger

log = get_logger().bind(module="telegram")

TELEGRAM_API = "https://api.telegram.org"
MAX_LEN = 4096  # Telegram hard limit per message


def telegram_enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


async def send_telegram(text: str) -> bool:
    """Send a message to the configured chat. No-op when Telegram isn't configured."""
    if not telegram_enabled():
        return False

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    body = text[:MAX_LEN - 1] + "…" if len(text) > MAX_LEN else text
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": body,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        data = resp.json()
        if resp.status_code != 200 or not data.get("ok"):
            log.error("Telegram send failed", status=resp.status_code, desc=data.get("description"))
            return False
        return True
    except Exception as e:
        log.error("Telegram send failed", error=str(e))
        return False
