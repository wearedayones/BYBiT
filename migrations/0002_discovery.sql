-- Market discovery: a log of which symbols the discovery agent selected,
-- with the metrics that drove the choice and the equity at decision time.
CREATE TABLE IF NOT EXISTS discovered_markets (
  id                  BIGSERIAL PRIMARY KEY,
  symbol              TEXT NOT NULL,
  turnover_24h        DOUBLE PRECISION,
  volatility          DOUBLE PRECISION,
  price               DOUBLE PRECISION,
  min_notional        DOUBLE PRECISION,
  score               DOUBLE PRECISION,
  equity_at_discovery DOUBLE PRECISION,
  discovered_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_discovered_markets_symbol ON discovered_markets (symbol, discovered_at DESC);
