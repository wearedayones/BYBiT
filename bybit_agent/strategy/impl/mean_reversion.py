"""MeanReversion — ports src/strategy/impl/MeanReversion.ts."""

from __future__ import annotations

from ..base import Signal, StrategyContext


class MeanReversion:
    name = "mean_reversion"
    suitable_regimes = ["ranging", "crisis"]

    def evaluate(self, snap, ctx: StrategyContext) -> Signal:
        i = snap.indicators
        price = snap.lastPrice
        boll = i.boll

        if i.atrPct > 0.03:
            return Signal(action="hold", symbol=snap.symbol, strategy="mean_reversion",
                          confidence=0, rationale="Too volatile for mean reversion")

        oversold = price <= boll["lower"] and i.rsi14 < 35
        overbought = price >= boll["upper"] and i.rsi14 > 65

        if oversold:
            return Signal(
                action="enter_long", symbol=snap.symbol, strategy="mean_reversion",
                confidence=min(0.85, 0.4 + (35 - i.rsi14) / 50),
                suggestedEntry=price, suggestedStop=price - i.atr14 * 1.5, suggestedTp=boll["middle"],
                rationale=f"Price at lower Bollinger, RSI={i.rsi14:.1f}",
            )
        if overbought:
            return Signal(
                action="enter_short", symbol=snap.symbol, strategy="mean_reversion",
                confidence=min(0.85, 0.4 + (i.rsi14 - 65) / 50),
                suggestedEntry=price, suggestedStop=price + i.atr14 * 1.5, suggestedTp=boll["middle"],
                rationale=f"Price at upper Bollinger, RSI={i.rsi14:.1f}",
            )
        return Signal(action="hold", symbol=snap.symbol, strategy="mean_reversion",
                      confidence=0, rationale="No mean-reversion signal")
