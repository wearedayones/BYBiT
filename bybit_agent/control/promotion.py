"""Testnet → mainnet promotion — ports src/control/PromotionManager.ts.

Only evaluates when trading_mode = 'testnet_live' (real testnet fills accumulate).
On success it writes trading_mode = 'mainnet_live' AND env = 'mainnet' so the next
cycle fires real orders. Shadow mode never triggers promotion.
"""
from __future__ import annotations

from bybit_agent.config.constants import PROMOTION_DEFAULTS
from bybit_agent.core.logger import get_logger
from bybit_agent.persistence.db import NeonHttpClient

log = get_logger().bind(module="promotion")


class PromotionManager:
    def __init__(self, db: NeonHttpClient) -> None:
        self._db = db

    async def evaluate(self) -> bool:
        ag_rows = await self._db.fetch("SELECT * FROM agent_state WHERE id = 'singleton' LIMIT 1")
        ag = ag_rows[0] if ag_rows else None
        if not ag:
            return False

        # Only evaluate when the loop is actively placing real testnet orders.
        mode = (ag.get("trading_mode") or "shadow").strip()
        if mode != "testnet_live":
            return False

        raw_criteria = ag.get("promotion_criteria") or {}
        criteria = {
            "min_cycles":        int(raw_criteria.get("min_cycles",        PROMOTION_DEFAULTS["MIN_CYCLES"])),
            "min_sharpe":        float(raw_criteria.get("min_sharpe",      PROMOTION_DEFAULTS["MIN_SHARPE"])),
            "min_win_rate":      float(raw_criteria.get("min_win_rate",    PROMOTION_DEFAULTS["MIN_WIN_RATE"])),
            "max_drawdown_pct":  float(raw_criteria.get("max_drawdown_pct",PROMOTION_DEFAULTS["MAX_DRAWDOWN_PCT"])),
        }

        cycle_count = int(ag.get("promotion_cycle_count") or 0)
        if cycle_count < criteria["min_cycles"]:
            await self._db.execute(
                "UPDATE agent_state SET promotion_cycle_count = promotion_cycle_count + 1 WHERE id = 'singleton'"
            )
            return False

        # Real testnet fills (is_paper=false) from the last 3 days.
        trade_rows = await self._db.fetch(
            """SELECT AVG(CASE WHEN realized_pnl > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                      SUM(realized_pnl) as total_pnl, COUNT(*) as count
               FROM trades WHERE is_paper = false AND closed_at > now() - INTERVAL '3 days'"""
        )
        equity_rows = await self._db.fetch(
            """SELECT MIN(drawdown_pct) as min_drawdown,
                      AVG(total_equity) as avg_equity,
                      STDDEV(total_equity) as stddev_equity
               FROM equity_snapshots WHERE ts > now() - INTERVAL '3 days' AND env = 'testnet'"""
        )

        trades = trade_rows[0] if trade_rows else {}
        equity = equity_rows[0] if equity_rows else {}

        count = int(trades.get("count") or 0)
        if count < 10:
            log.debug("Not enough live testnet trades for promotion", count=count)
            await self._db.execute(
                "UPDATE agent_state SET promotion_cycle_count = promotion_cycle_count + 1 WHERE id = 'singleton'"
            )
            return False

        win_rate = float(trades.get("win_rate") or 0)
        total_pnl = float(trades.get("total_pnl") or 0)
        max_drawdown = abs(float(equity.get("min_drawdown") or 0))
        avg_equity = float(equity.get("avg_equity") or 1) or 1
        stddev = float(equity.get("stddev_equity") or 0)
        daily_return = total_pnl / avg_equity / 3
        sharpe = (daily_return / (stddev / avg_equity)) if stddev > 0 else 0.0

        log.info("Promotion evaluation",
                 win_rate=win_rate, sharpe=sharpe,
                 max_drawdown=max_drawdown, criteria=criteria, trades=count)

        if (
            win_rate >= criteria["min_win_rate"]
            and sharpe >= criteria["min_sharpe"]
            and max_drawdown <= criteria["max_drawdown_pct"]
        ):
            # Flip trading_mode + env in agent_state (single source of truth).
            await self._db.execute(
                """UPDATE agent_state
                   SET trading_mode = 'mainnet_live', env = 'mainnet',
                       last_promoted_at = now(), updated_at = now()
                   WHERE id = 'singleton'"""
            )
            log.info("PROMOTED TO MAINNET — trading_mode=mainnet_live, env=mainnet",
                     win_rate=win_rate, sharpe=sharpe, trades=count)
            try:
                from bybit_agent.reports.telegram import send_alert
                import asyncio
                asyncio.create_task(send_alert(
                    "info",
                    event="PROMOTED TO MAINNET",
                    win_rate=f"{win_rate:.1%}",
                    sharpe=f"{sharpe:.2f}",
                    trades=count,
                ))
            except Exception:
                pass
            return True

        await self._db.execute(
            "UPDATE agent_state SET promotion_cycle_count = promotion_cycle_count + 1 WHERE id = 'singleton'"
        )
        return False
