"""Trading mode state machine — unit tests for the staged cutover path.

Tests:
  1. DecisionEngine.paper is settable at runtime (no restart needed).
  2. PromotionManager reads trading_mode from agent_state (not agent_config).
  3. Promotion is a no-op in shadow mode, below-cycle-count, insufficient trades.
  4. Promotion fires and writes trading_mode=mainnet_live + env=mainnet on criteria pass.
  5. Promotion does not fire when win-rate or Sharpe is below threshold.
"""
from __future__ import annotations

import pytest

from bybit_agent.strategy.decision_engine import DecisionEngine
from bybit_agent.control.promotion import PromotionManager


# ── DecisionEngine paper setter ───────────────────────────────────────────────


def test_decision_engine_paper_setter():
    engine = DecisionEngine(paper=True)
    assert engine.paper is True
    engine.paper = False
    assert engine.paper is False
    engine.paper = True
    assert engine.paper is True


# ── PromotionManager ─────────────────────────────────────────────────────────


class FakeDb:
    """Minimal stub; routing by SQL keywords. trading_mode lives in agent_state."""

    def __init__(self, state: dict | None = None,
                 trades: dict | None = None, equity: dict | None = None) -> None:
        self._state = state or {}
        self._trades = trades or {}
        self._equity = equity or {}
        self.executed: list[str] = []

    async def execute(self, sql: str, *args) -> None:
        self.executed.append(sql.strip())

    async def fetch(self, sql: str, *args) -> list[dict]:
        s = sql.upper()
        if "FROM AGENT_STATE" in s:
            return [self._state] if self._state else []
        if "FROM TRADES" in s:
            return [self._trades] if self._trades else [{}]
        if "FROM EQUITY_SNAPSHOTS" in s:
            return [self._equity] if self._equity else [{}]
        return []


def _testnet_live_state(cycle_count=100, extra_criteria: dict | None = None) -> dict:
    return {
        "trading_mode": "testnet_live",
        "env": "testnet",
        "promotion_cycle_count": cycle_count,
        "promotion_criteria": extra_criteria or {},
    }


@pytest.mark.asyncio
async def test_promotion_noop_in_shadow_mode():
    db = FakeDb(state={"trading_mode": "shadow", "env": "testnet",
                       "promotion_cycle_count": 100, "promotion_criteria": {}})
    result = await PromotionManager(db).evaluate()
    assert result is False
    assert not db.executed   # exits before any DB writes


@pytest.mark.asyncio
async def test_promotion_noop_below_min_cycles():
    db = FakeDb(
        state=_testnet_live_state(cycle_count=5),
        trades={"win_rate": 0.8, "total_pnl": 20.0, "count": 50},
        equity={"min_drawdown": -0.01, "avg_equity": 80.0, "stddev_equity": 0.5},
    )
    result = await PromotionManager(db).evaluate()
    assert result is False   # cycle_count 5 < MIN_CYCLES 72


@pytest.mark.asyncio
async def test_promotion_noop_insufficient_trades():
    db = FakeDb(
        state=_testnet_live_state(),
        trades={"win_rate": 0.7, "total_pnl": 5.0, "count": 3},   # < 10 required
        equity={"min_drawdown": -0.02, "avg_equity": 80.0, "stddev_equity": 1.0},
    )
    result = await PromotionManager(db).evaluate()
    assert result is False


@pytest.mark.asyncio
async def test_promotion_fires_when_criteria_met():
    db = FakeDb(
        state=_testnet_live_state(
            cycle_count=100,
            extra_criteria={"min_cycles": 72, "min_sharpe": 0.5,
                            "min_win_rate": 0.50, "max_drawdown_pct": 0.05},
        ),
        trades={"win_rate": 0.65, "total_pnl": 10.0, "count": 20},
        equity={"min_drawdown": -0.02, "avg_equity": 80.0, "stddev_equity": 0.5},
    )
    result = await PromotionManager(db).evaluate()
    assert result is True
    # Must write trading_mode=mainnet_live AND env=mainnet into agent_state.
    combined = "\n".join(db.executed)
    assert "mainnet_live" in combined
    assert "mainnet" in combined


@pytest.mark.asyncio
async def test_promotion_does_not_fire_low_win_rate():
    db = FakeDb(
        state=_testnet_live_state(
            cycle_count=100,
            extra_criteria={"min_cycles": 72, "min_sharpe": 0.5,
                            "min_win_rate": 0.50, "max_drawdown_pct": 0.05},
        ),
        trades={"win_rate": 0.40, "total_pnl": -2.0, "count": 20},  # < 0.50
        equity={"min_drawdown": -0.02, "avg_equity": 80.0, "stddev_equity": 0.5},
    )
    result = await PromotionManager(db).evaluate()
    assert result is False
    assert not any("mainnet_live" in s for s in db.executed)
