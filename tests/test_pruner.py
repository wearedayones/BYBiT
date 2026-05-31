"""DB pruner — unit tests confirming retention policy and never-prune guarantees."""
from __future__ import annotations

import pytest

from bybit_agent.maintenance.pruner import prune, _RETENTION, PruneResult


class FakeDb:
    """Stub that simulates stale rows in prunable tables; tracks DELETE calls."""

    def __init__(self, stale_counts: dict[str, int] | None = None) -> None:
        self._stale = stale_counts or {}
        self.deleted: list[str] = []
        self.updated: list[str] = []

    async def execute(self, sql: str, *args) -> None:
        s = sql.upper().strip()
        if "DELETE FROM" in s:
            for table, _, _ in _RETENTION:
                if table.upper() in s:
                    self.deleted.append(table)
                    break
            if "PENDING_EVENTS" in s:
                self.deleted.append("pending_events")
        elif "UPDATE BRAIN_NOTES" in s:
            self.updated.append("brain_notes")

    async def fetch(self, sql: str, *args) -> list[dict]:
        s = sql.upper()
        for table, _, _ in _RETENTION:
            if f"FROM {table.upper()}" in s and "COUNT" in s:
                return [{"c": self._stale.get(table, 0)}]
        if "FROM PENDING_EVENTS" in s and "COUNT" in s:
            return [{"c": self._stale.get("pending_events", 0)}]
        if "FROM BRAIN_NOTES" in s and "COUNT" in s:
            return [{"c": self._stale.get("brain_notes_old", 0)}]
        return [{"c": 0}]


@pytest.mark.asyncio
async def test_prune_empty_db_is_noop():
    db = FakeDb()
    result = await prune(db)
    assert result.total_deleted == 0
    assert result.archived_notes == 0
    assert not db.deleted
    assert not result.errors


@pytest.mark.asyncio
async def test_prune_deletes_stale_decision_log():
    db = FakeDb({"decision_log": 5000})
    result = await prune(db)
    assert "decision_log" in db.deleted
    assert result.deleted.get("decision_log") == 5000
    assert result.total_deleted == 5000


@pytest.mark.asyncio
async def test_prune_archives_old_brain_notes_not_deletes():
    db = FakeDb({"brain_notes_old": 12})
    result = await prune(db)
    assert result.archived_notes == 12
    assert "brain_notes" in db.updated
    assert "brain_notes" not in db.deleted   # archive only — never hard-delete


@pytest.mark.asyncio
async def test_prune_result_dataclass():
    result = PruneResult()
    result.deleted["decision_log"] = 100
    result.deleted["equity_snapshots"] = 50
    assert result.total_deleted == 150


@pytest.mark.asyncio
async def test_retention_table_covers_known_tables():
    tables = {t for t, _, _ in _RETENTION}
    assert "decision_log" in tables
    assert "equity_snapshots" in tables
    assert "discovered_markets" in tables
    # Financial records must NOT be in the retention list.
    for protected in ("trades", "orders", "learned_signals", "strategy_weights"):
        assert protected not in tables, f"{protected} should never be pruned"
