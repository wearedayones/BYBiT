-- Durable event queue: the four AI wake-trigger kinds.
-- The 24/7 loop NEVER blocks on the AI — every event carries a default_action
-- and expires_at. If the queue is not drained before expiry the loop applies
-- the safe default and marks status = 'auto_resolved'.
--
-- kind ∈ {scheduled_review, risk_escalation, market_event, ambiguous_decision}
-- severity ∈ {info, warning, critical}
-- status ∈ {pending, claimed, resolved, auto_resolved}

CREATE TABLE IF NOT EXISTS pending_events (
  id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind           TEXT        NOT NULL,
  severity       TEXT        NOT NULL DEFAULT 'info',
  status         TEXT        NOT NULL DEFAULT 'pending',
  symbol         TEXT,
  cycle_id       UUID,
  title          TEXT        NOT NULL,
  summary        TEXT        NOT NULL,
  context        JSONB       NOT NULL DEFAULT '{}',
  options        JSONB       NOT NULL DEFAULT '[]',
  default_action TEXT        NOT NULL,
  expires_at     TIMESTAMPTZ NOT NULL,
  dedupe_key     TEXT,
  claimed_by     TEXT,
  resolution     JSONB,
  resolved_at    TIMESTAMPTZ
);

-- Prevent event flooding: one pending event per dedupe_key at a time.
CREATE UNIQUE INDEX IF NOT EXISTS pending_events_dedupe
  ON pending_events (dedupe_key)
  WHERE status = 'pending' AND dedupe_key IS NOT NULL;

-- Fast polling for the loop.
CREATE INDEX IF NOT EXISTS pending_events_status_ts
  ON pending_events (status, ts DESC);
