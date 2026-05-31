"""Phase 2: indicator parity vs the technicalindicators npm library.

The golden fixture was produced by running the exact JS functions used in
src/market/indicators.ts on a fixed 120-bar series. Python must match to 1e-9
(RSI matches the library's 2-dp rounding exactly).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from bybit_agent.market.indicators import adx, atr, bollinger, ema, macd, rsi

GOLDEN = json.loads((pathlib.Path(__file__).parent / "fixtures" / "indicators_golden.json").read_text())
INP = GOLDEN["input"]
OHLCV = {"open": INP["open"], "high": INP["high"], "low": INP["low"], "close": INP["close"], "volume": INP["volume"]}

TOL = 1e-9


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= TOL + 1e-9 * abs(b)


@pytest.mark.parametrize("period_key", ["ema9", "ema21", "ema50"])
def test_ema_parity(period_key):
    period = int(period_key[3:])
    got = ema(INP["close"], period)
    exp = GOLDEN[period_key]
    assert len(got) == len(exp)
    assert all(_close(g, e) for g, e in zip(got, exp))


def test_rsi_parity():
    got = rsi(INP["close"], 14)
    exp = GOLDEN["rsi14"]
    assert len(got) == len(exp)
    # RSI is rounded to 2 dp by the library — require exact equality.
    assert got == exp


def test_atr_parity():
    got = atr(OHLCV, 14)
    exp = GOLDEN["atr14"]
    assert len(got) == len(exp)
    assert all(_close(g, e) for g, e in zip(got, exp))


def test_bollinger_parity():
    got = bollinger(INP["close"], 20, 2)
    exp = GOLDEN["boll"]
    assert len(got) == len(exp)
    for g, e in zip(got, exp):
        assert _close(g.upper, e["upper"])
        assert _close(g.middle, e["middle"])
        assert _close(g.lower, e["lower"])


def test_macd_parity():
    got = macd(INP["close"], 12, 26, 9)
    exp = GOLDEN["macd"]
    assert len(got) == len(exp)
    for g, e in zip(got, exp):
        assert _close(g["MACD"], e["MACD"])
        if e.get("signal") is None:
            assert g["signal"] is None
        else:
            assert _close(g["signal"], e["signal"])
            assert _close(g["histogram"], e["histogram"])


def test_adx_parity():
    got = adx(OHLCV, 14)
    exp = GOLDEN["adx14"]
    assert len(got) == len(exp)
    for g, e in zip(got, exp):
        assert _close(g.adx, e["adx"])
        assert _close(g.pdi, e["pdi"])
        assert _close(g.mdi, e["mdi"])
