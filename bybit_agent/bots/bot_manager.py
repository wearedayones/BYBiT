"""Grid bot manager — ports src/bots/BotManager.ts.

Monitors active bots, launches new spot-grid bots in ranging regimes (5% equity per bot,
minimum 10 USDT). Validate-before-create rule: validateSpotGrid() must succeed first.
is_paper tags DB rows by environment (testnet vs mainnet) for reporting only.
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

MIN_GRID_INVESTMENT_USDT = 10


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
                    "UPDATE bot_instances SET status = 'stopped', updated_at = now() WHERE id = $1",
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
        for snap in snapshots:
            if classify_regime(snap) != "ranging":
                continue

            existing = await self._db.fetch(
                "SELECT id FROM bot_instances WHERE symbol = $1 AND status = 'active' LIMIT 1",
                snap.symbol,
            )
            if existing:
                continue

            investment = portfolio.equity * 0.05
            if investment < MIN_GRID_INVESTMENT_USDT:
                continue

            try:
                await self._create_grid_bot(snap, investment)
            except Exception as e:
                log.warning("Failed to create grid bot", symbol=snap.symbol, error=str(e))

    async def _create_grid_bot(self, snap: MarketSnapshot, investment: float) -> None:
        price = snap.lastPrice
        atr = snap.indicators.atr14
        min_price = math.floor((price - atr * 3) * 100) / 100
        max_price = math.ceil((price + atr * 3) * 100) / 100
        cell_number = 10
        config = {
            "symbol": snap.symbol,
            "minPrice": min_price, "maxPrice": max_price,
            "cellNumber": cell_number, "totalInvestment": investment,
        }

        # Validate first — Bybit enforces per-symbol minimums.
        await self._client.validate_spot_grid({
            "symbol": snap.symbol,
            "min_price": str(min_price), "max_price": str(max_price),
            "cell_number": cell_number, "total_investment": str(investment),
        })
        result = await self._client.create_spot_grid({
            "symbol": snap.symbol,
            "min_price": str(min_price), "max_price": str(max_price),
            "cell_number": cell_number, "total_investment": str(investment),
        })

        await self._db.execute(
            """INSERT INTO bot_instances
                 (bot_type, exchange_bot_id, symbol, category, status, config, state, is_paper)
               VALUES ('spot_grid', $1, $2, 'spot', 'active', $3::jsonb, '{}'::jsonb, $4)""",
            result.get("grid_id"), snap.symbol, json.dumps(config), self._is_testnet,
        )
        log.info("Created spot grid bot", symbol=snap.symbol,
                 grid_id=result.get("grid_id"), testnet=self._is_testnet)
