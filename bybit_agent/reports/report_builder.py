"""Report builder — ports src/reports/ReportBuilder.ts.

Builds a structured HTML + Telegram summary for daily/weekly/monthly periods.
Falls back gracefully when any query fails (each is independent).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from bybit_agent.core.logger import get_logger
from bybit_agent.persistence.db import get_db
from bybit_agent.reports.emailer import send_report
from bybit_agent.reports.telegram import send_telegram, telegram_enabled

log = get_logger().bind(module="report-builder")

_INTERVAL = {"daily": "1 day", "weekly": "7 days", "monthly": "30 days"}


async def build_and_send_report(period: str) -> None:
    interval = _INTERVAL.get(period, "1 day")
    db = get_db()
    now = datetime.now(timezone.utc)

    async def _safe(coro):
        try:
            return await coro
        except Exception:
            return []

    trades, equity, risk_events, bot_perf, leaders = await asyncio.gather(
        _safe(db.fetch(
            f"""SELECT strategy,
                  SUM(realized_pnl)::text AS pnl,
                  SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END)::text AS wins,
                  COUNT(*)::text AS count
                FROM trades
                WHERE closed_at > now() - INTERVAL '{interval}'
                  AND strategy IS NOT NULL AND strategy != 'unknown'
                GROUP BY strategy ORDER BY SUM(realized_pnl) DESC NULLS LAST"""
        )),
        _safe(db.fetch(
            f"""SELECT total_equity::text, drawdown_pct::text, ts
                FROM equity_snapshots
                WHERE ts > now() - INTERVAL '{interval}'
                ORDER BY ts ASC LIMIT 500"""
        )),
        _safe(db.fetch(
            f"""SELECT type, COUNT(*)::text AS count FROM risk_events
                WHERE ts > now() - INTERVAL '{interval}' GROUP BY type"""
        )),
        _safe(db.fetch(
            f"""SELECT bi.bot_type,
                  COALESCE(SUM(bp.realized_pnl), 0)::text AS realized_pnl,
                  COUNT(*)::text AS count
                FROM bot_performance bp
                JOIN bot_instances bi ON bp.bot_instance_id = bi.id
                WHERE bp.ts > now() - INTERVAL '{interval}'
                GROUP BY bi.bot_type"""
        )),
        _safe(db.fetch(
            f"""SELECT cl.nickname, cl.score::text,
                  COALESCE(SUM(clp.our_realized_pnl), 0)::text AS our_realized_pnl
                FROM copy_leaders cl
                LEFT JOIN copy_leader_performance clp
                  ON clp.copy_leader_id = cl.id
                  AND clp.ts > now() - INTERVAL '{interval}'
                WHERE cl.status = 'following'
                GROUP BY cl.nickname, cl.score
                ORDER BY COALESCE(SUM(clp.our_realized_pnl), 0) DESC"""
        )),
    )

    # Coerce all string numbers (Neon returns NUMERIC as text).
    def _f(v) -> float: return float(v or 0)
    def _i(v) -> int:   return int(v or 0)

    trades_parsed = [
        {"strategy": r["strategy"], "pnl": _f(r["pnl"]),
         "wins": _i(r["wins"]), "count": _i(r["count"])}
        for r in trades
    ]
    equity_parsed = [
        {"total_equity": _f(r["total_equity"]), "drawdown_pct": _f(r["drawdown_pct"]), "ts": r["ts"]}
        for r in equity
    ]
    risk_parsed   = [{"type": r["type"], "count": _i(r["count"])} for r in risk_events]
    bot_parsed    = [{"bot_type": r["bot_type"], "pnl": _f(r["realized_pnl"]), "count": _i(r["count"])} for r in bot_perf]
    leader_parsed = [{"nickname": r["nickname"], "score": _f(r["score"]), "pnl": _f(r["our_realized_pnl"])} for r in leaders]

    total_pnl     = sum(t["pnl"] for t in trades_parsed)
    latest_equity = equity_parsed[-1]["total_equity"] if equity_parsed else 0.0
    max_dd        = max((e["drawdown_pct"] for e in equity_parsed), default=0.0)

    html = _build_html(period, now, total_pnl, latest_equity, max_dd,
                       trades_parsed, equity_parsed, risk_parsed, bot_parsed, leader_parsed)
    subject = (
        f"[BYBiT Agent] {period.capitalize()} Report — "
        f"{now.strftime('%a %b %d %Y')} | PnL: ${total_pnl:.2f}"
    )
    await send_report(subject, html, period, db)

    if telegram_enabled():
        lines = [
            f"📊 <b>{period.capitalize()} Report</b> — {now.strftime('%a %b %d %Y')}",
            f"💰 PnL: <b>${total_pnl:.2f}</b>",
            f"📈 Equity: ${latest_equity:.2f}",
            f"📉 Max DD: {max_dd * 100:.2f}%",
        ]
        if trades_parsed:
            lines += ["", "<b>Strategies</b>"]
            for t in trades_parsed[:8]:
                lines.append(f"• {t['strategy']}: ${t['pnl']:.2f} ({t['wins']}/{t['count']})")
        if risk_parsed:
            risk_str = ", ".join(f"{r['type']}×{r['count']}" for r in risk_parsed)
            lines.append(f"\n⚠️ Risk: {risk_str}")
        await send_telegram("\n".join(lines))


# ── HTML builder ──────────────────────────────────────────────────────────────

def _build_html(
    period: str, now: datetime, total_pnl: float, latest_equity: float, max_dd: float,
    trades: list[dict], equity: list[dict], risk_events: list[dict],
    bot_perf: list[dict], leaders: list[dict],
) -> str:
    pnl_color = "#16a34a" if total_pnl >= 0 else "#dc2626"

    trade_rows = "".join(
        f"<tr>"
        f"<td>{t['strategy'] or '—'}</td>"
        f"<td style='color:{('#16a34a' if t['pnl'] >= 0 else '#dc2626')}'>${t['pnl']:.2f}</td>"
        f"<td>{t['count']}</td>"
        f"<td>{(t['wins'] / t['count'] * 100):.1f}%</td>"
        f"</tr>"
        for t in trades
    ) or "<tr><td colspan='4' style='color:#555'>No closed trades</td></tr>"

    bot_rows = "".join(
        f"<tr>"
        f"<td>{b['bot_type']}</td>"
        f"<td style='color:{('#16a34a' if b['pnl'] >= 0 else '#dc2626')}'>${b['pnl']:.2f}</td>"
        f"<td>{b['count']}</td>"
        f"</tr>"
        for b in bot_perf
    )
    leader_rows = "".join(
        f"<tr>"
        f"<td>{l['nickname']}</td>"
        f"<td>{l['score']:.2f}</td>"
        f"<td style='color:{('#16a34a' if l['pnl'] >= 0 else '#dc2626')}'>${l['pnl']:.2f}</td>"
        f"</tr>"
        for l in leaders
    )
    risk_rows = "".join(
        f"<tr><td>{r['type']}</td><td>{r['count']}</td></tr>"
        for r in risk_events
    )

    def stat_card(label: str, value: str, color: str) -> str:
        return (
            f"<div style='background:#1a1a1a;border:1px solid #333;border-radius:8px;"
            f"padding:16px;flex:1;text-align:center'>"
            f"<div style='font-size:11px;color:#888;text-transform:uppercase;"
            f"letter-spacing:1px'>{label}</div>"
            f"<div style='font-size:24px;font-weight:700;color:{color};"
            f"margin-top:4px'>{value}</div></div>"
        )

    bot_section = (
        f"<h2 style='color:#ccc;font-size:16px;margin-top:24px'>Trading Bots</h2>"
        f"<table width='100%' style='border-collapse:collapse;font-size:13px'>"
        f"<thead><tr style='color:#888;border-bottom:1px solid #333'>"
        f"<th align='left'>Type</th><th align='left'>P&L</th><th align='left'>Updates</th></tr></thead>"
        f"<tbody>{bot_rows}</tbody></table>"
    ) if bot_perf else ""

    leader_section = (
        f"<h2 style='color:#ccc;font-size:16px;margin-top:24px'>Copy Trading Leaders</h2>"
        f"<table width='100%' style='border-collapse:collapse;font-size:13px'>"
        f"<thead><tr style='color:#888;border-bottom:1px solid #333'>"
        f"<th align='left'>Leader</th><th align='left'>Score</th><th align='left'>Our P&L</th></tr></thead>"
        f"<tbody>{leader_rows}</tbody></table>"
    ) if leaders else ""

    risk_section = (
        f"<h2 style='color:#ccc;font-size:16px;margin-top:24px'>Risk Events</h2>"
        f"<table width='100%' style='border-collapse:collapse;font-size:13px'>"
        f"<thead><tr style='color:#888;border-bottom:1px solid #333'>"
        f"<th align='left'>Type</th><th align='left'>Count</th></tr></thead>"
        f"<tbody>{risk_rows}</tbody></table>"
    ) if risk_events else ""

    return (
        f"<!DOCTYPE html><html><body style='font-family:system-ui,sans-serif;"
        f"background:#0f0f0f;color:#e5e5e5;margin:0;padding:24px'>"
        f"<div style='max-width:680px;margin:auto'>"
        f"<h1 style='color:#fff;font-size:22px;border-bottom:2px solid #333;padding-bottom:12px'>"
        f"📊 BYBiT Agent — {period.capitalize()} Report"
        f"<span style='float:right;font-size:14px;color:#888'>{now.strftime('%a, %d %b %Y %H:%M UTC')}</span>"
        f"</h1>"
        f"<div style='display:flex;gap:16px;margin:20px 0'>"
        f"{stat_card('Total P&L', f'${total_pnl:.2f}', pnl_color)}"
        f"{stat_card('Equity', f'${latest_equity:.2f}', '#60a5fa')}"
        f"{stat_card('Max Drawdown', f'{max_dd * 100:.2f}%', '#dc2626' if max_dd > 0.05 else '#888')}"
        f"</div>"
        f"<h2 style='color:#ccc;font-size:16px'>Strategy P&L</h2>"
        f"<table width='100%' style='border-collapse:collapse;font-size:13px'>"
        f"<thead><tr style='color:#888;border-bottom:1px solid #333'>"
        f"<th align='left'>Strategy</th><th align='left'>P&L</th>"
        f"<th align='left'>Trades</th><th align='left'>Win%</th></tr></thead>"
        f"<tbody>{trade_rows}</tbody></table>"
        f"{bot_section}{leader_section}{risk_section}"
        f"<p style='color:#555;font-size:11px;margin-top:32px;border-top:1px solid #222;"
        f"padding-top:12px'>BYBiT Agent — Automated report. All decisions logged in Supabase.</p>"
        f"</div></body></html>"
    )
