"""Technical indicators — hand-ported from the `technicalindicators` npm library
used by src/market/indicators.ts, to byte/float parity (RSI matches its 2-dp
``toFixed`` rounding; others are full precision).

The library computes everything with incremental generators seeded off a simple
moving average. We mirror that with small provider classes so emission timing and
seeding match exactly; parity is locked by tests/fixtures/indicators_golden.json.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TypedDict


class OHLCV(TypedDict):
    open: list[float]
    high: list[float]
    low: list[float]
    close: list[float]
    volume: list[float]


def _to_fixed_2(x: float) -> float:
    """Replicate JS Number.prototype.toFixed(2) on the exact IEEE double."""
    return float(Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ── Incremental providers (mirror technicalindicators generators) ─────────────
class _SMA:
    def __init__(self, period: int) -> None:
        self.period = period
        self._list: deque[float] = deque()
        self._list.append(0.0)
        self._sum = 0.0
        self._counter = 1

    def next_value(self, x: float) -> float | None:
        if self._counter < self.period:
            self._counter += 1
            self._list.append(x)
            self._sum += x
            return None
        self._sum = self._sum - self._list.popleft() + x
        self._list.append(x)
        return self._sum / self.period


class _EMA:
    def __init__(self, period: int) -> None:
        self._exp = 2 / (period + 1)
        self._sma = _SMA(period)
        self._prev: float | None = None

    def next_value(self, x: float) -> float | None:
        if self._prev is not None:
            self._prev = (x - self._prev) * self._exp + self._prev
            return self._prev
        seed = self._sma.next_value(x)
        if seed:  # library uses truthiness here (prices are never 0)
            self._prev = seed
            return seed
        return None


class _WEMA:
    """Wilder's moving average (RMA): exponent 1/period, SMA seed."""

    def __init__(self, period: int) -> None:
        self._exp = 1 / period
        self._sma = _SMA(period)
        self._prev: float | None = None

    def next_value(self, x: float) -> float | None:
        if self._prev is not None:
            self._prev = (x - self._prev) * self._exp + self._prev
            return self._prev
        seed = self._sma.next_value(x)
        if seed is not None:
            self._prev = seed
            return seed
        return None


class _WilderSmoothing:
    """Seeds with the SUM of the first `period`, then result - result/period + x."""

    def __init__(self, period: int) -> None:
        self.period = period
        self._counter = 1
        self._sum = 0.0
        self._result: float | None = 0.0

    def next_value(self, x: float) -> float | None:
        if self._counter < self.period:
            self._counter += 1
            self._sum += x
            self._result = None
        elif self._counter == self.period:
            self._counter += 1
            self._sum += x
            self._result = self._sum
        else:
            assert self._result is not None
            self._result = self._result - (self._result / self.period) + x
        return self._result


class _SD:
    def __init__(self, period: int) -> None:
        self.period = period
        self._sma = _SMA(period)
        self._set: deque[float] = deque(maxlen=period)

    def next_value(self, x: float) -> float | None:
        self._set.append(x)
        mean = self._sma.next_value(x)
        if mean:
            s = sum((v - mean) ** 2 for v in self._set)
            return math.sqrt(s / self.period)
        return None


class _AverageGainLoss:
    def __init__(self, period: int, gain: bool) -> None:
        self.period = period
        self._gain = gain
        self._counter = 1
        self._sum = 0.0
        self._avg: float | None = None
        self._last: float | None = None
        self._primed = False

    def next_value(self, x: float) -> float | None:
        if not self._primed:
            self._last = x
            self._primed = True
            return None
        assert self._last is not None
        diff = x - self._last if self._gain else self._last - x
        move = diff if diff > 0 else 0.0
        if move > 0:
            self._sum += move
        if self._counter < self.period:
            self._counter += 1
        elif self._avg is None:
            self._avg = self._sum / self.period
        else:
            self._avg = (self._avg * (self.period - 1) + move) / self.period
        self._last = x
        return self._avg


class _TrueRange:
    def __init__(self) -> None:
        self._prev_close: float | None = None

    def next_value(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is None:
            self._prev_close = close
            return None
        tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        return tr


class _PlusDM:
    def __init__(self) -> None:
        self._last: tuple[float, float] | None = None  # (high, low)

    def next_value(self, high: float, low: float) -> float | None:
        if self._last is None:
            self._last = (high, low)
            return None
        up = high - self._last[0]
        down = self._last[1] - low
        self._last = (high, low)
        return up if (up > down and up > 0) else 0.0


class _MinusDM:
    def __init__(self) -> None:
        self._last: tuple[float, float] | None = None

    def next_value(self, high: float, low: float) -> float | None:
        if self._last is None:
            self._last = (high, low)
            return None
        up = high - self._last[0]
        down = self._last[1] - low
        self._last = (high, low)
        return down if (down > up and down > 0) else 0.0


# ── Public calculate-style functions (match the *.calculate API) ──────────────
def ema(values: list[float], period: int) -> list[float]:
    prov = _EMA(period)
    out: list[float] = []
    for v in values:
        r = prov.next_value(v)
        if r is not None:
            out.append(r)
    return out


def rsi(values: list[float], period: int = 14) -> list[float]:
    gain = _AverageGainLoss(period, gain=True)
    loss = _AverageGainLoss(period, gain=False)
    out: list[float] = []
    for v in values:
        ag = gain.next_value(v)
        al = loss.next_value(v)
        if ag is not None and al is not None:
            if al == 0:
                out.append(100.0)
            elif ag == 0:
                out.append(0.0)
            else:
                rs = ag / al
                out.append(_to_fixed_2(100 - (100 / (1 + rs))))
    return out


class MACDResult(TypedDict):
    MACD: float | None
    signal: float | None
    histogram: float | None


def macd(
    values: list[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> list[MACDResult]:
    fast = ema(values, fast_period)
    slow = ema(values, slow_period)
    # Align tails: fast starts at index (fast_period-1), slow at (slow_period-1).
    offset = (slow_period - 1) - (fast_period - 1)
    macd_line = [fast[i + offset] - slow[i] for i in range(len(slow))]
    signal_line = ema(macd_line, signal_period)
    sig_offset = signal_period - 1
    out: list[MACDResult] = []
    for i, mv in enumerate(macd_line):
        if i >= sig_offset:
            sv = signal_line[i - sig_offset]
            out.append({"MACD": mv, "signal": sv, "histogram": mv - sv})
        else:
            out.append({"MACD": mv, "signal": None, "histogram": None})
    return out


def atr(ohlcv: OHLCV, period: int = 14) -> list[float]:
    tr = _TrueRange()
    wema = _WEMA(period)
    out: list[float] = []
    highs, lows, closes = ohlcv["high"], ohlcv["low"], ohlcv["close"]
    for h, low_, c in zip(highs, lows, closes):
        t = tr.next_value(h, low_, c)
        if t is None:
            continue
        a = wema.next_value(t)
        if a is not None:
            out.append(a)
    return out


@dataclass
class BollResult:
    upper: float
    middle: float
    lower: float
    pb: float


def bollinger(values: list[float], period: int = 20, std_dev: float = 2) -> list[BollResult]:
    sma = _SMA(period)
    sd = _SD(period)
    out: list[BollResult] = []
    for v in values:
        mean = sma.next_value(v)
        s = sd.next_value(v)
        if mean:
            assert s is not None
            upper = mean + s * std_dev
            lower = mean - s * std_dev
            out.append(BollResult(upper=upper, middle=mean, lower=lower,
                                  pb=(v - lower) / (upper - lower) if upper != lower else 0.5))
    return out


@dataclass
class ADXResult:
    adx: float
    pdi: float
    mdi: float


def adx(ohlcv: OHLCV, period: int = 14) -> list[ADXResult]:
    tr = _TrueRange()
    plus_dm = _PlusDM()
    minus_dm = _MinusDM()
    ema_tr = _WilderSmoothing(period)
    ema_pdm = _WilderSmoothing(period)
    ema_mdm = _WilderSmoothing(period)
    ema_dx = _WEMA(period)
    out: list[ADXResult] = []
    highs, lows, closes = ohlcv["high"], ohlcv["low"], ohlcv["close"]
    last_pdi = last_mdi = 0.0
    for h, low_, c in zip(highs, lows, closes):
        calc_tr = tr.next_value(h, low_, c)
        calc_pdm = plus_dm.next_value(h, low_)
        calc_mdm = minus_dm.next_value(h, low_)
        if calc_tr is None:
            continue
        s_tr = ema_tr.next_value(calc_tr)
        s_pdm = ema_pdm.next_value(calc_pdm)  # type: ignore[arg-type]
        s_mdm = ema_mdm.next_value(calc_mdm)  # type: ignore[arg-type]
        if s_tr is not None and s_pdm is not None and s_mdm is not None:
            last_pdi = s_pdm * 100 / s_tr
            last_mdi = s_mdm * 100 / s_tr
            di_diff = abs(last_pdi - last_mdi)
            di_sum = last_pdi + last_mdi
            dx = (di_diff / di_sum) * 100 if di_sum > 0 else 0.0
            smoothed = ema_dx.next_value(dx)
            if smoothed is not None:
                out.append(ADXResult(adx=smoothed, pdi=last_pdi, mdi=last_mdi))
    return out


def last(arr: list):
    return arr[-1] if arr else None
