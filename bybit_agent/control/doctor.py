"""Doctor — the self-diagnosing health engine behind ``bybit doctor`` and ``bybit watch``.

A Doctor runs a battery of checks against live DB state, then either:
  • **auto-fixes** the deterministic, unambiguous problems itself (restart a dead
    process, reset a legacy ``status='killed'`` row whose kill flag is already clear), or
  • **escalates** the judgment calls to the installing agent by enqueueing a
    deduplicated event — kill-engaged, signal drought, risk-gate blockage,
    drawdown breach, undrained queue backlog.

The split is the whole point: the Doctor never touches anything that requires
human/agent judgment (it will never clear an *engaged* kill switch, never widen a
risk param), but it also never lets a mechanically-recoverable fault sit. Every
escalation carries a concrete ``recommendation`` — the exact ``bybit`` command the
agent should run — so the report is actionable, not just descriptive.

Checks are pure-ish: each reads state and returns a Finding. ``treat()`` is the
only method that mutates (auto-fix) or enqueues (escalate), so the engine is easy
to unit-test by inspecting ``diagnose()`` output without side effects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from bybit_agent.config.constants import RISK_DEFAULTS
from bybit_agent.core.logger import get_logger
from bybit_agent.persistence.db import NeonHttpClient

log = get_logger().bind(module="doctor")

# Severity ranks for sorting / health roll-up.
_RANK = {"ok": 0, "info": 1, "warn": 2, "critical": 3}

# Thresholds.
STALE_CYCLE_S = 180             # loop cycle should refresh well under 3 min
SIGNAL_WINDOW = "2 hours"       # lookback for signal-flow analysis
MIN_SIGNALS_FOR_FLOW = 5        # need a few signals before judging approval rate
LOW_APPROVAL_RATE = 0.10        # below this → warn (paper-approved counts as approved)
EVENT_BACKLOG_WARN = 5          # undrained pending events the agent should look at


@dataclass
class Finding:
    """One diagnostic result. ``severity`` drives the health roll-up."""
    check: str
    severity: str                 # ok | info | warn | critical
    detail: str
    auto_fixed: bool = False
    fix_action: str | None = None       # what the Doctor did, if anything
    escalated: bool = False             # whether an event was enqueued
    recommendation: str | None = None   # concrete bybit command for the agent

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "detail": self.detail,
            "auto_fixed": self.auto_fixed,
            "fix_action": self.fix_action,
            "escalated": self.escalated,
            "recommendation": self.recommendation,
        }


@dataclass
class DoctorReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def worst(self) -> str:
        return max((f.severity for f in self.findings), key=lambda s: _RANK[s], default="ok")

    @property
    def healthy(self) -> bool:
        return self.worst in ("ok", "info")

    @property
    def actionable(self) -> list[Finding]:
        """Findings the agent should act on — escalated, or warn/critical not auto-fixed."""
        return [
            f for f in self.findings
            if f.escalated or (_RANK[f.severity] >= _RANK["warn"] and not f.auto_fixed)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "worst_severity": self.worst,
            "findings": [f.to_dict() for f in self.findings],
            "actionable_count": len(self.actionable),
        }


class Doctor:
    """Runs health checks, applies safe auto-fixes, escalates judgment calls.

    Parameters
    ----------
    db:
        Live DB handle.
    process_alive:
        Whether the ``bybit run`` process is up (the caller knows; pgrep etc.).
    restart_fn:
        Optional callback to (re)spawn the trading process. If ``None`` the Doctor
        reports a dead process but cannot fix it (e.g. one-shot ``bybit doctor``).
    """

    def __init__(
        self,
        db: NeonHttpClient,
        *,
        process_alive: bool = True,
        restart_fn: Callable[[], None] | None = None,
    ) -> None:
        self._db = db
        self._alive = process_alive
        self._restart = restart_fn

    # ── public API ────────────────────────────────────────────────────────────

    async def diagnose(self) -> DoctorReport:
        """Run all checks read-only and return findings. No mutations, no enqueues."""
        report = DoctorReport()

        # Fetch shared state once; a DB failure here is itself the top finding.
        try:
            ag_rows = await self._db.fetch("SELECT * FROM agent_state WHERE id = 'singleton' LIMIT 1")
            snap_rows = await self._db.fetch(
                "SELECT total_equity, drawdown_pct, open_positions, ts "
                "FROM equity_snapshots ORDER BY ts DESC LIMIT 1"
            )
        except Exception as exc:  # noqa: BLE001
            report.findings.append(Finding(
                check="db_connectivity", severity="critical",
                detail=f"DB unreachable: {exc}",
                recommendation="Run `bybit doctor` to validate Neon HTTP 443 connectivity and env.",
            ))
            return report

        ag = ag_rows[0] if ag_rows else {}
        snap = snap_rows[0] if snap_rows else {}

        report.findings.append(self._check_process())
        report.findings.append(self._check_legacy_killed_state(ag))
        report.findings.append(self._check_kill_engaged(ag))
        report.findings.append(self._check_cycle_freshness(ag))
        report.findings.append(self._check_drawdown(snap, ag))
        report.findings.append(await self._check_signal_flow())
        report.findings.append(await self._check_event_backlog())

        return report

    async def treat(self) -> DoctorReport:
        """Diagnose, then apply auto-fixes and escalate judgment calls. Returns the
        report annotated with what was fixed / escalated."""
        report = await self.diagnose()
        for f in report.findings:
            try:
                await self._treat_one(f)
            except Exception as exc:  # noqa: BLE001
                log.warning("Doctor treatment failed", check=f.check, error=str(exc))
        return report

    # ── checks (read-only) ──────────────────────────────────────────────────────

    def _check_process(self) -> Finding:
        if self._alive:
            return Finding("process", "ok", "bybit run process is alive.")
        return Finding(
            "process", "critical", "bybit run process is NOT running.",
            recommendation="bybit run --testnet   (or let the watchdog auto-restart)",
        )

    def _check_legacy_killed_state(self, ag: dict) -> Finding:
        """Old TS kill switch wrote status='killed' but left kill_engaged=false; the
        Python loop only blocks on kill_engaged/paused, so this row silently halts
        nothing yet looks alarming. Safe to reset deterministically."""
        status = ag.get("status")
        if status == "killed" and not ag.get("kill_engaged"):
            return Finding(
                "legacy_killed_state", "warn",
                "status='killed' but kill_engaged=false (legacy TS artifact).",
                recommendation="Auto-resettable: status → 'running'.",
            )
        return Finding("legacy_killed_state", "ok", "No legacy killed-state mismatch.")

    def _check_kill_engaged(self, ag: dict) -> Finding:
        if ag.get("kill_engaged"):
            reason = ag.get("kill_reason") or "unknown"
            return Finding(
                "kill_switch", "critical",
                f"Kill switch ENGAGED — reason: {reason}",
                recommendation=(
                    "Investigate kill_reason and recent decision_log. Do NOT auto-clear. "
                    "After root cause is resolved, clear via the kill-clear SQL in AGENT.md, "
                    "then `bybit run`."
                ),
            )
        return Finding("kill_switch", "ok", "Kill switch not engaged.")

    def _check_cycle_freshness(self, ag: dict) -> Finding:
        last_at = ag.get("last_cycle_at")
        if not last_at:
            return Finding("cycle_freshness", "info", "No cycle timestamp yet (fresh start).")
        age = _age_seconds(last_at)
        if age is None:
            return Finding("cycle_freshness", "info", f"Unparseable last_cycle_at: {last_at!r}")
        if age <= STALE_CYCLE_S:
            return Finding("cycle_freshness", "ok", f"Last cycle {age}s ago.")
        # Stale. If the process is dead the process check already covers restart;
        # if it's alive the loop is hung — escalate, don't auto-kill.
        if self._alive:
            return Finding(
                "cycle_freshness", "critical",
                f"Loop appears HUNG — last cycle {age}s ago but process is alive.",
                recommendation="Inspect /tmp/bybit-run.log for a stuck await; `bybit pause` then `bybit resume`.",
            )
        return Finding(
            "cycle_freshness", "warn",
            f"Last cycle {age}s ago and process is down — restart will refresh it.",
        )

    def _check_drawdown(self, snap: dict, ag: dict) -> Finding:
        dd = float(snap.get("drawdown_pct") or 0)
        kill_pct = float((ag.get("kill_level_pct") if ag else None) or RISK_DEFAULTS["KILL_LEVEL_PCT"])
        cb_pct = float((ag.get("circuit_breaker_pct") if ag else None) or RISK_DEFAULTS["CIRCUIT_BREAKER_PCT"])
        if dd >= kill_pct:
            return Finding(
                "drawdown", "critical",
                f"Drawdown {dd * 100:.2f}% ≥ kill level {kill_pct * 100:.0f}% — kill should have tripped.",
                recommendation="Verify kill switch engaged; review what drove the loss before resuming.",
            )
        if dd >= cb_pct:
            return Finding(
                "drawdown", "warn",
                f"Drawdown {dd * 100:.2f}% ≥ circuit breaker {cb_pct * 100:.0f}% — sizing halved.",
                recommendation="Review open positions; consider `bybit tune --set maxRiskPct=...` lower.",
            )
        return Finding("drawdown", "ok", f"Drawdown {dd * 100:.2f}% within limits.")

    async def _check_signal_flow(self) -> Finding:
        """The core 'is the bot actually able to trade?' check, paper-inclusive."""
        try:
            rows = await self._db.fetch(
                f"""SELECT
                        COUNT(*)::int                                                            AS total,
                        SUM(CASE WHEN approved = true OR outcome = 'paper' THEN 1 ELSE 0 END)::int AS approved,
                        SUM(CASE WHEN approved = false AND outcome != 'paper'
                                  AND reject_reason IS NOT NULL THEN 1 ELSE 0 END)::int           AS rejected
                    FROM decision_log
                    WHERE ts > now() - INTERVAL '{SIGNAL_WINDOW}'"""
            )
            rej = await self._db.fetch(
                f"""SELECT reject_reason, COUNT(*)::int AS count
                    FROM decision_log
                    WHERE approved = false AND outcome != 'paper'
                      AND reject_reason IS NOT NULL
                      AND ts > now() - INTERVAL '{SIGNAL_WINDOW}'
                    GROUP BY reject_reason ORDER BY COUNT(*) DESC LIMIT 1"""
            )
        except Exception as exc:  # noqa: BLE001
            return Finding("signal_flow", "info", f"Signal-flow query failed: {exc}")

        r = rows[0] if rows else {}
        total = int(r.get("total") or 0)
        approved = int(r.get("approved") or 0)
        rejected = int(r.get("rejected") or 0)
        top_rej = rej[0]["reject_reason"] if rej else None

        if total == 0:
            return Finding(
                "signal_flow", "warn",
                f"No signals generated in the last {SIGNAL_WINDOW}.",
                recommendation=(
                    "Check regime classification + strategy suitable_regimes. "
                    "If scores cluster just under 0.40: `bybit tune --set confidenceFloor=0.35`."
                ),
            )
        if total < MIN_SIGNALS_FOR_FLOW:
            return Finding("signal_flow", "info",
                           f"{total} signals in {SIGNAL_WINDOW} (too few to judge approval rate).")

        # Zero approved, but ALSO zero recorded rejections → the loop is generating
        # signals yet not recording approve/reject outcomes. That's a code/version
        # gap (e.g. paper approvals not persisted), not a risk-gate block.
        if approved == 0 and rejected == 0:
            return Finding(
                "signal_flow", "warn",
                f"{total} signals but neither approved nor rejected outcomes recorded — "
                "decision outcomes are not being persisted.",
                recommendation=(
                    "The running process likely predates the paper-approval fix. "
                    "Restart it: stop `bybit run` and relaunch so decision_log records outcomes."
                ),
            )
        if approved == 0:
            size_related = top_rej and any(
                w in top_rej.lower() for w in ("size", "qty", "zero")
            )
            rec = (
                f"`bybit tune --set maxRiskPct=0.03` (top rejection '{top_rej}' is sizing — "
                "equity likely too small for min lot sizes), or fund the account."
                if size_related else
                f"Review rejection '{top_rej}'; adjust risk params or EV threshold via `bybit tune`."
            )
            return Finding(
                "signal_flow", "critical",
                f"{total} signals but ZERO approved over {SIGNAL_WINDOW} — risk gate is blocking everything.",
                recommendation=rec,
            )

        # Approval rate is computed over *resolved* decisions (approved + rejected),
        # not the raw total — unresolved rows (no outcome recorded yet) shouldn't
        # drag the rate down. ``total`` is still reported for context.
        resolved = approved + rejected
        rate = approved / resolved if resolved else 1.0
        if rate < LOW_APPROVAL_RATE:
            return Finding(
                "signal_flow", "warn",
                f"Only {rate * 100:.0f}% of {resolved} resolved signals approved "
                f"(of {total} total; top rejection: {top_rej}).",
                recommendation="Review top rejection reasons; tune risk params if persistent.",
            )
        return Finding(
            "signal_flow", "ok",
            f"{approved}/{resolved} resolved signals approved ({rate * 100:.0f}%) "
            f"over {SIGNAL_WINDOW} ({total} total).",
        )

    async def _check_event_backlog(self) -> Finding:
        try:
            rows = await self._db.fetch(
                "SELECT COUNT(*)::int AS n FROM pending_events WHERE status = 'pending'"
            )
        except Exception as exc:  # noqa: BLE001
            return Finding("event_backlog", "info", f"Event-backlog query failed: {exc}")
        n = int(rows[0]["n"]) if rows else 0
        if n >= EVENT_BACKLOG_WARN:
            return Finding(
                "event_backlog", "warn",
                f"{n} undrained pending events — the agent has unread escalations.",
                recommendation="`bybit events --json` then resolve each with `bybit decide` / `bybit resolve`.",
            )
        if n > 0:
            return Finding("event_backlog", "info", f"{n} pending event(s) awaiting the agent.")
        return Finding("event_backlog", "ok", "No pending events.")

    # ── treatment (mutations + escalations) ──────────────────────────────────────

    async def _treat_one(self, f: Finding) -> None:
        if f.severity == "ok" or f.severity == "info":
            return

        # 1. Deterministic auto-fixes.
        if f.check == "process" and self._restart is not None:
            self._restart()
            f.auto_fixed = True
            f.fix_action = "Restarted bybit run process."
            log.info("Doctor auto-fix: restarted process")
            return

        if f.check == "legacy_killed_state":
            await self._db.execute(
                "UPDATE agent_state SET status = 'running', updated_at = now() "
                "WHERE id = 'singleton' AND status = 'killed' AND kill_engaged = false"
            )
            f.auto_fixed = True
            f.fix_action = "Reset legacy status='killed' → 'running'."
            log.info("Doctor auto-fix: reset legacy killed state")
            return

        # 2. Escalate judgment calls to the agent (deduplicated).
        await self._escalate(f)

    async def _escalate(self, f: Finding) -> None:
        from bybit_agent.events.triggers import trigger_signal_drought
        from bybit_agent.events.queue import enqueue

        # Signal-flow drought reuses the dedicated trigger so its rich context
        # (rejection breakdown) is captured.
        if f.check == "signal_flow":
            try:
                rej_rows = await self._db.fetch(
                    f"""SELECT strategy, reject_reason, COUNT(*)::int AS cnt
                        FROM decision_log
                        WHERE approved = false AND outcome != 'paper'
                          AND reject_reason IS NOT NULL
                          AND ts > now() - INTERVAL '{SIGNAL_WINDOW}'
                        GROUP BY strategy, reject_reason ORDER BY cnt DESC LIMIT 10"""
                )
                top = [
                    {"strategy": r["strategy"], "reason": r["reject_reason"], "count": r["cnt"]}
                    for r in (rej_rows or [])
                ]
                tot = sum(t["count"] for t in top)
                eid = await trigger_signal_drought(
                    self._db,
                    signals_fired=tot,
                    cycles_blocked=0,  # doctor-fired (time-window based), not cycle-counted
                    rejection_summary={"top_rejections": top, "source": "doctor"},
                )
                f.escalated = eid is not None
            except Exception as exc:  # noqa: BLE001
                log.warning("Doctor signal_flow escalation failed", error=str(exc))
            return

        # Generic escalation: one risk_escalation event per check per 4h window.
        severity = "critical" if f.severity == "critical" else "warning"
        eid = await enqueue(
            self._db,
            kind="risk_escalation",
            severity=severity,
            title=f"Doctor finding: {f.check}",
            summary=f"{f.detail}\n\nRecommended fix: {f.recommendation or 'investigate manually.'}",
            default_action="acknowledge",
            context={
                "check": f.check,
                "detail": f.detail,
                "recommendation": f.recommendation,
                "source": "doctor",
            },
            options=[
                {"action": "acknowledge", "label": "Acknowledge — will investigate"},
                {"action": "pause", "label": "Pause the agent for manual inspection"},
            ],
            dedupe_key=f"doctor:{f.check}",
            ttl=timedelta(hours=4),
        )
        f.escalated = eid is not None


def _age_seconds(last_at: Any) -> int | None:
    """Seconds since a TIMESTAMPTZ value returned by Neon (str or datetime)."""
    try:
        if isinstance(last_at, datetime):
            dt = last_at
        else:
            raw = str(last_at)
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:  # noqa: BLE001
        return None
