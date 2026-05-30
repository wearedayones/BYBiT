"""Numeric defaults — ports src/config/constants.ts verbatim (no ANTHROPIC).

These are the *defaults*; the live trading loop overlays any overrides from the
``agent_config`` table (written by ``bybit tune``), read each cycle.
"""

from __future__ import annotations

import os

SKILL_VERSION = "1.4.1"
RECV_WINDOW = 5000
USER_AGENT = f"bybit-skill/{SKILL_VERSION}"
X_REFERER = "bybit-skill"

_proxy = os.environ.get("BYBIT_PROXY_URL", "")
MAINNET_REST = f"{_proxy}/mainnet" if _proxy else "https://api.bybit.com"
TESTNET_REST = f"{_proxy}/testnet" if _proxy else "https://api-testnet.bybit.com"
MAINNET_WS_PUBLIC = "wss://stream.bybit.com/v5/public"
MAINNET_WS_PRIVATE = "wss://stream.bybit.com/v5/private"
TESTNET_WS_PRIVATE = "wss://stream-testnet.bybit.com/v5/private"

RATE_LIMIT = {
    "GET_MIN_MS": 100,
    "POST_MIN_MS": 300,
    "RATE_LIMIT_BACKOFF_MIN_MS": 500,
    "RATE_LIMIT_BACKOFF_MAX_MS": 1500,
    "RATE_LIMIT_MAX_RETRIES": 3,
    "CONSECUTIVE_LIMIT_PAUSE_MS": 10_000,
    "BOT_CREATE_QPS": 3,
}

RISK_DEFAULTS = {
    "MAX_RISK_PCT": 0.015,       # 1.5% per trade
    "MAX_DAILY_LOSS_PCT": 0.08,  # 8% daily hard loss limit
    "KILL_LEVEL_PCT": 0.20,      # 20% drawdown from peak = kill
    "CIRCUIT_BREAKER_PCT": 0.10, # 10% drawdown = de-risk mode
    "MAX_CONCURRENT_POSITIONS": 8,
    "MAX_CORRELATED_EXPOSURE": 0.30,
}

# Bybit trading fees (VIP 0, fraction of notional).
FEES = {
    "PERP_TAKER": 0.00055,
    "PERP_MAKER": 0.00020,
    "SPOT_TAKER": 0.001,
    "SPOT_MAKER": 0.001,
}

COST_DEFAULTS = {
    "SLIPPAGE_PCT": 0.0005,
    "MIN_NET_EDGE_PCT": 0.0005,
    "MIN_REWARD_RISK": 1.2,
}

EXECUTION_DEFAULTS = {
    "MAKER_WAIT_MS": 5_000,
    "MAKER_OFFSET_PCT": 0.0001,
}

POSITION_MGMT = {
    "BREAKEVEN_AT_R": 1.0,
    "BREAKEVEN_BUFFER_PCT": 0.0008,
    "TRAIL_START_R": 2.0,
    "TRAIL_ATR_MULT": 2.0,
    "PARTIAL_TP_AT_R": 1.0,
    "PARTIAL_TP_FRACTION": 0.5,
    "MAX_HOLD_CYCLES": 30,
    "STALE_PNL_R": 0.25,
}

PROMOTION_DEFAULTS = {
    "MIN_CYCLES": 72,
    "MIN_SHARPE": 0.5,
    "MIN_WIN_RATE": 0.50,
    "MAX_DRAWDOWN_PCT": 0.05,
}

CYCLE_INTERVAL_MS = 60_000
INSTRUMENT_CACHE_TTL_MS = 2 * 60 * 60 * 1000
LEADER_REVIEW_INTERVAL_MS = 4 * 60 * 60 * 1000
DEEP_REVIEW_INTERVAL_MS = 24 * 60 * 60 * 1000
REPO_POLL_INTERVAL_MS = 15 * 60 * 1000
