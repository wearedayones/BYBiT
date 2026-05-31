"""Breakout — ports src/strategy/impl/Breakout.ts."""

from __future__ import annotations

from ..base import Signal, StrategyContext


class Breakout:
    name = "breakout"
    suitable_regimes = ["ranging", "high_volatility"]

    def evaluate(self, snap, ctx: StrategyContext) -> Signal:
        i = snap.indicators
        close, high, low = snap.ohlcv["close"], snap.ohlcv["high"], snap.ohlcv["low"]
        price = snap.lastPrice

        if len(close) < 20:
            return Signal(action="hold", symbol=snap.symbol, strategy="breakout",
                          confidence=0, rationale="Not enough data")

        lookback = 20
        recent_highs = high[-lookback:]
        recent_lows = low[-lookback:]
        range_high = max(recent_highs[:-1])
        range_low = min(recent_lows[:-1])

        breakout_up = price > range_high and i.adxValue < 30 and i.rsi14 > 55
        breakout_down = price < range_low and i.adxValue < 30 and i.rsi14 < 45

        if breakout_up:
            return Signal(
                action="enter_long", symbol=snap.symbol, strategy="breakout", confidence=0.65,
                suggestedEntry=price, suggestedStop=range_low,
                suggestedTp=price + (price - range_low) * 1.5,
                rationale=f"Breakout above {range_high:.2f}, ATRpct={i.atrPct * 100:.2f}%",
            )
        if breakout_down:
            return Signal(
                action="enter_short", symbol=snap.symbol, strategy="breakout", confidence=0.65,
                suggestedEntry=price, suggestedStop=range_high,
                suggestedTp=price - (range_high - price) * 1.5,
                rationale=f"Breakdown below {range_low:.2f}, ATR14={i.atr14:.2f}",
            )
        return Signal(action="hold", symbol=snap.symbol, strategy="breakout",
                      confidence=0, rationale="No breakout")
