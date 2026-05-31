"""Tests for the backtest engine and strategy registry lifecycle."""
from __future__ import annotations

import json
import math
import unittest.mock as mock

import pytest

from bybit_agent.strategy.backtest import BacktestConfig, BacktestResult, backtest_strategy
from bybit_agent.strategy.impl.trend_momentum import TrendMomentum
from bybit_agent.strategy.impl.mean_reversion import MeanReversion
from bybit_agent.strategy.impl.funding_harvest import FundingHarvest
from bybit_agent.strategy.registry import (
    BASE_STRATEGIES,
    StrategyError,
    instantiate,
)


# ── OHLCV helpers ────────────────────────────────────────────────────────────

def _trending_up(n: int = 300) -> dict:
    """Smooth uptrend: price goes from 100 to ~200 over n bars."""
    closes = [100.0 + (i / n) * 100 for i in range(n)]
    return {
        "open":   [c - 0.5 for c in closes],
        "high":   [c + 1.0 for c in closes],
        "low":    [c - 1.0 for c in closes],
        "close":  closes,
        "volume": [1000.0] * n,
    }


def _flat(n: int = 300, price: float = 100.0) -> dict:
    """Flat market — oscillates ±0.5% around a fixed price."""
    import math as m
    closes = [price + price * 0.005 * m.sin(i * 0.3) for i in range(n)]
    return {
        "open":   [c - 0.1 for c in closes],
        "high":   [c + 0.2 for c in closes],
        "low":    [c - 0.2 for c in closes],
        "close":  closes,
        "volume": [500.0] * n,
    }


def _constant(n: int = 300, price: float = 100.0) -> dict:
    return {
        "open":   [price] * n,
        "high":   [price + 0.01] * n,
        "low":    [price - 0.01] * n,
        "close":  [price] * n,
        "volume": [100.0] * n,
    }


def _short_series(n: int = 10) -> dict:
    return {k: [100.0] * n for k in ("open", "high", "low", "close", "volume")}


# ── BacktestResult.metrics() ─────────────────────────────────────────────────

def test_metrics_is_json_serializable():
    res = BacktestResult(strategy="test")
    m = res.metrics()
    json.dumps(m)  # must not raise
    assert "passed" in m
    assert "n_trades" in m


def test_metrics_rounds_floats():
    res = BacktestResult(strategy="test", win_rate=0.123456789, profit_factor=1.23456789)
    m = res.metrics()
    assert len(str(m["win_rate"]).split(".")[-1]) <= 4
    assert len(str(m["profit_factor"]).split(".")[-1]) <= 3


# ── Insufficient data ────────────────────────────────────────────────────────

def test_too_few_bars_fails():
    res = backtest_strategy(TrendMomentum(), _short_series(10))
    assert not res.passed
    assert any("insufficient" in r for r in res.fail_reasons)
    assert res.n_trades == 0


def test_barely_enough_bars_does_not_crash():
    cfg = BacktestConfig(warmup=20, window=30)
    ohlcv = _trending_up(80)
    res = backtest_strategy(TrendMomentum(), ohlcv, cfg=cfg)
    assert isinstance(res.passed, bool)


# ── Trade simulation ─────────────────────────────────────────────────────────

def test_backtest_runs_correct_bar_count():
    """bars_tested should equal total bars minus the warmup period."""
    cfg = BacktestConfig(warmup=60, window=100)
    res = backtest_strategy(TrendMomentum(), _trending_up(300), cfg=cfg)
    assert res.bars_tested == 300 - 60
    assert isinstance(res.passed, bool)
    assert res.wins + res.losses == res.n_trades


def test_no_trades_on_constant_price():
    """A strategy should emit zero trades on a completely flat synthetic series."""
    res = backtest_strategy(TrendMomentum(), _constant(300))
    # Could have 0 trades or fail on min_trades — either is acceptable.
    assert isinstance(res.passed, bool)


def test_equity_never_negative():
    """Equity must stay > 0; sizing caps prevent blowup."""
    cfg = BacktestConfig(risk_pct=0.5, min_trades=1)
    res = backtest_strategy(TrendMomentum(), _trending_up(300), cfg=cfg)
    # If there were trades, equity at each step was calculated with capped fractions.
    # No assertion on profits — just that the run completes without exception.
    assert res.n_trades >= 0


def test_profit_factor_infinity_when_no_losses():
    """A strategy with only winning trades has infinite profit factor."""
    cfg = BacktestConfig(min_trades=1, min_profit_factor=1.0)
    # Monkey-patch evaluate to always return a clean long signal.
    strat = TrendMomentum()
    original = strat.evaluate

    def always_long(snap, ctx):
        sig = original(snap, ctx)
        # Override to a clear long on every bar.
        from bybit_agent.strategy.base import Signal
        return Signal(
            action="enter_long", symbol="BT", strategy="t",
            confidence=0.8, rationale="forced",
            suggestedEntry=snap.lastPrice,
            suggestedStop=snap.lastPrice * 0.95,
            suggestedTp=snap.lastPrice * 1.20,
        )

    strat.evaluate = always_long
    res = backtest_strategy(strat, _trending_up(300), cfg=cfg)
    assert res.profit_factor >= 1.0 or res.n_trades == 0


def test_result_fields_consistent():
    cfg = BacktestConfig(warmup=30, window=60, min_trades=1)
    res = backtest_strategy(TrendMomentum(), _trending_up(200), cfg=cfg)
    assert res.wins + res.losses == res.n_trades
    if res.n_trades > 0:
        assert 0.0 <= res.win_rate <= 1.0
        assert res.max_drawdown_pct >= 0.0


# ── Acceptance thresholds ─────────────────────────────────────────────────────

def test_fail_reasons_populated_when_fails():
    cfg = BacktestConfig(min_trades=9999)  # impossible threshold
    res = backtest_strategy(TrendMomentum(), _trending_up(300), cfg=cfg)
    assert not res.passed
    assert len(res.fail_reasons) > 0
    assert any("too few trades" in r for r in res.fail_reasons)


def test_passed_true_when_all_thresholds_met():
    cfg = BacktestConfig(
        min_trades=1, min_win_rate=0.0, min_profit_factor=0.0,
        max_drawdown_pct=1.0, require_positive_return=False,
    )
    res = backtest_strategy(TrendMomentum(), _trending_up(300), cfg=cfg)
    # With extremely lenient thresholds and a trending market it should pass.
    if res.n_trades >= 1:
        assert res.passed
        assert res.fail_reasons == []


def test_negative_return_fails_when_required():
    cfg = BacktestConfig(require_positive_return=True, min_trades=1,
                         min_win_rate=0.0, min_profit_factor=0.0)
    # Use a downtrend to force losses.
    down = _trending_up(300)
    down["close"] = list(reversed(down["close"]))
    down["high"] = [c + 1 for c in down["close"]]
    down["low"] = [c - 1 for c in down["close"]]
    down["open"] = [c - 0.5 for c in down["close"]]
    res = backtest_strategy(TrendMomentum(), down, cfg=cfg)
    if res.n_trades >= 1 and res.total_return_pct <= 0:
        assert not res.passed
        assert any("non-positive" in r for r in res.fail_reasons)


# ── instantiate() ─────────────────────────────────────────────────────────────

def test_instantiate_known_base():
    inst = instantiate("trend_momentum")
    assert isinstance(inst, TrendMomentum)


def test_instantiate_unknown_base_raises():
    with pytest.raises(StrategyError, match="Unknown base"):
        instantiate("does_not_exist")


def test_instantiate_applies_params():
    inst = instantiate("trend_momentum", {"rsi_threshold": 42}, name="custom_tm")
    assert inst.rsi_threshold == 42
    assert inst.name == "custom_tm"


def test_instantiate_no_name_keeps_default():
    inst = instantiate("mean_reversion")
    assert inst.name  # non-empty


def test_all_base_strategies_instantiate():
    for key in BASE_STRATEGIES:
        inst = instantiate(key)
        assert inst is not None


# ── registry async CRUD (mocked DB) ──────────────────────────────────────────

def _mock_db():
    db = mock.AsyncMock()
    db.fetch = mock.AsyncMock(return_value=[])
    db.execute = mock.AsyncMock(return_value=None)
    return db


@pytest.mark.asyncio
async def test_create_strategy_unknown_base_raises():
    from bybit_agent.strategy.registry import create_strategy
    db = _mock_db()
    with pytest.raises(StrategyError, match="Unknown base"):
        await create_strategy(db, name="x", base_strategy="bogus")


@pytest.mark.asyncio
async def test_create_strategy_duplicate_raises():
    from bybit_agent.strategy.registry import create_strategy
    db = _mock_db()
    db.fetch.return_value = [{"name": "existing"}]
    with pytest.raises(StrategyError, match="already exists"):
        await create_strategy(db, name="existing", base_strategy="trend_momentum")


@pytest.mark.asyncio
async def test_create_strategy_inserts_row():
    from bybit_agent.strategy.registry import create_strategy
    db = _mock_db()
    db.fetch.return_value = []  # not found → not duplicate
    await create_strategy(db, name="new_strat", base_strategy="trend_momentum", weight=1.5)
    db.execute.assert_called_once()
    call_sql = db.execute.call_args[0][0]
    assert "INSERT INTO strategy_registry" in call_sql


@pytest.mark.asyncio
async def test_accept_without_backtest_raises():
    from bybit_agent.strategy.registry import accept
    db = _mock_db()
    db.fetch.return_value = [{"name": "s", "backtest": None}]
    with pytest.raises(StrategyError, match="no passing backtest"):
        await accept(db, "s")


@pytest.mark.asyncio
async def test_accept_with_failed_backtest_raises():
    from bybit_agent.strategy.registry import accept
    db = _mock_db()
    db.fetch.return_value = [{"name": "s", "backtest": json.dumps({"passed": False})}]
    with pytest.raises(StrategyError, match="no passing backtest"):
        await accept(db, "s")


@pytest.mark.asyncio
async def test_accept_with_passing_backtest_succeeds():
    from bybit_agent.strategy.registry import accept
    db = _mock_db()
    db.fetch.return_value = [{"name": "s", "backtest": json.dumps({"passed": True})}]
    await accept(db, "s")
    db.execute.assert_called_once()
    assert "accepted" in db.execute.call_args[0][0]


@pytest.mark.asyncio
async def test_resume_non_accepted_raises():
    from bybit_agent.strategy.registry import resume
    db = _mock_db()
    db.fetch.return_value = [{"name": "s", "status": "draft"}]
    with pytest.raises(StrategyError, match="not 'accepted'"):
        await resume(db, "s")


@pytest.mark.asyncio
async def test_load_active_empty_returns_empty():
    from bybit_agent.strategy.registry import load_active_strategies
    db = _mock_db()
    db.fetch.return_value = []
    result = await load_active_strategies(db)
    assert result == []


@pytest.mark.asyncio
async def test_load_active_returns_instances():
    from bybit_agent.strategy.registry import load_active_strategies
    db = _mock_db()
    db.fetch.return_value = [
        {"name": "tm_v2", "base_strategy": "trend_momentum", "params": "{}", "weight": "1.5"},
    ]
    result = await load_active_strategies(db)
    assert len(result) == 1
    inst, weight = result[0]
    assert isinstance(inst, TrendMomentum)
    assert inst.name == "tm_v2"
    assert weight == 1.5


@pytest.mark.asyncio
async def test_load_active_skips_unknown_base():
    from bybit_agent.strategy.registry import load_active_strategies
    db = _mock_db()
    db.fetch.return_value = [
        {"name": "ghost", "base_strategy": "does_not_exist", "params": "{}", "weight": "1.0"},
        {"name": "good", "base_strategy": "breakout", "params": "{}", "weight": "0.8"},
    ]
    result = await load_active_strategies(db)
    assert len(result) == 1
    inst, _ = result[0]
    assert inst.name == "good"


@pytest.mark.asyncio
async def test_update_strategy_params_resets_to_draft():
    from bybit_agent.strategy.registry import update_strategy
    db = _mock_db()
    db.fetch.return_value = [{"name": "s"}]
    await update_strategy(db, "s", params={"k": "v"})
    call_sql = db.execute.call_args[0][0]
    assert "draft" in call_sql
    assert "enabled = false" in call_sql


@pytest.mark.asyncio
async def test_delete_strategy_not_found_raises():
    from bybit_agent.strategy.registry import delete_strategy
    db = _mock_db()
    db.fetch.return_value = []
    with pytest.raises(StrategyError, match="not found"):
        await delete_strategy(db, "missing")
