"""Grid bot manager — ports src/bots/BotManager.ts.

Monitors active bots, launches new spot-grid bots in ranging regimes.

Investment sizing:
  - Per-bot allocation = max(MIN_GRID_USDT_FLOOR, equity × GRID_EQUITY_PCT)
  - Capped by MAX_CONCURRENT_GRID_BOTS so total grid exposure ≤ equity × GRID_EQUITY_PCT × max_bots
  - With $78 equity: max($15, $19.50) = $19.50 per bot, up to 3 bots = $58.50 max

Symbol source:
  - AgentLoop discovery finds affordable ranging-regime symbols every 5 min
  - BotManager takes those snapshots, filters for ranging regime, and opens grids
  - Grid range = price ± 4 × ATR14 (wider than 3× to give the bot breathing room)
"""
from __future__ import annotations

import json
import math
from typing import Any

from bybit_agent.core.logger import get_logger
from bybit_agent.market.market_data import MarketSnapshot, classify_regime
from bybit_agent.persistence.db import NeonHttpClient
from bybit_agent.risk.risk_manager import PortfolioState

log = get_logger().bind(module="bot-manager")

MIN_GRID_USDT_FLOOR = 15.0       # never allocate less than this per grid bot
GRID_EQUITY_PCT     = 0.25       # target 25% of equity per bot (was 5% — too small for small accounts)
MAX_CONCURRENT_GRID_BOTS = 3     # cap total active grids so equity isn't fully consumed


class BotManager:
    def __init__(self, client: Any, db: NeonHttpClient, is_testnet: bool) -> None:
        self._client = client
        self._db = db
        self._is_testnet = is_testnet

    async def tick(self, snapshots: list[MarketSnapshot], portfolio: PortfolioState) -> None:
        await self._monitor_active_bots()
        await self._consider_new_bots(snapshots, portfolio)

    async def stop_all(self) -> None:
        bots = await self._db.fetch(
            "SELECT * FROM bot_instances WHERE status = 'active' AND is_paper = $1",
            self._is_testnet,
        )
        for bot in bots:
            try:
                if bot["bot_type"] == "spot_grid":
                    await self._client.close_spot_grid(bot["exchange_bot_id"], 1)
                await self._db.execute(
                    "UPDATE bot_instances SET status = 'stopped' WHERE id = $1",
                    bot["id"],
                )
            except Exception as e:
                log.error("Error stopping bot", bot_id=bot["id"], error=str(e))

    async def _monitor_active_bots(self) -> None:
        bots = await self._db.fetch(
            "SELECT * FROM bot_instances WHERE status = 'active' AND is_paper = $1",
            self._is_testnet,
        )
        for bot in bots:
            try:
                detail: dict | None = None
                if bot["bot_type"] == "spot_grid" and bot.get("exchange_bot_id"):
                    detail = await self._client.get_spot_grid_detail(bot["exchange_bot_id"])
                elif bot["bot_type"] == "futures_grid" and bot.get("exchange_bot_id"):
                    detail = {"bot_id": bot["exchange_bot_id"]}

                if detail:
                    pnl = float(detail.get("profit") or detail.get("realized_pnl") or 0)
                    await self._db.execute(
                        "INSERT INTO bot_performance (bot_instance_id, realized_pnl, unrealized_pnl, roi) VALUES ($1, $2, 0, 0)",
                        bot["id"], pnl,
                    )
            except Exception as e:
                log.warning("Error monitoring bot", bot_id=bot["id"], error=str(e))

    async def _consider_new_bots(
        self, snapshots: list[MarketSnapshot], portfolio: PortfolioState
    ) -> None:
        # How many grids are already running?
        active_rows = await self._db.fetch(
            "SELECT COUNT(*) AS n FROM bot_instances WHERE status = 'active' AND is_paper = $1",
            self._is_testnet,
        )
        active_count = int((active_rows[0].get("n") or 0) if active_rows else 0)
        if active_count >= MAX_CONCURRENT_GRID_BOTS:
            log.debug("Max grid bots reached — not creating more", active=active_count)
            return

        # Per-bot investment: 25% of equity, but at least MIN_GRID_USDT_FLOOR.
        investment = max(MIN_GRID_USDT_FLOOR, portfolio.equity * GRID_EQUITY_PCT)
        # Safety cap: never put more than 50% of equity into a single grid.
        investment = min(investment, portfolio.equity * 0.50)

        if investment < MIN_GRID_USDT_FLOOR:
            log.debug("Equity too small for grid bot", equity=portfolio.equity,
                      needed=MIN_GRID_USDT_FLOOR)
            return

        for snap in snapshots:
            if active_count >= MAX_CONCURRENT_GRID_BOTS:
                break
            if classify_regime(snap) != "ranging":
                continue

            # Already have an active grid on this symbol?
            existing = await self._db.fetch(
                "SELECT id FROM bot_instances WHERE symbol = $1 AND status = 'active' LIMIT 1",
                snap.symbol,
            )
            if existing:
                continue

            try:
                await self._create_grid_bot(snap, investment)
                active_count += 1
            except Exception as e:
                log.warning("Failed to create grid bot", symbol=snap.symbol, error=str(e))

    async def _create_grid_bot(self, snap: MarketSnapshot, investment: float) -> None:
        price = snap.lastPrice
        atr = snap.indicators.atr14

        # 4× ATR range gives the grid plenty of room on small accounts.
        # floor/ceil to 2 decimal places so Bybit accepts the price levels.
        min_price = math.floor((price - atr * 4) * 100) / 100
        max_price = math.ceil((price + atr * 4) * 100) / 100
        cell_number = 10

        # Strip the perpetual suffix if present (BTCUSDT-PERP → BTCUSDT for spot).
        spot_symbol = snap.symbol.replace("-PERP", "").replace("PERP", "")

        config = {
            "symbol": spot_symbol,
            "minPrice": min_price, "maxPrice": max_price,
            "cellNumber": cell_number, "totalInvestment": investment,
        }

        log.info("Creating spot grid bot", symbol=spot_symbol,
                 price=price, min_price=min_price, max_price=max_price,
                 cell_number=cell_number, investment=round(investment, 2))

        # Validate first — Bybit enforces per-symbol minimums and price tick rules.
        await self._client.validate_spot_grid({
            "symbol": spot_symbol,
            "min_price": str(min_price), "max_price": str(max_price),
            "cell_number": cell_number, "total_investment": str(investment),
        })
        result = await self._client.create_spot_grid({
            "symbol": spot_symbol,
            "min_price": str(min_price), "max_price": str(max_price),
            "cell_number": cell_number, "total_investment": str(investment),
        })

        # Bybit returns orderId on mainnet; testnet may return 0/None.
        grid_id = (result.get("orderId") or result.get("grid_id")
                   or result.get("gridId") or result.get("botId"))

        await self._db.execute(
            """INSERT INTO bot_instances
                 (bot_type, exchange_bot_id, symbol, category, status, config, state, is_paper)
               VALUES ('spot_grid', $1, $2, 'spot', 'active', $3::jsonb, '{}'::jsonb, $4)""",
            str(grid_id) if grid_id else None, spot_symbol, json.dumps(config), self._is_testnet,
        )
        log.info("Spot grid bot created", symbol=spot_symbol,
                 grid_id=grid_id, investment=round(investment, 2),
                 testnet=self._is_testnet)
