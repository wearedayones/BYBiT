import Decimal from 'decimal.js';

Decimal.set({ precision: 28, rounding: Decimal.ROUND_DOWN });

export interface SizingInput {
  equity: number;
  maxRiskPct: number;
  entryPrice: number;
  stopPrice: number;
  atr14: number;
  minQty: number;
  qtyStep: number;
  maxQty: number;
  maxExposurePct?: number;
  /**
   * Hard ceiling on a single min-lot trade's stop-loss risk, as a fraction of
   * equity. When the risk-budget sizing rounds below the exchange minimum, we
   * still take the trade if min-lot risk stays under this cap. Scales with any
   * balance, so a $10 and a $10k account both "know what to do". Default 0.10.
   */
  maxAbsoluteRiskPct?: number;
}

export interface SizingResult {
  qty: number;
  riskAmount: number;
  notional: number;
  riskPct: number;
}

export function computePositionSize(input: SizingInput): SizingResult {
  const equity = new Decimal(input.equity);
  const maxRiskPct = new Decimal(input.maxRiskPct);
  const entry = new Decimal(input.entryPrice);
  const stop = new Decimal(input.stopPrice);
  const atr = new Decimal(input.atr14);
  const qtyStep = new Decimal(input.qtyStep);
  const minQty = new Decimal(input.minQty);
  const maxQty = new Decimal(input.maxQty);

  const riskBudget = equity.mul(maxRiskPct);
  const riskPerUnit = entry.minus(stop).abs();

  if (riskPerUnit.lte(0)) {
    return { qty: 0, riskAmount: 0, notional: 0, riskPct: 0 };
  }

  // Volatility-scale: reduce size when ATR is large relative to entry
  const atrPct = atr.div(entry);
  const volScale = new Decimal(1).div(new Decimal(1).plus(atrPct.mul(10)));
  const adjustedBudget = riskBudget.mul(volScale);

  let qty = adjustedBudget.div(riskPerUnit);

  // Snap to step
  qty = qty.div(qtyStep).floor().mul(qtyStep);

  // Clamp to maxQty
  if (qty.gt(maxQty)) qty = maxQty;

  // Cap notional at maxExposurePct if provided
  if (input.maxExposurePct) {
    const maxNotional = equity.mul(input.maxExposurePct);
    const notional = qty.mul(entry);
    if (notional.gt(maxNotional)) {
      qty = maxNotional.div(entry).div(qtyStep).floor().mul(qtyStep);
    }
  }

  // Snap to step after caps
  qty = qty.div(qtyStep).floor().mul(qtyStep);

  // Min-lot rounding: when risk-based qty falls below the exchange minimum,
  // round up to minQty only if the resulting stop-loss risk stays within an
  // absolute fraction of equity (default 10%). This scales with any balance —
  // a $10 account trading a cheap coin and a $10k account trading ETH both
  // clear the same proportional bar — while still blocking instruments whose
  // min-lot would risk a dangerous chunk of the account in one trade.
  const maxAbsoluteRiskPct = new Decimal(input.maxAbsoluteRiskPct ?? 0.10);
  if (qty.lt(minQty)) {
    const minLotRisk = minQty.mul(riskPerUnit);
    if (minLotRisk.lte(equity.mul(maxAbsoluteRiskPct))) {
      qty = minQty;
    } else {
      return { qty: 0, riskAmount: 0, notional: 0, riskPct: 0 };
    }
  }

  const finalNotional = qty.mul(entry);
  const finalRisk = qty.mul(riskPerUnit);
  const finalRiskPct = finalRisk.div(equity);

  return {
    qty: qty.toNumber(),
    riskAmount: finalRisk.toNumber(),
    notional: finalNotional.toNumber(),
    riskPct: finalRiskPct.toNumber(),
  };
}
