"""Phase 2: strategy + regime smoke/parity tests using the indicator fixture."""

from __future__ import annotations

import json
import pathlib

from bybit_agent.market.indicators import OHLCV
from bybit_agent.market.market_data import Indicators, MarketSnapshot, classify_regime
from bybit_agent.strategy.impl.breakout import Breakout
from bybit_agent.strategy.impl.funding_harvest import FundingHarvest
from bybit_agent.strategy.impl.mean_reversion import MeanReversion
from bybit_agent.strategy.impl.trend_momentum import TrendMomentum

GOLDEN = json.loads((pathlib.Path(__file__).parent / "fixtures" / "indicators_golden.json").read_text())
INP = GOLDEN["input"]


def _snap(funding=0.0, **ind_over) -> MarketSnapshot:
    ind = dict(
        ema9=110, ema21=108, ema50=105, rsi14=60, macdValue=1, macdSignal=0.5,
        macdHistogram=0.5, atr14=2.0, atrPct=0.018,
        boll={"upper": 112, "middle": 108, "lower": 104}, adxValue=28, pdi=30, mdi=15,
    )
    ind.update(ind_over)
    ohlcv: OHLCV = {k: INP[k] for k in ("open", "high", "low", "close", "volume")}
    return MarketSnapshot(
        symbol="BTCUSDT", lastPrice=111.0, markPrice=111.0, fundingRate=funding,
        nextFundingMs=0, ohlcv=ohlcv, indicators=Indicators(**ind),
        orderbook={"bidDepth": 1, "askDepth": 1, "imbalance": 0},
    )


def test_trend_momentum_goes_long_on_bull_stack():
    sig = TrendMomentum().evaluate(_snap(), None)
    assert sig.action == "enter_long"
    assert sig.suggestedStop < sig.suggestedEntry < sig.suggestedTp


def test_trend_momentum_holds_without_stack():
    sig = TrendMomentum().evaluate(_snap(ema9=104, ema21=106, ema50=108, macdHistogram=-0.5), None)
    assert sig.action in ("hold", "enter_short")


def test_mean_reversion_long_at_lower_band():
    snap = _snap(rsi14=30, atrPct=0.01, boll={"upper": 120, "middle": 115, "lower": 111})
    sig = MeanReversion().evaluate(snap, None)
    assert sig.action == "enter_long"


def test_mean_reversion_skips_when_volatile():
    sig = MeanReversion().evaluate(_snap(atrPct=0.05), None)
    assert sig.action == "hold"


def test_breakout_needs_price_above_range():
    sig = Breakout().evaluate(_snap(adxValue=20, rsi14=60), None)
    assert sig.action in ("enter_long", "hold")


def test_funding_harvest_shorts_high_positive_funding():
    sig = FundingHarvest().evaluate(_snap(funding=0.002), None)  # ~219% APR
    assert sig.action == "enter_short"
    assert sig.suggestedTp is None  # no price target


def test_funding_harvest_holds_low_funding():
    sig = FundingHarvest().evaluate(_snap(funding=0.00001), None)
    assert sig.action == "hold"


def test_classify_regime_matches_thresholds():
    assert classify_regime(_snap(adxValue=30, atrPct=0.01)) == "trending"
    assert classify_regime(_snap(adxValue=10, atrPct=0.03)) == "high_volatility"
    assert classify_regime(_snap(adxValue=10, atrPct=0.01)) == "ranging"
    assert classify_regime(_snap(adxValue=10, atrPct=0.09)) == "crisis"
    assert classify_regime(_snap(funding=0.003)) == "crisis"
