"""16-step trading cycle — ports src/control/AgentLoop.ts (paper=True, no LLM calls).

The loop runs paper=True (shadow decisions, no live execution) until Phase 6
cutover. It reads agent_state.env each cycle so the transition is live-hot
once the DB row is flipped.
"""
from __future__ import annotations

import asyncio
import time
import uuid

from bybit_agent.config.constants import (
    CYCLE_INTERVAL_MS,
    REPO_POLL_INTERVAL_MS,
)
from bybit_agent.core.errors import KillSwitchError
from bybit_agent.core.logger import get_logger
from bybit_agent.exchange.bybit_client import BybitClient
from bybit_agent.market.market_data import MarketDataService
from bybit_agent.market.discovery import MarketDiscovery
from bybit_agent.strategy.decision_engine import DecisionEngine
from bybit_agent.risk.risk_manager import RiskManager
from bybit_agent.portfolio.portfolio_manager import PortfolioManager
from bybit_agent.positions.position_health import PositionHealthManager
from bybit_agent.execution.execution_router import ExecutionRouter, EnterParams
from bybit_agent.control.kill_switch import KillSwitch
from bybit_agent.control.promotion import PromotionManager
from bybit_agent.persistence.db import NeonHttpClient
from bybit_agent.review.self_review import SelfReview
from bybit_agent.bots.bot_manager import BotManager
from bybit_agent.copy.copy_manager import CopyTradingManager

log = get_logger().bind(module="agent-loop")

FALLBACK_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
DISCOVERY_INTERVAL_MS = 5 * 60 * 1000


class AgentLoop:
    def __init__(self, client: BybitClient, db: NeonHttpClient, is_testnet: bool) -> None:
        self._client = client
        self._db = db
        self._is_testnet = is_testnet
        self._running = False

        self._market = MarketDataService(client)
        self._discovery = MarketDiscovery(client)
        self._portfolio = PortfolioManager(client, db)
        self._risk = RiskManager(self._portfolio.get_state)
        self._decisions = DecisionEngine(paper=True)
        self._position_health = PositionHealthManager(client)
        self._execution = ExecutionRouter(client, is_sim=False)
        self._kill = KillSwitch(client, db)
        self._promotion = PromotionManager(db)
        self._review = SelfReview()
        self._bots = BotManager(client, db, is_testnet)
        self._copy = CopyTradingManager(client, db, is_paper=is_testnet)

        self._last_daily_report = 0.0
        self._last_weekly_report = 0.0
        self._last_monthly_report = 0.0
        self._last_repo_check = 0.0
        self._last_discovery = 0.0
        self._watched_symbols: list[str] = list(FALLBACK_SYMBOLS)

    async def start(self) -> None:
        self._running = True
        log.info("Agent loop starting", testnet=self._is_testnet)

        # Check for manual kill in DB on startup.
        rows = await self._db.fetch("SELECT kill_engaged FROM agent_state WHERE id = 'singleton' LIMIT 1")
        if rows and rows[0].get("kill_engaged"):
            log.critical("Kill switch is engaged in DB. Clear manually before restarting.")
            return

        while self._running:
            try:
                await self._cycle()
            except KillSwitchError as e:
                log.critical("Kill switch triggered — agent halted", reason=str(e))
                self._running = False
                break
            except Exception as e:
                log.error("Cycle error", error=str(e))
            await asyncio.sleep(self._adaptive_interval() / 1000.0)

    def stop(self) -> None:
        self._running = False

    async def _cycle(self) -> None:
        cycle_id = str(uuid.uuid4())

        # ── 1. Check agent_state for manual pause/kill ─────────────────────
        rows = await self._db.fetch("SELECT * FROM agent_state WHERE id = 'singleton' LIMIT 1")
        ag = rows[0] if rows else None
        if ag and ag.get("kill_engaged"):
            raise KillSwitchError("Manual DB kill")
        if ag and ag.get("status") == "paused":
            log.info("Agent paused — skipping cycle")
            return

        current_env = (ag or {}).get("env", "testnet")
        is_testnet = current_env == "testnet"
        self._client.set_testnet(is_testnet)

        # ── 2. Hard limits ──────────────────────────────────────────────────
        await self._portfolio.refresh()
        limits = await self._risk.check_hard_limits(cycle_id)
        if limits.killTripped:
            await self._kill.engage("Kill level breached")
            return

        # ── 2b. Market discovery ────────────────────────────────────────────
        now_ms = time.time() * 1000
        if now_ms - self._last_discovery > DISCOVERY_INTERVAL_MS:
            self._last_discovery = now_ms
            equity = self._portfolio.get_state().equity
            try:
                symbols = await self._discovery.discover(equity)
                if symbols:
                    self._watched_symbols = symbols
            except Exception as e:
                log.warning("Discovery failed", error=str(e))

        # ── 3. Market snapshots ─────────────────────────────────────────────
        snapshot_tasks = [
            self._market.get_snapshot(s, "linear") for s in self._watched_symbols
        ]
        results = await asyncio.gather(*snapshot_tasks, return_exceptions=True)
        snapshots = [r for r in results if not isinstance(r, Exception) and r is not None]

        if not snapshots:
            log.warning("No market data — skipping cycle")
            return

        # ── 3b. Manage open positions ───────────────────────────────────────
        if not self._kill.is_engaged():
            try:
                await self._position_health.tick(snapshots)
            except Exception as e:
                log.error("Position health error", error=str(e))

        # ── 4. Decisions ────────────────────────────────────────────────────
        daily_limit_hit = limits.dailyLimitHit
        circuit_breaker = limits.circuitBreaker
        signals = []
        if not daily_limit_hit:
            try:
                signals = await self._decisions.run(snapshots, cycle_id)
            except Exception as e:
                log.error("Decision engine error", error=str(e))

        # ── 5. Execute signals (paper=True means shadow-only) ───────────────
        for sig in signals:
            if self._kill.is_engaged():
                break
            if self._portfolio.has_open_position(sig["symbol"]):
                log.debug("Skipping entry — position already open", symbol=sig["symbol"])
                continue

            snap = next((s for s in snapshots if s.symbol == sig["symbol"]), None)
            if not snap:
                continue

            instrument = None
            try:
                instrument = await self._market.get_instrument("linear", sig["symbol"])
            except Exception:
                continue
            if not instrument:
                continue

            approval = self._risk.approve(sig, instrument, circuit_breaker)
            econ = {
                "ev": approval.ev,
                "costPct": approval.costPct,
                "rewardRisk": approval.rewardRisk,
            }

            if not approval.approved:
                log.debug("Signal rejected", reason=approval.reason, symbol=sig["symbol"])
                await self._db.execute(
                    """UPDATE decision_log
                       SET approved = false, reject_reason = $1,
                           inputs = COALESCE(inputs, '{}'::jsonb) || $2::jsonb
                       WHERE cycle_id = $3::uuid AND symbol = $4 AND strategy = $5""",
                    approval.reason,
                    __import__("json").dumps(econ),
                    cycle_id, sig["symbol"], sig["strategy"],
                )
                continue

            # paper=True: DecisionEngine already wrote the shadow row. We skip live execution.
            # When paper=False (Phase 6 cutover), the block below fires.
            if not self._decisions.paper:
                try:
                    side = "Buy" if sig["action"] == "enter_long" else "Sell"
                    order_link_id = f"agent-{sig['strategy']}-{sig['symbol']}-{int(time.time() * 1000)}"
                    ref_price = sig.get("suggestedEntry") or snap.lastPrice
                    result = await self._execution.enter(EnterParams(
                        category="linear",
                        symbol=sig["symbol"],
                        side=side,
                        qty=approval.qty,
                        refPrice=ref_price,
                        stopLoss=approval.stopPrice or None,
                        takeProfit=approval.tpPrice,
                        orderLinkId=order_link_id,
                    ))
                    import json
                    await self._db.execute(
                        """INSERT INTO orders
                             (decision_id, exchange_order_id, order_link_id, symbol,
                              category, side, order_type, qty, is_paper)
                           SELECT id, $1, $2, $3, 'linear', $4, $5, $6, $7
                           FROM decision_log
                           WHERE cycle_id = $8::uuid AND symbol = $3 AND strategy = $9
                           LIMIT 1""",
                        result.orderId, result.orderLinkId, sig["symbol"],
                        side,
                        "Limit" if result.fillType == "maker" else "Market",
                        approval.qty, is_testnet, cycle_id, sig["strategy"],
                    )
                    await self._db.execute(
                        """UPDATE decision_log
                           SET approved = true, outcome = 'executed',
                               inputs = COALESCE(inputs, '{}'::jsonb) || $1::jsonb
                           WHERE cycle_id = $2::uuid AND symbol = $3 AND strategy = $4""",
                        json.dumps(econ), cycle_id, sig["symbol"], sig["strategy"],
                    )
                    log.info("Order placed", symbol=sig["symbol"], side=side,
                             qty=approval.qty, strategy=sig["strategy"],
                             fill=result.fillType)
                except Exception as e:
                    log.error("Order failed", symbol=sig["symbol"], error=str(e))
                    import json
                    await self._db.execute(
                        """UPDATE decision_log
                           SET outcome = 'failed', outcome_detail = $1::jsonb
                           WHERE cycle_id = $2::uuid AND symbol = $3 AND strategy = $4""",
                        json.dumps({"error": str(e)}),
                        cycle_id, sig["symbol"], sig["strategy"],
                    )

        # ── 6. Bot tick ─────────────────────────────────────────────────────
        if not daily_limit_hit and not self._kill.is_engaged():
            try:
                await self._bots.tick(snapshots, self._portfolio.get_state())
            except Exception as e:
                log.error("Bot tick error", error=str(e))

        # ── 6b. Copy trading tick ────────────────────────────────────────────
        try:
            await self._copy.tick(self._portfolio.get_state())
        except Exception as e:
            log.error("Copy tick error", error=str(e))

        # ── 6c. Self-review ──────────────────────────────────────────────────
        try:
            await self._review.tick(cycle_id, self._db)
        except Exception as e:
            log.error("Self-review error", error=str(e))

        # ── 7. Promotion check ──────────────────────────────────────────────
        if is_testnet:
            try:
                promoted = await self._promotion.evaluate()
                if promoted:
                    log.info("Promoted to mainnet! Reloading with mainnet settings")
                    self._client.set_testnet(False)
            except Exception as e:
                log.warning("Promotion check failed", error=str(e))

        # ── 8. Reports (Phase 5 will wire ReportBuilder) ───────────────────
        now = time.time() * 1000
        if now - self._last_daily_report > 24 * 60 * 60 * 1000:
            self._last_daily_report = now
            asyncio.create_task(self._send_report("daily"))
        if now - self._last_weekly_report > 7 * 24 * 60 * 60 * 1000:
            self._last_weekly_report = now
            asyncio.create_task(self._send_report("weekly"))
        if now - self._last_monthly_report > 30 * 24 * 60 * 60 * 1000:
            self._last_monthly_report = now
            asyncio.create_task(self._send_report("monthly"))

        # ── 9. Repo update check ────────────────────────────────────────────
        if now - self._last_repo_check > REPO_POLL_INTERVAL_MS:
            self._last_repo_check = now
            asyncio.create_task(self._repo_check())

        # ── 10. Update agent_state ──────────────────────────────────────────
        await self._db.execute(
            "UPDATE agent_state SET last_cycle_at = now(), updated_at = now() WHERE id = 'singleton'"
        )

    def _adaptive_interval(self) -> float:
        state = self._portfolio.get_state()
        base = float(CYCLE_INTERVAL_MS)

        if state.peakEquity > 0:
            drawdown = (state.peakEquity - state.equity) / state.peakEquity
            if drawdown > 0.08:
                return 30_000.0

        try:
            from bybit_agent.exchange.rate_limiter import rate_limiter
            worst = rate_limiter.get_worst_bucket()
            if worst and worst.get("limit", 0) > 0:
                usage = (worst["limit"] - worst.get("remaining", worst["limit"])) / worst["limit"]
                if usage > 0.90:
                    log.warning("API quota pressure — tripling cycle interval",
                                usage=round(usage * 100), endpoint=worst.get("endpoint"))
                    return base * 3
                if usage > 0.75:
                    return base * 2
        except Exception:
            pass

        return base

    async def _send_report(self, period: str) -> None:
        try:
            from bybit_agent.reports.report_builder import build_and_send_report
            await build_and_send_report(period)
        except Exception as e:
            log.error("Report failed", period=period, error=str(e))

    async def _repo_check(self) -> None:
        try:
            from bybit_agent.updater.repo_updater import check_and_update
            await check_and_update()
        except Exception as e:
            log.warning("Repo check failed", error=str(e))
