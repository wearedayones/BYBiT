"""brain.md — DB-backed memory ledger renderer.

Design rule (do not break): Postgres is the single source of truth. brain.md is
RENDERED FROM the database — it is never parsed back into config. The 60-second
trading loop never reads or writes this file. Config still flows only through
`bybit tune` / `bybit weights` into agent_config / strategy_weights.

What this gives the AI agent:
  • a living, human-readable snapshot of account state, queue, weights, and learning
  • a durable lessons/decision ledger (brain_notes table) that survives container
    resets and is never lost
  • automatic handling of file growth: only the most recent notes are rendered;
    older ones stay in the DB and can be archived, so brain.md never balloons.

Public API:
  add_note(db, note, category=, tags=, cycle_id=)  -> persist a lesson/decision
  render(db)                                        -> markdown string
  write_file(db, path="brain.md")                   -> render + write, returns path
"""
from __future__ import annotations

from datetime import datetime, timezone

# How many recent notes per category to render inline. The rest live in the DB.
_RENDER_LIMIT = 15

_STATIC_HEADER = """# 🧠 BYBiT — AUTONOMOUS BRAIN & STATE LEDGER

> **This file is RENDERED from the database by `bybit brain`. Do not hand-edit for
> config** — it will be overwritten on the next render. Postgres is the single
> source of truth. To change behavior use `bybit tune` / `bybit weights`. To record
> a lesson use `bybit brain --note "…"` (it persists to the `brain_notes` table).
>
> Mission: grow capital while defending the daily drawdown limit. The deterministic
> Python loop trades 24/7; you (the AI) wake via the event queue to review, tune, and
> document — never in the hot path.
"""


async def add_note(
    db,
    note: str,
    *,
    category: str = "lesson",
    tags: list[str] | None = None,
    cycle_id: str | None = None,
) -> None:
    """Persist a lesson / decision / directive / observation to brain_notes."""
    await db.execute(
        """INSERT INTO brain_notes (category, note, tags, cycle_id)
           VALUES ($1, $2, $3, $4::uuid)""",
        category, note, tags, cycle_id,
    )


async def _safe_fetch(db, query: str, *args):
    try:
        return await db.fetch(query, *args)
    except Exception:
        return []


async def render(db) -> str:
    """Build the brain.md markdown from live DB state. Read-only."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts: list[str] = [_STATIC_HEADER, f"\n_Last rendered: {now}_\n", "\n---\n"]

    # ── 1. Account state ──────────────────────────────────────────────────────
    state_rows = await _safe_fetch(
        db,
        """SELECT env, status, kill_engaged, kill_reason, equity, peak_equity,
                  day_start_equity, daily_realized_pnl, max_risk_pct,
                  promotion_cycle_count, last_cycle_at
           FROM agent_state WHERE id = 'singleton' LIMIT 1""",
    )
    parts.append("## 🎯 1. Account State\n")
    if state_rows:
        s = state_rows[0]
        kill = "🔴 ENGAGED" if s.get("kill_engaged") else "🟢 clear"
        equity = float(s.get("equity") or 0)
        peak = float(s.get("peak_equity") or 0)
        dd = ((peak - equity) / peak) if peak > 0 else 0.0
        parts.append(
            f"| Field | Value |\n|---|---|\n"
            f"| Environment | `{s.get('env')}` |\n"
            f"| Status | `{s.get('status')}` |\n"
            f"| Kill switch | {kill} |\n"
            f"| Equity | `{equity:.4f}` |\n"
            f"| Peak equity | `{peak:.4f}` |\n"
            f"| Drawdown | `{dd:.2%}` |\n"
            f"| Daily realized P&L | `{s.get('daily_realized_pnl')}` |\n"
            f"| Max risk / trade | `{s.get('max_risk_pct')}` |\n"
            f"| Promotion cycles | `{s.get('promotion_cycle_count')}` |\n"
            f"| Last cycle | `{s.get('last_cycle_at')}` |\n"
        )
    else:
        parts.append("_agent_state unavailable._\n")

    # ── 2. Trade performance ──────────────────────────────────────────────────
    perf = await _safe_fetch(
        db,
        """SELECT count(*)::int n,
                  count(*) FILTER (WHERE realized_pnl > 0)::int wins,
                  COALESCE(sum(realized_pnl), 0) total_pnl,
                  count(*) FILTER (WHERE is_paper)::int paper
           FROM trades WHERE realized_pnl IS NOT NULL""",
    )
    parts.append("\n## 📊 2. Trade Performance (closed)\n")
    if perf and perf[0]["n"]:
        p = perf[0]
        wr = (p["wins"] / p["n"]) if p["n"] else 0.0
        parts.append(
            f"- Closed trades: **{p['n']}** ({p['paper']} paper)\n"
            f"- Win rate: **{wr:.1%}**\n"
            f"- Net realized P&L: **{float(p['total_pnl']):+.4f}**\n"
        )
    else:
        parts.append("_No closed trades yet — paper simulation will populate this._\n")

    # ── 3. Pending event queue ────────────────────────────────────────────────
    events = await _safe_fetch(
        db,
        """SELECT kind, severity, symbol, title, expires_at
           FROM pending_events WHERE status = 'pending'
           ORDER BY ts DESC LIMIT 10""",
    )
    parts.append("\n## 📥 3. Pending Event Queue\n")
    if events:
        parts.append("| Kind | Sev | Symbol | Title | Expires |\n|---|---|---|---|---|\n")
        for e in events:
            parts.append(
                f"| {e.get('kind')} | {e.get('severity')} | {e.get('symbol') or '—'} "
                f"| {e.get('title')} | {e.get('expires_at')} |\n"
            )
    else:
        parts.append("_Queue empty._\n")

    # ── 4. Strategy weights ───────────────────────────────────────────────────
    weights = await _safe_fetch(
        db,
        """SELECT strategy, weight, enabled, win_rate, rolling_win_48h, trades_count
           FROM strategy_weights ORDER BY weight DESC""",
    )
    parts.append("\n## ⚙️ 4. Live Strategy Weights\n")
    if weights:
        parts.append("| Strategy | Weight | On | Win rate | 48h win | Trades |\n|---|---|---|---|---|---|\n")
        for w in weights:
            on = "✅" if w.get("enabled") else "⛔"
            parts.append(
                f"| {w.get('strategy')} | {w.get('weight')} | {on} "
                f"| {w.get('win_rate')} | {w.get('rolling_win_48h')} | {w.get('trades_count')} |\n"
            )
    else:
        parts.append("_No strategy weights configured._\n")

    # ── 5. Tuned parameters (agent_config) ────────────────────────────────────
    cfg = await _safe_fetch(db, "SELECT key, value FROM agent_config ORDER BY key")
    parts.append("\n## 🔧 5. Tuned Parameters (agent_config)\n")
    if cfg:
        parts.append("| Key | Value |\n|---|---|\n")
        for c in cfg:
            parts.append(f"| `{c.get('key')}` | `{c.get('value')}` |\n")
    else:
        parts.append("_Defaults in use (no overrides)._\n")

    # ── 6. Learned priors ─────────────────────────────────────────────────────
    learned = await _safe_fetch(
        db,
        """SELECT strategy, regime, n_trades, win_rate, avg_pnl
           FROM learned_signals WHERE n_trades >= 1
           ORDER BY n_trades DESC LIMIT 10""",
    )
    parts.append("\n## 🎓 6. Learned Priors (top by sample size)\n")
    if learned:
        parts.append("| Strategy | Regime | n | Win rate | Avg P&L |\n|---|---|---|---|---|\n")
        for l in learned:
            parts.append(
                f"| {l.get('strategy')} | {l.get('regime')} | {l.get('n_trades')} "
                f"| {l.get('win_rate')} | {l.get('avg_pnl')} |\n"
            )
    else:
        parts.append("_No learned priors yet — accumulates as trades close._\n")

    # ── 7. Lessons & decision ledger (brain_notes) ────────────────────────────
    parts.append("\n## 🪵 7. Lessons & Decision Ledger\n")
    total_notes = await _safe_fetch(
        db, "SELECT count(*)::int n FROM brain_notes WHERE NOT archived"
    )
    n_total = total_notes[0]["n"] if total_notes else 0
    for cat, heading in [
        ("directive", "Active Directives"),
        ("lesson", "Learned Lessons"),
        ("decision", "Recent Decisions"),
        ("observation", "Observations"),
    ]:
        rows = await _safe_fetch(
            db,
            """SELECT created_at, note FROM brain_notes
               WHERE category = $1 AND NOT archived
               ORDER BY created_at DESC LIMIT $2""",
            cat, _RENDER_LIMIT,
        )
        if rows:
            parts.append(f"\n### {heading}\n")
            for r in rows:
                ts = str(r.get("created_at"))[:16]
                parts.append(f"- _{ts}_ — {r.get('note')}\n")
    if n_total > _RENDER_LIMIT * 4:
        parts.append(
            f"\n> ℹ️ {n_total} active notes in DB; showing most recent "
            f"{_RENDER_LIMIT} per category. Older notes remain queryable "
            f"(`bybit brain --note` history is never lost).\n"
        )
    if n_total == 0:
        parts.append("_No notes recorded yet. Add one with `bybit brain --note \"…\"`._\n")

    parts.append("\n---\n_Rendered from Postgres — edits here are not read by the loop._\n")
    return "".join(parts)


async def write_file(db, path: str = "brain.md") -> str:
    """Render and write brain.md to disk. Returns the absolute path written."""
    import os

    content = await render(db)
    abspath = os.path.abspath(path)
    with open(abspath, "w", encoding="utf-8") as fh:
        fh.write(content)
    return abspath
