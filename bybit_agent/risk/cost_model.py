"""CostModel — single source of truth for trading friction. Ports src/risk/CostModel.ts.

Turns a signal's stop/TP geometry into an expected value NET of fees + slippage, so the
RiskManager can refuse trades that cannot pay for themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config.constants import COST_DEFAULTS, FEES


def fee_rate(category: str, maker: bool) -> float:
    if category == "spot":
        return FEES["SPOT_MAKER"] if maker else FEES["SPOT_TAKER"]
    return FEES["PERP_MAKER"] if maker else FEES["PERP_TAKER"]


def round_trip_cost_pct(category: str, entry_maker: bool, exit_maker: bool) -> float:
    fees = fee_rate(category, entry_maker) + fee_rate(category, exit_maker)
    slippage = COST_DEFAULTS["SLIPPAGE_PCT"] * 2
    return fees + slippage


@dataclass
class ExpectancyResult:
    ev: float
    rewardRisk: float
    winProb: float
    avgWin: float
    avgLoss: float
    costPct: float


def expected_value(signal: Any, learned_prior: float, cost_pct: float) -> ExpectancyResult | None:
    entry = _get(signal, "suggestedEntry")
    stop = _get(signal, "suggestedStop")
    tp = _get(signal, "suggestedTp")
    if not entry or not stop or not tp or entry <= 0:
        return None

    avg_win = abs(tp - entry) / entry
    avg_loss = abs(entry - stop) / entry
    if avg_loss <= 0:
        return None

    # Unknown prior (~1.0) → slight positive bias 0.55; otherwise clamp to [0.05, 0.95].
    if learned_prior >= 0.999:
        win_prob = 0.55
    else:
        win_prob = min(0.95, max(0.05, learned_prior))

    ev = win_prob * avg_win - (1 - win_prob) * avg_loss - cost_pct
    reward_risk = avg_win / avg_loss
    return ExpectancyResult(ev=ev, rewardRisk=reward_risk, winProb=win_prob,
                            avgWin=avg_win, avgLoss=avg_loss, costPct=cost_pct)


def _get(signal: Any, key: str):
    if isinstance(signal, dict):
        return signal.get(key)
    return getattr(signal, key, None)


MIN_NET_EDGE_PCT = COST_DEFAULTS["MIN_NET_EDGE_PCT"]
MIN_REWARD_RISK = COST_DEFAULTS["MIN_REWARD_RISK"]
