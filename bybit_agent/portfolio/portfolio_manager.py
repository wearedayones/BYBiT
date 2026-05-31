"""Portfolio state tracking with daily UTC reset — ports src/portfolio/PortfolioManager.ts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bybit_agent.core.logger import get_logger
from bybit_agent.persistence.db import NeonHttpClient
from bybit_agent.risk.risk_manager import PortfolioState

log = get_logger().bind(module="portfolio")


class PortfolioManager:
    def __init__(self, client: Any, db: NeonHttpClient) -> None:
        self._client = client
        self._db = db
        self._state = PortfolioState(
            equity=0, peakEquity=0, daySartEquity=0,
            dailyRealizedPnl=0, openPositionCount=0,
            dailyLossLimit=0.08, killLevelPct=0.20,
            circuitBreakerPct=0.10, maxRiskPct=0.015,
        )
        self._open_symbols: set[str] = set()

    def get_state(self) -> PortfolioState:
        return self._state

    def has_open_position(self, symbol: str) -> bool:
        return symbol in self._open_symbols

    async def refresh(self) -> PortfolioState:
        try:
            wallet, positions, ag_rows = await _gather(
                self._client.get_wallet_balance(),
                self._client.get_positions("linear"),
                self._db.fetch("SELECT * FROM agent_state WHERE id = 'singleton' LIMIT 1"),
            )

            ag = ag_rows[0] if ag_rows else None
            equity = float((wallet or {}).get("totalEquity") or 0)
            unrealized_pnl = float((wallet or {}).get("totalPerpUPL") or 0)
            open_pos = [p for p in (positions or []) if float(p.get("size", 0)) > 0]
            self._open_symbols = {p["symbol"] for p in open_pos}

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            trading_day = ""
            if ag and ag.get("trading_day"):
                raw = ag["trading_day"]
                trading_day = raw[:10] if isinstance(raw, str) else str(raw)[:10]

            day_start_equity = float(ag.get("day_start_equity") or 0) if ag else 0
            daily_realized_pnl = float(ag.get("daily_realized_pnl") or 0) if ag else 0

            if trading_day != today:
                day_start_equity = equity
                daily_realized_pnl = 0.0
                await self._db.execute(
                    """UPDATE agent_state SET
                         trading_day = $1::date,
                         day_start_equity = $2,
                         daily_realized_pnl = 0,
                         updated_at = now()
                       WHERE id = 'singleton'""",
                    today, equity,
                )

            peak_equity = max(float(ag.get("peak_equity") or 0) if ag else 0, equity)
            drawdown_pct = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            avail = float((wallet or {}).get("totalAvailableBalance") or 0)

            self._state = PortfolioState(
                equity=equity,
                peakEquity=peak_equity,
                daySartEquity=day_start_equity,
                dailyRealizedPnl=daily_realized_pnl,
                openPositionCount=len(open_pos),
                dailyLossLimit=float(ag.get("daily_loss_limit") or 0.08) if ag else 0.08,
                killLevelPct=float(ag.get("kill_level_pct") or 0.20) if ag else 0.20,
                circuitBreakerPct=float(ag.get("circuit_breaker_pct") or 0.10) if ag else 0.10,
                maxRiskPct=float(ag.get("max_risk_pct") or 0.015) if ag else 0.015,
            )

            await self._db.execute(
                "UPDATE agent_state SET equity = $1, peak_equity = $2, updated_at = now() WHERE id = 'singleton'",
                equity, peak_equity,
            )
            await self._db.execute(
                """INSERT INTO equity_snapshots
                     (total_equity, available, unrealized_pnl, realized_pnl_day,
                      open_positions, drawdown_pct, env)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                equity, avail, unrealized_pnl, daily_realized_pnl,
                len(open_pos), drawdown_pct, ag.get("env", "testnet") if ag else "testnet",
            )

            log.debug("Portfolio refreshed",
                      equity=equity,
                      drawdown_pct=f"{drawdown_pct * 100:.2f}%",
                      open_positions=len(open_pos))
        except Exception as e:
            log.error("Portfolio refresh failed", error=str(e))

        return self._state


async def _gather(*coros: Any) -> tuple[Any, ...]:
    import asyncio
    return tuple(await asyncio.gather(*coros, return_exceptions=False))
