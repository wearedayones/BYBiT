"""News / sentiment research — ports src/market/NewsResearchService.ts.

Fear & Greed (alternative.me), CoinGecko trending, and headline keyword scoring feed a
sentiment multiplier into the decision engine. Network failures degrade gracefully to
neutral. computeSentimentMultiplier is pure and unit-tested for parity.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from ..core.logger import child_logger

log = child_logger(module="news-research")

CACHE_TTL_MS = 15 * 60 * 1000

POSITIVE_KEYWORDS = [
    "bull", "surge", "rally", "gain", "rise", "breakout", "bullish", "ath",
    "adoption", "milestone", "upgrade", "launch", "partnership", "growth",
    "profit", "recover", "buy", "pump", "support", "accumulate", "inflow",
]
NEGATIVE_KEYWORDS = [
    "bear", "crash", "drop", "fall", "decline", "bearish", "hack", "exploit",
    "sec", "ban", "regulation", "lawsuit", "fraud", "collapse", "plunge",
    "fear", "warning", "sell", "dump", "outflow", "liquidation", "vulnerable",
]
SYMBOL_NAMES = {
    "BTCUSDT": ["bitcoin", "btc"],
    "ETHUSDT": ["ethereum", "eth", "ether"],
    "SOLUSDT": ["solana", "sol"],
}


@dataclass
class ResearchData:
    fearGreedIndex: int
    fearGreedLabel: str
    globalSentimentScore: float
    symbolSentimentScore: float
    trendingRank: int | None = None
    sources: list[str] = field(default_factory=list)


def _score_headlines(headlines: list[str]) -> float:
    if not headlines:
        return 0.0
    pos = neg = 0
    for h in headlines:
        lower = h.lower()
        pos += sum(1 for k in POSITIVE_KEYWORDS if k in lower)
        neg += sum(1 for k in NEGATIVE_KEYWORDS if k in lower)
    total = pos + neg
    if total == 0:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / total))


@dataclass
class _Global:
    fearGreedIndex: int
    fearGreedLabel: str
    globalSentimentScore: float
    trendingCoins: list[dict]
    headlines: list[str]


class NewsResearchService:
    def __init__(self, news_api_key: str | None = None) -> None:
        self._news_api_key = news_api_key
        self._cache: tuple[_Global, int] | None = None

    async def get_research(self, symbol: str) -> ResearchData:
        g = await self._get_global()
        names = SYMBOL_NAMES.get(symbol, [symbol.replace("USDT", "").lower()])
        symbol_headlines = [h for h in g.headlines if any(n in h.lower() for n in names)]
        symbol_score = _score_headlines(symbol_headlines) if symbol_headlines else g.globalSentimentScore
        trending = next(
            (c for c in g.trendingCoins
             if any(c["symbol"].lower() == n or n in c["id"].lower() for n in names)),
            None,
        )
        return ResearchData(
            fearGreedIndex=g.fearGreedIndex,
            fearGreedLabel=g.fearGreedLabel,
            globalSentimentScore=g.globalSentimentScore,
            symbolSentimentScore=symbol_score,
            trendingRank=trending["rank"] if trending else None,
        )

    async def _get_global(self) -> _Global:
        now = int(time.time() * 1000)
        if self._cache and now < self._cache[1]:
            return self._cache[0]
        fg = await self._fetch_fear_greed()
        trending = await self._fetch_trending()
        headlines = await self._fetch_headlines()
        g = _Global(fg[0], fg[1], _score_headlines(headlines), trending, headlines)
        self._cache = (g, now + CACHE_TTL_MS)
        return g

    async def _fetch_fear_greed(self) -> tuple[int, str]:
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get("https://api.alternative.me/fng/?limit=1")
                item = (r.json().get("data") or [None])[0]
                if item:
                    return int(item["value"]), item["value_classification"]
        except Exception:  # noqa: BLE001
            pass
        return 50, "Neutral"

    async def _fetch_trending(self) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get("https://api.coingecko.com/api/v3/search/trending",
                                headers={"Accept": "application/json"})
                coins = r.json().get("coins") or []
                return [{"id": x["item"]["id"], "symbol": x["item"]["symbol"], "rank": i + 1}
                        for i, x in enumerate(coins)]
        except Exception:  # noqa: BLE001
            return []

    async def _fetch_headlines(self) -> list[str]:
        # NewsAPI when keyed, else best-effort skip (RSS parsing omitted in the port).
        if not self._news_api_key:
            return []
        try:
            url = ("https://newsapi.org/v2/everything?q=bitcoin+OR+ethereum+OR+crypto"
                   "&language=en&sortBy=publishedAt&pageSize=30&apiKey=" + self._news_api_key)
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(url)
                arts = r.json().get("articles") or []
                return [f"{a.get('title', '')} {a.get('description', '')}" for a in arts]
        except Exception:  # noqa: BLE001
            return []


def compute_sentiment_multiplier(research: ResearchData | None, action: str) -> float:
    if research is None:
        return 1.0
    fgi = research.fearGreedIndex
    ss = research.symbolSentimentScore

    fg = 1.0
    if fgi < 20:
        fg = 1.15 if action == "enter_long" else 0.85
    elif fgi < 40:
        fg = 1.05 if action == "enter_long" else 0.95
    elif fgi > 80:
        fg = 1.15 if action == "enter_short" else 0.85
    elif fgi > 60:
        fg = 1.05 if action == "enter_short" else 0.95

    news = 1.0
    if ss > 0.3:
        news = 1.08 if action == "enter_long" else 0.92
    elif ss < -0.3:
        news = 1.08 if action == "enter_short" else 0.92

    return fg * 0.6 + news * 0.4
