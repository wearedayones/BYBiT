"""Phase 4: event queue CRUD, TTL auto-expire, and dedup logic (all in-memory)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from bybit_agent.events.queue import (
    apply_expired_defaults,
    enqueue,
    get_event,
    list_events,
    resolve_event,
)


class FakeDb:
    """In-memory stub that faithfully reproduces the NeonHttpClient execute/fetch surface."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    # ── NeonHttpClient surface ────────────────────────────────────────────────

    async def execute(self, sql: str, *args) -> None:
        sql_upper = sql.upper().strip()

        if "UPDATE PENDING_EVENTS" in sql_upper and "STATUS = $1" in sql_upper:
            # resolve_event — args: status, resolution_json, event_id
            new_status, resolution_s, ev_id = args
            row = self._rows.get(ev_id)
            if row and row.get("status") == "pending":
                row["status"] = new_status
                row["resolution"] = json.loads(resolution_s)
                row["resolved_at"] = datetime.now(timezone.utc)

        elif "UPDATE PENDING_EVENTS" in sql_upper and "AUTO_RESOLVED" in sql_upper:
            # apply_expired_defaults — args: resolution_json, event_id
            resolution_s, ev_id = args
            row = self._rows.get(ev_id)
            if row:
                row["status"] = "auto_resolved"
                row["resolution"] = json.loads(resolution_s)
                row["resolved_at"] = datetime.now(timezone.utc)

    async def fetch(self, sql: str, *args) -> list[dict]:
        sql_upper = sql.upper().strip()

        if "INSERT INTO PENDING_EVENTS" in sql_upper and "RETURNING ID" in sql_upper:
            # enqueue — args: id, kind, severity, symbol, cycle_id, title, summary,
            #                  context, options, default_action, expires_at, dedupe_key
            (ev_id, kind, severity, symbol, cycle_id, title, summary,
             context_s, options_s, default_action, expires_at_s, dedupe_key) = args

            # Simulate ON CONFLICT DO NOTHING for the partial unique index.
            if dedupe_key:
                for row in self._rows.values():
                    if row.get("dedupe_key") == dedupe_key and row.get("status") == "pending":
                        return []  # conflict — RETURNING returns no rows

            expires_at = datetime.fromisoformat(expires_at_s.replace("Z", "+00:00"))
            self._rows[ev_id] = {
                "id": ev_id, "kind": kind, "severity": severity,
                "symbol": symbol, "cycle_id": cycle_id,
                "title": title, "summary": summary,
                "context": json.loads(context_s),
                "options": json.loads(options_s),
                "default_action": default_action,
                "expires_at": expires_at,
                "dedupe_key": dedupe_key,
                "status": "pending",
                "resolution": None,
                "resolved_at": None,
                "ts": datetime.now(timezone.utc),
            }
            return [{"id": ev_id}]

        if "EXPIRES_AT < NOW()" in sql_upper:
            now = datetime.now(timezone.utc)
            return [
                {"id": r["id"], "default_action": r["default_action"]}
                for r in self._rows.values()
                if r.get("status") == "pending"
                and r.get("expires_at", now) < now
            ]

        # get_event — single row by UUID (check before the broader WHERE handler)
        if "WHERE ID = $1::UUID" in sql_upper and args:
            ev_id = args[0]
            row = self._rows.get(ev_id)
            return [row] if row else []

        # list_events — check SQL keywords to know arg positions
        if "FROM PENDING_EVENTS WHERE" in sql_upper:
            status_filter = args[0] if args else "pending"
            results = [r for r in self._rows.values() if r.get("status") == status_filter]
            remaining = list(args[1:])
            if "AND KIND" in sql_upper and remaining:
                results = [r for r in results if r.get("kind") == remaining.pop(0)]
            if "AND SEVERITY" in sql_upper and remaining:
                results = [r for r in results if r.get("severity") == remaining.pop(0)]
            limit = int(remaining[-1]) if remaining else 20
            return results[:limit]

        return []

    def json(self, obj) -> str:  # NeonHttpClient.json compat
        return json.dumps(obj)


# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enqueue_and_get():
    db = FakeDb()
    ev_id = await enqueue(
        db,
        kind="scheduled_review",
        title="Daily review",
        summary="Review the strategy.",
        default_action="acknowledge",
    )
    assert ev_id is not None
    row = await get_event(db, ev_id)
    assert row is not None
    assert row["kind"] == "scheduled_review"
    assert row["status"] == "pending"
    assert row["default_action"] == "acknowledge"


@pytest.mark.asyncio
async def test_dedup_blocks_second_enqueue_with_same_key():
    db = FakeDb()
    id1 = await enqueue(
        db, kind="market_event", title="Spike", summary="s",
        default_action="hold", dedupe_key="mkt:BTC:spike",
    )
    id2 = await enqueue(
        db, kind="market_event", title="Spike again", summary="s",
        default_action="hold", dedupe_key="mkt:BTC:spike",
    )
    # Second enqueue returns None (ON CONFLICT DO NOTHING) and no duplicate stored.
    assert id1 is not None
    assert id2 is None
    rows = await list_events(db)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_resolve_event_marks_resolved():
    db = FakeDb()
    ev_id = await enqueue(
        db, kind="ambiguous_decision", title="Signal", summary="s",
        default_action="reject",
    )
    await resolve_event(db, ev_id, action="approve")

    row = await get_event(db, ev_id)
    assert row["status"] == "resolved"
    assert row["resolution"]["action"] == "approve"


@pytest.mark.asyncio
async def test_list_events_returns_pending_only():
    db = FakeDb()
    id1 = await enqueue(db, kind="market_event", title="E1", summary="s", default_action="hold")
    id2 = await enqueue(db, kind="market_event", title="E2", summary="s", default_action="hold",
                        dedupe_key=None)
    await resolve_event(db, id2, action="hold")

    rows = await list_events(db)
    ids = [r["id"] for r in rows]
    assert id1 in ids
    assert id2 not in ids


@pytest.mark.asyncio
async def test_apply_expired_defaults_auto_resolves():
    db = FakeDb()
    ev_id = await enqueue(
        db, kind="risk_escalation", title="DD", summary="s",
        default_action="acknowledge",
        ttl=timedelta(seconds=-1),  # already expired
    )
    count = await apply_expired_defaults(db)
    assert count == 1

    row = await get_event(db, ev_id)
    assert row["status"] == "auto_resolved"
    assert row["resolution"]["action"] == "acknowledge"
    assert row["resolution"]["by"] == "auto"


@pytest.mark.asyncio
async def test_get_event_returns_none_for_unknown():
    db = FakeDb()
    result = await get_event(db, "00000000-0000-0000-0000-000000000000")
    assert result is None
