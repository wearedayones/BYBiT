"""brain.md DB-backed renderer: add_note persistence + render content (in-memory)."""
from __future__ import annotations

import pytest

from bybit_agent.control import brain


class FakeDb:
    """Minimal NeonHttpClient stub: routes queries by table keyword to canned rows."""

    def __init__(self, rows: dict | None = None) -> None:
        self._rows = rows or {}
        self.executed: list[tuple] = []

    async def execute(self, sql: str, *args) -> None:
        self.executed.append((sql, args))

    async def fetch(self, sql: str, *args) -> list[dict]:
        s = sql.upper()
        if "FROM AGENT_STATE" in s:
            return [{
                "env": "testnet", "status": "running", "kill_engaged": False,
                "kill_reason": None, "equity": 78.0, "peak_equity": 80.0,
                "day_start_equity": 79.0, "daily_realized_pnl": 0,
                "max_risk_pct": 0.015, "promotion_cycle_count": 5,
                "last_cycle_at": "2026-05-31 06:00:00+00",
            }]
        if "FROM TRADES" in s:
            return [{"n": 4, "wins": 3, "total_pnl": 1.25, "paper": 4}]
        if "FROM PENDING_EVENTS" in s:
            return [{"kind": "market_event", "severity": "warning", "symbol": "ETHUSDT",
                     "title": "vol_spike", "expires_at": "2026-05-31 07:00:00+00"}]
        if "FROM STRATEGY_WEIGHTS" in s:
            return [{"strategy": "trend_momentum", "weight": 1.2, "enabled": True,
                     "win_rate": 0.6, "rolling_win_48h": 0.55, "trades_count": 4}]
        if "FROM AGENT_CONFIG" in s:
            return [{"key": "defaultLeverage", "value": "5"}]
        if "FROM LEARNED_SIGNALS" in s:
            return [{"strategy": "trend_momentum", "regime": "trending_up",
                     "n_trades": 4, "win_rate": 0.6, "avg_pnl": 0.3}]
        if "FROM BRAIN_NOTES" in s and "COUNT(*)" in s:
            return [{"n": 2}]
        if "FROM BRAIN_NOTES" in s:
            # category filter is the first arg
            cat = args[0] if args else "lesson"
            if cat == "lesson":
                return [{"created_at": "2026-05-31 12:00:00+00",
                         "note": "Avoid mean reversion in vol spikes."}]
            return []
        return []


@pytest.mark.asyncio
async def test_add_note_inserts_into_brain_notes():
    db = FakeDb()
    await brain.add_note(db, "Test lesson", category="lesson", tags=["risk"])
    assert len(db.executed) == 1
    sql, args = db.executed[0]
    assert "INSERT INTO brain_notes" in sql
    assert args[0] == "lesson"
    assert args[1] == "Test lesson"


@pytest.mark.asyncio
async def test_render_contains_live_sections():
    db = FakeDb()
    md = await brain.render(db)
    # Static header + all seven sections render.
    assert "AUTONOMOUS BRAIN" in md
    assert "1. Account State" in md
    assert "testnet" in md
    assert "Win rate: **75.0%**" in md          # 3 wins / 4 trades
    assert "trend_momentum" in md
    assert "defaultLeverage" in md
    assert "Avoid mean reversion" in md
    # Drawdown computed from peak vs equity: (80-78)/80 = 2.5%
    assert "2.50%" in md


@pytest.mark.asyncio
async def test_render_degrades_when_db_errors():
    class BrokenDb:
        async def fetch(self, sql, *args):
            raise RuntimeError("db down")

    md = await brain.render(BrokenDb())
    # _safe_fetch swallows errors → sections show their empty-state text, no crash.
    assert "AUTONOMOUS BRAIN" in md
    assert "agent_state unavailable" in md
