"""MarketDiscovery — ports src/market/MarketDiscovery.ts.

Surveys every linear perp and returns a ranked, balance-affordable watch list, so a
$10 and a $10k account each see a tradable universe. Falls back to majors on failure.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from ..core.logger import child_logger
from ..exchange.bybit_client import BybitClient
from ..persistence.db import get_db

log = child_logger(module="market-discovery")


@dataclass
class DiscoveryConfig:
    maxSymbols: int = 6
    minTurnover24h: float = 1_000
    minVolatility: float = 0.0
    maxVolatility: float = 0.40
    affordabilityLeverage: float = 5
    maxMarginFraction: float = 0.5


DISCOVERY_DEFAULTS = DiscoveryConfig()


@dataclass
class ScoredMarket:
    symbol: str
    turnover24h: float
    volatility: float
    price: float
    minOrderQty: float
    minNotional: float
    score: float
    affordable: bool


class MarketDiscovery:
    INSTRUMENT_TTL = 2 * 60 * 60 * 1000

    def __init__(self, client: BybitClient, cfg: DiscoveryConfig = DISCOVERY_DEFAULTS) -> None:
        self._client = client
        self._cfg = cfg
        self._instruments: dict[str, dict] = {}
        self._last_instrument_fetch = 0

    async def discover(self, equity: float) -> list[str]:
        try:
            tickers = await self._client.get_tickers("linear")
            await self._refresh_instruments()

            scored: list[ScoredMarket] = []
            for t in tickers:
                inst = self._instruments.get(t["symbol"])
                if not inst or inst.get("status") != "Trading":
                    continue
                if inst.get("quoteCoin") != "USDT":
                    continue
                m = self._score_market(t, inst, equity)
                if m:
                    scored.append(m)

            tradable = sorted([m for m in scored if m.affordable],
                              key=lambda m: m.score, reverse=True)[:self._cfg.maxSymbols]

            if not tradable:
                log.warning("No affordable markets found — falling back to majors", equity=equity)
                return self._fallback()

            await self._persist(tradable, equity)
            symbols = [m.symbol for m in tradable]
            log.info("Market discovery complete", equity=equity, count=len(symbols), symbols=symbols)
            return symbols
        except Exception as e:  # noqa: BLE001
            log.error("Discovery failed — using fallback symbols", error=str(e))
            return self._fallback()

    def _score_market(self, t: dict, inst: dict, equity: float) -> ScoredMarket | None:
        price = _f(t.get("lastPrice"))
        turnover = _f(t.get("turnover24h"))
        volatility = abs(_f(t.get("price24hPcnt")))
        min_qty = _f(inst["lotSizeFilter"]["minOrderQty"])
        if not (price > 0) or not (min_qty > 0):
            return None

        min_notional = min_qty * price
        if turnover < self._cfg.minTurnover24h:
            return None
        if self._cfg.minVolatility > 0 and volatility < self._cfg.minVolatility:
            return None
        if volatility > self._cfg.maxVolatility:
            return None

        required_margin = min_notional / self._cfg.affordabilityLeverage
        affordable = equity > 0 and required_margin <= equity * self._cfg.maxMarginFraction

        liquidity_score = math.log10(turnover + 1) / 10
        vol_score = min(volatility, 0.15) * 2
        headroom = (1 - min(1, required_margin / (equity * self._cfg.maxMarginFraction))) if equity > 0 else 0
        score = liquidity_score + vol_score + headroom * 0.3

        return ScoredMarket(t["symbol"], turnover, volatility, price, min_qty, min_notional, score, affordable)

    async def _refresh_instruments(self) -> None:
        now = int(time.time() * 1000)
        if now - self._last_instrument_fetch < self.INSTRUMENT_TTL and self._instruments:
            return
        lst = await self._client.get_instruments_info("linear")
        self._instruments = {i["symbol"]: i for i in lst}
        self._last_instrument_fetch = now
        log.debug("Instrument universe refreshed", count=len(self._instruments))

    async def _persist(self, markets: list[ScoredMarket], equity: float) -> None:
        db = get_db()
        for m in markets:
            try:
                await db.execute(
                    """
                    INSERT INTO discovered_markets
                      (symbol, turnover_24h, volatility, price, min_notional, score, equity_at_discovery)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    m.symbol, m.turnover24h, m.volatility, m.price, m.minNotional, m.score, equity,
                )
            except Exception:  # noqa: BLE001
                pass

    def _fallback(self) -> list[str]:
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default
