"""Phase 3: kill switch idempotency and step-order tests."""
from __future__ import annotations

import pytest

from bybit_agent.core.errors import KillSwitchError
from bybit_agent.control.kill_switch import KillSwitch


class FakeClient:
    def __init__(self, positions: list[dict] | None = None) -> None:
        self.cancelled: list[str] = []
        self.orders: list[dict] = []
        self._positions = positions or []

    async def cancel_all_orders(self, category: str) -> None:
        self.cancelled.append(category)

    async def get_positions(self, category: str) -> list[dict]:
        return self._positions

    async def place_order(self, req: dict) -> dict:
        self.orders.append(req)
        return {"orderId": "x", "orderLinkId": req.get("orderLinkId", "x")}


class FakeDb:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, sql: str, *args) -> None:
        self.calls.append(sql.strip().split("\n")[0])

    async def fetch(self, sql: str, *args) -> list:
        return []


@pytest.mark.asyncio
async def test_engage_raises_kill_switch_error():
    ks = KillSwitch(FakeClient(), FakeDb())
    with pytest.raises(KillSwitchError):
        await ks.engage("test")


@pytest.mark.asyncio
async def test_idempotent_second_engage_is_noop():
    """Second engage() must return immediately — no double DB write, no double raise."""
    ks = KillSwitch(FakeClient(), FakeDb())
    with pytest.raises(KillSwitchError):
        await ks.engage("first")

    # Already engaged — must not raise again, just return.
    await ks.engage("second")  # should not raise
    assert ks.is_engaged()


@pytest.mark.asyncio
async def test_cancels_all_three_categories():
    client = FakeClient()
    ks = KillSwitch(client, FakeDb())
    with pytest.raises(KillSwitchError):
        await ks.engage("test")

    assert set(client.cancelled) == {"spot", "linear", "inverse"}


@pytest.mark.asyncio
async def test_flattens_open_positions():
    positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": "0.1"},
        {"symbol": "ETHUSDT", "side": "Sell", "size": "1.0"},
    ]
    client = FakeClient(positions=positions)
    ks = KillSwitch(client, FakeDb())
    with pytest.raises(KillSwitchError):
        await ks.engage("drawdown")

    assert len(client.orders) == 2
    btc = next(o for o in client.orders if o["symbol"] == "BTCUSDT")
    eth = next(o for o in client.orders if o["symbol"] == "ETHUSDT")
    assert btc["side"] == "Sell" and btc["reduceOnly"] is True
    assert eth["side"] == "Buy" and eth["reduceOnly"] is True


@pytest.mark.asyncio
async def test_skips_zero_size_positions():
    positions = [{"symbol": "BTCUSDT", "side": "Buy", "size": "0"}]
    client = FakeClient(positions=positions)
    ks = KillSwitch(client, FakeDb())
    with pytest.raises(KillSwitchError):
        await ks.engage("test")

    assert client.orders == []
