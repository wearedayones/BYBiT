"""Tests for the self-diagnosing Doctor engine (control/doctor.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bybit_agent.control.doctor import Doctor, Finding, DoctorReport


class FakeDb:
    """Configurable in-memory DB. Each query is matched by SQL keywords."""

    def __init__(
        self,
        *,
        agent_state: dict | None = None,
        equity: dict | None = None,
        signal_totals: dict | None = None,
        top_rejection: list[dict] | None = None,
        rejection_breakdown: list[dict] | None = None,
        pending_events: int = 0,
        raise_on_fetch: bool = False,
    ) -> None:
        self._ag = agent_state if agent_state is not None else {"status": "running", "kill_engaged": False}
        self._eq = equity if equity is not None else {"drawdown_pct": 0.01}
        self._sig = signal_totals or {"total": 0, "approved": 0, "rejected": 0}
        self._top_rej = top_rejection or []
        self._rej_breakdown = rejection_breakdown or []
        self._pending = pending_events
        self._raise = raise_on_fetch
        self.executed: list[str] = []

    async def fetch(self, sql: str, *args) -> list[dict]:
        if self._raise:
            raise RuntimeError("simulated DB outage")
        u = sql.upper()
        if "FROM AGENT_STATE" in u:
            return [self._ag] if self._ag else []
        if "FROM EQUITY_SNAPSHOTS" in u:
            return [self._eq] if self._eq else []
        if "FROM DECISION_LOG" in u and "COUNT(*)::INT" in u and "AS TOTAL" in u:
            return [self._sig]
        if "GROUP BY REJECT_REASON" in u:
            return self._top_rej
        if "GROUP BY STRATEGY, REJECT_REASON" in u:
            return self._rej_breakdown
        if "FROM PENDING_EVENTS" in u and "COUNT(*)" in u:
            return [{"n": self._pending}]
        return []

    async def execute(self, sql: str, *args) -> None:
        self.executed.append(sql.strip())

    def json(self, obj):
        import json
        return json.dumps(obj)


# ── report roll-up ──────────────────────────────────────────────────────────

def test_report_health_rollup():
    r = DoctorReport(findings=[
        Finding("a", "ok", "fine"),
        Finding("b", "info", "fyi"),
    ])
    assert r.healthy is True
    assert r.worst == "info"

    r2 = DoctorReport(findings=[Finding("a", "ok", "fine"), Finding("b", "critical", "bad")])
    assert r2.healthy is False
    assert r2.worst == "critical"
    assert len(r2.actionable) == 1


# ── individual checks ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_healthy_system_all_green():
    db = FakeDb(
        agent_state={"status": "running", "kill_engaged": False,
                     "last_cycle_at": datetime.now(timezone.utc).isoformat()},
        equity={"drawdown_pct": 0.01},
        signal_totals={"total": 20, "approved": 15, "rejected": 5},
        pending_events=0,
    )
    report = await Doctor(db, process_alive=True).diagnose()
    assert report.healthy
    assert report.worst == "ok"


@pytest.mark.asyncio
async def test_dead_process_is_critical():
    db = FakeDb(agent_state={"status": "running", "kill_engaged": False,
                             "last_cycle_at": datetime.now(timezone.utc).isoformat()})
    report = await Doctor(db, process_alive=False, restart_fn=None).diagnose()
    proc = next(f for f in report.findings if f.check == "process")
    assert proc.severity == "critical"


@pytest.mark.asyncio
async def test_dead_process_auto_restart():
    restarted = []
    db = FakeDb(agent_state={"status": "running", "kill_engaged": False,
                             "last_cycle_at": datetime.now(timezone.utc).isoformat()})
    doc = Doctor(db, process_alive=False, restart_fn=lambda: restarted.append(True))
    report = await doc.treat()
    proc = next(f for f in report.findings if f.check == "process")
    assert proc.auto_fixed is True
    assert restarted == [True]


@pytest.mark.asyncio
async def test_legacy_killed_state_auto_reset():
    db = FakeDb(agent_state={"status": "killed", "kill_engaged": False,
                             "last_cycle_at": datetime.now(timezone.utc).isoformat()})
    doc = Doctor(db, process_alive=True)
    report = await doc.treat()
    legacy = next(f for f in report.findings if f.check == "legacy_killed_state")
    assert legacy.auto_fixed is True
    assert any("status = 'running'" in s for s in db.executed)


@pytest.mark.asyncio
async def test_kill_engaged_never_auto_cleared():
    db = FakeDb(agent_state={"status": "running", "kill_engaged": True,
                             "kill_reason": "Kill level breached",
                             "last_cycle_at": datetime.now(timezone.utc).isoformat()})
    doc = Doctor(db, process_alive=True)
    report = await doc.treat()
    ks = next(f for f in report.findings if f.check == "kill_switch")
    assert ks.severity == "critical"
    assert ks.auto_fixed is False  # never auto-clear an engaged kill
    # Should NOT have run any UPDATE that clears kill_engaged
    assert not any("kill_engaged = false" in s.lower() for s in db.executed)


@pytest.mark.asyncio
async def test_stale_cycle_with_live_process_is_hung():
    old = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
    db = FakeDb(agent_state={"status": "running", "kill_engaged": False, "last_cycle_at": old})
    report = await Doctor(db, process_alive=True).diagnose()
    cf = next(f for f in report.findings if f.check == "cycle_freshness")
    assert cf.severity == "critical"
    assert "HUNG" in cf.detail


@pytest.mark.asyncio
async def test_drawdown_circuit_breaker_warn():
    db = FakeDb(
        agent_state={"status": "running", "kill_engaged": False, "circuit_breaker_pct": 0.10,
                     "kill_level_pct": 0.20, "last_cycle_at": datetime.now(timezone.utc).isoformat()},
        equity={"drawdown_pct": 0.12},
    )
    report = await Doctor(db, process_alive=True).diagnose()
    dd = next(f for f in report.findings if f.check == "drawdown")
    assert dd.severity == "warn"


@pytest.mark.asyncio
async def test_signal_flow_zero_signals_warns():
    db = FakeDb(signal_totals={"total": 0, "approved": 0, "rejected": 0})
    report = await Doctor(db, process_alive=True).diagnose()
    sf = next(f for f in report.findings if f.check == "signal_flow")
    assert sf.severity == "warn"
    assert "No signals" in sf.detail


@pytest.mark.asyncio
async def test_signal_flow_all_rejected_is_critical_with_sizing_advice():
    db = FakeDb(
        signal_totals={"total": 50, "approved": 0, "rejected": 49},
        top_rejection=[{"reject_reason": "Position size too small or zero", "count": 49}],
    )
    report = await Doctor(db, process_alive=True).diagnose()
    sf = next(f for f in report.findings if f.check == "signal_flow")
    assert sf.severity == "critical"
    assert "maxRiskPct" in (sf.recommendation or "")


@pytest.mark.asyncio
async def test_signal_flow_unrecorded_outcomes_warns_about_restart():
    # Signals exist but neither approved nor rejected → version/code gap.
    db = FakeDb(signal_totals={"total": 100, "approved": 0, "rejected": 0})
    report = await Doctor(db, process_alive=True).diagnose()
    sf = next(f for f in report.findings if f.check == "signal_flow")
    assert sf.severity == "warn"
    assert "not being persisted" in sf.detail


@pytest.mark.asyncio
async def test_signal_flow_rate_over_resolved_not_total():
    # 6 approved, 0 rejected, 200 unresolved → rate should be 100% (6/6), not 3%.
    db = FakeDb(signal_totals={"total": 206, "approved": 6, "rejected": 0})
    report = await Doctor(db, process_alive=True).diagnose()
    sf = next(f for f in report.findings if f.check == "signal_flow")
    assert sf.severity == "ok"
    assert "100%" in sf.detail


@pytest.mark.asyncio
async def test_event_backlog_warns_when_undrained():
    db = FakeDb(pending_events=7)
    report = await Doctor(db, process_alive=True).diagnose()
    eb = next(f for f in report.findings if f.check == "event_backlog")
    assert eb.severity == "warn"


@pytest.mark.asyncio
async def test_db_outage_is_single_critical_finding():
    db = FakeDb(raise_on_fetch=True)
    report = await Doctor(db, process_alive=True).diagnose()
    assert len(report.findings) == 1
    assert report.findings[0].check == "db_connectivity"
    assert report.findings[0].severity == "critical"
