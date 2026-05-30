import { randomUUID } from 'crypto';
import { childLogger } from '../core/logger';
import { getDb } from '../persistence/db';
import { BybitClient } from '../exchange/BybitClient';
import { MarketDataService } from '../market/MarketDataService';
import { MarketDiscovery } from '../market/MarketDiscovery';
import { DecisionEngine } from '../strategy/DecisionEngine';
import { RiskManager } from '../risk/RiskManager';
import { PortfolioManager } from '../portfolio/PortfolioManager';
import { PositionHealthManager } from '../positions/PositionHealthManager';
import { ExecutionRouter } from '../execution/ExecutionRouter';
import { BotManager } from '../bots/BotManager';
import { CopyTradingManager } from '../copy/CopyTradingManager';
import { SelfReview } from '../review/SelfReview';
import { KillSwitch } from './KillSwitch';
import { PromotionManager } from './PromotionManager';
import { KillSwitchError } from '../core/errors';
import { CYCLE_INTERVAL_MS, REPO_POLL_INTERVAL_MS } from '../config/constants';
import { rateLimiter } from '../exchange/rateLimiter';
import { buildAndSendReport } from '../reports/ReportBuilder';
import { checkAndUpdate } from '../updater/RepoUpdater';
import type { WeightedSignal } from '../strategy/DecisionEngine';

const log = childLogger({ module: 'agent-loop' });

// Seed list used only until the first market-discovery pass populates the
// balance-aware watch list; after that the agent picks its own universe.
const FALLBACK_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'];
const DISCOVERY_INTERVAL_MS = 5 * 60 * 1000; // re-survey the market every 5 min

export class AgentLoop {
  private running = false;
  private lastDailyReport = 0;
  private lastWeeklyReport = 0;
  private lastMonthlyReport = 0;
  private lastRepoCheck = 0;
  private lastDiscovery = 0;
  private watchedSymbols: string[] = [...FALLBACK_SYMBOLS];

  private readonly market: MarketDataService;
  private readonly discovery: MarketDiscovery;
  private readonly decisions: DecisionEngine;
  private readonly riskManager: RiskManager;
  private readonly portfolio: PortfolioManager;
  private readonly positionHealth: PositionHealthManager;
  private readonly execution: ExecutionRouter;
  private readonly bots: BotManager;
  private readonly copy: CopyTradingManager;
  private readonly review: SelfReview;
  private readonly killSwitch: KillSwitch;
  private readonly promotion: PromotionManager;

  constructor(private readonly client: BybitClient, private readonly isTestnet: boolean) {
    this.market = new MarketDataService(client);
    this.discovery = new MarketDiscovery(client);
    this.decisions = new DecisionEngine();
    this.portfolio = new PortfolioManager(client);
    this.riskManager = new RiskManager(() => this.portfolio.getState());
    this.positionHealth = new PositionHealthManager(client);
    this.execution = new ExecutionRouter(client, false);
    this.bots = new BotManager(client, isTestnet);
    this.copy = new CopyTradingManager(client, isTestnet);
    this.review = new SelfReview();
    this.killSwitch = new KillSwitch(client);
    this.promotion = new PromotionManager();
  }

  async start(): Promise<void> {
    this.running = true;
    log.info({ testnet: this.isTestnet }, '🚀 Agent loop starting');

    // Check for manual kill in DB on startup
    const ag = await getDb()`SELECT kill_engaged FROM agent_state WHERE id = 'singleton' LIMIT 1`.then(r => r[0]);
    if (ag?.kill_engaged) {
      log.fatal('Kill switch is engaged in DB. Clear manually before restarting.');
      process.exit(1);
    }

    while (this.running) {
      try {
        await this.cycle();
      } catch (e) {
        if (e instanceof KillSwitchError) {
          log.fatal({ reason: e.reason }, 'Kill switch triggered — agent halted');
          this.running = false;
          break;
        }
        log.error({ e }, 'Cycle error');
      }
      await sleep(this.adaptiveInterval());
    }
  }

  private async cycle(): Promise<void> {
    const cycleId = randomUUID();
    const cyclelog = log.child({ cycleId });

    // ── 1. Check agent_state for manual pause/kill ─────────────────────────
    const ag = await getDb()`SELECT * FROM agent_state WHERE id = 'singleton' LIMIT 1`.then(r => r[0]);
    if (ag?.kill_engaged) throw new KillSwitchError('Manual DB kill');
    if (ag?.status === 'paused') { cyclelog.info('Agent paused — skipping cycle'); return; }

    const currentEnv = ag?.env ?? 'testnet';
    const isTestnet = currentEnv === 'testnet';
    this.client.setTestnet(isTestnet);

    // ── 2. Hard limits ────────────────────────────────────────────
    await this.portfolio.refresh();
    const { killTripped, dailyLimitHit, circuitBreaker } = await this.riskManager.checkHardLimits(cycleId);
    if (killTripped) {
      await this.killSwitch.engage('Kill level breached');
      return;
    }

    // ── 2b. Market discovery ─────────────────────────────────────
    // Periodically re-survey the exchange and rebuild the watch list around
    // what this balance can actually trade. The agent owns its own universe.
    const now0 = Date.now();
    if (now0 - this.lastDiscovery > DISCOVERY_INTERVAL_MS) {
      this.lastDiscovery = now0;
      const equity = this.portfolio.getState().equity;
      const symbols = await this.discovery.discover(equity).catch(() => null);
      if (symbols && symbols.length > 0) this.watchedSymbols = symbols;
    }

    // ── 3. Market snapshots ──────────────────────────────────────
    const snapshots = await Promise.all(
      this.watchedSymbols.map(s => this.market.getSnapshot(s, 'linear').catch(() => null))
    ).then(r => r.filter(Boolean) as Awaited<ReturnType<MarketDataService['getSnapshot']>>[]);

    if (snapshots.length === 0) { cyclelog.warn('No market data — skipping cycle'); return; }

    // ── 3b. Manage open positions (breakeven / trail / partial TP / time exit) ──
    if (!this.killSwitch.isEngaged()) {
      await this.positionHealth.tick(snapshots).catch(e => cyclelog.error({ e }, 'Position health error'));
    }

    // ── 4. Decisions ───────────────────────────────────────────
    const signals: WeightedSignal[] = dailyLimitHit
      ? []
      : await this.decisions.run(snapshots, cycleId);

    // ── 5. Execute signals ──────────────────────────────────────
    for (const sig of signals) {
      if (this.killSwitch.isEngaged()) break;

      // Let the agent manage existing positions via PositionHealthManager.
      // Don't stack entries on top of an already-open position for the same symbol.
      if (this.portfolio.hasOpenPosition(sig.symbol)) {
        cyclelog.debug({ symbol: sig.symbol }, 'Skipping entry — position already open');
        continue;
      }

      const snap = snapshots.find(s => s.symbol === sig.symbol);
      if (!snap) continue;

      const instrument = await this.market.getInstrument('linear', sig.symbol).catch(() => null);
      if (!instrument) continue;

      const approval = this.riskManager.approve(sig, instrument, circuitBreaker);
      const econ = { ev: approval.ev ?? null, costPct: approval.costPct ?? null, rewardRisk: approval.rewardRisk ?? null };
      if (!approval.approved) {
        cyclelog.debug({ reason: approval.reason, symbol: sig.symbol }, 'Signal rejected');
        await getDb()`
          UPDATE decision_log SET approved = false, reject_reason = ${approval.reason ?? null},
            inputs = COALESCE(inputs, '{}'::jsonb) || ${getDb().json(econ)}
          WHERE cycle_id = ${cycleId}::uuid AND symbol = ${sig.symbol} AND strategy = ${sig.strategy}
        `.catch(() => {});
        continue;
      }

      try {
        const side = sig.action === 'enter_long' ? 'Buy' : 'Sell';
        const orderLinkId = `agent-${sig.strategy}-${sig.symbol}-${Date.now()}`;

        // Maker-first: rest a PostOnly limit, fall back to market if unfilled.
        const result = await this.execution.enter({
          category: 'linear',
          symbol: sig.symbol,
          side,
          qty: approval.qty,
          refPrice: approval.stopPrice && sig.suggestedEntry ? sig.suggestedEntry : snap.lastPrice,
          stopLoss: approval.stopPrice || undefined,
          takeProfit: approval.tpPrice,
          orderLinkId,
        });

        await getDb()`
          INSERT INTO orders (decision_id, exchange_order_id, order_link_id, symbol, category, side, order_type, qty, is_paper)
          SELECT id, ${result.orderId}, ${result.orderLinkId}, ${sig.symbol}, 'linear', ${side},
            ${result.fillType === 'maker' ? 'Limit' : 'Market'}, ${approval.qty}, ${isTestnet}
          FROM decision_log WHERE cycle_id = ${cycleId}::uuid AND symbol = ${sig.symbol} AND strategy = ${sig.strategy}
          LIMIT 1
        `.catch(() => {});

        await getDb()`
          UPDATE decision_log SET approved = true, outcome = 'executed',
            inputs = COALESCE(inputs, '{}'::jsonb) || ${getDb().json(econ)}
          WHERE cycle_id = ${cycleId}::uuid AND symbol = ${sig.symbol} AND strategy = ${sig.strategy}
        `.catch(() => {});

        cyclelog.info({ symbol: sig.symbol, side, qty: approval.qty, strategy: sig.strategy, fill: result.fillType }, '✅ Order placed');
      } catch (e) {
        cyclelog.error({ e, symbol: sig.symbol }, 'Order failed');
        await getDb()`
          UPDATE decision_log SET outcome = 'failed', outcome_detail = ${JSON.stringify({ error: String(e) })}::jsonb
          WHERE cycle_id = ${cycleId}::uuid AND symbol = ${sig.symbol} AND strategy = ${sig.strategy}
        `.catch(() => {});
      }
    }

    // ── 6. Bot tick ───────────────────────────────────────────────
    if (!dailyLimitHit && !this.killSwitch.isEngaged()) {
      await this.bots.tick(snapshots, this.portfolio.getState()).catch(e => cyclelog.error({ e }, 'Bot tick error'));
    }

    // ── 7. Copy trading tick ───────────────────────────────────────
    await this.copy.tick(this.portfolio.getState()).catch(e => cyclelog.error({ e }, 'Copy tick error'));

    // ── 8. Self-review ───────────────────────────────────────────
    await this.review.tick(cycleId).catch(e => cyclelog.error({ e }, 'Review error'));

    // ── 9. Promotion check ──────────────────────────────────────
    if (isTestnet) {
      const promoted = await this.promotion.evaluate().catch(() => false);
      if (promoted) {
        cyclelog.info('🎉 Promoted to mainnet! Reloading with mainnet settings…');
        this.client.setTestnet(false);
      }
    }

    // ── 10. Reports ────────────────────────────────────────────
    const now = Date.now();
    if (now - this.lastDailyReport > 24 * 60 * 60 * 1000) {
      this.lastDailyReport = now;
      buildAndSendReport('daily').catch(e => cyclelog.error({ e }, 'Daily report failed'));
    }
    if (now - this.lastWeeklyReport > 7 * 24 * 60 * 60 * 1000) {
      this.lastWeeklyReport = now;
      buildAndSendReport('weekly').catch(e => cyclelog.error({ e }, 'Weekly report failed'));
    }
    if (now - this.lastMonthlyReport > 30 * 24 * 60 * 60 * 1000) {
      this.lastMonthlyReport = now;
      buildAndSendReport('monthly').catch(e => cyclelog.error({ e }, 'Monthly report failed'));
    }

    // ── 11. Repo update check ─────────────────────────────────────
    if (now - this.lastRepoCheck > REPO_POLL_INTERVAL_MS) {
      this.lastRepoCheck = now;
      checkAndUpdate().catch(e => cyclelog.warn({ e }, 'Repo check failed'));
    }

    // ── 12. Update agent_state ────────────────────────────────────
    await getDb()`
      UPDATE agent_state SET last_cycle_at = now(), updated_at = now() WHERE id = 'singleton'
    `.catch(() => {});
  }

  private adaptiveInterval(): number {
    const state = this.portfolio.getState();
    const base = CYCLE_INTERVAL_MS;

    // High drawdown → cycle faster for faster de-risk response
    const drawdown = state?.peakEquity > 0
      ? (state.peakEquity - state.equity) / state.peakEquity : 0;
    if (drawdown > 0.08) return 30_000;

    // Rate-limit pressure → back off to protect the quota window
    const worst = rateLimiter.getWorstBucket();
    if (worst && worst.limit > 0) {
      const usage = (worst.limit - worst.remaining) / worst.limit;
      if (usage > 0.90) {
        // Slow to 3× to shed load while window recovers
        log.warn({ usage: Math.round(usage * 100), endpoint: worst.endpoint }, 'API quota pressure — tripling cycle interval');
        return base * 3;
      }
      if (usage > 0.75) {
        return base * 2;
      }
    }

    return base;
  }

  stop(): void { this.running = false; }
}

function sleep(ms: number): Promise<void> {
  return new Promise(r => setTimeout(r, ms));
}
