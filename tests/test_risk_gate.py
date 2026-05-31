"""Phase 2: risk approval/EV gate parity — ports test/riskGate.test.ts."""

from __future__ import annotations

import re

from bybit_agent.risk.risk_manager import PortfolioState, RiskManager

STATE = PortfolioState(
    equity=10_000, peakEquity=10_000, daySartEquity=10_000, dailyRealizedPnl=0,
    openPositionCount=0, dailyLossLimit=0.08, killLevelPct=0.20,
    circuitBreakerPct=0.10, maxRiskPct=0.015,
)

INSTRUMENT = {
    "symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading",
    "lotSizeFilter": {"minOrderQty": "0.001", "maxOrderQty": "1000", "qtyStep": "0.001"},
    "priceFilter": {"minPrice": "0.01", "maxPrice": "9999999", "tickSize": "0.01"},
}


def _sig(entry, stop, tp, strategy="trend_momentum", learned_prior=1.0):
    return {"action": "enter_long", "symbol": "BTCUSDT", "strategy": strategy,
            "confidence": 0.7, "suggestedEntry": entry, "suggestedStop": stop, "suggestedTp": tp,
            "rationale": "test", "learnedPrior": learned_prior}


rm = RiskManager(lambda: STATE)


def test_approves_high_rr_positive_ev():
    r = rm.approve(_sig(100, 99, 103), INSTRUMENT, False)
    assert r.approved is True
    assert r.qty > 0
    assert r.ev > 0


def test_rejects_low_reward_risk():
    r = rm.approve(_sig(100, 99, 100.5), INSTRUMENT, False)  # R:R 0.5
    assert r.approved is False
    assert re.search(r"reward:risk", r.reason, re.I)


def test_rejects_negative_ev_from_poor_prior():
    r = rm.approve(_sig(100, 99, 101.3, learned_prior=0.3), INSTRUMENT, False)
    assert r.approved is False
    assert re.search(r"expected value", r.reason, re.I)


def test_funding_harvest_exempt_from_price_gate():
    r = rm.approve(_sig(100, 95, None, strategy="funding_harvest"), INSTRUMENT, False)
    assert r.approved is True
