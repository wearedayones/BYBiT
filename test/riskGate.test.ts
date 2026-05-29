import { describe, it, expect } from 'vitest';
import { RiskManager, type PortfolioState } from '../src/risk/RiskManager';
import type { WeightedSignal } from '../src/strategy/DecisionEngine';
import type { InstrumentInfo } from '../src/exchange/types';

const state: PortfolioState = {
  equity: 10_000, peakEquity: 10_000, daySartEquity: 10_000,
  dailyRealizedPnl: 0, openPositionCount: 0,
  dailyLossLimit: 0.08, killLevelPct: 0.20, circuitBreakerPct: 0.10, maxRiskPct: 0.015,
};

const instrument = {
  symbol: 'BTCUSDT', baseCoin: 'BTC', quoteCoin: 'USDT', status: 'Trading',
  lotSizeFilter: { minOrderQty: '0.001', maxOrderQty: '1000', qtyStep: '0.001' },
  priceFilter: { minPrice: '0.01', maxPrice: '9999999', tickSize: '0.01' },
} as InstrumentInfo;

const signal = (entry: number, stop: number, tp: number | undefined, strategy = 'trend_momentum'): WeightedSignal => ({
  action: 'enter_long', symbol: 'BTCUSDT', strategy,
  confidence: 0.7, suggestedEntry: entry, suggestedStop: stop, suggestedTp: tp,
  rationale: 'test',
  compositeScore: 1, weight: 1, learnedPrior: 1.0, sentimentMultiplier: 1, trendingBoost: 1,
});

describe('RiskManager expectancy gate', () => {
  const rm = new RiskManager(() => state);

  it('approves a high reward:risk, positive-EV trade', () => {
    const r = rm.approve(signal(100, 99, 103), instrument, false);
    expect(r.approved).toBe(true);
    expect(r.qty).toBeGreaterThan(0);
    expect(r.ev).toBeGreaterThan(0);
  });

  it('rejects a trade whose reward:risk is below the floor', () => {
    const r = rm.approve(signal(100, 99, 100.5), instrument, false); // R:R = 0.5
    expect(r.approved).toBe(false);
    expect(r.reason).toMatch(/reward:risk/i);
  });

  it('rejects a thin-edge trade with negative expected value', () => {
    const r = rm.approve(signal(100, 99, 101.3), instrument, false); // R:R 1.3, coin flip
    expect(r.approved).toBe(false);
    expect(r.reason).toMatch(/expected value/i);
  });

  it('exempts funding-harvest legs (no TP) from the price-move gate', () => {
    const r = rm.approve(signal(100, 95, undefined, 'funding_harvest'), instrument, false);
    expect(r.approved).toBe(true);
  });
});
