"""Active management of open positions, ticked every cycle.

  - Break-even at +1R  — move the stop to entry (+ a cost buffer) once safe.
  - ATR trail at +2R   — trail the stop behind the high-water mark.
  - Partial TP at +1R  — bank half the position, let the rest run on the trail.
  - Time / regime exit — close stale, ~flat trades or ones the trend turned on.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from bybit_agent.config.constants import POSITION_MGMT
from bybit_agent.core.logger import get_logger
from bybit_agent.market.market_data import MarketSnapshot

log = get_logger().bind(module="position-health")


class PositionClient(Protocol):
    async def get_positions(self, category: str, symbol: str | None = None) -> list[dict[str, Any]]: ...
    async def set_trading_stop(
        self,
        category: str,
        symbol: str,
        **opts: Any,
    ) -> None: ...
    async def place_order(self, req: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class _PosState:
    entry: float
    risk_per_unit: float  # |entry - initial stop|
    hwm: float            # best favorable price (high for long, low for short)
    cycles_held: int = 0
    partial_taken: bool = False
    breakeven_set: bool = False


class PositionHealthManager:
    def __init__(self, client: PositionClient) -> None:
        self._client = client
        self._state: dict[str, _PosState] = {}

    async def tick(self, snapshots: list[MarketSnapshot]) -> None:
        snap_by_symbol = {s.symbol: s for s in snapshots}
        try:
            positions = await self._client.get_positions("linear")
        except Exception as e:
            log.error("Failed to fetch positions", error=str(e))
            return

        live_keys: set[str] = set()

        for pos in positions:
            size = float(pos.get("size", 0))
            if size <= 0:
                continue
            key = f"{pos['symbol']}-{pos['side']}"
            live_keys.add(key)

            snap = snap_by_symbol.get(pos["symbol"])
            if not snap:
                continue

            is_long = pos["side"] == "Buy"
            entry = float(pos.get("avgPrice") or snap.lastPrice)
            price = float(pos.get("markPrice") or snap.lastPrice)
            atr = snap.indicators.atr14

            # Initialize per-position memory on first sight.
            st = self._state.get(key)
            if st is None:
                raw_stop = float(pos.get("stopLoss") or 0)
                initial_stop = raw_stop if raw_stop > 0 else (
                    entry - atr * 2 if is_long else entry + atr * 2
                )
                risk = abs(entry - initial_stop) or atr * 2 or entry * 0.01
                st = _PosState(entry=entry, risk_per_unit=risk, hwm=price)
                self._state[key] = st

            st.cycles_held += 1
            st.hwm = max(st.hwm, price) if is_long else min(st.hwm, price)

            current_r = (
                (price - entry) / st.risk_per_unit
                if is_long
                else (entry - price) / st.risk_per_unit
            )

            # 1) Partial take-profit at +1R — bank half, let the rest ride.
            if not st.partial_taken and current_r >= POSITION_MGMT["PARTIAL_TP_AT_R"]:
                close_qty = _floor_to_str(size * POSITION_MGMT["PARTIAL_TP_FRACTION"], pos["size"])
                if float(close_qty) > 0:
                    await self._reduce(pos, close_qty, "partial_tp", current_r)
                    st.partial_taken = True

            # 2) Break-even at +1R — move stop to entry plus a cost buffer.
            if not st.breakeven_set and current_r >= POSITION_MGMT["BREAKEVEN_AT_R"]:
                buf = POSITION_MGMT["BREAKEVEN_BUFFER_PCT"]
                be = entry * (1 + buf) if is_long else entry * (1 - buf)
                await self._move_stop(pos, be, "breakeven")
                st.breakeven_set = True

            # 3) ATR trailing at +2R — only ever tightens the stop.
            if current_r >= POSITION_MGMT["TRAIL_START_R"]:
                mult = POSITION_MGMT["TRAIL_ATR_MULT"]
                trail = (st.hwm - atr * mult) if is_long else (st.hwm + atr * mult)
                cur_stop = float(pos.get("stopLoss") or 0)
                tighter = (trail > cur_stop) if is_long else (cur_stop == 0 or trail < cur_stop)
                if tighter:
                    await self._move_stop(pos, trail, "atr_trail")

            # 4) Time / regime exit.
            ind = snap.indicators
            trend_flipped = (
                (ind.ema9 < ind.ema21 and ind.ema21 < ind.ema50)
                if is_long
                else (ind.ema9 > ind.ema21 and ind.ema21 > ind.ema50)
            )
            stale = (
                st.cycles_held > POSITION_MGMT["MAX_HOLD_CYCLES"]
                and abs(current_r) < POSITION_MGMT["STALE_PNL_R"]
            )
            if trend_flipped or stale:
                reason = "regime_flip" if trend_flipped else "time_stop"
                await self._reduce(pos, pos["size"], reason, current_r)
                self._state.pop(key, None)

        # Forget positions that are no longer open.
        for key in list(self._state):
            if key not in live_keys:
                self._state.pop(key, None)

    async def _move_stop(self, pos: dict[str, Any], stop: float, reason: str) -> None:
        try:
            await self._client.set_trading_stop(
                "linear", pos["symbol"],
                stopLoss=str(round(stop, 2)),
                positionIdx=pos.get("positionIdx", 0),
            )
            log.info("Stop adjusted", symbol=pos["symbol"], side=pos["side"],
                     stop=round(stop, 2), reason=reason)
        except Exception as e:
            log.warning("Failed to adjust stop", symbol=pos["symbol"], reason=reason, error=str(e))

    async def _reduce(self, pos: dict[str, Any], qty: str, reason: str, r: float) -> None:
        try:
            side = "Sell" if pos["side"] == "Buy" else "Buy"
            await self._client.place_order({
                "category": "linear",
                "symbol": pos["symbol"],
                "side": side,
                "orderType": "Market",
                "qty": qty,
                "reduceOnly": True,
                "orderLinkId": f"phm-{reason}-{pos['symbol']}-{int(time.time() * 1000)}",
            })
            log.info("Position reduced", symbol=pos["symbol"], side=pos["side"],
                     qty=qty, reason=reason, r=round(r, 2))
        except Exception as e:
            log.warning("Failed to reduce position", symbol=pos["symbol"], reason=reason, error=str(e))


def _floor_to_str(qty: float, size_str: str) -> str:
    """Floor qty to the same decimal precision the position size string uses."""
    decimals = len((size_str.split(".")[1]) if "." in size_str else "")
    factor = 10 ** decimals
    return str(math.floor(qty * factor) / factor)
