-- Strategy registry: lifecycle + backtest-gated acceptance for every strategy.
--
-- A strategy must PASS a backtest (status='backtested', backtest.passed=true)
-- before it can be 'accepted' and traded. The decision engine reads this table
-- and runs only rows that are status='accepted' AND enabled=true.
--
-- `base_strategy` ties a registry row to a Python strategy class in
-- bybit_agent/strategy/impl/. `params` overlays that class's tunable knobs,
-- letting the strategy agent create multiple instances of one base with
-- different parameters (each independently backtested).

CREATE TABLE IF NOT EXISTS strategy_registry (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT UNIQUE NOT NULL,              -- instance name (what decision_log records)
  base_strategy   TEXT NOT NULL,                     -- class key in BASE_STRATEGIES
  params          JSONB NOT NULL DEFAULT '{}'::jsonb,
  status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','backtested','accepted','paused','rejected')),
  enabled         BOOLEAN NOT NULL DEFAULT false,    -- only true is eligible to trade
  weight          NUMERIC NOT NULL DEFAULT 1.0,
  backtest        JSONB,                             -- last BacktestResult.metrics()
  backtested_at   TIMESTAMPTZ,
  notes           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_strategy_registry_status ON strategy_registry (status, enabled);

-- Seed the four hand-ported base strategies as already-accepted (they are the
-- proven baseline carried over from the TS reference). New instances created
-- via `bybit strategy create` start as 'draft' and must pass a backtest.
INSERT INTO strategy_registry (name, base_strategy, params, status, enabled, weight, notes)
VALUES
  ('trend_momentum',  'trend_momentum',  '{}'::jsonb, 'accepted', true, 1.0, 'Baseline (ported from TS reference)'),
  ('mean_reversion',  'mean_reversion',  '{}'::jsonb, 'accepted', true, 1.0, 'Baseline (ported from TS reference)'),
  ('breakout',        'breakout',        '{}'::jsonb, 'accepted', true, 0.8, 'Baseline (ported from TS reference)'),
  ('funding_harvest', 'funding_harvest', '{}'::jsonb, 'accepted', true, 1.2, 'Baseline (ported from TS reference)')
ON CONFLICT (name) DO NOTHING;
