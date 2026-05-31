"""The ``bybit`` CLI — the universal surface any AI agent uses to operate the skill.

All commands talk to Postgres directly and work as a short-lived process independent
of the running ``bybit run`` service. Never places orders; the loop alone does that.

Commands:
  migrate      Apply all migrations/*.sql (idempotent).
  doctor       Validate env, DB connectivity, JSONB round-trip.
  run          Launch the 24/7 trading service.
  status       Show agent_state summary.
  positions    List open positions.
  executions   Show recent trade fills from the exchange.
  report       Structured performance digest.
  events       List pending events (AI wake queue).
  event        Show one event with full context.
  decide       Resolve an ambiguous_decision event.
  resolve      Generic event resolution.
  tune         Validate + write a numeric param to agent_config.
  weights      Show or adjust strategy weights.
  review       Run an immediate SelfReview pass.
  pause        Set agent status = 'paused'.
  resume       Set agent status = 'running'.
  kill         Engage the kill switch (cancel-all + flatten).
  watch        Watchdog: health-check every N seconds, auto-restart if dead.
  strategy     Strategy management (create/edit/backtest/accept/pause/…).
  algo         Algo order management (list active TWAP/Iceberg orders).
  skill        Official Bybit Exchange AI skill hub (list/show/refresh).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

app = typer.Typer(add_completion=False, help="Agent-operated Bybit trading skill.")


def _migrations_dir() -> Path:
    return Path.cwd() / "migrations"


# ── helpers ──────────────────────────────────────────────────────────────────

async def _run_migrations() -> tuple[int, int]:
    from .persistence.db import close_db, get_db
    db = get_db()
    files = sorted(_migrations_dir().glob("*.sql"))
    stmt_count = 0
    for f in files:
        stmt_count += await db.execute_script(f.read_text())
    await close_db()
    return len(files), stmt_count


def _print_json(data: object) -> None:
    typer.echo(json.dumps(data, default=str, indent=2))


async def _get_db():
    from .persistence.db import get_db
    return get_db()


async def _is_testnet(db=None) -> bool:
    """Read trading_mode from agent_state to determine the active API target."""
    try:
        _db = db or await _get_db()
        rows = await _db.fetch(
            "SELECT trading_mode FROM agent_state WHERE id = 'singleton' LIMIT 1"
        )
        mode = (rows[0].get("trading_mode") if rows else None) or "shadow"
        return mode != "mainnet_live"
    except Exception:
        return True  # testnet is the safe default


# ── migrate ──────────────────────────────────────────────────────────────────

@app.command()
def migrate() -> None:
    """Apply all migrations/*.sql in order (idempotent)."""
    files, stmts = asyncio.run(_run_migrations())
    typer.echo(f"✅ Applied {files} migration file(s), {stmts} statement(s).")


# ── doctor ───────────────────────────────────────────────────────────────────

async def _doctor() -> bool:
    from .config.env import get_env
    from .core.logger import mask_secret
    from .persistence.db import close_db, get_db

    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "✅" if passed else "❌"
        ok = ok and passed
        typer.echo(f"  {mark} {label}{(' — ' + detail) if detail else ''}")

    typer.echo("bybit doctor — pre-flight checks\n")

    try:
        env = get_env()
        check("env loaded & validated", True)
        signing = (
            "RSA (inline PEM)" if env.BYBIT_API_PRIVATE_KEY
            else "RSA (key file)" if env.BYBIT_API_PRIVATE_KEY_PATH
            else "HMAC"
        )
        typer.echo(f"     API key:  {mask_secret(env.BYBIT_API_KEY)}")
        typer.echo(f"     signing:  {signing}")
        typer.echo(f"     proxy:    {'set' if env.BYBIT_PROXY_URL else 'none'}")
    except Exception as exc:
        check("env loaded & validated", False, str(exc)[:160])
        return False

    try:
        db = get_db()
        val = await db.fetchval("SELECT 1 AS one")
        check("DB reachable over HTTPS:443", val == 1)
        row = await db.fetchrow(
            "SELECT ($1::jsonb)->>'k' AS v", db.json({"k": "neon-http-ok"})
        )
        check("JSONB round-trip", bool(row) and row.get("v") == "neon-http-ok")
    except Exception as exc:
        check("DB reachable over HTTPS:443", False, str(exc)[:160])
    finally:
        await close_db()

    # Skill version check — always shown so any agent knows immediately.
    try:
        from .skill.version_checker import check as _vc, refresh as _vr
        info = await _vc()
        embedded, latest, bump = info["embedded"], info.get("latest"), info["bump"]
        if bump == "current":
            check("official skill", True, f"v{embedded} — up to date")
        elif bump in ("patch", "minor") and latest:
            typer.echo(f"  🔄 official skill: v{embedded} → v{latest} ({bump}) — auto-refreshing...")
            result = await _vr(latest)
            new_ver = result.get("version", latest)
            errs = result.get("errors", [])
            check("official skill", not errs,
                  f"refreshed to v{new_ver}" + (f" ({len(errs)} errors)" if errs else ""))
        elif bump == "major" and latest:
            check("official skill", False,
                  f"MAJOR update v{embedded} → v{latest} — run `bybit skill refresh` after reviewing changes")
        else:
            check("official skill", True, f"v{embedded} (latest version unavailable offline)")
    except Exception as exc:  # noqa: BLE001
        check("official skill", True, f"version check skipped ({exc})")

    return ok


@app.command()
def doctor(
    deep: Annotated[bool, typer.Option("--deep", help="Also run the live health Doctor (signal flow, kill state, drawdown, backlog) and escalate findings.")] = False,
    treat: Annotated[bool, typer.Option("--treat/--dry-run", help="With --deep: apply safe auto-fixes and enqueue escalations (default), or just report.")] = True,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate env + DB + signing; with --deep, run the full self-diagnosing Doctor."""
    # With --json the pre-flight's human output would pollute the JSON stream, so we
    # skip it: the deep Doctor's db_connectivity/process checks cover the same ground,
    # and signing is validated at service startup. JSON callers get clean JSON only.
    ok = True
    if not as_json:
        ok = asyncio.run(_doctor())

    if not deep:
        typer.echo("")
        if ok:
            typer.echo("All checks passed.")
        else:
            typer.echo("Some checks failed — see above.")
            sys.exit(1)
        return

    async def _deep() -> dict:
        from .control.doctor import Doctor
        db = await _get_db()
        try:
            # No restart_fn here — a one-shot CLI invocation shouldn't spawn a daemon.
            doc = Doctor(db, process_alive=_proc_running(), restart_fn=None)
            report = await (doc.treat() if treat else doc.diagnose())
            return report.to_dict()
        finally:
            from .persistence.db import close_db
            await close_db()

    report = asyncio.run(_deep())
    if as_json:
        _print_json(report)
    else:
        typer.echo(f"\n── DOCTOR ({'treated' if treat else 'dry-run'}) ──")
        typer.echo(f"Health: {report['worst_severity']}  |  actionable findings: {report['actionable_count']}\n")
        for f in report["findings"]:
            mark = "✓" if f["auto_fixed"] else ("⚕" if f["escalated"] or f["severity"] in ("warn", "critical") else "·")
            typer.echo(f"  {mark} {f['check']:20s} [{f['severity']:8s}] {f['detail']}")
            if f["auto_fixed"]:
                typer.echo(f"      fixed: {f['fix_action']}")
            elif f["recommendation"]:
                typer.echo(f"      → {f['recommendation']}")
    if not ok or not report["healthy"]:
        sys.exit(1)


def _proc_running() -> bool:
    import subprocess
    return subprocess.run(["pgrep", "-f", "bybit run"], capture_output=True).returncode == 0


# ── run ──────────────────────────────────────────────────────────────────────

@app.command()
def run() -> None:
    """Launch the 24/7 deterministic trading service.

    Trading mode (shadow / testnet_live / mainnet_live) is read from the DB
    each cycle — use `bybit cutover <mode>` to change it at runtime.
    """
    from .service import start_service
    asyncio.run(start_service())


# ── status ───────────────────────────────────────────────────────────────────

@app.command()
def status(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show agent state: env, equity, drawdown, status, kill flag."""

    async def _run() -> None:
        db = await _get_db()
        try:
            rows = await db.fetch(
                "SELECT * FROM agent_state WHERE id = 'singleton' LIMIT 1"
            )
            snap_rows = await db.fetch(
                """SELECT total_equity, drawdown_pct, open_positions, ts
                   FROM equity_snapshots ORDER BY ts DESC LIMIT 1"""
            )
        finally:
            from .persistence.db import close_db
            await close_db()

        ag = rows[0] if rows else {}
        snap = snap_rows[0] if snap_rows else {}
        kill_engaged = bool(ag.get("kill_engaged", False))
        data = {
            "trading_mode": ag.get("trading_mode", "shadow"),
            "env": ag.get("env", "unknown"),
            "status": ag.get("status", "unknown"),
            "kill_engaged": kill_engaged,
            "kill_reason": ag.get("kill_reason") if kill_engaged else None,
            "equity": float(snap.get("total_equity") or 0),
            "drawdown_pct": round(float(snap.get("drawdown_pct") or 0) * 100, 2),
            "open_positions": snap.get("open_positions", 0),
            "last_cycle_at": str(ag.get("last_cycle_at", "")),
            "promotion_cycle_count": ag.get("promotion_cycle_count", 0),
        }
        if as_json:
            _print_json(data)
        else:
            mode_icons = {"shadow": "🔵", "testnet_live": "🟡", "mainnet_live": "🟢"}
            mode = data["trading_mode"]
            typer.echo(f"trading_mode:     {mode_icons.get(mode, '⚙️ ')} {mode}")
            typer.echo(f"env:              {data['env']}")
            typer.echo(f"status:           {data['status']}")
            typer.echo(f"kill_engaged:     {data['kill_engaged']}"
                       + (f"  ({data['kill_reason']})" if data["kill_reason"] else ""))
            typer.echo(f"equity:           ${data['equity']:,.2f}")
            typer.echo(f"drawdown:         {data['drawdown_pct']:.2f}%")
            typer.echo(f"open_positions:   {data['open_positions']}")
            typer.echo(f"last_cycle_at:    {data['last_cycle_at']}")
            typer.echo(f"promotion_cycles: {data['promotion_cycle_count']}"
                       + ("  ← feeds promotion gate" if mode == "testnet_live" else ""))

    asyncio.run(_run())


# ── positions ─────────────────────────────────────────────────────────────────

@app.command()
def positions(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List open positions from the exchange."""

    async def _run() -> None:
        from .config.env import get_env
        from .exchange.bybit_client import BybitClient
        from .exchange.credentials import resolve_bybit_auth

        env = get_env()
        auth = resolve_bybit_auth(env)
        client = BybitClient(auth, is_testnet=await _is_testnet())
        try:
            pos_list = await client.get_positions("linear")
            open_pos = [p for p in pos_list if float(p.get("size", 0)) > 0]
        finally:
            await client.aclose()

        if as_json:
            _print_json(open_pos)
        else:
            if not open_pos:
                typer.echo("No open positions.")
                return
            for p in open_pos:
                upnl = float(p.get("unrealisedPnl") or 0)
                typer.echo(
                    f"{p['symbol']:12s}  {p['side']:4s}  size={p['size']:>10s}"
                    f"  entry={p.get('avgPrice','?'):>10s}"
                    f"  mark={p.get('markPrice','?'):>10s}"
                    f"  uPnL={upnl:+.4f}"
                    f"  SL={p.get('stopLoss',''):>8s}"
                )

    asyncio.run(_run())


# ── report ────────────────────────────────────────────────────────────────────

@app.command()
def report(
    period: Annotated[str, typer.Option("--period", help="daily|weekly|monthly")] = "daily",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Structured performance digest — the AI's reasoning input for tune/weights decisions."""

    _INTERVAL = {"daily": "1 day", "weekly": "7 days", "monthly": "30 days"}

    async def _run() -> None:
        import datetime as _dt
        interval = _INTERVAL.get(period, "1 day")
        db = await _get_db()
        try:
            # ── core queries ────────────────────────────────────────────
            snap_rows, trade_rows, weight_rows, config_rows = await asyncio.gather(
                db.fetch(
                    f"""SELECT total_equity, drawdown_pct, ts
                        FROM equity_snapshots
                        WHERE ts > now() - INTERVAL '{interval}'
                        ORDER BY ts"""
                ),
                db.fetch(
                    f"""SELECT strategy, side, realized_pnl, closed_at
                        FROM trades
                        WHERE closed_at > now() - INTERVAL '{interval}'
                          AND is_paper = false
                        ORDER BY closed_at DESC LIMIT 100"""
                ),
                db.fetch(
                    "SELECT strategy, weight, enabled FROM strategy_weights ORDER BY strategy"
                ),
                db.fetch(
                    "SELECT key, value, description FROM agent_config ORDER BY key"
                ),
            )
            # ── signal analytics (paper-inclusive — primary learning input) ──
            sig_by_strategy, rejection_rows, last_approved_rows, regime_rows = await asyncio.gather(
                db.fetch(
                    f"""SELECT strategy,
                            COUNT(*)::int                                                          AS signals,
                            SUM(CASE WHEN approved = true OR outcome = 'paper' THEN 1 ELSE 0 END)::int AS approved_count,
                            ROUND(AVG(composite_score)::numeric, 3)                               AS avg_score,
                            ROUND(MAX(composite_score)::numeric, 3)                               AS max_score
                        FROM decision_log
                        WHERE ts > now() - INTERVAL '{interval}'
                        GROUP BY strategy
                        ORDER BY COUNT(*) DESC"""
                ),
                db.fetch(
                    f"""SELECT reject_reason, COUNT(*)::int AS count
                        FROM decision_log
                        WHERE approved = false
                          AND outcome IS DISTINCT FROM 'paper'
                          AND reject_reason IS NOT NULL
                          AND ts > now() - INTERVAL '{interval}'
                        GROUP BY reject_reason
                        ORDER BY COUNT(*) DESC LIMIT 10"""
                ),
                db.fetch(
                    "SELECT ts FROM decision_log WHERE approved = true ORDER BY ts DESC LIMIT 1"
                ),
                db.fetch(
                    f"""SELECT regime, COUNT(*)::int AS count
                        FROM decision_log
                        WHERE regime IS NOT NULL
                          AND ts > now() - INTERVAL '{interval}'
                        GROUP BY regime ORDER BY COUNT(*) DESC"""
                ),
            )
        finally:
            from .persistence.db import close_db
            await close_db()

        # ── aggregate ───────────────────────────────────────────────────
        wins = sum(1 for t in trade_rows if float(t.get("realized_pnl") or 0) > 0)
        total = len(trade_rows)
        total_pnl = sum(float(t.get("realized_pnl") or 0) for t in trade_rows)
        equities = [float(r["total_equity"]) for r in snap_rows if r.get("total_equity")]
        max_dd = max((float(r.get("drawdown_pct") or 0) for r in snap_rows), default=0)

        total_signals = sum(int(r.get("signals") or 0) for r in sig_by_strategy)
        total_approved = sum(int(r.get("approved_count") or 0) for r in sig_by_strategy)
        approval_rate = round(total_approved / total_signals, 4) if total_signals else None

        hours_since_approved: float | None = None
        if last_approved_rows:
            ts = last_approved_rows[0]["ts"]
            if isinstance(ts, str):
                ts = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_dt.timezone.utc)
            hours_since_approved = round(
                (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds() / 3600, 1
            )

        # ── auto-diagnose ───────────────────────────────────────────────
        bottleneck = "ok"
        recommendation = "System operating normally."

        if total_signals == 0:
            bottleneck = "no_signals"
            recommendation = (
                "No signals generated this period. Check regime classification and "
                "strategy suitable_regimes. Try: inspect recent decision_log, then "
                "`bybit tune --set confidenceFloor=0.35` if scores cluster just below 0.40."
            )
        elif total_approved == 0:
            top_rej = rejection_rows[0]["reject_reason"] if rejection_rows else "unknown"
            n_str = int(sig_by_strategy[0].get("signals") or 0) if sig_by_strategy else 0
            avg_all = (
                sum(float(r.get("avg_score") or 0) for r in sig_by_strategy)
                / max(len(sig_by_strategy), 1)
            )
            bottleneck = "risk_gate"
            recommendation = (
                f"{total_signals} signals generated but ALL rejected by risk gate. "
                f"Top rejection: '{top_rej}'. Avg composite score: {avg_all:.3f}. "
                "Try: `bybit tune --set maxRiskPct=0.015` or review EV inputs if "
                "reason is ev_negative."
            )
        elif approval_rate is not None and approval_rate < 0.25:
            bottleneck = "low_approval"
            top_rej2 = rejection_rows[0]["reject_reason"] if rejection_rows else ""
            if "size" in top_rej2.lower() or "qty" in top_rej2.lower() or "zero" in top_rej2.lower():
                recommendation = (
                    f"Only {approval_rate * 100:.0f}% of signals pass — top rejection: "
                    f"'{top_rej2}'. Equity may be too small for minimum lot sizes. "
                    "Fix: `bybit tune --set maxRiskPct=0.03` (raises position budget) "
                    "or let discovery find lower-minimum instruments."
                )
            else:
                recommendation = (
                    f"Only {approval_rate * 100:.0f}% of signals pass the risk gate. "
                    f"Top rejection: '{top_rej2}'. "
                    "Review top rejection reasons and adjust risk parameters or EV threshold."
                )
        elif total == 0 and total_approved > 0:
            bottleneck = "paper_mode"
            recommendation = (
                f"Paper/shadow mode — {total_approved} signals approved (shadow) of {total_signals}. "
                "No live trades until promotion gate passes. System healthy."
            )
        elif total > 0 and total > 0 and wins / total < 0.40:
            bottleneck = "low_win_rate"
            recommendation = (
                f"Win rate {wins / total:.0%} below 40%. "
                "Consider reducing weight on underperforming strategies via `bybit weights`."
            )

        # ── assemble ────────────────────────────────────────────────────
        signal_analytics = {
            "total_signals": total_signals,
            "approved_signals": total_approved,
            "approval_rate": approval_rate,
            "hours_since_last_approved": hours_since_approved,
            "by_strategy": [
                {
                    "strategy": r["strategy"],
                    "signals": int(r.get("signals") or 0),
                    "approved": int(r.get("approved_count") or 0),
                    "avg_score": float(r.get("avg_score") or 0),
                    "max_score": float(r.get("max_score") or 0),
                }
                for r in sig_by_strategy
            ],
            "top_rejections": [
                {"reason": r["reject_reason"], "count": int(r.get("count") or 0)}
                for r in rejection_rows
            ],
            "regime_distribution": [
                {"regime": r["regime"], "count": int(r.get("count") or 0)}
                for r in regime_rows
            ],
        }

        data = {
            "period": period,
            "trade_count": total,
            "win_rate": round(wins / total, 4) if total else None,
            "total_pnl": round(total_pnl, 4),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "equity_start": round(equities[0], 2) if equities else None,
            "equity_end": round(equities[-1], 2) if equities else None,
            "signal_analytics": signal_analytics,
            "interpretation": {
                "bottleneck": bottleneck,
                "recommendation": recommendation,
            },
            "strategy_weights": [dict(w) for w in weight_rows],
            "agent_config_overrides": [dict(c) for c in config_rows],
            "trades": [dict(t) for t in trade_rows[:10]],
        }

        if as_json:
            _print_json(data)
        else:
            typer.echo(f"\n── {period.upper()} REPORT ──")
            typer.echo(f"Trades:      {total}  |  Win rate: {data['win_rate'] or 'n/a'}")
            typer.echo(f"Total PnL:   {data['total_pnl']:+.4f}")
            typer.echo(f"Max DD:      {data['max_drawdown_pct']:.2f}%")
            if equities:
                typer.echo(f"Equity:      {data['equity_start']} → {data['equity_end']}")

            typer.echo(f"\n── SIGNAL ANALYTICS ({period}) ──")
            typer.echo(
                f"Signals:     {total_signals}  |  Approved: {total_approved}"
                f"  |  Rate: {f'{approval_rate:.0%}' if approval_rate is not None else 'n/a'}"
            )
            if hours_since_approved is not None:
                typer.echo(f"Last approved: {hours_since_approved}h ago")
            if sig_by_strategy:
                typer.echo(f"\n  {'Strategy':<22}  {'signals':>7}  {'approved':>8}  avg_score")
                for r in sig_by_strategy:
                    typer.echo(
                        f"  {r['strategy']:<22}  {int(r.get('signals') or 0):>7}  "
                        f"{int(r.get('approved_count') or 0):>8}  "
                        f"{float(r.get('avg_score') or 0):.3f}"
                    )
            if rejection_rows:
                typer.echo("\n  Top rejection reasons:")
                for r in rejection_rows[:6]:
                    typer.echo(f"    {r['reject_reason']:<30}  ×{int(r.get('count') or 0)}")
            if regime_rows:
                typer.echo(f"\n  Regime distribution: "
                           + "  ".join(f"{r['regime']}×{int(r.get('count') or 0)}" for r in regime_rows))

            typer.echo(f"\n── DIAGNOSIS ──")
            typer.echo(f"Bottleneck:  {bottleneck}")
            typer.echo(f"Advice:      {recommendation}")

            typer.echo("\nStrategy weights:")
            for w in weight_rows:
                typer.echo(f"  {w['strategy']:20s}  weight={w['weight']}  enabled={w['enabled']}")
            if config_rows:
                typer.echo("\nAgent config overrides:")
                for c in config_rows:
                    typer.echo(f"  {c['key']:22s}  = {c['value']}")

    asyncio.run(_run())


# ── events ────────────────────────────────────────────────────────────────────

@app.command()
def events(
    kind: Annotated[Optional[str], typer.Option("--kind")] = None,
    severity: Annotated[Optional[str], typer.Option("--severity")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List pending events in the AI wake queue."""

    async def _run() -> None:
        from .events.queue import list_events
        db = await _get_db()
        try:
            rows = await list_events(db, kind=kind, severity=severity, limit=limit)
        finally:
            from .persistence.db import close_db
            await close_db()

        if as_json:
            _print_json([dict(r) for r in rows])
            return
        if not rows:
            typer.echo("No pending events.")
            return
        for r in rows:
            typer.echo(
                f"[{r['id']}]  {r['kind']:22s}  {r['severity']:8s}  {r['title']}"
                f"  (expires {str(r.get('expires_at',''))[:16]})"
            )

    asyncio.run(_run())


# ── event ─────────────────────────────────────────────────────────────────────

@app.command()
def event(
    event_id: str,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one event with full context and available options."""

    async def _run() -> None:
        from .events.queue import get_event
        db = await _get_db()
        try:
            row = await get_event(db, event_id)
        finally:
            from .persistence.db import close_db
            await close_db()

        if not row:
            typer.echo(f"Event {event_id} not found.", err=True)
            sys.exit(1)

        if as_json:
            _print_json(dict(row))
            return

        typer.echo(f"\nEvent:    {row['id']}")
        typer.echo(f"Kind:     {row['kind']}  |  Severity: {row['severity']}")
        typer.echo(f"Status:   {row['status']}")
        typer.echo(f"Symbol:   {row.get('symbol') or '—'}")
        typer.echo(f"Title:    {row['title']}")
        typer.echo(f"\nSummary:\n{row['summary']}\n")
        typer.echo(f"Default action: {row['default_action']}")
        typer.echo(f"Expires:  {str(row.get('expires_at',''))[:19]}")
        ctx = row.get("context") or {}
        if ctx:
            typer.echo(f"\nContext:\n{json.dumps(ctx, indent=2, default=str)}")
        opts = row.get("options") or []
        if opts:
            typer.echo("\nOptions:")
            for o in opts:
                typer.echo(f"  --action {o['action']:20s}  {o.get('label','')}")

    asyncio.run(_run())


# ── decide ────────────────────────────────────────────────────────────────────

@app.command()
def decide(
    event_id: str,
    action: Annotated[str, typer.Option("--action", help="approve|reject|hold or any option action")],
    param: Annotated[Optional[list[str]], typer.Option("--param", help="k=v pairs stored with the resolution")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Resolve an ambiguous_decision event (approve/reject/hold)."""

    async def _run() -> None:
        from .events.queue import get_event, resolve_event
        db = await _get_db()
        try:
            row = await get_event(db, event_id)
            if not row:
                typer.echo(f"Event {event_id} not found.", err=True)
                sys.exit(1)
            if row["kind"] not in ("ambiguous_decision", "market_event"):
                typer.echo(f"Event is kind='{row['kind']}'. Use `bybit resolve` for other kinds.")
                sys.exit(1)
            extra: dict[str, str] = {}
            for kv in (param or []):
                k, _, v = kv.partition("=")
                extra[k.strip()] = v.strip()
            # Skill major-update: 'approve' triggers an immediate refresh.
            ctx = row.get("context") or {}
            if ctx.get("check") == "skill_freshness" and action == "approve":
                typer.echo("Running `bybit skill refresh`...")
                from .skill.version_checker import refresh as _vr
                result = await _vr()
                typer.echo(f"✅ Refreshed to v{result['version']} ({len(result['updated'])} modules)")
            await resolve_event(db, event_id, action=action, params=extra)
        finally:
            from .persistence.db import close_db
            await close_db()

        result = {"event_id": event_id, "action": action, "params": extra}
        if as_json:
            _print_json(result)
        else:
            typer.echo(f"✅ Event {event_id} resolved — action={action}")

    asyncio.run(_run())


# ── resolve ───────────────────────────────────────────────────────────────────

@app.command()
def resolve(
    event_id: str,
    action: Annotated[str, typer.Option("--action")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Generic event resolution (scheduled_review, risk_escalation, market_event)."""

    async def _run() -> None:
        from .events.queue import resolve_event
        db = await _get_db()
        try:
            await resolve_event(db, event_id, action=action)
        finally:
            from .persistence.db import close_db
            await close_db()

        result = {"event_id": event_id, "action": action}
        if as_json:
            _print_json(result)
        else:
            typer.echo(f"✅ Event {event_id} resolved — action={action}")

    asyncio.run(_run())


# ── tune ──────────────────────────────────────────────────────────────────────

@app.command()
def tune(
    set_kv: Annotated[Optional[list[str]], typer.Option("--set", help="KEY=VALUE")] = None,
    list_params: Annotated[bool, typer.Option("--list")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate and write numeric params to agent_config (read by loop each cycle)."""
    from .config.tuning import PARAM_WHITELIST, validate_param

    async def _run() -> None:
        db = await _get_db()
        try:
            if list_params or not set_kv:
                current = await db.fetch("SELECT key, value FROM agent_config ORDER BY key")
                cur_map = {r["key"]: r["value"] for r in current}
                rows = []
                for name, spec in PARAM_WHITELIST.items():
                    cur_val = cur_map.get(spec["db_key"])
                    rows.append({
                        "param": name,
                        "db_key": spec["db_key"],
                        "type": spec["type"],
                        "min": spec["min"],
                        "max": spec["max"],
                        "current": cur_val,
                        "desc": spec["desc"],
                    })
                if as_json:
                    _print_json(rows)
                else:
                    typer.echo(f"{'PARAM':22s}  {'CURRENT':>12s}  {'RANGE'}")
                    for r in rows:
                        cur = f"{r['current']}" if r["current"] is not None else "(default)"
                        typer.echo(f"{r['param']:22s}  {cur:>12s}  [{r['min']}, {r['max']}]  {r['desc']}")
                return

            applied = []
            for kv in set_kv:
                key, _, raw = kv.partition("=")
                key = key.strip()
                raw = raw.strip()
                try:
                    value, db_key = validate_param(key, raw)
                except ValueError as e:
                    typer.echo(f"❌ {e}", err=True)
                    sys.exit(1)

                await db.execute(
                    """INSERT INTO agent_config (key, value, description, updated_at, updated_by)
                       VALUES ($1, $2, $3, now(), 'cli-agent')
                       ON CONFLICT (key) DO UPDATE
                         SET value = $2, updated_at = now(), updated_by = 'cli-agent'""",
                    db_key,
                    float(value),
                    PARAM_WHITELIST[key]["desc"],
                )
                applied.append({"param": key, "db_key": db_key, "value": value})
                typer.echo(f"✅ {key} ({db_key}) = {value}")

            if as_json:
                _print_json(applied)
        finally:
            from .persistence.db import close_db
            await close_db()

    asyncio.run(_run())


# ── weights ───────────────────────────────────────────────────────────────────

@app.command()
def weights(
    set_kv: Annotated[Optional[list[str]], typer.Option("--set", help="strategy=weight (0.0–2.0)")] = None,
    toggle: Annotated[Optional[str], typer.Option("--toggle", help="strategy name to enable/disable")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show or adjust strategy weights (read by DecisionEngine each cycle)."""
    from .config.tuning import STRATEGY_NAMES

    async def _run() -> None:
        db = await _get_db()
        try:
            if toggle:
                rows = await db.fetch(
                    "SELECT enabled FROM strategy_weights WHERE strategy = $1", toggle
                )
                if not rows:
                    typer.echo(f"❌ Unknown strategy '{toggle}'. Known: {', '.join(STRATEGY_NAMES)}", err=True)
                    sys.exit(1)
                new_enabled = not rows[0]["enabled"]
                await db.execute(
                    "UPDATE strategy_weights SET enabled = $1, updated_at = now() WHERE strategy = $2",
                    new_enabled, toggle,
                )
                typer.echo(f"✅ {toggle} {'enabled' if new_enabled else 'disabled'}")
                return

            if set_kv:
                for kv in set_kv:
                    strategy, _, raw = kv.partition("=")
                    strategy = strategy.strip()
                    raw = raw.strip()
                    if strategy not in STRATEGY_NAMES:
                        typer.echo(f"❌ Unknown strategy '{strategy}'. Known: {', '.join(STRATEGY_NAMES)}", err=True)
                        sys.exit(1)
                    try:
                        w = float(raw)
                    except ValueError:
                        typer.echo(f"❌ Weight must be a float (got '{raw}')", err=True)
                        sys.exit(1)
                    if not (0.0 <= w <= 2.0):
                        typer.echo(f"❌ Weight must be in [0.0, 2.0] (got {w})", err=True)
                        sys.exit(1)
                    await db.execute(
                        "UPDATE strategy_weights SET weight = $1, updated_at = now() WHERE strategy = $2",
                        w, strategy,
                    )
                    typer.echo(f"✅ {strategy} weight = {w}")
                return

            # Default: list current weights.
            rows = await db.fetch(
                "SELECT strategy, weight, enabled FROM strategy_weights ORDER BY strategy"
            )
            if as_json:
                _print_json([dict(r) for r in rows])
            else:
                for r in rows:
                    state = "enabled " if r["enabled"] else "disabled"
                    typer.echo(f"  {r['strategy']:22s}  weight={r['weight']:<6}  {state}")
        finally:
            from .persistence.db import close_db
            await close_db()

    asyncio.run(_run())


# ── review ────────────────────────────────────────────────────────────────────

@app.command()
def review() -> None:
    """Run an immediate SelfReview pass (Phase 5 wires the full implementation)."""

    async def _run() -> None:
        try:
            from .review.self_review import SelfReview
            sr = SelfReview()
            db = await _get_db()
            try:
                await sr.tick(cycle_id=None, db=db)
                typer.echo("✅ SelfReview pass complete.")
            finally:
                from .persistence.db import close_db
                await close_db()
        except ImportError:
            typer.echo("SelfReview not yet implemented (Phase 5). Stub only.")

    asyncio.run(_run())


# ── pause / resume / kill ─────────────────────────────────────────────────────

@app.command()
def pause() -> None:
    """Set agent status = 'paused' — the loop will skip cycles until resumed."""

    async def _run() -> None:
        db = await _get_db()
        try:
            await db.execute(
                "UPDATE agent_state SET status = 'paused', updated_at = now() WHERE id = 'singleton'"
            )
        finally:
            from .persistence.db import close_db
            await close_db()
        typer.echo("✅ Agent paused. Use `bybit resume` to restart cycles.")

    asyncio.run(_run())


@app.command()
def resume() -> None:
    """Set agent status = 'running' — resumes the trading loop."""

    async def _run() -> None:
        db = await _get_db()
        try:
            await db.execute(
                "UPDATE agent_state SET status = 'running', updated_at = now() WHERE id = 'singleton'"
            )
        finally:
            from .persistence.db import close_db
            await close_db()
        typer.echo("✅ Agent resumed.")

    asyncio.run(_run())


@app.command()
def cutover(
    mode: Annotated[str, typer.Argument(help="shadow | testnet_live | mainnet_live")],
    force: Annotated[bool, typer.Option("--force", help="Skip confirmation for mainnet_live")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Switch the trading execution mode at runtime (no restart needed).

    \b
    shadow       — paper mode, no real orders (safe default)
    testnet_live — real orders on testnet; fills feed the promotion gate
    mainnet_live — real orders on mainnet (real money)

    The loop reads this every cycle so the change takes effect within 60 seconds.
    """
    _VALID = {"shadow", "testnet_live", "mainnet_live"}
    if mode not in _VALID:
        typer.echo(f"❌ Invalid mode '{mode}'. Choose: {', '.join(sorted(_VALID))}", err=True)
        raise typer.Exit(1)

    if mode == "mainnet_live" and not force:
        typer.echo(
            "\n⚠️  MAINNET LIVE MODE — real money will be at risk on the next cycle.\n"
            "   Ensure you have:\n"
            "   • A mainnet Bybit API key (Read + Trade only — NO Withdraw)\n"
            "   • Sufficient capital and tested strategy weights\n"
            "   • Telegram alerts configured to monitor risk events\n"
        )
        if not typer.confirm("Proceed to mainnet_live?", default=False):
            typer.echo("Aborted.")
            raise typer.Exit(0)

    async def _run() -> None:
        db = await _get_db()
        current_rows = await db.fetch(
            "SELECT trading_mode FROM agent_state WHERE id = 'singleton' LIMIT 1"
        )
        current = (current_rows[0].get("trading_mode") if current_rows else None) or "shadow"
        new_env = "mainnet" if mode == "mainnet_live" else "testnet"
        await db.execute(
            """UPDATE agent_state
               SET trading_mode = $1, env = $2, updated_at = now()
               WHERE id = 'singleton'""",
            mode, new_env,
        )
        try:
            from .control.brain import add_note
            await add_note(db, f"Trading mode switched: {current} → {mode}", category="directive")
        except Exception:
            pass
        if as_json:
            _print_json({"previous": current, "current": mode})
        else:
            icons = {"shadow": "🔵", "testnet_live": "🟡", "mainnet_live": "🟢"}
            typer.echo(
                f"{icons.get(mode, '⚙️')}  Trading mode: {current} → {mode}\n"
                f"   Takes effect within one loop cycle (~60 s)."
            )
        if mode == "mainnet_live":
            typer.echo("🔴 REAL MONEY IS NOW LIVE. Monitor Telegram alerts closely.")

    asyncio.run(_run())


@app.command()
def kill(
    reason: Annotated[str, typer.Option("--reason", help="Reason for the kill")] = "Manual CLI kill",
    force: Annotated[bool, typer.Option("--force", help="Skip confirmation prompt")] = False,
) -> None:
    """Engage the kill switch: cancel all orders, flatten all positions, halt loop."""
    if not force:
        confirm = typer.confirm(
            "⚠️  This will cancel ALL orders and flatten ALL positions. Continue?",
            default=False,
        )
        if not confirm:
            typer.echo("Aborted.")
            return

    async def _run() -> None:
        from .config.env import get_env
        from .control.kill_switch import KillSwitch
        from .core.errors import KillSwitchError
        from .exchange.bybit_client import BybitClient
        from .exchange.credentials import resolve_bybit_auth

        env = get_env()
        auth = resolve_bybit_auth(env)
        client = BybitClient(auth, is_testnet=await _is_testnet())
        db = await _get_db()
        ks = KillSwitch(client, db)
        try:
            await ks.engage(reason)
        except KillSwitchError:
            pass  # expected — kill switch always raises after completing
        finally:
            from .persistence.db import close_db
            await client.aclose()
            await close_db()
        typer.echo("🔴 Kill switch engaged. All orders cancelled, all positions flattened.")

    asyncio.run(_run())


# ── watch ─────────────────────────────────────────────────────────────────────

@app.command()
def watch(
    interval: Annotated[int, typer.Option("--interval", help="Seconds between checks")] = 90,
    restart: Annotated[bool, typer.Option("--restart/--no-restart", help="Auto-restart dead process")] = True,
    as_json: Annotated[bool, typer.Option("--json", help="Emit one JSON object per tick")] = False,
) -> None:
    """Watchdog: health-check every N seconds, auto-restart if dead, emit structured status.

    Any agent can run this to become the on-call doctor for the bot.
    One output line per tick — tail it, pipe it, or read --json for automation.
    Ctrl-C to stop.
    """
    import subprocess
    from datetime import datetime, timezone

    _LOG = Path("/tmp/bybit-run.log")
    _bot_proc: list[subprocess.Popen] = [None]  # mutable cell so inner funcs can rebind

    def _any_running() -> bool:
        r = subprocess.run(["pgrep", "-f", "bybit run"], capture_output=True)
        return r.returncode == 0

    def _is_alive() -> bool:
        p = _bot_proc[0]
        if p is not None and p.poll() is None:
            return True
        return _any_running()

    def _spawn() -> None:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        fh = open(_LOG, "a")  # noqa: SIM115 — intentionally left open for subprocess lifetime
        _bot_proc[0] = subprocess.Popen(["bybit", "run"], stdout=fh, stderr=fh)

    async def _tick() -> dict:
        from .events.queue import list_events
        from .control.doctor import Doctor

        db = await _get_db()
        try:
            ag_rows   = await db.fetch("SELECT * FROM agent_state WHERE id = 'singleton' LIMIT 1")
            snap_rows = await db.fetch(
                "SELECT total_equity, drawdown_pct, open_positions, ts FROM equity_snapshots ORDER BY ts DESC LIMIT 1"
            )
            ev_rows   = await list_events(db, status="pending", limit=200)

            # The Doctor runs while the DB is open: it auto-fixes deterministic
            # faults (dead process, legacy killed-state) and escalates judgment
            # calls (kill engaged, signal drought, drawdown) as dedup'd events.
            doc = Doctor(db, process_alive=_is_alive(), restart_fn=(_spawn if restart else None))
            report = await doc.treat()
        finally:
            from .persistence.db import close_db
            await close_db()

        ag   = ag_rows[0]   if ag_rows   else {}
        snap = snap_rows[0] if snap_rows else {}
        now  = datetime.now(timezone.utc)

        cycle_age_s: int | None = None
        last_at = ag.get("last_cycle_at")
        if last_at:
            try:
                raw = str(last_at)
                dt  = datetime.fromisoformat(raw) if "+" in raw else datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
                cycle_age_s = int((now - dt).total_seconds())
            except Exception:
                pass

        return {
            "ts":             now.strftime("%H:%M:%S"),
            "env":            ag.get("env",    "unknown"),
            "status":         ag.get("status", "unknown"),
            "kill_engaged":   bool(ag.get("kill_engaged", False)),
            "kill_reason":    ag.get("kill_reason"),
            "equity":         round(float(snap.get("total_equity") or 0), 4),
            "drawdown_pct":   round(float(snap.get("drawdown_pct") or 0) * 100, 3),
            "open_positions": snap.get("open_positions", 0),
            "promo_cycles":   ag.get("promotion_cycle_count", 0),
            "events_pending": len(ev_rows),
            "cycle_age_s":    cycle_age_s,
            "last_cycle_at":  str(last_at or ""),
            "doctor":         report.to_dict(),
        }

    async def _loop() -> None:
        typer.echo("bybit watch — doctor mode (auto-fix + escalate). Press Ctrl-C to stop.\n")
        prev_promo = 0
        prev_cycle = ""

        while True:
            t0 = asyncio.get_event_loop().time()

            # DB tick — the Doctor runs inside it (auto-fix dead process, escalate
            # judgment calls). It owns restart now, so no separate liveness logic here.
            try:
                d = await _tick()
            except Exception as exc:
                msg = {"ts": datetime.now().strftime("%H:%M:%S"), "error": str(exc)}
                typer.echo(json.dumps(msg) if as_json else f"[{msg['ts']}] ERROR — {exc}")
                await asyncio.sleep(interval)
                continue

            report = d.get("doctor", {})
            findings = report.get("findings", [])

            # Flags — Doctor findings drive the headline; legacy flags kept for parity.
            flags: list[str] = []
            for f in findings:
                if f.get("auto_fixed"):
                    flags.append(f"FIXED:{f['check']}")
                elif f.get("escalated"):
                    flags.append(f"ESCALATED:{f['check']}")
            if d["kill_engaged"]:                            flags.append(f"KILL({d['kill_reason']})")
            if d["cycle_age_s"] is not None and d["cycle_age_s"] > 180:
                                                             flags.append(f"STALE({d['cycle_age_s']}s)")
            if d["drawdown_pct"] > 0:                        flags.append(f"DD={d['drawdown_pct']}%")
            if d["events_pending"] > 0:                      flags.append(f"EVENTS={d['events_pending']}")
            if d["last_cycle_at"] != prev_cycle:             flags.append("NEW_CYCLE")
            if d["promo_cycles"] > prev_promo:               flags.append(f"PROMO={d['promo_cycles']}")

            prev_cycle = d["last_cycle_at"]
            prev_promo = d["promo_cycles"]
            d["flags"]         = flags
            d["process_alive"] = _is_alive()

            # Emit
            if as_json:
                typer.echo(json.dumps(d, default=str))
            else:
                flag_str = "  ".join(flags)
                health = report.get("worst_severity", "ok")
                typer.echo(
                    f"[{d['ts']}]  {d['env']}/{d['status']}"
                    f"  eq=${d['equity']:.4f}"
                    f"  pos={d['open_positions']}"
                    f"  promo={d['promo_cycles']}"
                    f"  age={d['cycle_age_s']}s"
                    f"  health={health}"
                    + (f"  {flag_str}" if flag_str else "")
                )
                # Print actionable doctor findings inline so the on-call agent sees the fix.
                for f in findings:
                    if f.get("escalated") or (f.get("severity") in ("warn", "critical") and not f.get("auto_fixed")):
                        rec = f.get("recommendation")
                        typer.echo(f"           ⚕ {f['check']} [{f['severity']}]: {f['detail']}")
                        if rec:
                            typer.echo(f"             → {rec}")
                    elif f.get("auto_fixed"):
                        typer.echo(f"           ✓ {f['check']}: {f.get('fix_action')}")

            elapsed = asyncio.get_event_loop().time() - t0
            await asyncio.sleep(max(0.0, interval - elapsed))

    try:
        asyncio.run(_loop())
    except KeyboardInterrupt:
        typer.echo("\nbybit watch stopped.")


# ── strategy management (the "strategy agent" surface) ─────────────────────────

strategy_app = typer.Typer(add_completion=False, help="Manage strategies: create, edit, backtest, accept, pause.")
app.add_typer(strategy_app, name="strategy")


def _bt_klines(symbol: str, interval: str, bars: int) -> dict:
    """Fetch klines and convert to the OHLCV the backtester consumes."""
    from .config.env import get_env
    from .exchange.bybit_client import BybitClient
    from .exchange.credentials import resolve_bybit_auth
    from .market.market_data import klines_to_ohlcv

    async def _run():
        env = get_env()
        client = BybitClient(resolve_bybit_auth(env), is_testnet=(env.BYBIT_ENV == "testnet"))
        try:
            kl = await client.get_kline("linear", symbol, interval, min(bars, 1000))
        finally:
            await client.aclose()
        return klines_to_ohlcv(kl)

    return asyncio.run(_run())


@strategy_app.command("list")
def strategy_list(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List all strategies and their lifecycle state."""
    async def _run():
        from .strategy.registry import list_strategies
        db = await _get_db()
        try:
            rows = await list_strategies(db)
        finally:
            from .persistence.db import close_db
            await close_db()
        return rows

    rows = asyncio.run(_run())
    if as_json:
        _print_json([dict(r) for r in rows])
        return
    typer.echo(f"\n{'name':<20} {'base':<16} {'status':<11} {'on':<4} {'weight':<7} backtest")
    typer.echo("-" * 78)
    for r in rows:
        bt = r.get("backtest")
        if isinstance(bt, str):
            try:
                bt = json.loads(bt)
            except Exception:
                bt = None
        bt_str = "—"
        if bt:
            mark = "✓PASS" if bt.get("passed") else "✗FAIL"
            bt_str = f"{mark} ret={bt.get('total_return_pct')}% win={bt.get('win_rate')} n={bt.get('n_trades')}"
        typer.echo(f"{r['name']:<20} {r['base_strategy']:<16} {r['status']:<11} "
                   f"{'yes' if r['enabled'] else 'no':<4} {float(r['weight']):<7.2f} {bt_str}")


@strategy_app.command("show")
def strategy_show(name: str, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show one strategy's full config + last backtest."""
    async def _run():
        from .strategy.registry import get_strategy
        db = await _get_db()
        try:
            return await get_strategy(db, name)
        finally:
            from .persistence.db import close_db
            await close_db()

    s = asyncio.run(_run())
    if not s:
        typer.echo(f"Strategy '{name}' not found."); raise typer.Exit(1)
    _print_json(dict(s)) if as_json else _print_json(dict(s))


@strategy_app.command("create")
def strategy_create(
    name: str,
    base: Annotated[str, typer.Option("--base", help="Base strategy class key")],
    params: Annotated[str, typer.Option("--params", help='JSON params, e.g. \'{"threshold":0.2}\'')] = "{}",
    weight: Annotated[float, typer.Option("--weight")] = 1.0,
    notes: Annotated[Optional[str], typer.Option("--notes")] = None,
) -> None:
    """Create a new (draft) strategy. Must be backtested + accepted before it trades."""
    async def _run():
        from .strategy.registry import create_strategy, StrategyError
        db = await _get_db()
        try:
            await create_strategy(db, name=name, base_strategy=base,
                                  params=json.loads(params), weight=weight, notes=notes)
        finally:
            from .persistence.db import close_db
            await close_db()

    try:
        asyncio.run(_run())
    except Exception as e:
        typer.echo(f"❌ {e}"); raise typer.Exit(1)
    typer.echo(f"✅ Created draft strategy '{name}'. Next: bybit strategy backtest {name}")


@strategy_app.command("edit")
def strategy_edit(
    name: str,
    params: Annotated[Optional[str], typer.Option("--params", help="New JSON params (resets to draft)")] = None,
    weight: Annotated[Optional[float], typer.Option("--weight")] = None,
    notes: Annotated[Optional[str], typer.Option("--notes")] = None,
) -> None:
    """Edit a strategy. Changing --params invalidates its backtest (back to draft)."""
    async def _run():
        from .strategy.registry import update_strategy
        db = await _get_db()
        try:
            await update_strategy(db, name,
                                  params=json.loads(params) if params else None,
                                  weight=weight, notes=notes)
        finally:
            from .persistence.db import close_db
            await close_db()

    try:
        asyncio.run(_run())
    except Exception as e:
        typer.echo(f"❌ {e}"); raise typer.Exit(1)
    typer.echo(f"✅ Updated '{name}'." + (" Params changed — re-backtest required." if params else ""))


@strategy_app.command("delete")
def strategy_delete(
    name: str,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Delete a strategy from the registry."""
    if not force and not typer.confirm(f"Delete strategy '{name}'?", default=False):
        typer.echo("Aborted."); return

    async def _run():
        from .strategy.registry import delete_strategy
        db = await _get_db()
        try:
            await delete_strategy(db, name)
        finally:
            from .persistence.db import close_db
            await close_db()

    try:
        asyncio.run(_run())
    except Exception as e:
        typer.echo(f"❌ {e}"); raise typer.Exit(1)
    typer.echo(f"🗑️  Deleted '{name}'.")


@strategy_app.command("backtest")
def strategy_backtest(
    name: str,
    symbol: Annotated[str, typer.Option("--symbol")] = "BTCUSDT",
    interval: Annotated[str, typer.Option("--interval", help="Kline interval (e.g. 15, 60)")] = "15",
    bars: Annotated[int, typer.Option("--bars")] = 500,
    accept_if_pass: Annotated[bool, typer.Option("--accept", help="Auto-accept if it passes")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Backtest a strategy over historical klines and record the verdict."""
    from .strategy.backtest import backtest_strategy, BacktestConfig
    from .strategy.registry import instantiate, record_backtest, accept, get_strategy

    ohlcv = _bt_klines(symbol, interval, bars)

    async def _run():
        db = await _get_db()
        try:
            s = await get_strategy(db, name)
            if not s:
                return {"error": f"Strategy '{name}' not found."}
            params = s.get("params") or {}
            if isinstance(params, str):
                params = json.loads(params)
            inst = instantiate(s["base_strategy"], params, name=name)
            result = backtest_strategy(inst, ohlcv, symbol=symbol, cfg=BacktestConfig())
            await record_backtest(db, name, result.metrics(), result.passed)
            accepted = False
            if result.passed and accept_if_pass:
                await accept(db, name)
                accepted = True
            return {"metrics": result.metrics(), "accepted": accepted}
        finally:
            from .persistence.db import close_db
            await close_db()

    out = asyncio.run(_run())
    if "error" in out:
        typer.echo(f"❌ {out['error']}"); raise typer.Exit(1)
    if as_json:
        _print_json(out); return
    m = out["metrics"]
    verdict = "✓ PASSED" if m["passed"] else "✗ FAILED"
    typer.echo(f"\n── BACKTEST {name} on {symbol} ({bars} × {interval}m) ──")
    typer.echo(f"  trades={m['n_trades']}  win={m['win_rate']:.0%}  return={m['total_return_pct']:+.2f}%")
    typer.echo(f"  profit_factor={m['profit_factor']}  max_dd={m['max_drawdown_pct']:.1f}%  sharpe={m['sharpe_like']}")
    typer.echo(f"  {verdict}")
    if m["fail_reasons"]:
        for fr in m["fail_reasons"]:
            typer.echo(f"    • {fr}")
    if out["accepted"]:
        typer.echo(f"  ✅ Auto-accepted — '{name}' is now live (enabled).")
    elif m["passed"]:
        typer.echo(f"  → Passed. Accept with: bybit strategy accept {name}")


@strategy_app.command("accept")
def strategy_accept(name: str) -> None:
    """Accept a strategy (enable for trading). Requires a passing backtest."""
    async def _run():
        from .strategy.registry import accept
        db = await _get_db()
        try:
            await accept(db, name)
        finally:
            from .persistence.db import close_db
            await close_db()

    try:
        asyncio.run(_run())
    except Exception as e:
        typer.echo(f"❌ {e}"); raise typer.Exit(1)
    typer.echo(f"✅ Accepted '{name}' — now eligible to trade.")


@strategy_app.command("reject")
def strategy_reject(name: str) -> None:
    """Reject a strategy (disable, mark rejected)."""
    async def _run():
        from .strategy.registry import reject
        db = await _get_db()
        try:
            await reject(db, name)
        finally:
            from .persistence.db import close_db
            await close_db()
    try:
        asyncio.run(_run())
    except Exception as e:
        typer.echo(f"❌ {e}"); raise typer.Exit(1)
    typer.echo(f"🚫 Rejected '{name}'.")


@strategy_app.command("pause")
def strategy_pause(name: str) -> None:
    """Pause a strategy (stop trading it, keep it accepted)."""
    async def _run():
        from .strategy.registry import pause
        db = await _get_db()
        try:
            await pause(db, name)
        finally:
            from .persistence.db import close_db
            await close_db()
    try:
        asyncio.run(_run())
    except Exception as e:
        typer.echo(f"❌ {e}"); raise typer.Exit(1)
    typer.echo(f"⏸️  Paused '{name}'.")


@strategy_app.command("resume")
def strategy_resume(name: str) -> None:
    """Resume a paused strategy (must be accepted)."""
    async def _run():
        from .strategy.registry import resume
        db = await _get_db()
        try:
            await resume(db, name)
        finally:
            from .persistence.db import close_db
            await close_db()
    try:
        asyncio.run(_run())
    except Exception as e:
        typer.echo(f"❌ {e}"); raise typer.Exit(1)
    typer.echo(f"▶️  Resumed '{name}'.")


@strategy_app.command("bases")
def strategy_bases() -> None:
    """List the base strategy classes available to instantiate."""
    from .strategy.registry import BASE_STRATEGIES
    typer.echo("Available base strategies:")
    for k, cls in sorted(BASE_STRATEGIES.items()):
        regimes = ", ".join(getattr(cls, "suitable_regimes", []))
        typer.echo(f"  {k:<18} suitable_regimes=[{regimes}]")


# ── executions ────────────────────────────────────────────────────────────────

@app.command()
def executions(
    symbol: Annotated[Optional[str], typer.Option("--symbol")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 50,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show recent trade executions (fills) from the exchange."""

    async def _run() -> None:
        from .config.env import get_env
        from .exchange.bybit_client import BybitClient
        from .exchange.credentials import resolve_bybit_auth

        env = get_env()
        auth = resolve_bybit_auth(env)
        client = BybitClient(auth, is_testnet=await _is_testnet())
        try:
            fills = await client.get_executions("linear", symbol=symbol, limit=limit)
        finally:
            await client.aclose()

        if as_json:
            _print_json(fills)
            return
        if not fills:
            typer.echo("No recent executions.")
            return
        typer.echo(f"\n{'symbol':<12} {'side':<5} {'qty':>10} {'price':>12} {'fee':>10}  time")
        for f in fills:
            typer.echo(
                f"{f.get('symbol','?'):<12} {f.get('side','?'):<5}"
                f" {f.get('execQty','?'):>10} {f.get('execPrice','?'):>12}"
                f" {f.get('execFee','?'):>10}  {str(f.get('execTime',''))[:16]}"
            )

    asyncio.run(_run())


# ── algo ──────────────────────────────────────────────────────────────────────

algo_app = typer.Typer(add_completion=False, help="Algo (TWAP/Iceberg) order management.")
app.add_typer(algo_app, name="algo")


@algo_app.command("list")
def algo_list(
    symbol: Annotated[Optional[str], typer.Option("--symbol")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List active algo (TWAP/Iceberg/Chase/POV) orders."""

    async def _run() -> None:
        from .config.env import get_env
        from .exchange.bybit_client import BybitClient
        from .exchange.credentials import resolve_bybit_auth

        env = get_env()
        auth = resolve_bybit_auth(env)
        client = BybitClient(auth, is_testnet=await _is_testnet())
        try:
            orders = await client.list_algo_orders("UTA_USDT", symbol=symbol)
        finally:
            await client.aclose()

        if as_json:
            _print_json(orders)
            return
        if not orders:
            typer.echo("No active algo orders.")
            return
        typer.echo(f"\n{'algoOrderId':<20} {'symbol':<12} {'side':<5} {'type':<10} {'qty':>10}  status")
        for o in orders:
            typer.echo(
                f"{str(o.get('algoOrderId','?')):<20} {o.get('symbol','?'):<12}"
                f" {o.get('side','?'):<5} {o.get('orderType','?'):<10}"
                f" {str(o.get('qty','?')):>10}  {o.get('status','?')}"
            )

    asyncio.run(_run())


# ── skill ─────────────────────────────────────────────────────────────────────

skill_app = typer.Typer(add_completion=False, help="Official Bybit Exchange AI skill hub.")
app.add_typer(skill_app, name="skill")

_SKILLS_DIR = Path(__file__).parent.parent / "skills"
_MODULES_DIR = _SKILLS_DIR / "modules"

_MODULE_DESCRIPTIONS = {
    "market":       "Klines, tickers, open interest, L/S ratio, historical volatility",
    "derivatives":  "Perpetual futures orders, positions, order history",
    "spot":         "Spot orders, margin trading",
    "account":      "Balances, transfers, sub-accounts, unified account",
    "trading-bot":  "Spot/futures grid bots, DCA bot, martingale",
    "strategy":     "TWAP, Iceberg, Chase, POV algorithmic execution orders",
    "copy-trading": "Leader discovery, follower binding, copy settings",
    "earn":         "Savings, staking, liquidity mining, flexible products",
    "advanced":     "WebSocket streams, institutional loans, RFQ block trades",
    "alpha-trade":  "DEX token swaps and on-chain token access",
    "fiat":         "P2P trading, fiat conversion, bank transfers",
    "tradfi":       "Tokenised equities, commodities, MT5 copy trading",
}


@skill_app.command("list")
def skill_list(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List all embedded official Bybit skill modules."""
    version = (_SKILLS_DIR / "VERSION").read_text().strip() if (_SKILLS_DIR / "VERSION").exists() else "unknown"
    modules = []
    for name, desc in _MODULE_DESCRIPTIONS.items():
        path = _MODULES_DIR / f"{name}.md"
        modules.append({
            "module": name,
            "description": desc,
            "size_bytes": path.stat().st_size if path.exists() else 0,
            "available": path.exists(),
        })
    if as_json:
        _print_json({"version": version, "modules": modules})
        return
    typer.echo(f"Official Bybit Exchange skill hub  (embedded v{version})\n")
    typer.echo(f"  {'MODULE':<16}  {'DESCRIPTION'}")
    typer.echo("  " + "-" * 68)
    for m in modules:
        mark = "✓" if m["available"] else "✗"
        typer.echo(f"  {mark} {m['module']:<14}  {m['description']}")
    typer.echo(f"\nUse: bybit skill show <module>  |  bybit skill refresh")


@skill_app.command("show")
def skill_show(
    module: str,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Print an official Bybit skill module (API reference for that topic)."""
    path = _MODULES_DIR / f"{module}.md"
    if not path.exists():
        names = ", ".join(_MODULE_DESCRIPTIONS.keys())
        typer.echo(f"❌ Module '{module}' not found. Available: {names}", err=True)
        raise typer.Exit(1)
    content = path.read_text()
    if as_json:
        _print_json({"module": module, "content": content})
    else:
        typer.echo(content)


@skill_app.command("version")
def skill_version(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show embedded skill version and check GitHub for the latest release."""
    from .skill.version_checker import check as _vc

    info = asyncio.run(_vc())
    if as_json:
        _print_json(info)
        return
    embedded = info["embedded"]
    latest = info.get("latest") or "unavailable"
    bump = info["bump"]
    status = {
        "current":  "✅ up to date",
        "patch":    f"🔄 patch update → v{latest}",
        "minor":    f"🔄 minor update → v{latest}",
        "major":    f"⚠️  MAJOR update → v{latest} (review before refreshing)",
        "unknown":  "❓ latest version unavailable (network)",
    }.get(bump, bump)
    typer.echo(f"Embedded:  v{embedded}")
    typer.echo(f"Latest:    v{latest}")
    typer.echo(f"Status:    {status}")
    if bump in ("patch", "minor"):
        typer.echo("Run `bybit skill refresh` to update.")


@skill_app.command("refresh")
def skill_refresh(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Fetch latest official Bybit skill modules from GitHub and update skills/."""
    from .skill.version_checker import refresh as _vr

    result = asyncio.run(_vr())
    if as_json:
        _print_json(result)
        return
    typer.echo(f"✅ Refreshed {len(result['updated'])} modules (v{result['version']})")
    if result["errors"]:
        typer.echo("⚠️  Errors:")
        for e in result["errors"]:
            typer.echo(f"   {e}")


@app.command()
def pl(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show real-time P&L for all open positions (live and paper)."""

    async def _run() -> None:
        from .config.env import get_env
        from .exchange.bybit_client import BybitClient
        from .exchange.credentials import resolve_bybit_auth

        env = get_env()
        auth = resolve_bybit_auth(env)
        client = BybitClient(auth, is_testnet=await _is_testnet())
        try:
            pos_list = await client.get_positions("linear")
            open_pos = [p for p in pos_list if float(p.get("size", 0)) > 0]
        finally:
            await client.aclose()

        db = await _get_db()
        paper_rows = await db.fetch(
            "SELECT symbol, side, qty, entry_price, stop_price, tp_price, strategy, leverage "
            "FROM paper_positions ORDER BY symbol"
        ) if db else []

        total_upnl = 0.0
        rows_out = []

        for p in open_pos:
            upnl = float(p.get("unrealisedPnl") or 0)
            total_upnl += upnl
            rows_out.append({
                "symbol": p["symbol"], "side": p["side"],
                "qty": p.get("size"), "entry": p.get("avgPrice"),
                "mark": p.get("markPrice"), "uPnL": upnl,
                "sl": p.get("stopLoss"), "tp": p.get("takeProfit"),
                "mode": "live",
            })

        for r in paper_rows:
            rows_out.append({
                "symbol": r["symbol"], "side": r["side"],
                "qty": r["qty"], "entry": r["entry_price"],
                "mark": "—", "uPnL": 0.0,
                "sl": r.get("stop_price"), "tp": r.get("tp_price"),
                "mode": "paper",
            })

        if as_json:
            _print_json({"positions": rows_out, "total_uPnL": total_upnl})
            return

        if not rows_out:
            typer.echo("No open positions.")
            return
        typer.echo(f"{'Symbol':12s}  {'Side':4s}  {'Mode':5s}  {'Qty':>10s}  {'Entry':>10s}"
                   f"  {'Mark':>10s}  {'uPnL':>10s}  {'SL':>10s}  {'TP':>10s}")
        typer.echo("─" * 110)
        for p in rows_out:
            upnl = p["uPnL"]
            sign = "+" if upnl >= 0 else ""
            typer.echo(
                f"{p['symbol']:12s}  {p['side']:4s}  {p['mode']:5s}  {str(p['qty'] or ''):>10s}"
                f"  {str(p['entry'] or ''):>10s}  {str(p['mark']):>10s}"
                f"  {sign}{upnl:.4f}  {str(p['sl'] or ''):>10s}  {str(p['tp'] or ''):>10s}"
            )
        typer.echo("─" * 110)
        typer.echo(f"Total unrealised P&L (live): {'+' if total_upnl >= 0 else ''}{total_upnl:.4f}")

    asyncio.run(_run())


@app.command()
def signal(
    symbol: Annotated[str, typer.Argument(help="e.g. BTCUSDT")],
    category: Annotated[str, typer.Option("--category")] = "linear",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run the full signal pipeline for one symbol and print decisions (no execution)."""

    async def _run() -> None:
        import uuid as _uuid
        from .config.env import get_env
        from .exchange.bybit_client import BybitClient
        from .exchange.credentials import resolve_bybit_auth
        from .market.market_data import MarketDataService
        from .strategy.decision_engine import DecisionEngine

        env = get_env()
        auth = resolve_bybit_auth(env)
        client = BybitClient(auth, is_testnet=await _is_testnet())
        try:
            svc = MarketDataService(client)
            snap = await svc.get_snapshot(symbol.upper(), category)
        finally:
            await client.aclose()

        if not snap:
            typer.echo(f"❌ No market data for {symbol}", err=True)
            raise typer.Exit(1)

        engine = DecisionEngine(paper=True)
        cycle_id = str(_uuid.uuid4())
        signals = await engine.run([snap], cycle_id)

        if as_json:
            _print_json([
                {
                    "symbol": s.symbol,
                    "action": s.action,
                    "strategy": s.strategy,
                    "confidence": s.confidence,
                    "compositeScore": s.compositeScore,
                    "rationale": s.rationale,
                    "suggestedEntry": s.suggestedEntry,
                    "suggestedStop": s.suggestedStop,
                    "suggestedTp": s.suggestedTp,
                }
                for s in signals
            ])
            return

        if not signals:
            typer.echo(f"No actionable signals for {symbol} (all below confidence floor).")
            return
        for s in signals:
            typer.echo(
                f"[{s.strategy}]  {s.action}  confidence={s.confidence:.3f}"
                f"  score={s.compositeScore:.3f}"
                f"  entry={s.suggestedEntry}  sl={s.suggestedStop}  tp={s.suggestedTp}"
            )
            typer.echo(f"  rationale: {s.rationale}")

    asyncio.run(_run())


@app.command()
def train(
    window: Annotated[int, typer.Option("--window", help="Days of history to train on")] = 30,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Trigger a full batch retrain of learned_signals from trade history."""

    async def _run() -> None:
        from .ml.learner import retrain_from_history
        db = await _get_db()
        if not db:
            typer.echo("❌ DB unavailable", err=True)
            raise typer.Exit(1)
        result = await retrain_from_history(db, window_days=window)
        if as_json:
            _print_json(result)
            return
        typer.echo(f"✅ Retrain complete: {result['updated']} signal groups updated"
                   f" from {result['total_trades']} trades ({window}d window)")
        if result.get("error"):
            typer.echo(f"⚠️  Error: {result['error']}", err=True)

    asyncio.run(_run())


@app.command()
def prune(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be deleted without deleting")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Delete old DB rows to keep the system lean (safe; never touches trades/orders/model state).

    Retention: decision_log 7d · equity_snapshots 30d · discovered_markets 3d ·
    market_sentiment 30d · correlation_snapshots 7d · pnl_attribution 90d ·
    resolved events 30d · brain_notes archived after 90d (not deleted).

    Runs automatically once a week inside the trading loop.
    """

    async def _run() -> None:
        from .maintenance.pruner import prune as _prune, _RETENTION, _PENDING_EVENT_RETENTION_DAYS

        db = await _get_db()
        if not db:
            typer.echo("❌ DB unavailable", err=True)
            raise typer.Exit(1)

        if dry_run:
            typer.echo("🔍 Dry-run — estimating rows that would be deleted:\n")
            total = 0
            for table, ts_col, days in _RETENTION:
                try:
                    r = await db.fetch(
                        f"SELECT count(*) c FROM {table} "
                        f"WHERE {ts_col} < now() - INTERVAL '{days} days'"
                    )
                    c = int(r[0].get("c") or 0) if r else 0
                    total += c
                    typer.echo(f"  {table:30s}  {c:>6} rows  (>{days}d old)")
                except Exception as e:
                    typer.echo(f"  {table:30s}  ERROR: {e}")
            typer.echo(f"\n  Total would delete: {total} rows")
            return

        result = await _prune(db)
        if as_json:
            _print_json({
                "deleted": result.deleted,
                "total_deleted": result.total_deleted,
                "archived_notes": result.archived_notes,
                "errors": result.errors,
                "ran_at": result.ran_at,
            })
            return

        if result.total_deleted or result.archived_notes:
            typer.echo(f"✅ Pruned {result.total_deleted} rows, archived {result.archived_notes} brain notes")
            for table, count in result.deleted.items():
                typer.echo(f"   {table}: -{count}")
        else:
            typer.echo("✅ Nothing to prune — DB is already clean.")
        if result.errors:
            for e in result.errors:
                typer.echo(f"  ⚠️  {e}", err=True)

    asyncio.run(_run())


@app.command()
def brain(
    note: Annotated[str, typer.Option("--note", help="Persist a lesson/decision to the DB ledger")] = "",
    category: Annotated[str, typer.Option("--category", help="lesson|decision|directive|observation")] = "lesson",
    show: Annotated[bool, typer.Option("--show", help="Print brain.md to stdout instead of writing")] = False,
    path: Annotated[str, typer.Option("--path")] = "brain.md",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Render the DB-backed brain.md memory ledger, or append a note to it.

    brain.md is rendered FROM Postgres (the single source of truth) — it is never
    parsed back into config. Use --note to persist a durable lesson/decision.
    """

    async def _run() -> None:
        from .control import brain as _brain

        db = await _get_db()
        if not db:
            typer.echo("❌ DB unavailable", err=True)
            raise typer.Exit(1)

        if note:
            await _brain.add_note(db, note, category=category)
            if as_json:
                _print_json({"added": True, "category": category, "note": note})
            else:
                typer.echo(f"🧠 Note saved to brain_notes [{category}].")
            return

        content = await _brain.render(db)
        if show:
            if as_json:
                _print_json({"content": content})
            else:
                typer.echo(content)
            return

        import os
        abspath = os.path.abspath(path)
        with open(abspath, "w", encoding="utf-8") as fh:
            fh.write(content)
        if as_json:
            _print_json({"written": abspath, "bytes": len(content)})
        else:
            typer.echo(f"🧠 brain.md rendered → {abspath} ({len(content)} bytes)")

    asyncio.run(_run())


if __name__ == "__main__":
    app()
