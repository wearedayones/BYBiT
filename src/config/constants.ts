export const SKILL_VERSION = '1.4.1';
export const RECV_WINDOW = 5000;
export const USER_AGENT = `bybit-skill/${SKILL_VERSION}`;
export const X_REFERER = 'bybit-skill';

export const MAINNET_REST = 'https://api.bybit.com';
export const TESTNET_REST = 'https://api-testnet.bybit.com';
export const MAINNET_WS_PUBLIC = 'wss://stream.bybit.com/v5/public';
export const MAINNET_WS_PRIVATE = 'wss://stream.bybit.com/v5/private';
export const TESTNET_WS_PRIVATE = 'wss://stream-testnet.bybit.com/v5/private';

export const SKILL_MANIFEST_URL = 'https://api.bybit.com/skill/manifest';
export const SKILL_RAW_BASE = 'https://raw.githubusercontent.com/bybit-exchange/skills/main';

export const RATE_LIMIT = {
  GET_MIN_MS: 100,
  POST_MIN_MS: 300,
  RATE_LIMIT_BACKOFF_MIN_MS: 500,
  RATE_LIMIT_BACKOFF_MAX_MS: 1500,
  RATE_LIMIT_MAX_RETRIES: 3,
  CONSECUTIVE_LIMIT_PAUSE_MS: 10_000,
  BOT_CREATE_QPS: 3,
} as const;

export const RISK_DEFAULTS = {
  MAX_RISK_PCT: 0.015,       // 1.5% per trade (balanced profile)
  MAX_DAILY_LOSS_PCT: 0.08,  // 8% daily hard loss limit
  KILL_LEVEL_PCT: 0.20,      // 20% drawdown from peak = kill
  CIRCUIT_BREAKER_PCT: 0.10, // 10% drawdown = de-risk mode
  MAX_CONCURRENT_POSITIONS: 8,
  MAX_CORRELATED_EXPOSURE: 0.30, // 30% of equity in correlated cluster
} as const;

// Bybit trading fees (VIP 0, fraction of notional). Source: bybit.com fee schedule.
// Perp: 0.055% taker / 0.020% maker. Spot: 0.10% both sides.
export const FEES = {
  PERP_TAKER: 0.00055,
  PERP_MAKER: 0.00020,
  SPOT_TAKER: 0.001,
  SPOT_MAKER: 0.001,
} as const;

// Cost / expectancy gating — a trade must clear its own friction with margin to spare.
export const COST_DEFAULTS = {
  SLIPPAGE_PCT: 0.0005,      // 0.05% per fill — matches the sim's slippage model
  MIN_NET_EDGE_PCT: 0.0015,  // require ≥0.15% expected net move after all costs
  MIN_REWARD_RISK: 1.2,      // reject trades whose TP can't clear costs vs the stop
} as const;

// Maker-first execution — try PostOnly limit, fall back to market if unfilled.
export const EXECUTION_DEFAULTS = {
  MAKER_WAIT_MS: 5_000,      // how long to wait for a PostOnly limit to fill (live)
  MAKER_OFFSET_PCT: 0.0001,  // place limit 1 bps inside the touch to stay maker
} as const;

// Active position management knobs (PositionHealthManager).
export const POSITION_MGMT = {
  BREAKEVEN_AT_R: 1.0,       // move SL to entry once price is +1R in favor
  BREAKEVEN_BUFFER_PCT: 0.0008, // nudge breakeven SL past entry to cover round-trip cost
  TRAIL_START_R: 2.0,        // begin ATR trailing once +2R in favor
  TRAIL_ATR_MULT: 2.0,       // trail this many ATRs behind the high-water mark
  PARTIAL_TP_AT_R: 1.0,      // take partial profit at +1R
  PARTIAL_TP_FRACTION: 0.5,  // close this fraction of the position at the partial TP
  MAX_HOLD_CYCLES: 30,       // close a stale, ~flat position after this many cycles
  STALE_PNL_R: 0.25,         // "≈ flat" threshold in R-multiples for the time exit
} as const;

export const PROMOTION_DEFAULTS = {
  MIN_CYCLES: 72,
  MIN_SHARPE: 0.5,
  MIN_WIN_RATE: 0.50,
  MAX_DRAWDOWN_PCT: 0.05,
} as const;

export const CYCLE_INTERVAL_MS = 60_000; // 1 minute default
export const INSTRUMENT_CACHE_TTL_MS = 2 * 60 * 60 * 1000; // 2 hours per skill rule
export const LEADER_REVIEW_INTERVAL_MS = 4 * 60 * 60 * 1000; // 4 hours
export const DEEP_REVIEW_INTERVAL_MS = 24 * 60 * 60 * 1000;  // 24 hours
export const REPO_POLL_INTERVAL_MS = 15 * 60 * 1000;          // 15 minutes
