"""Phase 2: sizing parity — ports test/sizing.test.ts (Decimal prec 28, ROUND_DOWN)."""

from __future__ import annotations

from dataclasses import replace

from bybit_agent.risk.sizing import SizingInput, compute_position_size

BASE = SizingInput(
    equity=10000, maxRiskPct=0.01, entryPrice=50000, stopPrice=49000, atr14=500,
    minQty=0.001, qtyStep=0.001, maxQty=100,
)


def test_non_zero_size():
    assert compute_position_size(BASE).qty > 0


def test_risk_within_budget():
    r = compute_position_size(BASE)
    assert r.riskAmount <= BASE.equity * BASE.maxRiskPct * 1.01


def test_zero_when_entry_equals_stop():
    assert compute_position_size(replace(BASE, stopPrice=BASE.entryPrice)).qty == 0


def test_caps_at_max_qty():
    r = compute_position_size(replace(BASE, equity=100_000_000, maxQty=0.01))
    assert r.qty <= 0.01


def test_snaps_to_step():
    r = compute_position_size(replace(BASE, qtyStep=0.01))
    steps = round(r.qty / 0.01)
    assert abs(r.qty - steps * 0.01) < 1e-9


def test_notional_capped_by_exposure():
    r = compute_position_size(replace(BASE, equity=10000, maxExposurePct=0.05))
    assert r.notional <= 10000 * 0.05 * 1.01


def test_larger_equity_larger_position():
    r1 = compute_position_size(replace(BASE, equity=10000))
    r2 = compute_position_size(replace(BASE, equity=20000))
    assert r2.qty > r1.qty
