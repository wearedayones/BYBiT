import { describe, it, expect } from 'vitest';
import { computePositionSize } from '../src/risk/sizing';

describe('sizing', () => {
  const base = {
    equity: 10000,
    maxRiskPct: 0.01,
    entryPrice: 50000,
    stopPrice: 49000,
    atr14: 500,
    minQty: 0.001,
    qtyStep: 0.001,
    maxQty: 100,
  };

  it('computes a non-zero size', () => {
    const r = computePositionSize(base);
    expect(r.qty).toBeGreaterThan(0);
  });

  it('risk amount does not exceed maxRiskPct * equity', () => {
    const r = computePositionSize(base);
    expect(r.riskAmount).toBeLessThanOrEqual(base.equity * base.maxRiskPct * 1.01);
  });

  it('returns zero qty when entry === stop', () => {
    const r = computePositionSize({ ...base, stopPrice: base.entryPrice });
    expect(r.qty).toBe(0);
  });

  it('caps at maxQty', () => {
    const r = computePositionSize({ ...base, equity: 100_000_000, maxQty: 0.01 });
    expect(r.qty).toBeLessThanOrEqual(0.01);
  });

  it('snaps to qtyStep', () => {
    const r = computePositionSize({ ...base, qtyStep: 0.01 });
    // Use round-trip check — qty must be within 1e-9 of an exact multiple of 0.01
    const stepsCount = Math.round(r.qty / 0.01);
    expect(Math.abs(r.qty - stepsCount * 0.01)).toBeLessThan(1e-9);
  });

  it('notional is capped by maxExposurePct', () => {
    const r = computePositionSize({ ...base, equity: 10000, maxExposurePct: 0.05 });
    const maxNotional = 10000 * 0.05;
    expect(r.notional).toBeLessThanOrEqual(maxNotional * 1.01);
  });

  it('larger equity → proportionally larger position (compounding)', () => {
    const r1 = computePositionSize({ ...base, equity: 10000 });
    const r2 = computePositionSize({ ...base, equity: 20000 });
    expect(r2.qty).toBeGreaterThan(r1.qty);
  });
});
