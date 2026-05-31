"""Tests for event trigger functions — focused on signal_drought."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from bybit_agent.events.triggers import (
    trigger_ambiguous_decision,
    trigger_market_event,
    trigger_risk_escalation,
    trigger_scheduled_review,
    trigger_signal_drought,
)


class FakeDb:
    """Minimal NeonHttpClient stub — mirrors test_events_queue.FakeDb."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    async def execute(self, sql: str, *args) -> None:
        pass

    async def fetch(self, sql: str, *args) -> list[dict]:
        sql_upper = sql.upper().strip()

        if "INSERT INTO PENDING_EVENTS" in sql_upper and "RETURNING ID" in sql_upper:
            (ev_id, kind, severity, symbol, cycle_id, title, summary,
             context_s, options_s, default_action, expires_at_s, dedupe_key) = args

            if dedupe_key:
                for row in self._rows.values():
                    if row.get("dedupe_key") == dedupe_key and row.get("status") == "pending":
                        return []

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
            }
            return [{"id": ev_id}]

        return []

    def json(self, obj) -> str:
        return json.dumps(obj)


# ── trigger_signal_drought ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_signal_drought_enqueues_risk_escalation():
    db = FakeDb()
    ev_id = await trigger_signal_drought(
        db,
        signals_fired=45,
        cycles_blocked=60,
        rejection_summary={"top_rejections": [
            {"strategy": "trend_momentum", "reason": "ev_negative", "count": 30},
        ]},
        cycle_id="00000000-0000-0000-0000-000000000001",
    )
    assert ev_id is not None
    row = db._rows[ev_id]
    assert row["kind"] == "risk_escalation"
    assert row["severity"] == "warning"
    assert "60" in row["title"]
    assert "45" in row["title"]
    ctx = row["context"]
    assert ctx["signals_fired"] == 45
    assert ctx["cycles_blocked"] == 60
    assert ctx["top_rejections"][0]["strategy"] == "trend_momentum"


@pytest.mark.asyncio
async def test_signal_drought_deduplicates():
    db = FakeDb()
    id1 = await trigger_signal_drought(
        db, signals_fired=10, cycles_blocked=60,
        rejection_summary={"top_rejections": []},
    )
    id2 = await trigger_signal_drought(
        db, signals_fired=20, cycles_blocked=65,
        rejection_summary={"top_rejections": []},
    )
    assert id1 is not None
    assert id2 is None  # dedupe_key="signal_drought" blocks second


@pytest.mark.asyncio
async def test_signal_drought_has_acknowledge_default():
    db = FakeDb()
    ev_id = await trigger_signal_drought(
        db, signals_fired=5, cycles_blocked=60,
        rejection_summary={"top_rejections": []},
    )
    row = db._rows[ev_id]
    assert row["default_action"] == "acknowledge"
    actions = [opt["action"] for opt in row["options"]]
    assert "acknowledge" in actions
    assert "pause" in actions


@pytest.mark.asyncio
async def test_signal_drought_ttl_is_4h():
    db = FakeDb()
    before = datetime.now(timezone.utc)
    ev_id = await trigger_signal_drought(
        db, signals_fired=5, cycles_blocked=60,
        rejection_summary={"top_rejections": []},
    )
    after = datetime.now(timezone.utc)
    row = db._rows[ev_id]
    expected_min = before + timedelta(hours=4) - timedelta(seconds=1)
    expected_max = after + timedelta(hours=4) + timedelta(seconds=1)
    assert expected_min <= row["expires_at"] <= expected_max


@pytest.mark.asyncio
async def test_signal_drought_summary_contains_rejection_reasons():
    db = FakeDb()
    ev_id = await trigger_signal_drought(
        db, signals_fired=30, cycles_blocked=60,
        rejection_summary={"top_rejections": [
            {"strategy": "funding_harvest", "reason": "insufficient_equity", "count": 15},
            {"strategy": "mean_reversion", "reason": "ev_negative", "count": 10},
        ]},
    )
    row = db._rows[ev_id]
    assert "funding_harvest" in row["summary"]
    assert "mean_reversion" in row["summary"]


# ── other triggers smoke tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trigger_scheduled_review_smoke():
    db = FakeDb()
    ev_id = await trigger_scheduled_review(db)
    assert ev_id is not None
    assert db._rows[ev_id]["kind"] == "scheduled_review"
    assert db._rows[ev_id]["dedupe_key"] == "scheduled_review:daily"


@pytest.mark.asyncio
async def test_trigger_risk_escalation_smoke():
    db = FakeDb()
    ev_id = await trigger_risk_escalation(
        db, reason="max_drawdown", details={"drawdown": 0.12}
    )
    assert ev_id is not None
    assert db._rows[ev_id]["kind"] == "risk_escalation"
    assert db._rows[ev_id]["severity"] == "critical"


@pytest.mark.asyncio
async def test_trigger_market_event_smoke():
    db = FakeDb()
    ev_id = await trigger_market_event(
        db, symbol="BTCUSDT", event_type="vol_spike",
        details={"atr_pct": 0.09},
    )
    assert ev_id is not None
    assert db._rows[ev_id]["symbol"] == "BTCUSDT"
    assert db._rows[ev_id]["kind"] == "market_event"


@pytest.mark.asyncio
async def test_trigger_ambiguous_decision_smoke():
    db = FakeDb()
    ev_id = await trigger_ambiguous_decision(
        db, symbol="ETHUSDT", strategy="trend_momentum",
        signal={"action": "enter_long"}, score=0.42,
    )
    assert ev_id is not None
    assert db._rows[ev_id]["kind"] == "ambiguous_decision"
    assert db._rows[ev_id]["default_action"] == "reject"
