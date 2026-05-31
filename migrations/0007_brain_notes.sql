-- Brain memory ledger + learning integrity fixes.
-- Idempotent: IF NOT EXISTS everywhere; safe to re-run.

-- The learner upserts with ON CONFLICT (signal_hash, strategy); without this
-- unique index that statement errors. learned_signals previously only had a
-- PRIMARY KEY on id.
CREATE UNIQUE INDEX IF NOT EXISTS ux_learned_signals_hash_strategy
  ON learned_signals (signal_hash, strategy);

-- brain_notes is the DB-backed memory ledger. brain.md is RENDERED from this
-- table (never the other way around) so the deterministic loop keeps Postgres
-- as the single source of truth. Survives container resets; never lost.
CREATE TABLE IF NOT EXISTS brain_notes (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  category    TEXT NOT NULL DEFAULT 'lesson',  -- lesson | decision | directive | observation
  note        TEXT NOT NULL,
  tags        TEXT[],
  cycle_id    UUID,
  archived    BOOLEAN NOT NULL DEFAULT false   -- old notes archived (not deleted) when rendering large
);

CREATE INDEX IF NOT EXISTS idx_brain_notes_created ON brain_notes (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_brain_notes_category ON brain_notes (category);
