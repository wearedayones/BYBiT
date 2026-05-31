"""Phase 2: cost model parity — ports test/costModel.test.ts."""

from __future__ import annotations

from bybit_agent.config.constants import COST_DEFAULTS, FEES
from bybit_agent.risk.cost_model import expected_value, fee_rate, round_trip_cost_pct


def _sig(entry, stop, tp):
    return {"action": "enter_long", "symbol": "BTCUSDT", "strategy": "trend_momentum",
            "confidence": 0.7, "suggestedEntry": entry, "suggestedStop": stop, "suggestedTp": tp,
            "rationale": "test"}


def test_charges_right_per_side_fee():
    assert fee_rate("linear", True) == FEES["PERP_MAKER"]
    assert fee_rate("linear", False) == FEES["PERP_TAKER"]
    assert fee_rate("spot", False) == FEES["SPOT_TAKER"]


def test_round_trip_cost():
    c = round_trip_cost_pct("linear", True, False)
    expected = FEES["PERP_MAKER"] + FEES["PERP_TAKER"] + COST_DEFAULTS["SLIPPAGE_PCT"] * 2
    assert abs(c - expected) < 1e-12


def test_unknown_prior_is_055():
    cost = round_trip_cost_pct("linear", True, False)
    r = expected_value(_sig(100, 99, 103), 1.0, cost)
    assert r is not None and r.winProb == 0.55


def test_positive_ev_high_rr():
    cost = round_trip_cost_pct("linear", True, False)
    r = expected_value(_sig(100, 99, 103), 1.0, cost)  # R:R = 3
    assert abs(r.rewardRisk - 3) < 1e-6
    assert r.ev > 0


def test_negative_ev_poor_rr():
    cost = round_trip_cost_pct("linear", True, False)
    r = expected_value(_sig(100, 99, 100.5), 1.0, cost)  # R:R 0.5
    assert r.ev < 0


def test_null_without_target():
    assert expected_value({**_sig(100, 99, 101), "suggestedTp": None}, 0.6, 0.001) is None
