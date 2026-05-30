"""Maker-first order placement with a taker fallback.

Entries are not usually time-critical, so we first rest a PostOnly limit
just inside the touch to pay the maker fee (0.020%) instead of the taker
fee (0.055%). If it does not fill within MAKER_WAIT_MS we cancel and cross
the spread with a market order.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from bybit_agent.config.constants import EXECUTION_DEFAULTS
from bybit_agent.core.logger import get_logger

log = get_logger().bind(module="execution")

FillType = Literal["maker", "taker"]


@dataclass
class EnterParams:
    category: str
    symbol: str
    side: str
    qty: float
    refPrice: float
    stopLoss: float | None = None
    takeProfit: float | None = None
    orderLinkId: str = ""


@dataclass
class EnterResult:
    orderId: str
    orderLinkId: str
    fillType: FillType
    limitPrice: float


class OrderClient(Protocol):
    async def place_order(self, req: dict[str, Any]) -> dict[str, Any]: ...
    async def cancel_order(self, category: str, symbol: str, order_id: str) -> None: ...
    async def get_open_orders(self, category: str, symbol: str | None = None) -> list[Any]: ...


class ExecutionRouter:
    """
    isSim: In simulation the PostOnly limit is assumed to fill as maker
           immediately; the live path waits and falls back to market.
    waitMs: Override the maker wait window (tests / sim use 0).
    """

    def __init__(
        self,
        client: OrderClient,
        is_sim: bool,
        wait_ms: int = EXECUTION_DEFAULTS["MAKER_WAIT_MS"],
    ) -> None:
        self._client = client
        self._is_sim = is_sim
        self._wait_ms = wait_ms

    async def enter(self, p: EnterParams) -> EnterResult:
        limit_price = self._maker_limit_price(p.side, p.refPrice)
        base: dict[str, Any] = {
            "category": p.category,
            "symbol": p.symbol,
            "side": p.side,
            "qty": str(p.qty),
        }
        if p.stopLoss is not None:
            base["stopLoss"] = str(p.stopLoss)
        if p.takeProfit is not None:
            base["takeProfit"] = str(p.takeProfit)

        # 1) Try maker — PostOnly limit just inside the touch.
        maker_req = {
            **base,
            "orderType": "Limit",
            "timeInForce": "PostOnly",
            "price": str(limit_price),
            "orderLinkId": f"{p.orderLinkId}-m",
        }
        maker_order = await self._client.place_order(maker_req)

        if self._is_sim:
            return EnterResult(
                orderId=maker_order["orderId"],
                orderLinkId=maker_order.get("orderLinkId", f"{p.orderLinkId}-m"),
                fillType="maker",
                limitPrice=limit_price,
            )

        # 2) Live: give the limit a chance to fill.
        if self._wait_ms > 0:
            await asyncio.sleep(self._wait_ms / 1000.0)

        try:
            open_orders = await self._client.get_open_orders(p.category, p.symbol)
            still_open = any(
                _order_link_id(o) == f"{p.orderLinkId}-m" for o in open_orders
            )
        except Exception:
            still_open = False

        if not still_open:
            return EnterResult(
                orderId=maker_order["orderId"],
                orderLinkId=maker_order.get("orderLinkId", f"{p.orderLinkId}-m"),
                fillType="maker",
                limitPrice=limit_price,
            )

        # 3) Taker fallback — cancel the resting limit and cross with a market order.
        try:
            await self._client.cancel_order(p.category, p.symbol, maker_order["orderId"])
        except Exception as e:
            log.warning("Failed to cancel unfilled maker order before fallback", symbol=p.symbol, error=str(e))

        taker_req = {
            **base,
            "orderType": "Market",
            "orderLinkId": f"{p.orderLinkId}-t",
        }
        taker_order = await self._client.place_order(taker_req)
        log.info("Maker order unfilled — fell back to taker", symbol=p.symbol, side=p.side)
        return EnterResult(
            orderId=taker_order["orderId"],
            orderLinkId=taker_order.get("orderLinkId", f"{p.orderLinkId}-t"),
            fillType="taker",
            limitPrice=p.refPrice,
        )

    def _maker_limit_price(self, side: str, ref_price: float) -> float:
        off = EXECUTION_DEFAULTS["MAKER_OFFSET_PCT"]
        raw = ref_price * (1 - off) if side == "Buy" else ref_price * (1 + off)
        return round(raw, 2)


def _order_link_id(o: Any) -> str | None:
    if isinstance(o, dict):
        return o.get("orderLinkId")
    return None
