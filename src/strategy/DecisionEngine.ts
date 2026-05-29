import type { Signal, Strategy } from './Strategy';
import type { MarketSnapshot, Regime } from '../market/MarketDataService';
import { TrendMomentum } from './impl/TrendMomentum';
import { MeanReversion } from './impl/MeanReversion';
import { Breakout } from './impl/Breakout';
import { FundingHarvest } from './impl/FundingHarvest';
import { childLogger } from '../core/logger';
import { getDb } from '../persistence/db';

const log = childLogger({ module: 'decision-engine' });

const CONFIDENCE_FLOOR = 0.40;

export interface WeightedSignal extends Signal {
  compositeScore: number;
  weight: number;
  learnedPrior: number;
}

export interface StrategyWeightRow {
  strategy: string;
  weight: number;
  enabled: boolean;
}

export class DecisionEngine {
  private readonly strategies: Strategy[] = [
    TrendMomentum, MeanReversion, Breakout, FundingHarvest,
  ];

  async run(snapshots: MarketSnapshot[], cycleId: string): Promise<WeightedSignal[]> {
    const sql = getDb();
    const rows = await sql<StrategyWeightRow[]>`SELECT strategy, weight, enabled FROM strategy_weights`;
    const weightMap = Object.fromEntries(rows.map(r => [r.strategy, r]));

    const results: WeightedSignal[] = [];

    for (const snap of snapshots) {
      const regime: Regime = classifyRegime(snap);

      for (const strategy of this.strategies) {
        const wRow = weightMap[strategy.name];
        if (!wRow?.enabled) continue;

        const ctx = {
          strategyWeight: wRow?.weight ?? 1.0,
          learnedPrior: await this.getLearnedPrior(strategy.name, regime, snap.symbol),
        };

        const signal = strategy.evaluate(snap, ctx);
        if (signal.action === 'hold') continue;

        const regimeMultiplier = strategy.suitableRegimes.includes(regime) ? 1.0 : 0.3;
        const weight = wRow?.weight ?? 1.0;
        const learnedPrior = ctx.learnedPrior;
        const recencyDecay = 0.85; // down-weight old learned data

        const compositeScore =
          signal.confidence * weight * regimeMultiplier * (learnedPrior * recencyDecay + (1 - recencyDecay));

        if (compositeScore < CONFIDENCE_FLOOR) continue;

        results.push({ ...signal, compositeScore, weight, learnedPrior });
      }
    }

    // Sort by composite score descending
    results.sort((a, b) => b.compositeScore - a.compositeScore);

    // Persist to decision_log
    for (const sig of results) {
      const snap = snapshots.find(s => s.symbol === sig.symbol);
      await sql`
        INSERT INTO decision_log (cycle_id, symbol, action, strategy, confidence, regime,
          rationale, inputs, composite_score, approved, outcome, is_paper)
        VALUES (
          ${cycleId}::uuid, ${sig.symbol}, ${sig.action}, ${sig.strategy},
          ${sig.confidence}, ${snap ? classifyRegime(snap) : null},
          ${sig.rationale}, ${snap ? JSON.stringify(snap.indicators) : null}::jsonb,
          ${sig.compositeScore}, null, null, false
        )
      `.catch(e => log.error({ e }, 'Failed to persist decision'));
    }

    log.info({ signals: results.length, cycleId }, 'Decisions computed');
    return results;
  }

  private async getLearnedPrior(strategy: string, regime: string, symbol: string): Promise<number> {
    try {
      const sql = getDb();
      const rows = await sql<{ win_rate: number; n_trades: number }[]>`
        SELECT win_rate, n_trades FROM learned_signals
        WHERE strategy = ${strategy} AND regime = ${regime}
        AND n_trades >= 10
        LIMIT 1
      `;
      if (rows[0]?.win_rate) return Math.max(0.5, rows[0].win_rate);
    } catch { /* ignore on first run */ }
    return 1.0;
  }
}

export function classifyRegime(snap: MarketSnapshot): Regime {
  const { adxValue, atrPct } = snap.indicators;
  const fundingAbs = Math.abs(snap.fundingRate);
  if (atrPct > 0.05 || fundingAbs > 0.002) return 'crisis';
  if (adxValue > 25) return 'trending';
  if (atrPct > 0.025) return 'high_volatility';
  return 'ranging';
}
