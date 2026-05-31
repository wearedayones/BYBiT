"""Phase 3: position health parity — ports test/positionHealth.test.ts."""
from __future__ import annotations

import pytest

from bybit_agent.market.market_data import Indicators, MarketSnapshot
from bybit_agent.positions.position_health import PositionHealthManager


def _snapshot(over: dict | None = None) -> MarketSnapshot:
    ind_kwargs = dict(
        ema9=110, ema21=105, ema50=100,  # bullish stack — no trend flip for a long
        rsi14=60, macdValue=0, macdSignal=0, macdHistogram=0,
        atr14=2.0, atrPct=0.02,
        boll={"upper": 0, "middle": 0, "lower": 0},
        adxValue=30, pdi=0, mdi=0,
    )
    if over:
        ind_kwargs.update(over)
    return MarketSnapshot(
        symbol="BTCUSDT", lastPrice=102, markPrice=102,
        fundingRate=0, nextFundingMs=0,
        ohlcv={"open": [], "high": [], "low": [], "close": [], "volume": []},
        indicators=Indicators(**ind_kwargs),
        orderbook={"bidDepth": 1, "askDepth": 1, "imbalance": 0},
    )


def _long_pos(**over) -> dict:
    base = dict(
        symbol="BTCUSDT", side="Buy", size="1.0",
        avgPrice="100", markPrice="102",
        liqPrice="0", unrealisedPnl="2", leverage="5",
        positionIdx=0, positionStatus="Normal",
        stopLoss="98", takeProfit="", trailingStop="",
    )
    base.update(over)
    return base


class FakeClient:
    """Minimal stand-in for PositionClient — records stops and orders."""
    def __init__(self, positions: list[dict]) -> None:
        self._positions = positions
        self.stops: list[dict] = []
        self.orders: list[dict] = []

    async def get_positions(self, category: str, symbol: str | None = None) -> list[dict]:
        return self._positions

    async def set_trading_stop(self, category: str, symbol: str, **opts) -> None:
        self.stops.append({"symbol": symbol, **opts})

    async def place_order(self, req: dict) -> dict:
        self.orders.append(req)
        return {"orderId": "x", "orderLinkId": req.get("orderLinkId", "x")}

    def set_positions(self, p: list[dict]) -> None:
        self._positions = p


@pytest.mark.asyncio
async def test_breakeven_and_partial_at_plus_1r():
    # entry=100, stop=98 → riskPerUnit=2; markPrice=102 → +1R
    client = FakeClient([_long_pos()])
    phm = PositionHealthManager(client)
    await phm.tick([_snapshot()])

    # Break-even stop set at or above entry.
    assert len(client.stops) > 0
    assert float(client.stops[-1]["stopLoss"]) >= 100

    # Partial TP: a reduceOnly Sell for half the size.
    partial = next(
        (o for o in client.orders if o.get("reduceOnly") and o["side"] == "Sell"), None
    )
    assert partial is not None
    assert abs(float(partial["qty"]) - 0.5) < 1e-6


@pytest.mark.asyncio
async def test_time_stop_closes_stale_flat_position():
    # markPrice == entry → flat; time exit fires after MAX_HOLD_CYCLES (30).
    client = FakeClient([_long_pos(markPrice="100")])
    phm = PositionHealthManager(client)
    for _ in range(32):
        await phm.tick([_snapshot()])

    full_close = next(
        (o for o in client.orders
         if o.get("reduceOnly") and "time_stop" in (o.get("orderLinkId") or "")),
        None,
    )
    assert full_close is not None
    assert abs(float(full_close["qty"]) - 1.0) < 1e-6


@pytest.mark.asyncio
async def test_regime_flip_exits_long():
    # Bearish EMA stack: ema9 < ema21 < ema50
    client = FakeClient([_long_pos(markPrice="100")])
    phm = PositionHealthManager(client)
    await phm.tick([_snapshot({"ema9": 90, "ema21": 95, "ema50": 100})])

    flip_close = next(
        (o for o in client.orders
         if o.get("reduceOnly") and "regime_flip" in (o.get("orderLinkId") or "")),
        None,
    )
    assert flip_close is not None


@pytest.mark.asyncio
async def test_no_double_partial():
    # Tick twice at +1R — partial should only fire once.
    client = FakeClient([_long_pos()])
    phm = PositionHealthManager(client)
    await phm.tick([_snapshot()])
    first_count = len(client.orders)
    await phm.tick([_snapshot()])
    assert len(client.orders) == first_count  # no second partial


@pytest.mark.asyncio
async def test_state_cleaned_when_position_closes():
    client = FakeClient([_long_pos()])
    phm = PositionHealthManager(client)
    await phm.tick([_snapshot()])
    assert "BTCUSDT-Buy" in phm._state

    # Exchange closed the position — next tick with empty list.
    client.set_positions([])
    await phm.tick([_snapshot()])
    assert "BTCUSDT-Buy" not in phm._state
