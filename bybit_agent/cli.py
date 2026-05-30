"""The ``bybit`` CLI — the universal surface any AI agent uses to operate the skill.

All commands talk to Postgres directly and work as a short-lived process independent
of the running ``bybit run`` service. Never places orders; the loop alone does that.

Commands:
  migrate      Apply all migrations/*.sql (idempotent).
  doctor       Validate env, DB connectivity, JSONB round-trip.
  run          Launch the 24/7 trading service.
  status       Show agent_state summary.
  positions    List open positions.
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

    return ok


@app.command()
def doctor() -> None:
    """Validate env, DB connectivity over 443, and a JSONB round-trip."""
    ok = asyncio.run(_doctor())
    typer.echo("")
    if ok:
        typer.echo("All checks passed.")
    else:
        typer.echo("Some checks failed — see above.")
        sys.exit(1)


# ── run ──────────────────────────────────────────────────────────────────────

@app.command()
def run(
    testnet: Annotated[bool, typer.Option("--testnet/--mainnet", help="Override env setting.")] = True,
) -> None:
    """Launch the 24/7 deterministic trading service."""
    from .service import start_service
    asyncio.run(start_service(testnet=testnet))


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
        data = {
            "env": ag.get("env", "unknown"),
            "status": ag.get("status", "unknown"),
            "kill_engaged": ag.get("kill_engaged", False),
            "kill_reason": ag.get("kill_reason"),
            "equity": float(snap.get("total_equity") or 0),
            "drawdown_pct": round(float(snap.get("drawdown_pct") or 0) * 100, 2),
            "open_positions": snap.get("open_positions", 0),
            "last_cycle_at": str(ag.get("last_cycle_at", "")),
            "promotion_cycle_count": ag.get("promotion_cycle_count", 0),
        }
        if as_json:
            _print_json(data)
        else:
            typer.echo(f"env:              {data['env']}")
            typer.echo(f"status:           {data['status']}")
            typer.echo(f"kill_engaged:     {data['kill_engaged']}"
                       + (f"  ({data['kill_reason']})" if data["kill_reason"] else ""))
            typer.echo(f"equity:           ${data['equity']:,.2f}")
            typer.echo(f"drawdown:         {data['drawdown_pct']:.2f}%")
            typer.echo(f"open_positions:   {data['open_positions']}")
            typer.echo(f"last_cycle_at:    {data['last_cycle_at']}")
            typer.echo(f"promotion_cycles: {data['promotion_cycle_count']}")

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
        client = BybitClient(auth, is_testnet=(env.BYBIT_ENV == "testnet"))
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

    async def _run() -> None:
        db = await _get_db()
        try:
            # Equity curve summary
            snap_rows = await db.fetch(
                f"""SELECT total_equity, drawdown_pct, ts
                    FROM equity_snapshots
                    WHERE ts > now() - INTERVAL '1 {period}'
                    ORDER BY ts"""
            )
            # Trade summary
            trade_rows = await db.fetch(
                f"""SELECT strategy, side, realized_pnl, closed_at
                    FROM trades
                    WHERE closed_at > now() - INTERVAL '1 {period}'
                      AND is_paper = false
                    ORDER BY closed_at DESC LIMIT 100"""
            )
            # Decision log sample
            decision_rows = await db.fetch(
                f"""SELECT symbol, strategy, action, approved, composite_score, is_paper, created_at
                    FROM decision_log
                    WHERE created_at > now() - INTERVAL '1 {period}'
                    ORDER BY created_at DESC LIMIT 20"""
            )
            # Current weights
            weight_rows = await db.fetch(
                "SELECT strategy, weight, enabled FROM strategy_weights ORDER BY strategy"
            )
            # Agent config overrides
            config_rows = await db.fetch(
                "SELECT key, value, description FROM agent_config ORDER BY key"
            )
        finally:
            from .persistence.db import close_db
            await close_db()

        wins = sum(1 for t in trade_rows if float(t.get("realized_pnl") or 0) > 0)
        total = len(trade_rows)
        total_pnl = sum(float(t.get("realized_pnl") or 0) for t in trade_rows)
        equities = [float(r["total_equity"]) for r in snap_rows if r.get("total_equity")]
        max_dd = max((float(r.get("drawdown_pct") or 0) for r in snap_rows), default=0)

        data = {
            "period": period,
            "trade_count": total,
            "win_rate": round(wins / total, 4) if total else None,
            "total_pnl": round(total_pnl, 4),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "equity_start": round(equities[0], 2) if equities else None,
            "equity_end": round(equities[-1], 2) if equities else None,
            "trades": [dict(t) for t in trade_rows[:10]],
            "recent_decisions": [dict(d) for d in decision_rows],
            "strategy_weights": [dict(w) for w in weight_rows],
            "agent_config_overrides": [dict(c) for c in config_rows],
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
            if row["kind"] != "ambiguous_decision":
                typer.echo(f"Event is kind='{row['kind']}', not 'ambiguous_decision'. Use `bybit resolve`.")
                sys.exit(1)
            extra: dict[str, str] = {}
            for kv in (param or []):
                k, _, v = kv.partition("=")
                extra[k.strip()] = v.strip()
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
        client = BybitClient(auth, is_testnet=(env.BYBIT_ENV == "testnet"))
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


if __name__ == "__main__":
    app()
