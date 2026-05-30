"""PARAM_WHITELIST — the set of numeric params the installing agent may tune.

Every entry is a dict with keys: min, max, type ('float'|'int'), desc, db_key.
`db_key` is the column in agent_state (or the key in agent_config) that the loop reads.
The `bybit tune --set KEY=VALUE` command validates against this table before writing.
"""
from __future__ import annotations

from typing import TypedDict


class ParamSpec(TypedDict):
    min: float
    max: float
    type: str     # 'float' | 'int'
    desc: str
    db_key: str   # column in agent_state / key in agent_config


# 10 tuneable numeric parameters — exact parity with the old PARAM_WHITELIST.
# Bounds are conservative: too tight to cause immediate harm, loose enough to tune.
PARAM_WHITELIST: dict[str, ParamSpec] = {
    "maxRiskPct": {
        "min": 0.001, "max": 0.03, "type": "float",
        "desc": "Max fraction of equity risked per trade (0.001–0.03)",
        "db_key": "max_risk_pct",
    },
    "dailyLossLimit": {
        "min": 0.01, "max": 0.15, "type": "float",
        "desc": "Daily loss limit as fraction of day-start equity (0.01–0.15)",
        "db_key": "daily_loss_limit",
    },
    "killLevelPct": {
        "min": 0.05, "max": 0.40, "type": "float",
        "desc": "Peak-to-current drawdown that triggers the kill switch (0.05–0.40)",
        "db_key": "kill_level_pct",
    },
    "circuitBreakerPct": {
        "min": 0.03, "max": 0.20, "type": "float",
        "desc": "Drawdown level that halves position sizing (0.03–0.20)",
        "db_key": "circuit_breaker_pct",
    },
    "makerWaitMs": {
        "min": 0, "max": 30_000, "type": "int",
        "desc": "Milliseconds to wait for a PostOnly maker fill before taker fallback (0–30000)",
        "db_key": "maker_wait_ms",
    },
    "makerOffsetPct": {
        "min": 0.0, "max": 0.005, "type": "float",
        "desc": "Fraction inside the touch to rest the maker limit (0–0.005)",
        "db_key": "maker_offset_pct",
    },
    "breakevenAtR": {
        "min": 0.5, "max": 3.0, "type": "float",
        "desc": "R-multiple at which the stop is moved to break-even (0.5–3.0)",
        "db_key": "breakeven_at_r",
    },
    "trailStartR": {
        "min": 1.0, "max": 5.0, "type": "float",
        "desc": "R-multiple at which ATR trailing begins (1.0–5.0)",
        "db_key": "trail_start_r",
    },
    "partialTpAtR": {
        "min": 0.5, "max": 3.0, "type": "float",
        "desc": "R-multiple at which half the position is closed for partial TP (0.5–3.0)",
        "db_key": "partial_tp_at_r",
    },
    "maxHoldCycles": {
        "min": 10, "max": 200, "type": "int",
        "desc": "Cycles before a stale ~flat position is closed by the time exit (10–200)",
        "db_key": "max_hold_cycles",
    },
}

STRATEGY_NAMES = ["trend_momentum", "mean_reversion", "breakout", "funding_harvest"]


def validate_param(key: str, raw_value: str) -> tuple[float | int, str]:
    """Parse and bounds-check a raw string value for a whitelisted param.

    Returns (coerced_value, db_key) on success; raises ValueError with a
    human-readable message on any violation.
    """
    if key not in PARAM_WHITELIST:
        known = ", ".join(sorted(PARAM_WHITELIST))
        raise ValueError(f"Unknown param '{key}'. Known params: {known}")

    spec = PARAM_WHITELIST[key]
    try:
        value: float | int = int(raw_value) if spec["type"] == "int" else float(raw_value)
    except ValueError:
        raise ValueError(f"'{raw_value}' is not a valid {spec['type']} for {key}")

    if not (spec["min"] <= value <= spec["max"]):
        raise ValueError(
            f"{key} = {value} is out of bounds [{spec['min']}, {spec['max']}]"
        )

    return value, spec["db_key"]
