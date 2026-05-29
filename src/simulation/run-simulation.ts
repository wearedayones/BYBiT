/**
 * Simulation entry point — runs the full agent with simulated market data.
 * Cycles every 8 seconds so regime changes and decisions are visible quickly.
 */
import '../config/env';
import { readFileSync, existsSync } from 'fs';
import { join } from 'path';
import { getDb, closeDb } from '../persistence/db';
import { MarketSimulator } from './MarketSimulator';
import { SimulatedBybitClient } from './SimulatedBybitClient';
import { MarketDataService } from '../market/MarketDataService';
import { DecisionEngine, classifyRegime } from '../strategy/DecisionEngine';
import { RiskManager } from '../risk/RiskManager';
import { BotManager } from '../bots/BotManager';
import { CopyTradingManager } from '../copy/CopyTradingManager';
import { SelfReview } from '../review/SelfReview';
import { PromotionManager } from '../control/PromotionManager';
import { buildAndSendReport } from '../reports/ReportBuilder';
import { logger, childLogger } from '../core/logger';
import { randomUUID } from 'crypto';
import type { PortfolioState } from '../risk/RiskManager';

const log = childLogger({ module: 'simulation' });

const CYCLE_MS = 8_000;  // 8 seconds per cycle — fast enough to see everything
const MAX_CYCLES = 200;  // run for ~27 minutes then summarise

async function runSimulation() {
  logger.info('');
  logger.info('══════════════════════════════════════════════════════');
  logger.info('  BYBiT Agent — SIMULATION MODE (no real API calls)');
  logger.info('══════════════════════════════════════════════════════');
  logger.info('  Market data: synthetic (realistic regimes)');
  logger.info('  Database: local PostgreSQL');
  logger.info('  Cycle: every 8 seconds');
  logger.info('  Symbols: BTCUSDT · ETHUSDT · SOLUSDT');
  logger.info('══════════════════════════════════════════════════════');
  logger.info('');

  // ── Migrations ────────────────────────────────────────────────────────────
  const sql = getDb();
  const migPath = join(process.cwd(), 'migrations', '0001_init.sql');
  if (existsSync(migPath)) {
    await sql.unsafe(readFileSync(migPath, 'utf8'));
    log.info('✅ Database ready');
  }

  // ── Market simulator ──────────────────────────────────────────────────────
  const sim = new MarketSimulator([
    { symbol: 'BTCUSDT', startPrice: 67_000, regime: 'trending_up' },
    { symbol: 'ETHUSDT', startPrice: 3_500,  regime: 'ranging' },
    { symbol: 'SOLUSDT', startPrice: 175,    regime: 'high_volatility' },
  ]);

  const client = new SimulatedBybitClient(sim) as unknown as import('../exchange/BybitClient').BybitClient;
  const simClient = new SimulatedBybitClient(sim);

  // ── Modules ───────────────────────────────────────────────────────────────
  const market  = new MarketDataService(client);
  const decisions = new DecisionEngine();
  const bots    = new BotManager(client, true);       // paper = true
  const copy    = new CopyTradingManager(client, true);
  const review  = new SelfReview();
  const promote = new PromotionManager();

  let portfolioState: PortfolioState = {
    equity: 10_000, peakEquity: 10_000, daySartEquity: 10_000,
    dailyRealizedPnl: 0, openPositionCount: 0,
    dailyLossLimit: 0.08, killLevelPct: 0.20,
    circuitBreakerPct: 0.10, maxRiskPct: 0.015,
  };
  const riskManager = new RiskManager(() => portfolioState);

  // ── Main cycle ────────────────────────────────────────────────────────────
  let cycleNum = 0;

  while (cycleNum < MAX_CYCLES) {
    cycleNum++;
    sim.tick();
    simClient.checkStopsAndTakeProfit();

    const cycleId = randomUUID();
    const balance = simClient.getBalance();
    const unrealPnl = simClient.getVirtualPositions().reduce((s, p) => {
      const m = sim.getMarket(p.symbol);
      const px = m?.price ?? p.avgEntry;
      return s + (p.side === 'Buy' ? 1 : -1) * p.size * (px - p.avgEntry);
    }, 0);
    const equity = balance + unrealPnl;

    portfolioState = {
      equity, peakEquity: Math.max(portfolioState.peakEquity, equity),
      daySartEquity: portfolioState.daySartEquity || equity,
      dailyRealizedPnl: equity - portfolioState.daySartEquity,
      openPositionCount: simClient.getVirtualPositions().length,
      dailyLossLimit: 0.08, killLevelPct: 0.20,
      circuitBreakerPct: 0.10, maxRiskPct: 0.015,
    };

    // Update DB equity snapshot
    const drawdown = portfolioState.peakEquity > 0
      ? (portfolioState.peakEquity - equity) / portfolioState.peakEquity : 0;
    await sql`
      UPDATE agent_state SET equity = ${equity}, peak_equity = ${portfolioState.peakEquity},
        last_cycle_at = now(), updated_at = now() WHERE id = 'singleton'
    `.catch(() => {});
    await sql`
      INSERT INTO equity_snapshots (total_equity, available, unrealized_pnl, realized_pnl_day, open_positions, drawdown_pct, env)
      VALUES (${equity}, ${balance}, ${unrealPnl}, ${portfolioState.dailyRealizedPnl},
        ${portfolioState.openPositionCount}, ${drawdown}, 'testnet')
    `.catch(() => {});

    // Get market snapshots
    const snapshots = await Promise.all(
      ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'].map(s =>
        market.getSnapshot(s, 'linear').catch(() => null)
      )
    ).then(r => r.filter(Boolean) as Awaited<ReturnType<typeof market.getSnapshot>>[]);

    // Log header
    const pnlStr = (equity - 10_000).toFixed(2);
    const pnlSign = equity >= 10_000 ? '+' : '';
    log.info(`\n${'─'.repeat(58)}`);
    log.info(`  Cycle ${cycleNum.toString().padStart(3)} │ Equity: $${equity.toFixed(2)} (${pnlSign}$${pnlStr}) │ Positions: ${portfolioState.openPositionCount}`);
    for (const snap of snapshots) {
      const regime = classifyRegime(snap);
      const m = sim.getMarket(snap.symbol);
      log.info(`  ${snap.symbol.padEnd(8)} $${snap.lastPrice.toFixed(2).padStart(10)} │ ${regime.toUpperCase().padEnd(16)} │ RSI:${snap.indicators.rsi14.toFixed(0)} ADX:${snap.indicators.adxValue.toFixed(0)} Funding:${(snap.fundingRate * 100).toFixed(4)}%`);
    }

    // Hard limits
    const { killTripped, dailyLimitHit, circuitBreaker } = await riskManager.checkHardLimits(cycleId);
    if (killTripped) { log.fatal('🔴 KILL LEVEL HIT — stopping simulation'); break; }

    // Run decisions
    if (!dailyLimitHit) {
      const signals = await decisions.run(snapshots, cycleId);
      if (signals.length > 0) {
        log.info(`  📊 ${signals.length} signal(s):`);
        for (const sig of signals.slice(0, 3)) {
          log.info(`     ▸ ${sig.symbol} ${sig.action.toUpperCase()} via ${sig.strategy} | score:${sig.compositeScore.toFixed(2)} | ${sig.rationale}`);
        }
      } else {
        log.info(`  📊 No signals above threshold — HOLD`);
      }

      // Track open position keys to prevent duplicate entries in same symbol/direction
      const openPosKeys = new Set(
        simClient.getVirtualPositions().map(p => `${p.symbol}-${p.side}`)
      );

      // Execute top signals
      for (const sig of signals.slice(0, 2)) {
        if (!sig.suggestedEntry || !sig.suggestedStop) continue;
        const side = sig.action === 'enter_long' ? 'Buy' : 'Sell';

        // Skip if already holding this symbol in this direction
        if (openPosKeys.has(`${sig.symbol}-${side}`)) {
          log.info(`     ⏭ ${sig.symbol} ${side} skipped — position already open`);
          await sql`UPDATE decision_log SET approved = false, reject_reason = 'duplicate position'
            WHERE cycle_id = ${cycleId}::uuid AND symbol = ${sig.symbol} AND strategy = ${sig.strategy}
          `.catch(() => {});
          continue;
        }

        const instrument = (await market.getInstrument('linear', sig.symbol).catch(() => null));
        if (!instrument) continue;
        const approval = riskManager.approve(sig, instrument, circuitBreaker);

        // Write approval result back to decision_log
        await sql`UPDATE decision_log SET approved = ${approval.approved}, reject_reason = ${approval.reason ?? null}
          WHERE cycle_id = ${cycleId}::uuid AND symbol = ${sig.symbol} AND strategy = ${sig.strategy}
        `.catch(() => {});

        if (!approval.approved) {
          log.info(`     ✗ ${sig.symbol} rejected: ${approval.reason}`);
          continue;
        }
        await (simClient as unknown as import('../exchange/BybitClient').BybitClient).placeOrder({
          category: 'linear', symbol: sig.symbol, side, orderType: 'Market',
          qty: String(approval.qty),
          stopLoss: approval.stopPrice ? String(approval.stopPrice.toFixed(2)) : undefined,
          takeProfit: approval.tpPrice ? String(approval.tpPrice.toFixed(2)) : undefined,
          orderLinkId: `sim-${sig.strategy}-${sig.symbol}-${Date.now()}`,
        }).catch(() => {});

        openPosKeys.add(`${sig.symbol}-${side}`); // prevent second signal for same symbol this cycle

        // persist to orders table
        await sql`
          INSERT INTO orders (exchange_order_id, order_link_id, symbol, category, side, order_type, qty, is_paper)
          VALUES (${'sim-' + Date.now()}, ${'simlink-' + Date.now()}, ${sig.symbol}, 'linear', ${side}, 'Market', ${approval.qty}, true)
        `.catch(() => {});
      }
    }

    // Bot + copy + review
    await bots.tick(snapshots, portfolioState).catch(() => {});
    await copy.tick(portfolioState).catch(() => {});
    await review.tick(cycleId).catch(() => {});

    // Every 10 cycles, show strategy weights
    if (cycleNum % 10 === 0) {
      const weights = await sql`SELECT strategy, weight, enabled, win_rate FROM strategy_weights ORDER BY weight DESC`;
      log.info(`\n  📈 Strategy weights (cycle ${cycleNum}):`);
      for (const w of weights) {
        const wt = parseFloat(String(w.weight));
        const bar = '█'.repeat(Math.max(0, Math.round(wt * 5)));
        log.info(`     ${String(w.strategy).padEnd(18)} ${bar.padEnd(12)} ${wt.toFixed(3)} ${w.enabled ? '✓' : '✗'}`);
      }
    }

    // Every 50 cycles generate a report
    if (cycleNum % 50 === 0) {
      log.info('\n  📧 Generating report preview…');
      await buildAndSendReport('daily').catch(e => log.error({ e }, 'Report failed'));
    }

    await sleep(CYCLE_MS);
  }

  // Final summary
  const finalEquity = simClient.getBalance();
  const totalTrades = await sql`SELECT COUNT(*) as n, SUM(realized_pnl) as pnl FROM trades WHERE is_paper = true`.then(r => r[0]);
  const weights = await sql`SELECT strategy, weight FROM strategy_weights ORDER BY weight DESC`;

  logger.info('\n');
  logger.info('══════════════════════════════════════════════════════');
  logger.info('  SIMULATION COMPLETE');
  logger.info(`  Final balance:  $${finalEquity.toFixed(2)}`);
  logger.info(`  Total trades:   ${totalTrades?.n ?? 0}`);
  logger.info(`  Realized PnL:   $${parseFloat(totalTrades?.pnl ?? '0').toFixed(2)}`);
  logger.info('  Final strategy weights:');
  for (const w of weights) logger.info(`    ${w.strategy.padEnd(20)} ${w.weight.toFixed(3)}`);
  logger.info('══════════════════════════════════════════════════════');

  await closeDb();
}

function sleep(ms: number): Promise<void> { return new Promise(r => setTimeout(r, ms)); }

runSimulation().catch(e => { logger.fatal({ e }, 'Simulation crashed'); process.exit(1); });
