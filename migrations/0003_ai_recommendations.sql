-- Tuning / decision audit trail.
-- Written by the `bybit tune` / `bybit decide` CLI commands and by event resolutions.
-- The installing AI agent (Claude Code, Codex, OpenClaw, …) is the reasoner; there is
-- NO external LLM API. `model` records who/what produced the change (e.g. 'cli-agent').
CREATE TABLE IF NOT EXISTS ai_recommendations (
  id              BIGSERIAL PRIMARY KEY,
  run_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  model           TEXT NOT NULL DEFAULT 'cli-agent',
  analysis        TEXT NOT NULL,
  observations    TEXT,
  proposed_changes JSONB,
  applied_changes  JSONB,
  commit_sha      TEXT,
  tokens_used     INT
);

-- Key/value store for agent-proposed numeric overrides that take effect without a restart.
-- Values are always numeric. The trading loop reads this table each cycle; no code deploy needed.
-- Populated by `bybit tune --set KEY=VALUE` (bounds-checked against the PARAM_WHITELIST).
CREATE TABLE IF NOT EXISTS agent_config (
  key         TEXT PRIMARY KEY,
  value       DOUBLE PRECISION NOT NULL,
  description TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by  TEXT NOT NULL DEFAULT 'human'
);
