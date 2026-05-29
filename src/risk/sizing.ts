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

  // Clamp
  if (qty.lt(minQty)) return { qty: 0, riskAmount: 0, notional: 0, riskPct: 0 };
  if (qty.gt(maxQty)) qty = maxQty;

  // Cap notional at maxExposurePct if provided
  if (input.maxExposurePct) {
    const maxNotional = equity.mul(input.maxExposurePct);
    const notional = qty.mul(entry);
    if (notional.gt(maxNotional)) {
      qty = maxNotional.div(entry).div(qtyStep).floor().mul(qtyStep);
      if (qty.lt(minQty)) return { qty: 0, riskAmount: 0, notional: 0, riskPct: 0 };
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
