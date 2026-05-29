import { randomUUID } from 'crypto';
import { childLogger } from '../core/logger';
import { getDb } from '../persistence/db';
import { BybitClient } from '../exchange/BybitClient';
import { MarketDataService } from '../market/MarketDataService';
import { DecisionEngine } from '../strategy/DecisionEngine';
import { RiskManager } from '../risk/RiskManager';
import { PortfolioManager } from '../portfolio/PortfolioManager';
import { BotManager } from '../bots/BotManager';
import { CopyTradingManager } from '../copy/CopyTradingManager';
import { SelfReview } from '../review/SelfReview';
import { KillSwitch } from './KillSwitch';
import { PromotionManager } from './PromotionManager';
import { KillSwitchError } from '../core/errors';
import { CYCLE_INTERVAL_MS, REPO_POLL_INTERVAL_MS } from '../config/constants';
import { buildAndSendReport } from '../reports/ReportBuilder';
import { checkAndUpdate } from '../updater/RepoUpdater';
import type { WeightedSignal } from '../strategy/DecisionEngine';

const log = childLogger({ module: 'agent-loop' });

const WATCHED_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'];

export class AgentLoop {
  private running = false;
  private lastDailyReport = 0;
  private lastWeeklyReport = 0;
  private lastMonthlyReport = 0;
  private lastRepoCheck = 0;

  private readonly market: MarketDataService;
  private readonly decisions: DecisionEngine;
  private readonly riskManager: RiskManager;
  private readonly portfolio: PortfolioManager;
  private readonly bots: BotManager;
  private readonly copy: CopyTradingManager;
  private readonly review: SelfReview;
  private readonly killSwitch: KillSwitch;
  private readonly promotion: PromotionManager;

  constructor(private readonly client: BybitClient, private readonly isTestnet: boolean) {
    this.market = new MarketDataService(client);
    this.decisions = new DecisionEngine();
    this.portfolio = new PortfolioManager(client);
    this.riskManager = new RiskManager(() => this.portfolio.getState());
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

    // ── 1. Check agent_state for manual pause/kill ────────────────────────────
    const ag = await getDb()`SELECT * FROM agent_state WHERE id = 'singleton' LIMIT 1`.then(r => r[0]);
    if (ag?.kill_engaged) throw new KillSwitchError('Manual DB kill');
    if (ag?.status === 'paused') { cyclelog.info('Agent paused — skipping cycle'); return; }

    const currentEnv = ag?.env ?? 'testnet';
    const isTestnet = currentEnv === 'testnet';
    this.client.setTestnet(isTestnet);

    // ── 2. Hard limits ────────────────────────────────────────────────────────
    await this.portfolio.refresh();
    const { killTripped, dailyLimitHit, circuitBreaker } = await this.riskManager.checkHardLimits(cycleId);
    if (killTripped) {
      await this.killSwitch.engage('Kill level breached');
      return;
    }

    // ── 3. Market snapshots ───────────────────────────────────────────────────
    const snapshots = await Promise.all(
      WATCHED_SYMBOLS.map(s => this.market.getSnapshot(s, 'linear').catch(() => null))
    ).then(r => r.filter(Boolean) as Awaited<ReturnType<MarketDataService['getSnapshot']>>[]);

    if (snapshots.length === 0) { cyclelog.warn('No market data — skipping cycle'); return; }

    // ── 4. Decisions ──────────────────────────────────────────────────────────
    const signals: WeightedSignal[] = dailyLimitHit
      ? []
      : await this.decisions.run(snapshots, cycleId);

    // ── 5. Execute signals ────────────────────────────────────────────────────
    for (const sig of signals) {
      if (this.killSwitch.isEngaged()) break;
      const snap = snapshots.find(s => s.symbol === sig.symbol);
      if (!snap) continue;

      const instrument = await this.market.getInstrument('linear', sig.symbol).catch(() => null);
      if (!instrument) continue;

      const approval = this.riskManager.approve(sig, instrument, circuitBreaker);
      if (!approval.approved) {
        cyclelog.debug({ reason: approval.reason, symbol: sig.symbol }, 'Signal rejected');
        await getDb()`
          UPDATE decision_log SET approved = false, reject_reason = ${approval.reason ?? null}
          WHERE cycle_id = ${cycleId}::uuid AND symbol = ${sig.symbol} AND strategy = ${sig.strategy}
        `.catch(() => {});
        continue;
      }

      try {
        const side = sig.action === 'enter_long' ? 'Buy' : 'Sell';
        const orderLinkId = `agent-${sig.strategy}-${sig.symbol}-${Date.now()}`;

        const result = await this.client.placeOrder({
          category: 'linear',
          symbol: sig.symbol,
          side,
          orderType: 'Market',
          qty: String(approval.qty),
          stopLoss: approval.stopPrice ? String(approval.stopPrice) : undefined,
          takeProfit: approval.tpPrice ? String(approval.tpPrice) : undefined,
          positionIdx: 0,
          orderLinkId,
        });

        await getDb()`
          INSERT INTO orders (decision_id, exchange_order_id, order_link_id, symbol, category, side, order_type, qty, is_paper)
          SELECT id, ${result.orderId}, ${orderLinkId}, ${sig.symbol}, 'linear', ${side}, 'Market', ${approval.qty}, ${isTestnet}
          FROM decision_log WHERE cycle_id = ${cycleId}::uuid AND symbol = ${sig.symbol} AND strategy = ${sig.strategy}
          LIMIT 1
        `.catch(() => {});

        await getDb()`
          UPDATE decision_log SET approved = true, outcome = 'executed'
          WHERE cycle_id = ${cycleId}::uuid AND symbol = ${sig.symbol} AND strategy = ${sig.strategy}
        `.catch(() => {});

        cyclelog.info({ symbol: sig.symbol, side, qty: approval.qty, strategy: sig.strategy }, '✅ Order placed');
      } catch (e) {
        cyclelog.error({ e, symbol: sig.symbol }, 'Order failed');
        await getDb()`
          UPDATE decision_log SET outcome = 'failed', outcome_detail = ${JSON.stringify({ error: String(e) })}::jsonb
          WHERE cycle_id = ${cycleId}::uuid AND symbol = ${sig.symbol} AND strategy = ${sig.strategy}
        `.catch(() => {});
      }
    }

    // ── 6. Bot tick ───────────────────────────────────────────────────────────
    if (!dailyLimitHit && !this.killSwitch.isEngaged()) {
      await this.bots.tick(snapshots, this.portfolio.getState()).catch(e => cyclelog.error({ e }, 'Bot tick error'));
    }

    // ── 7. Copy trading tick ──────────────────────────────────────────────────
    await this.copy.tick(this.portfolio.getState()).catch(e => cyclelog.error({ e }, 'Copy tick error'));

    // ── 8. Self-review ────────────────────────────────────────────────────────
    await this.review.tick(cycleId).catch(e => cyclelog.error({ e }, 'Review error'));

    // ── 9. Promotion check ────────────────────────────────────────────────────
    if (isTestnet) {
      const promoted = await this.promotion.evaluate().catch(() => false);
      if (promoted) {
        cyclelog.info('🎉 Promoted to mainnet! Reloading with mainnet settings…');
        this.client.setTestnet(false);
      }
    }

    // ── 10. Reports ───────────────────────────────────────────────────────────
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

    // ── 11. Repo update check ─────────────────────────────────────────────────
    if (now - this.lastRepoCheck > REPO_POLL_INTERVAL_MS) {
      this.lastRepoCheck = now;
      checkAndUpdate().catch(e => cyclelog.warn({ e }, 'Repo check failed'));
    }

    // ── 12. Update agent_state ────────────────────────────────────────────────
    await getDb()`
      UPDATE agent_state SET last_cycle_at = now(), updated_at = now() WHERE id = 'singleton'
    `.catch(() => {});
  }

  private adaptiveInterval(): number {
    const state = this.portfolio.getState();
    if (!state) return CYCLE_INTERVAL_MS;
    const drawdown = state.peakEquity > 0 ? (state.peakEquity - state.equity) / state.peakEquity : 0;
    // Cycle faster in high drawdown (more responsive)
    if (drawdown > 0.08) return 30_000;
    return CYCLE_INTERVAL_MS;
  }

  stop(): void { this.running = false; }
}

function sleep(ms: number): Promise<void> {
  return new Promise(r => setTimeout(r, ms));
}
