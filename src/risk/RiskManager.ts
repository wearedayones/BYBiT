import Decimal from 'decimal.js';
import { childLogger } from '../core/logger';
import { getDb } from '../persistence/db';
import { computePositionSize } from './sizing';
import type { WeightedSignal } from '../strategy/DecisionEngine';
import type { InstrumentInfo } from '../exchange/types';

const log = childLogger({ module: 'risk' });

export interface PortfolioState {
  equity: number;
  peakEquity: number;
  daySartEquity: number;
  dailyRealizedPnl: number;
  openPositionCount: number;
  dailyLossLimit: number;
  killLevelPct: number;
  circuitBreakerPct: number;
  maxRiskPct: number;
}

export interface ApprovalResult {
  approved: boolean;
  qty: number;
  reason?: string;
  stopPrice: number;
  tpPrice?: number;
}

export class RiskManager {
  constructor(private readonly state: () => PortfolioState) {}

  async checkHardLimits(cycleId: string): Promise<{ killTripped: boolean; dailyLimitHit: boolean; circuitBreaker: boolean }> {
    const s = this.state();
    const sql = getDb();
    const drawdownPct = s.peakEquity > 0 ? (s.peakEquity - s.equity) / s.peakEquity : 0;

    if (drawdownPct >= s.killLevelPct) {
      log.fatal({ drawdownPct, killLevel: s.killLevelPct }, '🔴 Kill level breached');
      await this.logRiskEvent(cycleId, 'kill_level', 'critical', null, { drawdownPct });
      return { killTripped: true, dailyLimitHit: false, circuitBreaker: false };
    }

    const dailyLossAbs = s.daySartEquity > 0
      ? (s.daySartEquity - s.equity) / s.daySartEquity
      : 0;
    if (dailyLossAbs >= s.dailyLossLimit) {
      log.warn({ dailyLossAbs, limit: s.dailyLossLimit }, '🟡 Daily loss limit hit');
      await this.logRiskEvent(cycleId, 'daily_loss', 'warn', null, { dailyLossAbs });
      return { killTripped: false, dailyLimitHit: true, circuitBreaker: false };
    }

    if (drawdownPct >= s.circuitBreakerPct) {
      log.warn({ drawdownPct }, '🟠 Circuit breaker triggered — de-risking');
      await this.logRiskEvent(cycleId, 'circuit_breaker', 'warn', null, { drawdownPct });
      return { killTripped: false, dailyLimitHit: false, circuitBreaker: true };
    }

    return { killTripped: false, dailyLimitHit: false, circuitBreaker: false };
  }

  approve(signal: WeightedSignal, instrument: InstrumentInfo, circuitBreaker: boolean): ApprovalResult {
    const s = this.state();
    const maxRiskPct = circuitBreaker ? s.maxRiskPct * 0.5 : s.maxRiskPct;

    if (!signal.suggestedStop) {
      return { approved: false, qty: 0, reason: 'No stop loss provided', stopPrice: 0 };
    }

    const tickSize = parseFloat(instrument.priceFilter.tickSize);
    const minQty = parseFloat(instrument.lotSizeFilter.minOrderQty);
    const qtyStep = parseFloat(instrument.lotSizeFilter.qtyStep);
    const maxQty = parseFloat(instrument.lotSizeFilter.maxOrderQty);

    const sizingResult = computePositionSize({
      equity: s.equity,
      maxRiskPct,
      entryPrice: signal.suggestedEntry ?? s.equity,
      stopPrice: signal.suggestedStop,
      atr14: 0,
      minQty,
      qtyStep,
      maxQty,
      maxExposurePct: 0.20,
    });

    if (sizingResult.qty <= 0) {
      return { approved: false, qty: 0, reason: 'Position size too small or zero', stopPrice: signal.suggestedStop };
    }

    if (s.openPositionCount >= 8) {
      return { approved: false, qty: 0, reason: 'Max concurrent positions reached', stopPrice: signal.suggestedStop };
    }

    return {
      approved: true,
      qty: sizingResult.qty,
      stopPrice: signal.suggestedStop,
      tpPrice: signal.suggestedTp,
    };
  }

  private async logRiskEvent(cycleId: string, type: string, severity: string, symbol: string | null, detail: object) {
    try {
      const sql = getDb();
      await sql`
        INSERT INTO risk_events (type, severity, cycle_id, symbol, detail, action_taken)
        VALUES (${type}, ${severity}, ${cycleId}::uuid, ${symbol}, ${JSON.stringify(detail)}::jsonb, ${type})
      `;
    } catch (e) { log.error({ e }, 'Failed to log risk event'); }
  }
}
