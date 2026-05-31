"""Event queue operations — insert, claim, list, resolve, auto-expire.

All writes go to the `pending_events` table. The CLI reads from here;
the loop's detector writes to here. The loop also calls `apply_expired_defaults`
each cycle to honour TTLs without blocking on the AI.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from bybit_agent.core.logger import get_logger
from bybit_agent.persistence.db import NeonHttpClient

log = get_logger().bind(module="events.queue")

# Default TTLs per kind — the loop applies default_action after this window.
_DEFAULT_TTL: dict[str, timedelta] = {
    "scheduled_review": timedelta(hours=48),
    "risk_escalation":  timedelta(hours=4),
    "market_event":     timedelta(hours=2),
    "ambiguous_decision": timedelta(minutes=30),
}


async def enqueue(
    db: NeonHttpClient,
    *,
    kind: str,
    title: str,
    summary: str,
    default_action: str,
    severity: str = "info",
    symbol: str | None = None,
    cycle_id: str | None = None,
    context: dict[str, Any] | None = None,
    options: list[dict[str, Any]] | None = None,
    dedupe_key: str | None = None,
    ttl: timedelta | None = None,
) -> str | None:
    """Insert a new event; returns the UUID or None if deduped."""
    expires_at = datetime.now(timezone.utc) + (ttl or _DEFAULT_TTL.get(kind, timedelta(hours=24)))
    event_id = str(uuid.uuid4())
    try:
        rows = await db.fetch(
            """INSERT INTO pending_events
                 (id, kind, severity, symbol, cycle_id, title, summary,
                  context, options, default_action, expires_at, dedupe_key)
               VALUES ($1, $2, $3, $4, $5::uuid, $6, $7, $8::jsonb, $9::jsonb, $10, $11, $12)
               ON CONFLICT (dedupe_key) WHERE status = 'pending' AND dedupe_key IS NOT NULL
               DO NOTHING
               RETURNING id""",
            event_id, kind, severity, symbol,
            cycle_id, title, summary,
            json.dumps(context or {}),
            json.dumps(options or []),
            default_action,
            expires_at.isoformat(),
            dedupe_key,
        )
        if not rows:
            log.debug("Event deduped", kind=kind, dedupe_key=dedupe_key)
            return None
        log.info("Event enqueued", kind=kind, title=title, id=event_id)
        return event_id
    except Exception as e:
        log.warning("Failed to enqueue event", kind=kind, error=str(e))
        return None


async def list_events(
    db: NeonHttpClient,
    *,
    kind: str | None = None,
    severity: str | None = None,
    status: str = "pending",
    limit: int = 20,
) -> list[dict[str, Any]]:
    filters = ["status = $1"]
    params: list[Any] = [status]
    i = 2
    if kind:
        filters.append(f"kind = ${i}")
        params.append(kind)
        i += 1
    if severity:
        filters.append(f"severity = ${i}")
        params.append(severity)
        i += 1
    params.append(limit)
    where = " AND ".join(filters)
    rows = await db.fetch(
        f"SELECT * FROM pending_events WHERE {where} ORDER BY ts DESC LIMIT ${i}",
        *params,
    )
    return list(rows)


async def get_event(db: NeonHttpClient, event_id: str) -> dict[str, Any] | None:
    rows = await db.fetch(
        "SELECT * FROM pending_events WHERE id = $1::uuid", event_id
    )
    return rows[0] if rows else None


async def resolve_event(
    db: NeonHttpClient,
    event_id: str,
    *,
    action: str,
    params: dict[str, Any] | None = None,
    resolved_by: str = "cli-agent",
    status: str = "resolved",
) -> bool:
    resolution = {"action": action, "by": resolved_by, "params": params or {}}
    rows_affected = await db.execute(
        """UPDATE pending_events
           SET status = $1, resolution = $2::jsonb, resolved_at = now()
           WHERE id = $3::uuid AND status = 'pending'""",
        status,
        json.dumps(resolution),
        event_id,
    )
    return True  # NeonHttpClient.execute doesn't return rowcount; treat as success


async def apply_expired_defaults(db: NeonHttpClient) -> int:
    """Called each cycle: auto-resolve events whose TTL has elapsed."""
    rows = await db.fetch(
        """SELECT id, default_action FROM pending_events
           WHERE status = 'pending' AND expires_at < now()"""
    )
    count = 0
    for row in rows:
        resolution = {"action": row["default_action"], "by": "auto", "params": {}}
        try:
            await db.execute(
                """UPDATE pending_events
                   SET status = 'auto_resolved', resolution = $1::jsonb, resolved_at = now()
                   WHERE id = $2::uuid""",
                json.dumps(resolution), row["id"],
            )
            count += 1
        except Exception as e:
            log.warning("Failed to auto-resolve event", id=row["id"], error=str(e))
    if count:
        log.info("Auto-resolved expired events", count=count)
    return count
