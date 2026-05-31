"""Position sizing — ports src/risk/sizing.ts using stdlib Decimal (prec 28, ROUND_DOWN)
to match decimal.js exactly. Risk-budget sizing, volatility scaling, step snapping,
exposure cap, and min-lot rounding bounded by an absolute fraction of equity.

Leverage changes the *notional exposure* available but not the *risk budget*:
  risk_budget  = equity × maxRiskPct        (how much of real equity you can lose)
  max_notional = equity × maxExposurePct    (real buying-power cap; leverage applied below)
  qty          = risk_budget / risk_per_unit

With leverage L, the exchange lets you hold L× the position your margin supports.
The exposure cap accounts for this: max_notional_leveraged = max_notional × leverage.
The absolute-risk rescue also uses ACTUAL equity (not leveraged) so min-lot
orders never risk more than maxAbsoluteRiskPct × real equity.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, getcontext

getcontext().prec = 28
getcontext().rounding = ROUND_DOWN

_D = Decimal


@dataclass
class SizingInput:
    equity: float
    maxRiskPct: float
    entryPrice: float
    stopPrice: float
    atr14: float
    minQty: float
    qtyStep: float
    maxQty: float
    leverage: int = 1                   # exchange leverage (1 = no leverage)
    maxExposurePct: float | None = None
    maxAbsoluteRiskPct: float | None = None


@dataclass
class SizingResult:
    qty: float
    riskAmount: float
    notional: float
    riskPct: float


def _floor_step(qty: Decimal, step: Decimal) -> Decimal:
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def compute_position_size(inp: SizingInput) -> SizingResult:
    equity    = _D(str(inp.equity))
    max_risk  = _D(str(inp.maxRiskPct))
    entry     = _D(str(inp.entryPrice))
    stop      = _D(str(inp.stopPrice))
    atr       = _D(str(inp.atr14))
    qty_step  = _D(str(inp.qtyStep))
    min_qty   = _D(str(inp.minQty))
    max_qty   = _D(str(inp.maxQty))
    leverage  = _D(str(max(1, inp.leverage)))

    risk_budget  = equity * max_risk
    risk_per_unit = abs(entry - stop)

    if risk_per_unit <= 0:
        return SizingResult(0, 0, 0, 0)

    atr_pct    = atr / entry if entry > 0 else _D(0)
    vol_scale  = _D(1) / (_D(1) + atr_pct * _D(10))
    adj_budget = risk_budget * vol_scale

    qty = adj_budget / risk_per_unit
    qty = _floor_step(qty, qty_step)

    if qty > max_qty:
        qty = max_qty

    # Exposure cap uses LEVERAGED notional: with 10× the exchange lets you hold
    # 10× what your margin supports.  Cap is still anchored to real equity so
    # we never over-size, even with high leverage.
    if inp.maxExposurePct:
        max_notional = equity * _D(str(inp.maxExposurePct)) * leverage
        notional = qty * entry
        if notional > max_notional:
            qty = _floor_step(max_notional / entry, qty_step)

    qty = _floor_step(qty, qty_step)

    # Min-lot rescue: absolute risk check uses REAL equity (not leveraged).
    max_abs = _D(str(inp.maxAbsoluteRiskPct if inp.maxAbsoluteRiskPct is not None else 0.10))
    if qty < min_qty:
        min_lot_risk = min_qty * risk_per_unit
        if min_lot_risk <= equity * max_abs:
            qty = min_qty
        else:
            return SizingResult(0, 0, 0, 0)

    final_notional = qty * entry
    final_risk     = qty * risk_per_unit
    final_risk_pct = final_risk / equity   # expressed as fraction of REAL equity

    return SizingResult(
        qty=float(qty),
        riskAmount=float(final_risk),
        notional=float(final_notional),
        riskPct=float(final_risk_pct),
    )

