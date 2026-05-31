"""16-step trading cycle — ports src/control/AgentLoop.ts (paper=True, no LLM calls).

The loop runs paper=True (shadow decisions, no live execution) until Phase 6
cutover. It reads agent_state.env each cycle so the transition is live-hot
once the DB row is flipped.

Self-improvement happens inside the loop:
  • paper_positions are simulated each cycle → closed trades feed record_outcome()
  • adaptive_weight_decay() runs every ADAPT_EVERY_CYCLES to tune strategy weights
  • retrain_from_history() fires automatically when 50 new outcomes have accumulated
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone

from bybit_agent.config.constants import (
    CYCLE_INTERVAL_MS,
    LEVERAGE_DEFAULTS,
    MICRO_CAPITAL_EQUITY_THRESHOLD,
    MICRO_CAPITAL_SYMBOLS,
    REPO_POLL_INTERVAL_MS,
)

SIGNAL_DROUGHT_THRESHOLD = 60        # consecutive cycles with signals but no approval (~1 h)
ADAPT_EVERY_CYCLES = 10              # run adaptive_weight_decay every N cycles
AUTO_MANAGE_CYCLES = 30              # full autonomous self-management pass every N cycles (~5 min)
MARGIN_FAIL_THRESHOLD = 3            # consecutive 110007s before cooldown
MARGIN_COOLDOWN_CYCLES = 20          # skip symbol for ~20 cycles after threshold hit
BRAIN_RENDER_INTERVAL_MS  = 30 * 60 * 1_000        # re-render brain.md every 30 min
PRUNE_INTERVAL_MS         = 7 * 24 * 60 * 60 * 1_000  # prune DB once per week
from bybit_agent.core.errors import KillSwitchError
from bybit_agent.core.logger import get_logger
from bybit_agent.exchange.bybit_client import BybitClient
from bybit_agent.market.market_data import MarketDataService, classify_regime
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
from bybit_agent.reports.telegram import send_alert

log = get_logger().bind(module="agent-loop")

FALLBACK_SYMBOLS = MICRO_CAPITAL_SYMBOLS[:3]
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

        # Initialised to now; overwritten from DB in start() so restarts don't re-send reports.
        _now = time.time() * 1000
        self._last_daily_report = _now
        self._last_weekly_report = _now
        self._last_monthly_report = _now
        self._last_repo_check = 0.0
        self._last_discovery = 0.0
        self._last_brain_render = 0.0
        self._last_prune = 0.0
        self._watched_symbols: list[str] = list(FALLBACK_SYMBOLS)

        self._no_trade_cycles: int = 0
        self._drought_signals_seen: int = 0
        self._cycle_count: int = 0
        self._leverage: int = LEVERAGE_DEFAULTS["DEFAULT"]
        # Auto-management: remember last cycle where we changed maxRiskPct to avoid thrashing.
        self._last_risk_adjust_cycle: int = 0

        # In-memory paper positions: {symbol → position dict}
        # Persisted on close to trades table; rebuilt from DB on restart.
        self._paper_positions: dict[str, dict] = {}

        # Live position tracking: {symbol → {side, qty, entry_price, strategy, cycle_id, opened_at}}
        # Populated on successful order placement; used to detect closed positions each cycle.
        self._live_positions: dict[str, dict] = {}

        # Per-symbol order cooldown after 110007 (insufficient margin).
        # {symbol → cycle_number_when_cooldown_expires}
        self._margin_cooldown: dict[str, int] = {}
        self._margin_fail_count: dict[str, int] = {}

    async def start(self) -> None:
        self._running = True
        log.info("Agent loop starting", testnet=self._is_testnet)

        # Restore last-report timestamps from DB so restarts don't re-fire reports.
        try:
            ts_rows = await self._db.fetch(
                """SELECT last_daily_report_at, last_weekly_report_at, last_monthly_report_at
                   FROM agent_state WHERE id = 'singleton' LIMIT 1"""
            )
            if ts_rows:
                r = ts_rows[0]
                _epoch_ms = time.time() * 1000
                def _ts_to_ms(col: str) -> float:
                    v = r.get(col)
                    if v is None:
                        return 0.0
                    if hasattr(v, "timestamp"):
                        return v.timestamp() * 1000
                    from datetime import datetime, timezone
                    try:
                        return datetime.fromisoformat(str(v)).replace(tzinfo=timezone.utc).timestamp() * 1000
                    except Exception:
                        return 0.0
                self._last_daily_report   = _ts_to_ms("last_daily_report_at")   or _epoch_ms
                self._last_weekly_report  = _ts_to_ms("last_weekly_report_at")  or _epoch_ms
                self._last_monthly_report = _ts_to_ms("last_monthly_report_at") or _epoch_ms
        except Exception as e:
            log.warning("Could not restore report timestamps (non-fatal)", error=str(e))

        # Rebuild live position tracking from exchange + orders table on restart.
        try:
            await self._rebuild_live_positions()
        except Exception as e:
            log.warning("Live position rebuild failed (non-fatal)", error=str(e))

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
        self._cycle_count += 1

        # ── 1. Check agent_state for manual pause/kill ─────────────────────
        rows = await self._db.fetch("SELECT * FROM agent_state WHERE id = 'singleton' LIMIT 1")
        ag = rows[0] if rows else None
        if ag and ag.get("kill_engaged"):
            raise KillSwitchError("Manual DB kill")
        if ag and ag.get("status") == "paused":
            log.info("Agent paused — skipping cycle")
            return

        # ── 1b. Read trading mode — drives paper flag and API target ──────────
        # shadow       → paper=True,  testnet API  (default; no real orders ever)
        # testnet_live → paper=False, testnet API  (real fills; feeds promotion gate)
        # mainnet_live → paper=False, mainnet API  (real money)
        mode = await self._get_trading_mode()
        is_testnet = (mode != "mainnet_live")
        self._decisions.paper = (mode == "shadow")
        self._client.set_testnet(is_testnet)

        # Read configured leverage (agent can change at runtime via `bybit tune`)
        self._leverage = await self._get_configured_leverage()

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
                symbols = await self._discovery.discover(equity, self._leverage)
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

        # ── 3b. Tick paper position simulation (stop/TP hits) ──────────────
        if self._decisions.paper and self._paper_positions:
            await self._tick_paper_positions(snapshots, cycle_id)

        # ── 3b-live. Reconcile live positions (detect closes, write trades) ─
        if not self._decisions.paper and self._live_positions:
            asyncio.create_task(self._reconcile_live_positions())

        # ── 3c. Manage open positions ───────────────────────────────────────
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
        any_approved = False
        for sig in signals:
            if self._kill.is_engaged():
                break
            if self._portfolio.has_open_position(sig.symbol):
                log.debug("Skipping entry — position already open", symbol=sig.symbol)
                continue

            # Skip symbols in margin cooldown (repeated 110007 failures).
            cooldown_until = self._margin_cooldown.get(sig.symbol, 0)
            if self._cycle_count < cooldown_until:
                log.debug("Skipping entry — margin cooldown active",
                          symbol=sig.symbol,
                          resumes_at_cycle=cooldown_until)
                continue

            snap = next((s for s in snapshots if s.symbol == sig.symbol), None)
            if not snap:
                continue

            instrument = None
            try:
                instrument = await self._market.get_instrument("linear", sig.symbol)
            except Exception:
                continue
            if not instrument:
                continue

            approval = self._risk.approve(sig, instrument, circuit_breaker, leverage=self._leverage)
            econ = {
                "ev": approval.ev,
                "costPct": approval.costPct,
                "rewardRisk": approval.rewardRisk,
            }

            if not approval.approved:
                log.debug("Signal rejected", reason=approval.reason, symbol=sig.symbol)
                await self._db.execute(
                    """UPDATE decision_log
                       SET approved = false, reject_reason = $1,
                           inputs = COALESCE(inputs, '{}'::jsonb) || $2::jsonb
                       WHERE cycle_id = $3::uuid AND symbol = $4 AND strategy = $5""",
                    approval.reason,
                    __import__("json").dumps(econ),
                    cycle_id, sig.symbol, sig.strategy,
                )
                continue

            any_approved = True  # at least one signal passed the risk gate this cycle

            # Always record the approval in decision_log so report analytics are accurate.
            import json as _json
            await self._db.execute(
                """UPDATE decision_log
                   SET approved = true,
                       outcome = CASE WHEN $1 THEN 'paper' ELSE outcome END,
                       inputs = COALESCE(inputs, '{}'::jsonb) || $2::jsonb
                   WHERE cycle_id = $3::uuid AND symbol = $4 AND strategy = $5""",
                self._decisions.paper,
                _json.dumps(econ),
                cycle_id, sig.symbol, sig.strategy,
            )

            # paper=True: record virtual position for simulation; skip live execution.
            if self._decisions.paper:
                if sig.symbol not in self._paper_positions:
                    entry_px = sig.suggestedEntry or snap.lastPrice
                    self._paper_positions[sig.symbol] = {
                        "symbol": sig.symbol,
                        "side": "Buy" if sig.action == "enter_long" else "Sell",
                        "qty": approval.qty,
                        "entry_price": entry_px,
                        "stop_price": approval.stopPrice,
                        "tp_price": approval.tpPrice,
                        "strategy": sig.strategy,
                        "cycle_id": cycle_id,
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                    }
                    log.info("Paper position opened", symbol=sig.symbol,
                             side=self._paper_positions[sig.symbol]["side"],
                             entry=entry_px, strategy=sig.strategy)

            # When paper=False (Phase 6 cutover), the block below fires.
            if not self._decisions.paper:
                try:
                    side = "Buy" if sig.action == "enter_long" else "Sell"
                    # Bybit max orderLinkId length = 45 chars.
                    strat_abbr = sig.strategy[:8]
                    sym_abbr   = sig.symbol[:12]
                    order_link_id = f"ag-{strat_abbr}-{sym_abbr}-{int(time.time())}"[:45]
                    ref_price = sig.suggestedEntry or snap.lastPrice
                    # Fetch a fresh price so stop validation uses the actual current price,
                    # not the snapshot which can be up to 5 min stale.
                    try:
                        fresh = await self._client.get_ticker("linear", sig.symbol)
                        cur = float(fresh["lastPrice"]) if fresh else snap.lastPrice
                    except Exception:
                        cur = snap.lastPrice
                    raw_stop = approval.stopPrice or None
                    if raw_stop:
                        if side == "Sell" and raw_stop <= cur:
                            raw_stop = cur * 1.02  # 2% above live price for shorts
                        elif side == "Buy" and raw_stop >= cur:
                            raw_stop = cur * 0.98  # 2% below live price for longs
                    await self._client.set_leverage("linear", sig.symbol, self._leverage)
                    enter_params = EnterParams(
                        category="linear",
                        symbol=sig.symbol,
                        side=side,
                        qty=approval.qty,
                        refPrice=ref_price,
                        stopLoss=raw_stop,
                        takeProfit=approval.tpPrice,
                        orderLinkId=order_link_id,
                    )
                    try:
                        result = await self._execution.enter(enter_params)
                    except Exception as sl_err:
                        # Bybit error 10001 with "StopLoss" = stop is on the wrong side
                        # of their internal mark price (common on testnet with stale prices).
                        # Retry without stopLoss — position manager will set it post-fill.
                        if "10001" in str(sl_err) and "StopLoss" in str(sl_err):
                            log.warning("Stop loss rejected by exchange, retrying without SL",
                                        symbol=sig.symbol, stop=raw_stop, error=str(sl_err)[:80])
                            enter_params = EnterParams(
                                category="linear",
                                symbol=sig.symbol,
                                side=side,
                                qty=approval.qty,
                                refPrice=ref_price,
                                stopLoss=None,
                                takeProfit=approval.tpPrice,
                                orderLinkId=order_link_id + "b",
                            )
                            result = await self._execution.enter(enter_params)
                        else:
                            raise
                    import json
                    await self._db.execute(
                        """INSERT INTO orders
                             (decision_id, exchange_order_id, order_link_id, symbol,
                              category, side, order_type, qty, is_paper)
                           SELECT id, $1, $2, $3, 'linear', $4, $5, $6, $7
                           FROM decision_log
                           WHERE cycle_id = $8::uuid AND symbol = $3 AND strategy = $9
                           LIMIT 1""",
                        result.orderId, result.orderLinkId, sig.symbol,
                        side,
                        "Limit" if result.fillType == "maker" else "Market",
                        approval.qty, is_testnet, cycle_id, sig.strategy,
                    )
                    await self._db.execute(
                        """UPDATE decision_log
                           SET approved = true, outcome = 'executed',
                               inputs = COALESCE(inputs, '{}'::jsonb) || $1::jsonb
                           WHERE cycle_id = $2::uuid AND symbol = $3 AND strategy = $4""",
                        json.dumps(econ), cycle_id, sig.symbol, sig.strategy,
                    )
                    # Track entry for live position reconciliation.
                    self._live_positions[sig.symbol] = {
                        "side": side,
                        "qty": approval.qty,
                        "entry_price": ref_price,
                        "strategy": sig.strategy,
                        "cycle_id": cycle_id,
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                    }
                    # Order succeeded — reset any margin failure counter.
                    self._margin_fail_count.pop(sig.symbol, None)
                    self._margin_cooldown.pop(sig.symbol, None)
                    log.info("Order placed", symbol=sig.symbol, side=side,
                             qty=approval.qty, strategy=sig.strategy,
                             fill=result.fillType)
                    asyncio.create_task(send_alert(
                        "position_open",
                        symbol=sig.symbol, side=side, qty=approval.qty,
                        strategy=sig.strategy, fill=result.fillType,
                        entry=ref_price, leverage=self._leverage,
                    ))
                except Exception as e:
                    # 110007 = insufficient margin. Count consecutive failures; after
                    # MARGIN_FAIL_THRESHOLD hits, silence the symbol for MARGIN_COOLDOWN_CYCLES.
                    if "110007" in str(e):
                        count = self._margin_fail_count.get(sig.symbol, 0) + 1
                        self._margin_fail_count[sig.symbol] = count
                        if count >= MARGIN_FAIL_THRESHOLD:
                            self._margin_cooldown[sig.symbol] = self._cycle_count + MARGIN_COOLDOWN_CYCLES
                            self._margin_fail_count[sig.symbol] = 0
                            log.warning("Margin cooldown engaged — skipping symbol for next cycles",
                                        symbol=sig.symbol, cycles=MARGIN_COOLDOWN_CYCLES)
                        else:
                            log.debug("Insufficient margin", symbol=sig.symbol,
                                      consecutive_failures=count)
                    else:
                        log.error("Order failed", symbol=sig.symbol, error=str(e))
                    import json
                    await self._db.execute(
                        """UPDATE decision_log
                           SET outcome = 'failed', outcome_detail = $1::jsonb
                           WHERE cycle_id = $2::uuid AND symbol = $3 AND strategy = $4""",
                        json.dumps({"error": str(e)}),
                        cycle_id, sig.symbol, sig.strategy,
                    )

        # ── 5b. Signal drought tracking ─────────────────────────────────────
        if signals and not any_approved:
            self._no_trade_cycles += 1
            self._drought_signals_seen += len(signals)
            if self._no_trade_cycles % 10 == 0:
                log.warning("Signal drought accumulating",
                            cycles=self._no_trade_cycles,
                            signals_seen=self._drought_signals_seen)
            if self._no_trade_cycles >= SIGNAL_DROUGHT_THRESHOLD:
                asyncio.create_task(
                    self._fire_signal_drought(cycle_id, self._drought_signals_seen, self._no_trade_cycles)
                )
                self._no_trade_cycles = 0
                self._drought_signals_seen = 0
        else:
            self._no_trade_cycles = 0
            self._drought_signals_seen = 0

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

        # ── 8. Reports ─────────────────────────────────────────────────────
        now = time.time() * 1000
        _report_update: dict[str, str] = {}
        if now - self._last_daily_report > 24 * 60 * 60 * 1000:
            self._last_daily_report = now
            _report_update["last_daily_report_at"] = "now()"
            asyncio.create_task(self._send_report("daily"))
        if now - self._last_weekly_report > 7 * 24 * 60 * 60 * 1000:
            self._last_weekly_report = now
            _report_update["last_weekly_report_at"] = "now()"
            asyncio.create_task(self._send_report("weekly"))
        if now - self._last_monthly_report > 30 * 24 * 60 * 60 * 1000:
            self._last_monthly_report = now
            _report_update["last_monthly_report_at"] = "now()"
            asyncio.create_task(self._send_report("monthly"))
        if _report_update:
            cols = ", ".join(f"{k} = {v}" for k, v in _report_update.items())
            await self._db.execute(
                f"UPDATE agent_state SET {cols} WHERE id = 'singleton'"
            )

        # ── 8b. brain.md auto-render ────────────────────────────────────────
        if now - self._last_brain_render > BRAIN_RENDER_INTERVAL_MS:
            self._last_brain_render = now
            asyncio.create_task(self._render_brain())

        # ── 8c. Weekly DB prune ─────────────────────────────────────────────
        if now - self._last_prune > PRUNE_INTERVAL_MS:
            self._last_prune = now
            asyncio.create_task(self._run_prune())

        # ── 9. Repo update check ────────────────────────────────────────────
        if now - self._last_repo_check > REPO_POLL_INTERVAL_MS:
            self._last_repo_check = now
            asyncio.create_task(self._repo_check())

        # ── 9b. Continuous learning ─────────────────────────────────────────
        if self._cycle_count % ADAPT_EVERY_CYCLES == 0:
            try:
                from bybit_agent.ml.learner import adaptive_weight_decay
                await adaptive_weight_decay(self._db)
            except Exception as e:
                log.warning("Adaptive weight decay failed", error=str(e))

        try:
            from bybit_agent.ml.learner import outcomes_since_retrain, retrain_from_history
            if outcomes_since_retrain() >= 50:
                asyncio.create_task(retrain_from_history(self._db))
                log.info("Auto-retrain triggered", outcomes=outcomes_since_retrain())
        except Exception as e:
            log.warning("Retrain check failed", error=str(e))

        # ── 9c. Autonomous self-management ──────────────────────────────────
        if self._cycle_count % AUTO_MANAGE_CYCLES == 0:
            try:
                await self._auto_manage(snapshots)
            except Exception as e:
                log.warning("Auto-manage failed", error=str(e))

        # ── 10. Update agent_state ──────────────────────────────────────────
        await self._db.execute(
            "UPDATE agent_state SET last_cycle_at = now(), updated_at = now() WHERE id = 'singleton'"
        )

    async def _get_trading_mode(self) -> str:
        """Read trading_mode from agent_state. Safe default is 'shadow'."""
        _VALID = {"shadow", "testnet_live", "mainnet_live"}
        try:
            rows = await self._db.fetch(
                "SELECT trading_mode FROM agent_state WHERE id = 'singleton' LIMIT 1"
            )
            if rows:
                v = (rows[0].get("trading_mode") or "shadow").strip()
                if v in _VALID:
                    return v
        except Exception:
            pass
        return "shadow"

    async def _get_configured_leverage(self) -> int:
        try:
            rows = await self._db.fetch(
                "SELECT value FROM agent_config WHERE key = 'default_leverage' LIMIT 1"
            )
            if rows and rows[0].get("value") is not None:
                v = int(rows[0]["value"])
                return max(1, min(LEVERAGE_DEFAULTS["MAX"], v))
        except Exception:
            pass
        return LEVERAGE_DEFAULTS["DEFAULT"]

    async def _rebuild_live_positions(self) -> None:
        """Rebuild _live_positions from exchange data on startup.

        Fetches current open positions from the exchange and matches them to recent
        orders in the DB so we can reconcile closures even after a restart.
        """
        mode = await self._get_trading_mode()
        if mode == "shadow":
            return
        is_testnet = (mode != "mainnet_live")
        self._client.set_testnet(is_testnet)
        try:
            open_pos = await self._client.get_positions("linear")
        except Exception:
            return

        if not open_pos:
            return

        # Fetch recent orders so we can match strategy name.
        try:
            order_rows = await self._db.fetch(
                """SELECT o.symbol, o.side, o.qty, o.price, d.strategy, d.cycle_id, o.created_at
                   FROM orders o
                   JOIN decision_log d ON d.id = o.decision_id
                   WHERE o.is_paper = false AND o.created_at > now() - INTERVAL '7 days'
                   ORDER BY o.created_at DESC"""
            )
            order_map: dict[str, dict] = {}
            for row in order_rows:
                sym = row.get("symbol") or ""
                if sym and sym not in order_map:
                    order_map[sym] = row
        except Exception:
            order_map = {}

        for pos in open_pos:
            sym = pos.get("symbol") or ""
            size = float(pos.get("size") or 0)
            if not sym or size == 0:
                continue
            order_row = order_map.get(sym, {})
            self._live_positions[sym] = {
                "side": pos.get("side") or order_row.get("side") or "Buy",
                "qty": size,
                "entry_price": float(pos.get("avgPrice") or order_row.get("price") or 0),
                "strategy": order_row.get("strategy") or "unknown",
                "cycle_id": str(order_row.get("cycle_id") or ""),
                "opened_at": str(pos.get("createdTime") or order_row.get("created_at") or ""),
            }

        if self._live_positions:
            log.info("Live positions rebuilt on startup", symbols=list(self._live_positions))

    async def _tick_paper_positions(self, snapshots, cycle_id: str) -> None:
        """Check each paper position against current prices; close on stop/TP hit."""
        by_symbol = {s.symbol: s for s in snapshots}
        to_close: list[str] = []

        for sym, pos in self._paper_positions.items():
            snap = by_symbol.get(sym)
            if not snap:
                continue
            px = snap.lastPrice
            side = pos["side"]
            stop = pos.get("stop_price")
            tp = pos.get("tp_price")

            hit_stop = stop and (
                (side == "Buy" and px <= stop) or
                (side == "Sell" and px >= stop)
            )
            hit_tp = tp and (
                (side == "Buy" and px >= tp) or
                (side == "Sell" and px <= tp)
            )

            if hit_stop or hit_tp:
                exit_px = stop if hit_stop else tp
                pnl_pct = (exit_px - pos["entry_price"]) / pos["entry_price"]
                if side == "Sell":
                    pnl_pct = -pnl_pct
                pnl_abs = pnl_pct * pos["qty"] * pos["entry_price"]
                reason = "stop" if hit_stop else "tp"

                try:
                    import json as _json
                    regime = classify_regime(snap)
                    # trades schema: realized_pnl (not pnl); pnl_pct/close_reason live in meta jsonb.
                    await self._db.execute(
                        """INSERT INTO trades
                             (decision_id, symbol, category, side, qty, entry_price,
                              exit_price, realized_pnl, strategy, is_paper,
                              opened_at, closed_at, meta)
                           SELECT id, $1, 'linear', $2, $3, $4, $5, $6, $7, true,
                                  $8::timestamptz, now(), $9::jsonb
                           FROM decision_log
                           WHERE cycle_id = $10::uuid AND symbol = $1 AND strategy = $7
                           LIMIT 1""",
                        sym, side, pos["qty"], pos["entry_price"], exit_px,
                        pnl_abs, pos["strategy"], pos["opened_at"],
                        _json.dumps({"close_reason": reason, "pnl_pct": pnl_pct,
                                     "regime": regime, "paper_sim": True}),
                        pos["cycle_id"],
                    )
                    from bybit_agent.ml.learner import record_outcome
                    await record_outcome(
                        self._db, pos["strategy"], regime, sym, pnl_abs, is_paper=True
                    )
                    asyncio.create_task(send_alert(
                        "position_close", symbol=sym, reason=reason,
                        entry=pos["entry_price"], exit=exit_px,
                        pnl=round(pnl_abs, 4), mode="paper",
                    ))
                except Exception as e:
                    log.warning("Paper trade close failed", symbol=sym, error=str(e))

                log.info("Paper position closed", symbol=sym, reason=reason,
                         entry=pos["entry_price"], exit=exit_px, pnl=round(pnl_abs, 4))
                to_close.append(sym)

        for sym in to_close:
            self._paper_positions.pop(sym, None)

    async def _reconcile_live_positions(self) -> None:
        """Detect live positions that closed on the exchange and record them in trades."""
        try:
            open_pos = await self._client.get_positions("linear")
            open_symbols = {p["symbol"] for p in open_pos if float(p.get("size") or 0) > 0}
        except Exception as e:
            log.warning("reconcile: failed to fetch positions", error=str(e))
            return

        closed = [sym for sym in list(self._live_positions) if sym not in open_symbols]
        if not closed:
            return

        for sym in closed:
            pos = self._live_positions.pop(sym, None)
            if not pos:
                continue
            try:
                # Fetch recent executions to find the closing fill price.
                executions = await self._client.get_executions("linear", sym, limit=10)
                # Most recent execution for this symbol is the close fill.
                close_price = pos["entry_price"]  # fallback
                close_fee = 0.0
                if executions:
                    ex = executions[0]
                    close_price = float(ex.get("execPrice") or pos["entry_price"])
                    close_fee = float(ex.get("execFee") or 0)

                side = pos["side"]
                qty = float(pos["qty"])
                entry = float(pos["entry_price"])
                pnl_raw = (close_price - entry) * qty if side == "Buy" else (entry - close_price) * qty
                realized_pnl = pnl_raw - close_fee

                import json as _json
                await self._db.execute(
                    """INSERT INTO trades
                         (decision_id, symbol, category, side, qty, entry_price,
                          exit_price, realized_pnl, strategy, is_paper,
                          opened_at, closed_at, meta)
                       SELECT id, $1, 'linear', $2, $3, $4, $5, $6, $7, false,
                              $8::timestamptz, now(), $9::jsonb
                       FROM decision_log
                       WHERE cycle_id = $10::uuid AND symbol = $1 AND strategy = $7
                       LIMIT 1
                       ON CONFLICT DO NOTHING""",
                    sym, side, qty, entry, close_price, realized_pnl,
                    pos["strategy"], pos["opened_at"],
                    _json.dumps({"close_fee": close_fee, "reconciled": True}),
                    pos["cycle_id"],
                )
                from bybit_agent.ml.learner import record_outcome
                regime = "unknown"
                await record_outcome(self._db, pos["strategy"], regime, sym, realized_pnl, is_paper=False)
                log.info("Live position closed (reconciled)", symbol=sym, side=side,
                         entry=entry, exit=close_price, pnl=round(realized_pnl, 4))
                asyncio.create_task(send_alert(
                    "position_close", symbol=sym, side=side,
                    entry=entry, exit=close_price,
                    pnl=round(realized_pnl, 4), mode="live",
                ))
            except Exception as e:
                log.warning("reconcile: failed to record closed position", symbol=sym, error=str(e))

    async def _auto_manage(self, snapshots) -> None:
        """Fully autonomous self-management — runs every AUTO_MANAGE_CYCLES without user input.

        Three actions per pass:
          1. Regime-aware strategy weight tuning: up-weight strategies that fit the live
             regime mix, down-weight those that don't.
          2. Risk parameter auto-scaling: shrink maxRiskPct when drawdown is growing,
             expand it when equity is at peak and conditions are calm.
          3. Crisis protection: if the majority of symbols are in crisis regime, push
             all non-crisis-safe strategies toward minimum weight.

        Every adjustment is bounded by PARAM_WHITELIST and leaves a log trail so any
        AI agent that reads the logs can see exactly what changed and why.
        """
        # ── 1. Regime-aware weight tuning ────────────────────────────────────
        try:
            from bybit_agent.ml.learner import regime_aware_adjust
            regimes = [classify_regime(s) for s in snapshots if s is not None]
            await regime_aware_adjust(self._db, regimes)
        except Exception as e:
            log.debug("Regime weight adjust failed (non-fatal)", error=str(e))

        # ── 2. Risk parameter auto-scaling ───────────────────────────────────
        # Anti-thrash: only adjust if last change was at least 50 cycles ago.
        if self._cycle_count - self._last_risk_adjust_cycle < 50:
            return

        state = self._portfolio.get_state()
        if state.peakEquity <= 0 or state.equity <= 0:
            return

        drawdown = (state.peakEquity - state.equity) / state.peakEquity

        # Read current maxRiskPct from agent_config (CLI tune target) or agent_state fallback.
        current_risk = 0.01  # sensible default
        try:
            rows = await self._db.fetch(
                "SELECT value FROM agent_config WHERE key = 'max_risk_pct' LIMIT 1"
            )
            if rows and rows[0].get("value") is not None:
                current_risk = float(rows[0]["value"])
            else:
                state_rows = await self._db.fetch(
                    "SELECT max_risk_pct FROM agent_state WHERE id = 'singleton' LIMIT 1"
                )
                if state_rows and state_rows[0].get("max_risk_pct") is not None:
                    current_risk = float(state_rows[0]["max_risk_pct"])
        except Exception:
            pass

        # Scale risk inversely with drawdown, within PARAM_WHITELIST bounds [0.001, 0.03].
        if drawdown > 0.04:        # > 4% drawdown — cut risk sharply
            new_risk = max(0.002, current_risk * 0.70)
        elif drawdown > 0.02:      # 2–4% drawdown — reduce risk
            new_risk = max(0.003, current_risk * 0.85)
        elif drawdown < 0.005 and state.equity >= state.peakEquity * 0.998:
            # Near all-time-high equity and very low drawdown — can increase risk slightly.
            new_risk = min(0.02, current_risk * 1.05)
        else:
            return  # 0.5–2% drawdown: no change needed

        new_risk = round(new_risk, 5)
        if abs(new_risk - current_risk) / current_risk < 0.05:
            return  # Change is < 5% — not worth the noise.

        try:
            await self._db.execute(
                """INSERT INTO agent_config (key, value, updated_at)
                   VALUES ('max_risk_pct', $1::text, now())
                   ON CONFLICT (key) DO UPDATE SET value = $1::text, updated_at = now()""",
                str(new_risk),
            )
            self._last_risk_adjust_cycle = self._cycle_count
            log.info(
                "Auto-adjusted maxRiskPct",
                old=round(current_risk, 5),
                new=new_risk,
                drawdown_pct=round(drawdown * 100, 2),
                equity=round(state.equity, 2),
            )
        except Exception as e:
            log.debug("Auto risk param update failed (non-fatal)", error=str(e))

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

    async def _run_prune(self) -> None:
        try:
            from bybit_agent.maintenance.pruner import prune
            result = await prune(self._db)
            if result.total_deleted or result.archived_notes:
                log.info("Weekly prune done",
                         deleted=result.total_deleted, archived=result.archived_notes)
        except Exception as e:
            log.warning("Weekly prune failed", error=str(e))

    async def _render_brain(self) -> None:
        try:
            from bybit_agent.control.brain import write_file
            path = await write_file(self._db)
            log.debug("brain.md rendered", path=path)
        except Exception as e:
            log.warning("brain.md render failed", error=str(e))

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

    async def _fire_signal_drought(
        self, cycle_id: str, signals_seen: int, cycles_blocked: int
    ) -> None:
        try:
            from bybit_agent.events.triggers import trigger_signal_drought
            rows = await self._db.fetch(
                """SELECT strategy, reject_reason, COUNT(*)::int AS cnt
                   FROM decision_log
                   WHERE approved = false
                     AND reject_reason IS NOT NULL
                     AND created_at > now() - interval '2 hours'
                   GROUP BY strategy, reject_reason
                   ORDER BY cnt DESC
                   LIMIT 10"""
            )
            top_rejections = [
                {"strategy": r["strategy"], "reason": r["reject_reason"], "count": r["cnt"]}
                for r in (rows or [])
            ]
            await trigger_signal_drought(
                self._db,
                signals_fired=signals_seen,
                cycles_blocked=cycles_blocked,
                rejection_summary={"top_rejections": top_rejections},
                cycle_id=cycle_id,
            )
            log.info("Signal drought event fired",
                     cycles_blocked=cycles_blocked, signals_seen=signals_seen)
        except Exception as e:
            log.warning("Signal drought trigger failed", error=str(e))
