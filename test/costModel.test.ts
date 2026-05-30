import { describe, it, expect } from 'vitest';
import { roundTripCostPct, expectedValue, feeRate } from '../src/risk/CostModel';
import { FEES, COST_DEFAULTS } from '../src/config/constants';
import type { Signal } from '../src/strategy/Strategy';

const sig = (entry: number, stop: number, tp: number): Signal => ({
  action: 'enter_long', symbol: 'BTCUSDT', strategy: 'trend_momentum',
  confidence: 0.7, suggestedEntry: entry, suggestedStop: stop, suggestedTp: tp,
  rationale: 'test',
});

describe('CostModel', () => {
  it('charges the right per-side fee', () => {
    expect(feeRate('linear', true)).toBe(FEES.PERP_MAKER);
    expect(feeRate('linear', false)).toBe(FEES.PERP_TAKER);
    expect(feeRate('spot', false)).toBe(FEES.SPOT_TAKER);
  });

  it('round-trip cost = maker entry + taker exit + two slippages', () => {
    const c = roundTripCostPct('linear', true, false);
    const expected = FEES.PERP_MAKER + FEES.PERP_TAKER + COST_DEFAULTS.SLIPPAGE_PCT * 2;
    expect(c).toBeCloseTo(expected, 10);
  });

  it('treats an unknown prior (1.0) as a slight positive bias (0.55)', () => {
    // A directional signal that cleared the strategy + regime filters is not a
    // pure coin flip; with no learned history we assume a small edge so the
    // system can bootstrap trades before win-rates accumulate.
    const cost = roundTripCostPct('linear', true, false);
    const r = expectedValue(sig(100, 99, 103), 1.0, cost)!;
    expect(r.winProb).toBe(0.55);
  });

  it('positive EV for a high reward:risk trade', () => {
    const cost = roundTripCostPct('linear', true, false);
    const r = expectedValue(sig(100, 99, 103), 1.0, cost)!; // R:R = 3
    expect(r.rewardRisk).toBeCloseTo(3, 6);
    expect(r.ev).toBeGreaterThan(0);
  });

  it('negative EV when reward:risk is poor and costs eat the edge', () => {
    const cost = roundTripCostPct('linear', true, false);
    const r = expectedValue(sig(100, 99, 100.5), 1.0, cost)!; // R:R = 0.5, win < loss
    expect(r.ev).toBeLessThan(0);
  });

  it('returns null when the signal lacks a target', () => {
    const noTp: Signal = { ...sig(100, 99, 101), suggestedTp: undefined };
    expect(expectedValue(noTp, 0.6, 0.001)).toBeNull();
  });
});
