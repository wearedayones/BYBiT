"""Event detector — asyncio task that watches market conditions and fires triggers.

Runs as a cooperating task alongside the trading loop in `service.py`.
Fires market_event triggers when it observes:
  - Price move > PRICE_MOVE_PCT in one poll window
  - ATR% (volatility) above VOL_SPIKE_PCT
  - Funding rate > FUNDING_SPIKE_PCT (absolute)
  - The scheduled 24 h review timer

Does NOT fire risk_escalation or ambiguous_decision — those come from the loop.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from bybit_agent.core.logger import get_logger
from bybit_agent.events.triggers import trigger_market_event, trigger_scheduled_review
from bybit_agent.persistence.db import NeonHttpClient

log = get_logger().bind(module="events.detector")

# Thresholds
PRICE_MOVE_PCT  = 0.03   # 3% move in one poll window
VOL_SPIKE_PCT   = 0.05   # ATR% > 5%
FUNDING_SPIKE_PCT = 0.003  # funding rate > 0.3%
POLL_INTERVAL_S = 60     # check every 60 s (aligned with the trading cycle)
REVIEW_INTERVAL_S = 24 * 60 * 60  # 24 h


class EventDetector:
    def __init__(
        self,
        db: NeonHttpClient,
        market_getter: Any,          # MarketDataService or anything with get_snapshot()
        symbols: list[str],
    ) -> None:
        self._db = db
        self._market = market_getter
        self._symbols = symbols
        self._last_prices: dict[str, float] = {}
        self._last_review = 0.0
        self._running = False

    async def run(self) -> None:
        self._running = True
        log.info("Event detector started", symbols=self._symbols)
        while self._running:
            try:
                await self._poll()
            except Exception as e:
                log.error("Detector poll error", error=str(e))
            await asyncio.sleep(POLL_INTERVAL_S)

    def stop(self) -> None:
        self._running = False

    async def _poll(self) -> None:
        now = time.time()

        # Scheduled review timer.
        if now - self._last_review >= REVIEW_INTERVAL_S:
            self._last_review = now
            await trigger_scheduled_review(self._db)

        # Market condition checks.
        for symbol in self._symbols:
            try:
                snap = await self._market.get_snapshot(symbol, "linear")
            except Exception:
                continue

            price = snap.lastPrice
            prev = self._last_prices.get(symbol)
            self._last_prices[symbol] = price

            if prev and prev > 0:
                move = abs(price - prev) / prev
                if move >= PRICE_MOVE_PCT:
                    await trigger_market_event(
                        self._db,
                        symbol=symbol,
                        event_type="price_spike",
                        details={"prev": prev, "current": price, "move_pct": round(move * 100, 2)},
                    )

            ind = snap.indicators
            if ind.atrPct and ind.atrPct >= VOL_SPIKE_PCT:
                await trigger_market_event(
                    self._db,
                    symbol=symbol,
                    event_type="vol_spike",
                    details={"atrPct": ind.atrPct, "threshold": VOL_SPIKE_PCT},
                )

            if abs(snap.fundingRate) >= FUNDING_SPIKE_PCT:
                await trigger_market_event(
                    self._db,
                    symbol=symbol,
                    event_type="funding_spike",
                    details={"fundingRate": snap.fundingRate, "threshold": FUNDING_SPIKE_PCT},
                )
