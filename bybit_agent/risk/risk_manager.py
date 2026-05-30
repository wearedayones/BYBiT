"""RiskManager — ports src/risk/RiskManager.ts.

Hard portfolio limits (kill / daily-loss / circuit-breaker) and per-signal approval:
sizing + max-positions + the expectancy (EV / reward:risk) gate after costs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..core.logger import child_logger
from ..persistence.db import get_db
from .cost_model import (
    MIN_NET_EDGE_PCT,
    MIN_REWARD_RISK,
    expected_value,
    round_trip_cost_pct,
)
from .sizing import SizingInput, compute_position_size

log = child_logger(module="risk")


@dataclass
class PortfolioState:
    equity: float
    peakEquity: float
    daySartEquity: float
    dailyRealizedPnl: float
    openPositionCount: int
    dailyLossLimit: float
    killLevelPct: float
    circuitBreakerPct: float
    maxRiskPct: float


@dataclass
class ApprovalResult:
    approved: bool
    qty: float
    stopPrice: float
    reason: str | None = None
    tpPrice: float | None = None
    costPct: float | None = None
    ev: float | None = None
    rewardRisk: float | None = None


@dataclass
class HardLimits:
    killTripped: bool
    dailyLimitHit: bool
    circuitBreaker: bool


class RiskManager:
    def __init__(self, state: Callable[[], PortfolioState]) -> None:
        self._state = state

    async def check_hard_limits(self, cycle_id: str) -> HardLimits:
        s = self._state()
        drawdown = (s.peakEquity - s.equity) / s.peakEquity if s.peakEquity > 0 else 0.0

        if drawdown >= s.killLevelPct:
            log.critical("🔴 Kill level breached", drawdown=drawdown, kill_level=s.killLevelPct)
            await self._log_risk_event(cycle_id, "kill_level", "critical", None, {"drawdownPct": drawdown})
            return HardLimits(True, False, False)

        daily_loss = (s.daySartEquity - s.equity) / s.daySartEquity if s.daySartEquity > 0 else 0.0
        if daily_loss >= s.dailyLossLimit:
            log.warning("🟡 Daily loss limit hit", daily_loss=daily_loss, limit=s.dailyLossLimit)
            await self._log_risk_event(cycle_id, "daily_loss", "warn", None, {"dailyLossAbs": daily_loss})
            return HardLimits(False, True, False)

        if drawdown >= s.circuitBreakerPct:
            log.warning("🟠 Circuit breaker triggered — de-risking", drawdown=drawdown)
            await self._log_risk_event(cycle_id, "circuit_breaker", "warn", None, {"drawdownPct": drawdown})
            return HardLimits(False, False, True)

        return HardLimits(False, False, False)

    def approve(self, signal, instrument: dict, circuit_breaker: bool) -> ApprovalResult:
        s = self._state()
        max_risk_pct = s.maxRiskPct * 0.5 if circuit_breaker else s.maxRiskPct

        stop = _get(signal, "suggestedStop")
        if not stop:
            return ApprovalResult(False, 0, 0, reason="No stop loss provided")

        min_qty = float(instrument["lotSizeFilter"]["minOrderQty"])
        qty_step = float(instrument["lotSizeFilter"]["qtyStep"])
        max_qty = float(instrument["lotSizeFilter"]["maxOrderQty"])

        sizing = compute_position_size(SizingInput(
            equity=s.equity, maxRiskPct=max_risk_pct,
            entryPrice=_get(signal, "suggestedEntry") or s.equity, stopPrice=stop,
            atr14=0, minQty=min_qty, qtyStep=qty_step, maxQty=max_qty, maxExposurePct=0.20,
        ))

        if sizing.qty <= 0:
            return ApprovalResult(False, 0, stop, reason="Position size too small or zero")

        if s.openPositionCount >= 8:
            return ApprovalResult(False, 0, stop, reason="Max concurrent positions reached")

        cost_pct = round_trip_cost_pct("linear", True, False)
        exp = expected_value(signal, _get(signal, "learnedPrior"), cost_pct)

        is_funding_harvest = _get(signal, "strategy") == "funding_harvest"
        tp = _get(signal, "suggestedTp")
        if exp and not is_funding_harvest:
            if exp.rewardRisk < MIN_REWARD_RISK:
                return ApprovalResult(False, 0, stop, tpPrice=tp, costPct=cost_pct, ev=exp.ev,
                                      rewardRisk=exp.rewardRisk,
                                      reason=f"Reward:risk {exp.rewardRisk:.2f} below {MIN_REWARD_RISK}")
            if exp.ev < MIN_NET_EDGE_PCT:
                return ApprovalResult(False, 0, stop, tpPrice=tp, costPct=cost_pct, ev=exp.ev,
                                      rewardRisk=exp.rewardRisk,
                                      reason=f"Negative expected value after costs (EV {exp.ev * 100:.3f}%)")

        return ApprovalResult(True, sizing.qty, stop, tpPrice=tp, costPct=cost_pct,
                              ev=exp.ev if exp else None, rewardRisk=exp.rewardRisk if exp else None)

    async def _log_risk_event(self, cycle_id, type_, severity, symbol, detail) -> None:
        try:
            db = get_db()
            await db.execute(
                """
                INSERT INTO risk_events (type, severity, cycle_id, symbol, detail, action_taken)
                VALUES ($1, $2, $3::uuid, $4, $5::jsonb, $6)
                """,
                type_, severity, cycle_id, symbol, db.json(detail), type_,
            )
        except Exception as e:  # noqa: BLE001
            log.error("Failed to log risk event", error=str(e))


def _get(signal, key):
    if isinstance(signal, dict):
        return signal.get(key)
    return getattr(signal, key, None)
