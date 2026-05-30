"""Phase 3: execution router — maker fill path and maker→taker fallback."""
from __future__ import annotations

import pytest

from bybit_agent.execution.execution_router import EnterParams, ExecutionRouter


class FakeOrderClient:
    def __init__(self, *, still_open: bool = False) -> None:
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self._still_open = still_open  # controls whether the maker limit is still resting

    async def place_order(self, req: dict) -> dict:
        self.placed.append(req)
        return {"orderId": f"ord-{len(self.placed)}", "orderLinkId": req.get("orderLinkId", "")}

    async def cancel_order(self, category: str, symbol: str, order_id: str) -> None:
        self.cancelled.append(order_id)

    async def get_open_orders(self, category: str, symbol: str | None = None) -> list[dict]:
        if self._still_open:
            # Return the maker order so the router sees it's still resting.
            return [{"orderLinkId": "entry-m"}]
        return []


def _params(**over) -> EnterParams:
    base = EnterParams(
        category="linear", symbol="BTCUSDT", side="Buy",
        qty=0.1, refPrice=100.0, orderLinkId="entry",
    )
    for k, v in over.items():
        object.__setattr__(base, k, v)
    return base


@pytest.mark.asyncio
async def test_sim_always_maker():
    client = FakeOrderClient()
    router = ExecutionRouter(client, is_sim=True, wait_ms=0)
    result = await router.enter(_params())

    assert result.fillType == "maker"
    assert len(client.placed) == 1
    assert client.placed[0]["timeInForce"] == "PostOnly"


@pytest.mark.asyncio
async def test_live_maker_fills_before_wait():
    # open_orders returns empty → maker filled → no taker fallback
    client = FakeOrderClient(still_open=False)
    router = ExecutionRouter(client, is_sim=False, wait_ms=0)
    result = await router.enter(_params())

    assert result.fillType == "maker"
    assert len(client.placed) == 1
    assert not client.cancelled


@pytest.mark.asyncio
async def test_live_taker_fallback_when_still_open():
    # open_orders returns the resting limit → router cancels and fires market
    client = FakeOrderClient(still_open=True)
    router = ExecutionRouter(client, is_sim=False, wait_ms=0)
    result = await router.enter(_params())

    assert result.fillType == "taker"
    assert len(client.placed) == 2  # limit then market
    assert client.placed[1]["orderType"] == "Market"
    assert len(client.cancelled) == 1  # the resting limit was cancelled


@pytest.mark.asyncio
async def test_maker_limit_price_buy_is_below_ref():
    client = FakeOrderClient()
    router = ExecutionRouter(client, is_sim=True, wait_ms=0)
    result = await router.enter(_params(side="Buy", refPrice=100.0))
    assert result.limitPrice < 100.0


@pytest.mark.asyncio
async def test_maker_limit_price_sell_is_above_ref():
    client = FakeOrderClient()
    router = ExecutionRouter(client, is_sim=True, wait_ms=0)
    result = await router.enter(_params(side="Sell", refPrice=100.0))
    assert result.limitPrice > 100.0


@pytest.mark.asyncio
async def test_stop_loss_and_take_profit_forwarded():
    client = FakeOrderClient()
    router = ExecutionRouter(client, is_sim=True, wait_ms=0)
    await router.enter(_params(stopLoss=98.0, takeProfit=105.0))
    req = client.placed[0]
    assert req["stopLoss"] == "98.0"
    assert req["takeProfit"] == "105.0"
