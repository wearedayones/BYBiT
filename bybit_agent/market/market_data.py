"""Market data + indicators + regime — ports src/market/MarketDataService.ts.

Builds a MarketSnapshot (price, funding, OHLCV, indicators, orderbook imbalance,
research) per symbol with a 30s cache, and classifies the trading regime.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from ..config.constants import INSTRUMENT_CACHE_TTL_MS, MAINNET_REST
from ..core.logger import child_logger
from ..exchange.bybit_client import BybitClient
from .indicators import OHLCV, adx, atr, bollinger, ema, last, macd, rsi
from .news import NewsResearchService, ResearchData

log = child_logger(module="market")

Regime = str  # 'trending' | 'ranging' | 'high_volatility' | 'crisis'


@dataclass
class Indicators:
    ema9: float
    ema21: float
    ema50: float
    rsi14: float
    macdValue: float
    macdSignal: float
    macdHistogram: float
    atr14: float
    atrPct: float
    boll: dict  # {upper, middle, lower}
    adxValue: float
    pdi: float
    mdi: float


@dataclass
class MarketSnapshot:
    symbol: str
    lastPrice: float
    markPrice: float
    fundingRate: float
    nextFundingMs: int
    ohlcv: OHLCV
    indicators: Indicators
    orderbook: dict  # {bidDepth, askDepth, imbalance}
    openInterest: float | None = None
    research: ResearchData | None = None


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def klines_to_ohlcv(klines: list[dict]) -> OHLCV:
    s = list(reversed(klines))
    return {
        "open": [_f(k["openPrice"]) for k in s],
        "high": [_f(k["highPrice"]) for k in s],
        "low": [_f(k["lowPrice"]) for k in s],
        "close": [_f(k["closePrice"]) for k in s],
        "volume": [_f(k["volume"]) for k in s],
    }


def compute_indicators(ohlcv: OHLCV) -> Indicators:
    closes = ohlcv["close"]
    e9, e21, e50 = ema(closes, 9), ema(closes, 21), ema(closes, 50)
    r = rsi(closes, 14)
    m = macd(closes)
    a = atr(ohlcv, 14)
    b = bollinger(closes, 20, 2)
    d = adx(ohlcv, 14)

    last_close = last(closes) or 0.0
    last_atr = last(a) or 0.0
    last_macd = last(m)
    last_boll = last(b)
    last_adx = last(d)

    return Indicators(
        ema9=last(e9) or 0.0,
        ema21=last(e21) or 0.0,
        ema50=last(e50) or 0.0,
        rsi14=last(r) if last(r) is not None else 50.0,
        macdValue=(last_macd["MACD"] if last_macd and last_macd["MACD"] is not None else 0.0),
        macdSignal=(last_macd["signal"] if last_macd and last_macd["signal"] is not None else 0.0),
        macdHistogram=(last_macd["histogram"] if last_macd and last_macd["histogram"] is not None else 0.0),
        atr14=last_atr,
        atrPct=(last_atr / last_close if last_close > 0 else 0.0),
        boll=({"upper": last_boll.upper, "middle": last_boll.middle, "lower": last_boll.lower}
              if last_boll else {"upper": 0.0, "middle": 0.0, "lower": 0.0}),
        adxValue=(last_adx.adx if last_adx else 0.0),
        pdi=(last_adx.pdi if last_adx else 0.0),
        mdi=(last_adx.mdi if last_adx else 0.0),
    )


def classify_regime(snap: MarketSnapshot) -> Regime:
    adx_value = snap.indicators.adxValue
    atr_pct = snap.indicators.atrPct
    funding_abs = abs(snap.fundingRate)
    # Crisis = chaotic extreme volatility (high ATR without direction) OR funding blowout.
    if (atr_pct > 0.05 and adx_value < 25) or funding_abs > 0.002:
        return "crisis"
    if adx_value > 25:
        return "trending"
    if atr_pct > 0.025:
        return "high_volatility"
    return "ranging"


class MarketDataService:
    def __init__(self, client: BybitClient, news_api_key: str | None = None) -> None:
        self._client = client
        self._snapshot_cache: dict[str, tuple[MarketSnapshot, int]] = {}
        self._instrument_cache: dict[str, tuple[dict, int]] = {}
        self._news = NewsResearchService(news_api_key)

    async def get_snapshot(self, symbol: str, category: str = "linear") -> MarketSnapshot:
        cached = self._snapshot_cache.get(symbol)
        now = int(time.time() * 1000)
        if cached and now < cached[1]:
            return cached[0]

        klines = await self._client.get_kline(category, symbol, "15", 200)
        ticker = await self._client.get_ticker(category, symbol)
        orderbook = await self._client.get_orderbook(category, symbol, 50)
        try:
            funding = await self._funding_rate_public(symbol)
        except Exception:  # noqa: BLE001
            funding = 0.0
        try:
            research = await self._news.get_research(symbol)
        except Exception:  # noqa: BLE001
            research = None

        ohlcv = klines_to_ohlcv(klines)
        indicators = compute_indicators(ohlcv)
        bid_depth = sum(_f(b["size"]) for b in orderbook["bids"])
        ask_depth = sum(_f(a["size"]) for a in orderbook["asks"])
        imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth + 1e-9)

        t = ticker or {}
        snap = MarketSnapshot(
            symbol=symbol,
            lastPrice=_f(t.get("lastPrice")),
            markPrice=_f(t.get("markPrice") or t.get("lastPrice")),
            fundingRate=funding,
            nextFundingMs=int(_f(t.get("nextFundingTime"))),
            ohlcv=ohlcv,
            indicators=indicators,
            orderbook={"bidDepth": bid_depth, "askDepth": ask_depth, "imbalance": imbalance},
            openInterest=(_f(t.get("openInterest")) or None),
            research=research,
        )
        self._snapshot_cache[symbol] = (snap, now + 30_000)
        return snap

    async def get_instrument(self, category: str, symbol: str) -> dict | None:
        key = f"{category}:{symbol}"
        cached = self._instrument_cache.get(key)
        now = int(time.time() * 1000)
        if cached and now < cached[1]:
            return cached[0]
        lst = await self._client.get_instruments_info(category, symbol)
        info = lst[0] if lst else None
        if info:
            self._instrument_cache[key] = (info, now + INSTRUMENT_CACHE_TTL_MS)
        return info

    async def _funding_rate_public(self, symbol: str) -> float:
        try:
            url = f"{MAINNET_REST}/v5/market/tickers?category=linear&symbol={symbol}"
            async with httpx.AsyncClient(timeout=8.0) as c:
                body = (await c.get(url)).json()
                if body.get("retCode") == 0 and body["result"]["list"]:
                    return _f(body["result"]["list"][0].get("fundingRate"))
        except Exception:  # noqa: BLE001
            pass
        return 0.0
