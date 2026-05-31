"""MeanReversion — ports src/strategy/impl/MeanReversion.ts."""

from __future__ import annotations

from ..base import Signal, StrategyContext


class MeanReversion:
    name = "mean_reversion"
    # crowded_long / crowded_short = >70% / <30% longs — classic contrarian reversal setup.
    suitable_regimes = ["ranging", "crisis", "crowded_long", "crowded_short"]

    def evaluate(self, snap, ctx: StrategyContext) -> Signal:
        i = snap.indicators
        price = snap.lastPrice
        boll = i.boll
        regime = getattr(snap, "regime", None) or ""

        if i.atrPct > 0.03:
            return Signal(action="hold", symbol=snap.symbol, strategy="mean_reversion",
                          confidence=0, rationale="Too volatile for mean reversion")

        oversold = price <= boll["lower"] and i.rsi14 < 35
        overbought = price >= boll["upper"] and i.rsi14 > 65

        # Boost confidence when crowd positioning aligns with the signal direction.
        crowd_long_boost  = 0.10 if regime == "crowded_long"  else 0.0
        crowd_short_boost = 0.10 if regime == "crowded_short" else 0.0

        if oversold:
            return Signal(
                action="enter_long", symbol=snap.symbol, strategy="mean_reversion",
                confidence=min(0.90, 0.4 + (35 - i.rsi14) / 50 + crowd_short_boost),
                suggestedEntry=price, suggestedStop=price - i.atr14 * 1.5, suggestedTp=boll["middle"],
                rationale=f"Price at lower Bollinger, RSI={i.rsi14:.1f}"
                          + (", crowd SHORT" if crowd_short_boost else ""),
            )
        if overbought:
            return Signal(
                action="enter_short", symbol=snap.symbol, strategy="mean_reversion",
                confidence=min(0.90, 0.4 + (i.rsi14 - 65) / 50 + crowd_long_boost),
                suggestedEntry=price, suggestedStop=price + i.atr14 * 1.5, suggestedTp=boll["middle"],
                rationale=f"Price at upper Bollinger, RSI={i.rsi14:.1f}"
                          + (", crowd LONG" if crowd_long_boost else ""),
            )
        return Signal(action="hold", symbol=snap.symbol, strategy="mean_reversion",
                      confidence=0, rationale="No mean-reversion signal")
