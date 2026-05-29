-- BYBiT Agent — full schema
-- Idempotent: safe to run multiple times

CREATE TABLE IF NOT EXISTS agent_state (
  id                    TEXT PRIMARY KEY DEFAULT 'singleton',
  status                TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','paused','killed')),
  env                   TEXT NOT NULL DEFAULT 'testnet' CHECK (env IN ('testnet','mainnet')),
  kill_engaged          BOOLEAN NOT NULL DEFAULT false,
  kill_reason           TEXT,
  equity                NUMERIC,
  peak_equity           NUMERIC,
  day_start_equity      NUMERIC,
  daily_realized_pnl    NUMERIC DEFAULT 0,
  daily_loss_limit      NUMERIC DEFAULT 0.08,
  kill_level_pct        NUMERIC DEFAULT 0.20,
  circuit_breaker_pct   NUMERIC DEFAULT 0.10,
  max_risk_pct          NUMERIC DEFAULT 0.015,
  trading_day           DATE,
  skill_version         TEXT,
  promotion_criteria    JSONB DEFAULT '{"min_cycles":72,"min_sharpe":0.5,"min_win_rate":0.5,"max_drawdown_pct":0.05}'::jsonb,
  promotion_cycle_count INT DEFAULT 0,
  last_promoted_at      TIMESTAMPTZ,
  last_cycle_at         TIMESTAMPTZ,
  updated_at            TIMESTAMPTZ DEFAULT now()
);

INSERT INTO agent_state (id) VALUES ('singleton') ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS strategy_weights (
  strategy          TEXT PRIMARY KEY,
  weight            NUMERIC NOT NULL DEFAULT 1.0,
  enabled           BOOLEAN NOT NULL DEFAULT true,
  realized_pnl      NUMERIC DEFAULT 0,
  trades_count      INT DEFAULT 0,
  win_rate          NUMERIC,
  sharpe_like       NUMERIC,
  cooldown_until    TIMESTAMPTZ,
  last_adjusted_at  TIMESTAMPTZ DEFAULT now(),
  meta              JSONB
);

INSERT INTO strategy_weights (strategy, weight) VALUES
  ('trend_momentum', 1.0),
  ('mean_reversion', 1.0),
  ('breakout', 0.8),
  ('funding_harvest', 1.2),
  ('grid', 0.9),
  ('dca', 0.9)
ON CONFLICT (strategy) DO NOTHING;

CREATE TABLE IF NOT EXISTS decision_log (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  cycle_id        UUID NOT NULL,
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  symbol          TEXT,
  category        TEXT,
  action          TEXT NOT NULL CHECK (action IN ('enter_long','enter_short','exit','hold','switch','kill')),
  strategy        TEXT,
  confidence      NUMERIC,
  regime          TEXT,
  rationale       TEXT,
  inputs          JSONB,
  composite_score NUMERIC,
  approved        BOOLEAN,
  reject_reason   TEXT,
  outcome         TEXT CHECK (outcome IN ('executed','skipped','failed','paper')),
  outcome_detail  JSONB,
  is_paper        BOOLEAN DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_decision_log_ts ON decision_log (ts);
CREATE INDEX IF NOT EXISTS idx_decision_log_cycle ON decision_log (cycle_id);
CREATE INDEX IF NOT EXISTS idx_decision_log_strategy ON decision_log (strategy);

CREATE TABLE IF NOT EXISTS bot_instances (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  bot_type         TEXT NOT NULL CHECK (bot_type IN ('spot_grid','futures_grid','martingale','dca','combo')),
  exchange_bot_id  TEXT,
  symbol           TEXT NOT NULL,
  category         TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','stopped','paper')),
  config           JSONB NOT NULL,
  state            JSONB,
  is_paper         BOOLEAN DEFAULT false,
  created_at       TIMESTAMPTZ DEFAULT now(),
  updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bot_performance (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  bot_instance_id  UUID NOT NULL REFERENCES bot_instances(id),
  ts               TIMESTAMPTZ DEFAULT now(),
  realized_pnl     NUMERIC,
  unrealized_pnl   NUMERIC,
  filled_orders    INT,
  roi              NUMERIC,
  meta             JSONB
);
CREATE INDEX IF NOT EXISTS idx_bot_perf ON bot_performance (bot_instance_id, ts);

CREATE TABLE IF NOT EXISTS copy_leaders (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  leader_mark   TEXT NOT NULL UNIQUE,
  nickname      TEXT,
  status        TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate','following','dropped')),
  score         NUMERIC,
  investment_e8 TEXT,
  is_paper      BOOLEAN DEFAULT false,
  followed_at   TIMESTAMPTZ,
  dropped_at    TIMESTAMPTZ,
  drop_reason   TEXT,
  meta          JSONB
);

CREATE TABLE IF NOT EXISTS copy_leader_performance (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  copy_leader_id  UUID NOT NULL REFERENCES copy_leaders(id),
  ts              TIMESTAMPTZ DEFAULT now(),
  roi             NUMERIC,
  win_rate        NUMERIC,
  max_drawdown    NUMERIC,
  sharpe          NUMERIC,
  our_realized_pnl NUMERIC,
  score           NUMERIC,
  meta            JSONB
);
CREATE INDEX IF NOT EXISTS idx_copy_leader_perf ON copy_leader_performance (copy_leader_id, ts);

CREATE TABLE IF NOT EXISTS orders (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  decision_id        UUID REFERENCES decision_log(id),
  exchange_order_id  TEXT,
  order_link_id      TEXT UNIQUE,
  symbol             TEXT NOT NULL,
  category           TEXT NOT NULL,
  side               TEXT NOT NULL CHECK (side IN ('Buy','Sell')),
  order_type         TEXT NOT NULL CHECK (order_type IN ('Market','Limit')),
  qty                NUMERIC NOT NULL,
  price              NUMERIC,
  reduce_only        BOOLEAN DEFAULT false,
  status             TEXT NOT NULL DEFAULT 'New',
  bot_instance_id    UUID REFERENCES bot_instances(id),
  copy_leader_id     UUID REFERENCES copy_leaders(id),
  is_paper           BOOLEAN DEFAULT false,
  created_at         TIMESTAMPTZ DEFAULT now(),
  updated_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders (symbol, status);
CREATE INDEX IF NOT EXISTS idx_orders_link ON orders (order_link_id);

CREATE TABLE IF NOT EXISTS trades (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id         UUID REFERENCES orders(id),
  decision_id      UUID REFERENCES decision_log(id),
  strategy         TEXT,
  symbol           TEXT NOT NULL,
  category         TEXT NOT NULL,
  side             TEXT NOT NULL,
  entry_price      NUMERIC,
  exit_price       NUMERIC,
  qty              NUMERIC NOT NULL,
  fee              NUMERIC DEFAULT 0,
  realized_pnl     NUMERIC,
  opened_at        TIMESTAMPTZ,
  closed_at        TIMESTAMPTZ,
  bot_instance_id  UUID REFERENCES bot_instances(id),
  copy_leader_id   UUID REFERENCES copy_leaders(id),
  is_paper         BOOLEAN DEFAULT false,
  meta             JSONB
);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades (strategy, closed_at);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol);
CREATE INDEX IF NOT EXISTS idx_trades_paper ON trades (is_paper, closed_at);

CREATE TABLE IF NOT EXISTS positions (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol           TEXT NOT NULL,
  category         TEXT NOT NULL,
  side             TEXT NOT NULL,
  size             NUMERIC NOT NULL,
  avg_entry        NUMERIC,
  leverage         NUMERIC,
  liq_price        NUMERIC,
  stop_loss        NUMERIC,
  take_profit      NUMERIC,
  unrealized_pnl   NUMERIC,
  strategy         TEXT,
  bot_instance_id  UUID REFERENCES bot_instances(id),
  is_paper         BOOLEAN DEFAULT false,
  is_open          BOOLEAN DEFAULT true,
  opened_at        TIMESTAMPTZ DEFAULT now(),
  updated_at       TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_open ON positions (symbol, category, side, is_paper) WHERE is_open = true;

CREATE TABLE IF NOT EXISTS risk_events (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ts           TIMESTAMPTZ DEFAULT now(),
  type         TEXT NOT NULL,
  severity     TEXT NOT NULL CHECK (severity IN ('info','warn','critical')),
  cycle_id     UUID,
  symbol       TEXT,
  detail       JSONB,
  action_taken TEXT
);
CREATE INDEX IF NOT EXISTS idx_risk_events_ts ON risk_events (ts);
CREATE INDEX IF NOT EXISTS idx_risk_events_type ON risk_events (type, ts);

CREATE TABLE IF NOT EXISTS equity_snapshots (
  id               BIGSERIAL PRIMARY KEY,
  ts               TIMESTAMPTZ DEFAULT now(),
  total_equity     NUMERIC NOT NULL,
  available        NUMERIC,
  unrealized_pnl   NUMERIC,
  realized_pnl_day NUMERIC,
  open_positions   INT,
  drawdown_pct     NUMERIC,
  env              TEXT
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots (ts);

CREATE TABLE IF NOT EXISTS backtest_runs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy        TEXT NOT NULL,
  run_at          TIMESTAMPTZ DEFAULT now(),
  candles_n       INT,
  timeframe       TEXT,
  total_return    NUMERIC,
  sharpe          NUMERIC,
  max_drawdown    NUMERIC,
  win_rate        NUMERIC,
  profit_factor   NUMERIC,
  params          JSONB,
  passed          BOOLEAN
);
CREATE INDEX IF NOT EXISTS idx_backtest ON backtest_runs (strategy, run_at);

CREATE TABLE IF NOT EXISTS learned_signals (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_hash          TEXT NOT NULL,
  strategy             TEXT NOT NULL,
  regime               TEXT,
  symbol_type          TEXT,
  n_trades             INT DEFAULT 0,
  win_rate             NUMERIC,
  avg_rr               NUMERIC,
  avg_pnl              NUMERIC,
  confidence_interval  NUMERIC,
  last_seen            TIMESTAMPTZ,
  last_updated         TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_learned_signals ON learned_signals (signal_hash, strategy);

CREATE TABLE IF NOT EXISTS param_tuning_history (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy              TEXT NOT NULL,
  ts                    TIMESTAMPTZ DEFAULT now(),
  old_params            JSONB,
  new_params            JSONB,
  backtest_sharpe_old   NUMERIC,
  backtest_sharpe_new   NUMERIC,
  adopted               BOOLEAN DEFAULT false
);

CREATE TABLE IF NOT EXISTS execution_quality (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id       UUID REFERENCES orders(id),
  ts             TIMESTAMPTZ DEFAULT now(),
  symbol         TEXT,
  theoretical_price NUMERIC,
  actual_price   NUMERIC,
  slippage_bps   NUMERIC,
  fee_paid       NUMERIC,
  fee_saved      NUMERIC DEFAULT 0,
  order_route    TEXT,
  meta           JSONB
);

CREATE TABLE IF NOT EXISTS correlation_snapshots (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ts         TIMESTAMPTZ DEFAULT now(),
  matrix     JSONB NOT NULL,
  symbols    TEXT[] NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corr_ts ON correlation_snapshots (ts);

CREATE TABLE IF NOT EXISTS pnl_attribution (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  period_start       TIMESTAMPTZ NOT NULL,
  period_end         TIMESTAMPTZ NOT NULL,
  period_type        TEXT CHECK (period_type IN ('daily','weekly','monthly')),
  strategy           TEXT,
  bot_type           TEXT,
  regime             TEXT,
  realized_pnl       NUMERIC DEFAULT 0,
  trade_count        INT DEFAULT 0,
  win_count          INT DEFAULT 0,
  total_fees         NUMERIC DEFAULT 0,
  maker_savings      NUMERIC DEFAULT 0,
  avg_slippage_bps   NUMERIC
);

CREATE TABLE IF NOT EXISTS repo_updates (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ts              TIMESTAMPTZ DEFAULT now(),
  branch          TEXT,
  old_sha         TEXT,
  new_sha         TEXT,
  commit_messages TEXT[],
  files_changed   TEXT[],
  build_ok        BOOLEAN,
  restart_ok      BOOLEAN
);

CREATE TABLE IF NOT EXISTS skill_update_events (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ts            TIMESTAMPTZ DEFAULT now(),
  old_version   TEXT,
  new_version   TEXT,
  endpoint_diffs JSONB,
  breaking      BOOLEAN DEFAULT false,
  description   TEXT,
  email_sent    BOOLEAN DEFAULT false
);

CREATE TABLE IF NOT EXISTS report_history (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ts           TIMESTAMPTZ DEFAULT now(),
  period       TEXT CHECK (period IN ('daily','weekly','monthly','promotion','alert')),
  subject      TEXT,
  html_body    TEXT,
  delivered_to TEXT,
  delivered_ok BOOLEAN DEFAULT false
);
