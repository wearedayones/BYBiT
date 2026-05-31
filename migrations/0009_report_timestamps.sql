-- Persist last-sent report timestamps so restarts don't retrigger all reports immediately.
ALTER TABLE agent_state
  ADD COLUMN IF NOT EXISTS last_daily_report_at   TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_weekly_report_at  TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_monthly_report_at TIMESTAMPTZ;
