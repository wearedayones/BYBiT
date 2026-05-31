-- Continuous learning schema additions.
-- Idempotent: all statements use IF NOT EXISTS / DO NOTHING patterns.

-- Rolling performance tracking on strategy_weights so adaptive_weight_decay
-- can write back 48h win rates without touching user-set weights directly.
ALTER TABLE strategy_weights
  ADD COLUMN IF NOT EXISTS rolling_win_48h  NUMERIC,
  ADD COLUMN IF NOT EXISTS last_adapted_at  TIMESTAMPTZ;

-- Paper positions table: tracks virtual fills so the promotion gate works
-- without any live execution. Cleared when a paper position closes.
CREATE TABLE IF NOT EXISTS paper_positions (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol      TEXT NOT NULL,
  side        TEXT NOT NULL,            -- 'Buy' | 'Sell'
  entry_price NUMERIC NOT NULL,
  stop_price  NUMERIC,
  tp_price    NUMERIC,
  qty         NUMERIC NOT NULL,
  strategy    TEXT NOT NULL,
  regime      TEXT,
  opened_at   TIMESTAMPTZ DEFAULT now(),
  leverage    INT  DEFAULT 1,
  UNIQUE (symbol)                       -- one paper position per symbol
);

-- Index for the learner's retrain query (trades × decision_log join).
CREATE INDEX IF NOT EXISTS idx_trades_decision_id ON trades (decision_id);
CREATE INDEX IF NOT EXISTS idx_trades_closed_at   ON trades (closed_at);
