"""Kill switch — cancel-all → flatten → Telegram → DB flag → raise KillSwitchError."""
from __future__ import annotations

import json
import time
from typing import Any

from bybit_agent.core.errors import KillSwitchError
from bybit_agent.core.logger import get_logger
from bybit_agent.persistence.db import NeonHttpClient

log = get_logger().bind(module="kill-switch")


class KillSwitch:
    def __init__(self, client: Any, db: NeonHttpClient) -> None:
        self._client = client
        self._db = db
        self._engaged = False

    def is_engaged(self) -> bool:
        return self._engaged

    async def engage(self, reason: str) -> None:
        if self._engaged:
            return
        self._engaged = True
        log.critical("KILL SWITCH ENGAGED", reason=reason)

        # Best-effort Telegram alert — never block the kill.
        try:
            from bybit_agent.reports.telegram import send_telegram
            import asyncio
            asyncio.create_task(
                send_telegram(
                    f"🔴 <b>KILL SWITCH ENGAGED</b>\nReason: {reason}\n"
                    "Flattening all positions and cancelling orders."
                )
            )
        except Exception:
            pass

        # Persist DB flag first so a crash-restart doesn't re-enter.
        try:
            await self._db.execute(
                """UPDATE agent_state
                   SET kill_engaged = true, kill_reason = $1, status = 'killed', updated_at = now()
                   WHERE id = 'singleton'""",
                reason,
            )
        except Exception as e:
            log.error("Failed to persist kill state", error=str(e))

        # Cancel all orders across all categories.
        for cat in ("spot", "linear", "inverse"):
            try:
                await self._client.cancel_all_orders(cat)
            except Exception:
                pass
        log.info("All orders cancelled")

        # Flatten all open linear positions.
        try:
            positions = await self._client.get_positions("linear")
            for pos in positions:
                if float(pos.get("size", 0)) <= 0:
                    continue
                side = "Sell" if pos["side"] == "Buy" else "Buy"
                try:
                    await self._client.place_order({
                        "category": "linear",
                        "symbol": pos["symbol"],
                        "side": side,
                        "orderType": "Market",
                        "qty": pos["size"],
                        "reduceOnly": True,
                        "orderLinkId": f"kill-{pos['symbol']}-{int(time.time() * 1000)}",
                    })
                except Exception as e:
                    log.error("Error flattening position", symbol=pos["symbol"], error=str(e))
            log.info("All positions flattened")
        except Exception as e:
            log.error("Error flattening positions", error=str(e))

        # Audit trail.
        try:
            await self._db.execute(
                """INSERT INTO risk_events (type, severity, detail, action_taken)
                   VALUES ('kill_level', 'critical', $1::jsonb, 'kill_switch_engaged')""",
                json.dumps({"reason": reason}),
            )
        except Exception:
            pass

        raise KillSwitchError(reason)
