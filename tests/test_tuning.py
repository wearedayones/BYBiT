"""Phase 4: PARAM_WHITELIST bounds validation — the same checks bybit tune applies."""
from __future__ import annotations

import pytest

from bybit_agent.config.tuning import PARAM_WHITELIST, validate_param


def test_all_whitelist_params_have_required_keys():
    for name, spec in PARAM_WHITELIST.items():
        assert "min" in spec and "max" in spec, f"{name} missing min/max"
        assert "type" in spec and spec["type"] in ("float", "int"), f"{name} bad type"
        assert "db_key" in spec, f"{name} missing db_key"
        assert "desc" in spec, f"{name} missing desc"


def test_valid_float_param():
    value, db_key = validate_param("maxRiskPct", "0.01")
    assert value == 0.01
    assert db_key == "max_risk_pct"


def test_valid_int_param():
    value, db_key = validate_param("maxHoldCycles", "50")
    assert value == 50
    assert db_key == "max_hold_cycles"
    assert isinstance(value, int)


def test_unknown_param_raises():
    with pytest.raises(ValueError, match="Unknown param"):
        validate_param("nonExistentParam", "1.0")


def test_below_min_raises():
    with pytest.raises(ValueError, match="out of bounds"):
        validate_param("maxRiskPct", "0.0001")  # min is 0.001


def test_above_max_raises():
    with pytest.raises(ValueError, match="out of bounds"):
        validate_param("maxRiskPct", "0.99")  # max is 0.03


def test_non_numeric_raises():
    with pytest.raises(ValueError, match="not a valid"):
        validate_param("maxRiskPct", "not-a-number")


def test_boundary_values_accepted():
    # Exact min and max should be accepted.
    spec = PARAM_WHITELIST["maxRiskPct"]
    v_min, _ = validate_param("maxRiskPct", str(spec["min"]))
    v_max, _ = validate_param("maxRiskPct", str(spec["max"]))
    assert v_min == spec["min"]
    assert v_max == spec["max"]


def test_int_param_rejects_float_string():
    with pytest.raises(ValueError, match="not a valid int"):
        validate_param("maxHoldCycles", "10.5")


def test_all_params_accept_midpoint():
    for name, spec in PARAM_WHITELIST.items():
        mid = (spec["min"] + spec["max"]) / 2
        raw = str(int(mid)) if spec["type"] == "int" else str(mid)
        value, _ = validate_param(name, raw)
        assert spec["min"] <= value <= spec["max"]
