"""Automatic DB pruning — keeps the database lean for long 24/7 runs.

Retention policy (never touches financial records or model state):
  decision_log        7 days  — weekly report is the longest window needed
  equity_snapshots   30 days  — monthly report needs 30d; promotion gate needs 3d
  discovered_markets  3 days  — discovery history; not needed beyond one refresh cycle
  market_sentiment   30 days  — sentiment signal history
  correlation_snaps   7 days  — rolling correlation window
  execution_quality  30 days  — fill-quality analytics
  pnl_attribution    90 days  — quarterly P&L forensics
  pending_events     30 days  — audit trail for resolved/auto_resolved events
  brain_notes        90 days  — archive (not delete) old notes; DB stays queryable

NEVER pruned (financial records / model state):
  trades, orders, strategy_weights, learned_signals, backtest_runs,
  strategy_registry, grid_bots, bot_instances, copy_leaders, risk_events,
  agent_state, agent_config, param_tuning_history, ai_recommendations
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from bybit_agent.core.logger import get_logger

log = get_logger().bind(module="pruner")

# (table, timestamp_column, retention_days)
_RETENTION: list[tuple[str, str, int]] = [
    ("decision_log",          "ts",           7),
    ("equity_snapshots",      "ts",          30),
    ("discovered_markets",    "discovered_at", 3),
    ("market_sentiment",      "ts",          30),
    ("correlation_snapshots", "ts",           7),
    ("execution_quality",     "ts",          30),
    ("pnl_attribution",       "period_end",  90),
]
_PENDING_EVENT_RETENTION_DAYS = 30
_BRAIN_NOTES_ARCHIVE_DAYS     = 90


@dataclass
class PruneResult:
    ran_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    deleted: dict[str, int] = field(default_factory=dict)
    archived_notes: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())


async def prune(db) -> PruneResult:
    """Delete or archive old rows. Safe to run at any time; no locks held."""
    result = PruneResult()

    # ── time-windowed tables ──────────────────────────────────────────────────
    for table, ts_col, days in _RETENTION:
        try:
            rows = await db.fetch(
                f"SELECT count(*) c FROM {table} "
                f"WHERE {ts_col} < now() - INTERVAL '{days} days'"
            )
            count = int((rows[0].get("c") or 0) if rows else 0)
            if count > 0:
                await db.execute(
                    f"DELETE FROM {table} WHERE {ts_col} < now() - INTERVAL '{days} days'"
                )
                result.deleted[table] = count
                log.info("Pruned table", table=table, rows=count, retention_days=days)
        except Exception as e:  # noqa: BLE001
            result.errors.append(f"{table}: {e}")
            log.warning("Prune failed", table=table, error=str(e))

    # ── pending_events: only resolved/auto_resolved older than N days ─────────
    try:
        rows = await db.fetch(
            "SELECT count(*) c FROM pending_events "
            f"WHERE status IN ('resolved','auto_resolved') "
            f"AND resolved_at < now() - INTERVAL '{_PENDING_EVENT_RETENTION_DAYS} days'"
        )
        count = int((rows[0].get("c") or 0) if rows else 0)
        if count > 0:
            await db.execute(
                f"DELETE FROM pending_events "
                f"WHERE status IN ('resolved','auto_resolved') "
                f"AND resolved_at < now() - INTERVAL '{_PENDING_EVENT_RETENTION_DAYS} days'"
            )
            result.deleted["pending_events"] = count
            log.info("Pruned resolved events", rows=count)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"pending_events: {e}")

    # ── brain_notes: archive (not delete) old notes ───────────────────────────
    try:
        rows = await db.fetch(
            "SELECT count(*) c FROM brain_notes "
            f"WHERE NOT archived "
            f"AND created_at < now() - INTERVAL '{_BRAIN_NOTES_ARCHIVE_DAYS} days'"
        )
        count = int((rows[0].get("c") or 0) if rows else 0)
        if count > 0:
            await db.execute(
                "UPDATE brain_notes SET archived = true "
                f"WHERE NOT archived "
                f"AND created_at < now() - INTERVAL '{_BRAIN_NOTES_ARCHIVE_DAYS} days'"
            )
            result.archived_notes = count
            log.info("Archived old brain notes", rows=count)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"brain_notes archive: {e}")

    log.info("Prune complete",
             total_deleted=result.total_deleted,
             archived_notes=result.archived_notes,
             tables=list(result.deleted.keys()),
             errors=len(result.errors))
    return result
