"""FundingHarvest — ports src/strategy/impl/FundingHarvest.ts."""

from __future__ import annotations

from ..base import Signal, StrategyContext

FUNDING_THRESHOLD_ANNUALIZED = 0.10  # 10% APR (~0.027% per 8h funding payment)


class FundingHarvest:
    name = "funding_harvest"
    suitable_regimes = ["trending", "ranging", "high_volatility", "crisis", "crowded_long", "crowded_short"]

    def evaluate(self, snap, ctx: StrategyContext) -> Signal:
        funding_rate = snap.fundingRate
        annualized = funding_rate * 3 * 365  # funding every 8h → 3x/day

        if abs(annualized) < FUNDING_THRESHOLD_ANNUALIZED:
            return Signal(action="hold", symbol=snap.symbol, strategy="funding_harvest",
                          confidence=0,
                          rationale=f"Funding {annualized * 100:.1f}% APR below threshold")

        if annualized > 0:
            return Signal(
                action="enter_short", symbol=snap.symbol, strategy="funding_harvest",
                confidence=min(0.95, 0.5 + abs(annualized) / 2),
                rationale=f"High positive funding {annualized * 100:.1f}% APR — harvest via short perp",
                suggestedEntry=snap.markPrice, suggestedStop=snap.markPrice * 1.05,
            )
        return Signal(
            action="enter_long", symbol=snap.symbol, strategy="funding_harvest",
            confidence=min(0.95, 0.5 + abs(annualized) / 2),
            rationale=f"High negative funding {annualized * 100:.1f}% APR — harvest via long perp",
            suggestedEntry=snap.markPrice, suggestedStop=snap.markPrice * 0.95,
        )
