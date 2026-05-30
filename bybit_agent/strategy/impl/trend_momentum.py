"""TrendMomentum — ports src/strategy/impl/TrendMomentum.ts."""

from __future__ import annotations

from ..base import Signal, StrategyContext


class TrendMomentum:
    name = "trend_momentum"
    suitable_regimes = ["trending", "high_volatility"]

    def evaluate(self, snap, ctx: StrategyContext) -> Signal:
        i = snap.indicators
        price = snap.lastPrice
        bullish = i.ema9 > i.ema21 and i.ema21 > i.ema50 and i.macdHistogram > 0 and i.adxValue > 20
        bearish = i.ema9 < i.ema21 and i.ema21 < i.ema50 and i.macdHistogram < 0 and i.adxValue > 20
        rsi_ok = 45 < i.rsi14 < 75
        rsi_bear_ok = 25 < i.rsi14 < 55

        if bullish and rsi_ok:
            atr = i.atr14
            return Signal(
                action="enter_long", symbol=snap.symbol, strategy="trend_momentum",
                confidence=min(0.9, 0.5 + i.adxValue / 100 + (0.1 if i.macdHistogram > 0 else 0)),
                suggestedEntry=price, suggestedStop=price - atr * 2, suggestedTp=price + atr * 3,
                rationale=f"Bullish EMA stack, ADX={i.adxValue:.1f}, RSI={i.rsi14:.1f}",
            )
        if bearish and rsi_bear_ok:
            atr = i.atr14
            return Signal(
                action="enter_short", symbol=snap.symbol, strategy="trend_momentum",
                confidence=min(0.9, 0.5 + i.adxValue / 100 + (0.1 if i.macdHistogram < 0 else 0)),
                suggestedEntry=price, suggestedStop=price + atr * 2, suggestedTp=price - atr * 3,
                rationale=f"Bearish EMA stack, ADX={i.adxValue:.1f}, RSI={i.rsi14:.1f}",
            )
        return Signal(action="hold", symbol=snap.symbol, strategy="trend_momentum",
                      confidence=0, rationale="No trend signal")
