"""Phase 5: SelfReview weight update parity — ports src/review/SelfReview.ts logic."""
from __future__ import annotations

import json
import pytest

from bybit_agent.review.self_review import (
    DRAWDOWN_DISABLE_THRESHOLD,
    MAX_WEIGHT,
    MIN_WEIGHT,
    WEIGHT_ALPHA,
    SelfReview,
)


class FakeDb:
    """In-memory DB stub that captures the SQL calls SelfReview makes."""

    def __init__(
        self,
        trade_stats: list[dict] | None = None,
        initial_weight: float = 1.0,
    ) -> None:
        self._trade_stats = trade_stats or []
        self._weights: dict[str, float] = {}
        self._initial_weight = initial_weight
        self.updates: list[dict] = []        # UPDATE strategy_weights calls
        self.disables: list[str] = []        # strategies set enabled=false
        self.cooldown_clears: int = 0        # re-enable past-cooldown calls

    async def fetch(self, sql: str, *args) -> list[dict]:
        sql_up = sql.upper()
        if "FROM TRADES" in sql_up:
            return self._trade_stats
        if "FROM STRATEGY_WEIGHTS WHERE STRATEGY" in sql_up:
            strategy = args[0] if args else ""
            w = self._weights.get(strategy, self._initial_weight)
            return [{"weight": str(w)}]
        return []

    async def execute(self, sql: str, *args) -> None:
        sql_up = sql.upper()
        if "UPDATE STRATEGY_WEIGHTS" in sql_up:
            if "ENABLED = FALSE" in sql_up:
                self.disables.append(args[-1])  # strategy is last arg
            elif "ENABLED = TRUE" in sql_up:
                self.cooldown_clears += 1
            else:
                # weight update: args = (new_weight, pnl, count, win_rate, strategy)
                new_weight, pnl, count, win_rate, strategy = args
                self._weights[strategy] = float(new_weight)
                self.updates.append({
                    "strategy": strategy,
                    "new_weight": float(new_weight),
                    "win_rate": float(win_rate),
                    "count": int(count),
                })
        # Other SQL (learned_signals etc.) is silently accepted.


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_stats(
    strategy: str = "trend_momentum",
    pnl: float = 10.0,
    count: int = 10,
    win_rate: float = 0.6,
) -> dict:
    return {"strategy": strategy, "realized_pnl": pnl, "count": count, "win_rate": win_rate}


# ── tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_weight_increases_for_positive_pnl():
    db = FakeDb(
        trade_stats=[_make_stats(pnl=10.0)],
        initial_weight=1.0,
    )
    await SelfReview()._quick_weight_update(db)
    assert db.updates, "Expected at least one weight update"
    update = db.updates[0]
    assert update["new_weight"] > 1.0, "Positive PnL should push weight above initial"


@pytest.mark.asyncio
async def test_weight_decreases_for_negative_pnl():
    db = FakeDb(
        trade_stats=[_make_stats(pnl=-10.0)],
        initial_weight=1.5,
    )
    await SelfReview()._quick_weight_update(db)
    assert db.updates, "Expected at least one weight update"
    update = db.updates[0]
    assert update["new_weight"] < 1.5, "Negative PnL should pull weight below initial"


@pytest.mark.asyncio
async def test_weight_clamped_between_min_and_max():
    db = FakeDb(
        trade_stats=[_make_stats(pnl=1000.0)],
        initial_weight=MAX_WEIGHT,
    )
    await SelfReview()._quick_weight_update(db)
    if db.updates:
        assert db.updates[0]["new_weight"] <= MAX_WEIGHT

    db2 = FakeDb(
        trade_stats=[_make_stats(pnl=-1000.0)],
        initial_weight=MIN_WEIGHT,
    )
    await SelfReview()._quick_weight_update(db2)
    if db2.updates:
        assert db2.updates[0]["new_weight"] >= MIN_WEIGHT


@pytest.mark.asyncio
async def test_skips_strategy_below_min_trades():
    db = FakeDb(
        trade_stats=[_make_stats(count=3)],  # below MIN_TRADES_FOR_REVIEW=5
        initial_weight=1.0,
    )
    await SelfReview()._quick_weight_update(db)
    assert db.updates == [], "Should skip strategies with fewer than 5 trades"


@pytest.mark.asyncio
async def test_disables_deeply_negative_strategy():
    # One strategy deeply negative, one positive — the negative one gets disabled.
    db = FakeDb(
        trade_stats=[
            _make_stats("bad_strat", pnl=-50.0, count=10),
            _make_stats("good_strat", pnl=10.0, count=10),
        ],
        initial_weight=1.0,
    )
    await SelfReview()._quick_weight_update(db)
    assert "bad_strat" in db.disables, "Deeply negative strategy should be disabled"
    assert "good_strat" not in db.disables


@pytest.mark.asyncio
async def test_ema_blend_formula():
    """Verify the exact EMA blend: w_new = w * (1 - α) + norm_score * α * 2."""
    initial = 1.0
    pnl = 10.0
    total_pnl = abs(pnl)  # only one strategy
    relative_perf = pnl / total_pnl   # 1.0
    norm_score = 0.5 + relative_perf * 0.5  # 1.0
    expected = initial * (1 - WEIGHT_ALPHA) + norm_score * WEIGHT_ALPHA * 2  # 1.2

    db = FakeDb(trade_stats=[_make_stats(pnl=pnl)], initial_weight=initial)
    await SelfReview()._quick_weight_update(db)
    assert db.updates, "Expected a weight update"
    assert abs(db.updates[0]["new_weight"] - expected) < 1e-9


@pytest.mark.asyncio
async def test_no_update_when_no_trades():
    db = FakeDb(trade_stats=[], initial_weight=1.0)
    await SelfReview()._quick_weight_update(db)
    assert db.updates == []
