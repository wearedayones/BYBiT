"""CrowdedPositioning — contrarian entries based on Long/Short ratio extremes.

When >70% of traders are long (crowded_long), the crowded trade is to
be long. Contrarian thesis: the majority is wrong at extremes → enter short.
When <30% are long (crowded_short), the contrarian trade is long.

This is a direct L/S ratio signal with no price-indicator confirmation
required, so it fires in crowded regimes that other strategies miss.
Confidence scales linearly with how far the ratio is from the 0.5 midpoint.
"""
from __future__ import annotations

from ..base import Signal, StrategyContext

# Thresholds mirror classify_regime() in market_data.py.
CROWDED_LONG_THRESHOLD  = 0.70   # >70% longs → enter short (contrarian)
CROWDED_SHORT_THRESHOLD = 0.30   # <30% longs → enter long  (contrarian)
EXTREME_LONG_THRESHOLD  = 0.80   # >80% → higher confidence
EXTREME_SHORT_THRESHOLD = 0.20   # <20% → higher confidence


class CrowdedPositioning:
    name = "crowded_positioning"
    suitable_regimes = ["crowded_long", "crowded_short"]

    def evaluate(self, snap, ctx: StrategyContext) -> Signal:
        lsr = snap.longShortRatio
        if lsr is None:
            return Signal(action="hold", symbol=snap.symbol, strategy="crowded_positioning",
                          confidence=0, rationale="No L/S ratio data")

        price = snap.lastPrice
        atr = snap.indicators.atr14

        if lsr > CROWDED_LONG_THRESHOLD:
            # Crowd is too long → contrarian short.
            excess = lsr - CROWDED_LONG_THRESHOLD                     # 0 … 0.30
            confidence = min(0.88, 0.55 + excess * 1.1)               # 0.55 … 0.88
            if lsr > EXTREME_LONG_THRESHOLD:
                confidence = min(0.92, confidence + 0.05)
            return Signal(
                action="enter_short",
                symbol=snap.symbol,
                strategy="crowded_positioning",
                confidence=confidence,
                suggestedEntry=price,
                suggestedStop=price * 1.04,
                suggestedTp=price * 0.97,
                rationale=f"Crowded long: {lsr:.0%} longs — contrarian short",
            )

        if lsr < CROWDED_SHORT_THRESHOLD:
            # Crowd is too short → contrarian long.
            excess = CROWDED_SHORT_THRESHOLD - lsr                     # 0 … 0.30
            confidence = min(0.88, 0.55 + excess * 1.1)
            if lsr < EXTREME_SHORT_THRESHOLD:
                confidence = min(0.92, confidence + 0.05)
            return Signal(
                action="enter_long",
                symbol=snap.symbol,
                strategy="crowded_positioning",
                confidence=confidence,
                suggestedEntry=price,
                suggestedStop=price * 0.96,
                suggestedTp=price * 1.03,
                rationale=f"Crowded short: {lsr:.0%} longs — contrarian long",
            )

        return Signal(action="hold", symbol=snap.symbol, strategy="crowded_positioning",
                      confidence=0, rationale=f"L/S ratio neutral ({lsr:.0%} longs)")
