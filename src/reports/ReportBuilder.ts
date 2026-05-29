import { getDb } from '../persistence/db';
import { sendReport } from './emailer';
import { childLogger } from '../core/logger';

const log = childLogger({ module: 'report-builder' });

export async function buildAndSendReport(period: 'daily' | 'weekly' | 'monthly'): Promise<void> {
  const sql = getDb();
  const intervalMap = { daily: '1 day', weekly: '7 days', monthly: '30 days' };
  const interval = intervalMap[period];
  const now = new Date();

  try {
    const [trades, equity, riskEvents, botPerf, leaders] = await Promise.all([
      sql<{ strategy: string; pnl: number; wins: number; count: number }[]>`
        SELECT strategy,
          ROUND(SUM(realized_pnl)::numeric, 2) as pnl,
          SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
          COUNT(*) as count
        FROM trades
        WHERE closed_at > now() - ${interval}::interval
          AND strategy IS NOT NULL AND strategy != 'unknown'
        GROUP BY strategy ORDER BY pnl DESC
      `,
      sql<{ total_equity: number; drawdown_pct: number; ts: string }[]>`
        SELECT total_equity, drawdown_pct, ts
        FROM equity_snapshots
        WHERE ts > now() - ${interval}::interval
        ORDER BY ts ASC LIMIT 500
      `,
      sql<{ count: number; type: string }[]>`
        SELECT type, COUNT(*) as count FROM risk_events
        WHERE ts > now() - ${interval}::interval GROUP BY type
      `,
      sql<{ bot_type: string; realized_pnl: number; count: number }[]>`
        SELECT bi.bot_type, ROUND(SUM(bp.realized_pnl)::numeric, 2) as realized_pnl, COUNT(*) as count
        FROM bot_performance bp JOIN bot_instances bi ON bp.bot_instance_id = bi.id
        WHERE bp.ts > now() - ${interval}::interval GROUP BY bi.bot_type
      `,
      sql<{ nickname: string; score: number; our_realized_pnl: number }[]>`
        SELECT cl.nickname, cl.score, COALESCE(SUM(clp.our_realized_pnl),0) as our_realized_pnl
        FROM copy_leaders cl
        LEFT JOIN copy_leader_performance clp ON clp.copy_leader_id = cl.id
          AND clp.ts > now() - ${interval}::interval
        WHERE cl.status = 'following'
        GROUP BY cl.nickname, cl.score
        ORDER BY our_realized_pnl DESC
      `,
    ]);

    const totalPnl = trades.reduce((s, t) => s + (t.pnl ?? 0), 0);
    const latestEquity = equity[equity.length - 1]?.total_equity ?? 0;
    const maxDD = Math.max(...equity.map(e => e.drawdown_pct ?? 0), 0);

    const html = buildHtml({
      period, now, totalPnl, latestEquity, maxDD,
      trades, equity, riskEvents, botPerf, leaders,
    });

    const subject = `[BYBiT Agent] ${capitalize(period)} Report — ${now.toDateString()} | PnL: $${totalPnl.toFixed(2)}`;
    await sendReport(subject, html, period);
  } catch (e) {
    log.error({ e }, 'Report build failed');
  }
}

function buildHtml(data: {
  period: string; now: Date; totalPnl: number; latestEquity: number; maxDD: number;
  trades: { strategy: string; pnl: number; wins: number; count: number }[];
  equity: { total_equity: number; drawdown_pct: number; ts: string }[];
  riskEvents: { count: number; type: string }[];
  botPerf: { bot_type: string; realized_pnl: number; count: number }[];
  leaders: { nickname: string; score: number; our_realized_pnl: number }[];
}): string {
  const { period, now, totalPnl, latestEquity, maxDD, trades, riskEvents, botPerf, leaders } = data;
  const pnlColor = totalPnl >= 0 ? '#16a34a' : '#dc2626';

  const tradeRows = trades.map(t =>
    `<tr>
      <td>${t.strategy ?? '—'}</td>
      <td style="color:${(t.pnl ?? 0) >= 0 ? '#16a34a' : '#dc2626'}">$${(t.pnl ?? 0).toFixed(2)}</td>
      <td>${t.count}</td>
      <td>${t.count > 0 ? ((t.wins / t.count) * 100).toFixed(1) : 0}%</td>
    </tr>`
  ).join('');

  const botRows = botPerf.map(b =>
    `<tr><td>${b.bot_type}</td><td style="color:${(b.realized_pnl ?? 0) >= 0 ? '#16a34a' : '#dc2626'}">$${(b.realized_pnl ?? 0).toFixed(2)}</td><td>${b.count}</td></tr>`
  ).join('');

  const leaderRows = leaders.map(l =>
    `<tr><td>${l.nickname}</td><td>${l.score?.toFixed(2)}</td><td style="color:${(l.our_realized_pnl ?? 0) >= 0 ? '#16a34a' : '#dc2626'}">$${(l.our_realized_pnl ?? 0).toFixed(2)}</td></tr>`
  ).join('');

  const riskRows = riskEvents.map(r => `<tr><td>${r.type}</td><td>${r.count}</td></tr>`).join('');

  return `<!DOCTYPE html><html><body style="font-family:system-ui,sans-serif;background:#0f0f0f;color:#e5e5e5;margin:0;padding:24px">
<div style="max-width:680px;margin:auto">
  <h1 style="color:#fff;font-size:22px;border-bottom:2px solid #333;padding-bottom:12px">
    📊 BYBiT Agent — ${capitalize(period)} Report
    <span style="float:right;font-size:14px;color:#888">${now.toUTCString()}</span>
  </h1>

  <div style="display:flex;gap:16px;margin:20px 0">
    ${statCard('Total P&L', `$${totalPnl.toFixed(2)}`, pnlColor)}
    ${statCard('Equity', `$${latestEquity.toFixed(2)}`, '#60a5fa')}
    ${statCard('Max Drawdown', `${(maxDD * 100).toFixed(2)}%`, maxDD > 0.05 ? '#dc2626' : '#888')}
  </div>

  <h2 style="color:#ccc;font-size:16px">Strategy P&L</h2>
  <table width="100%" style="border-collapse:collapse;font-size:13px">
    <thead><tr style="color:#888;border-bottom:1px solid #333">
      <th align="left">Strategy</th><th align="left">P&L</th><th align="left">Trades</th><th align="left">Win%</th>
    </tr></thead>
    <tbody>${tradeRows || '<tr><td colspan="4" style="color:#555">No closed trades</td></tr>'}</tbody>
  </table>

  ${botPerf.length > 0 ? `
  <h2 style="color:#ccc;font-size:16px;margin-top:24px">Trading Bots</h2>
  <table width="100%" style="border-collapse:collapse;font-size:13px">
    <thead><tr style="color:#888;border-bottom:1px solid #333"><th align="left">Type</th><th align="left">P&L</th><th align="left">Updates</th></tr></thead>
    <tbody>${botRows}</tbody>
  </table>` : ''}

  ${leaders.length > 0 ? `
  <h2 style="color:#ccc;font-size:16px;margin-top:24px">Copy Trading Leaders</h2>
  <table width="100%" style="border-collapse:collapse;font-size:13px">
    <thead><tr style="color:#888;border-bottom:1px solid #333"><th align="left">Leader</th><th align="left">Score</th><th align="left">Our P&L</th></tr></thead>
    <tbody>${leaderRows}</tbody>
  </table>` : ''}

  ${riskEvents.length > 0 ? `
  <h2 style="color:#ccc;font-size:16px;margin-top:24px">Risk Events</h2>
  <table width="100%" style="border-collapse:collapse;font-size:13px">
    <thead><tr style="color:#888;border-bottom:1px solid #333"><th align="left">Type</th><th align="left">Count</th></tr></thead>
    <tbody>${riskRows}</tbody>
  </table>` : ''}

  <p style="color:#555;font-size:11px;margin-top:32px;border-top:1px solid #222;padding-top:12px">
    BYBiT Agent — Automated report. All decisions logged in Supabase.
  </p>
</div></body></html>`;
}

function statCard(label: string, value: string, color: string): string {
  return `<div style="background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:16px;flex:1;text-align:center">
    <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px">${label}</div>
    <div style="font-size:24px;font-weight:700;color:${color};margin-top:4px">${value}</div>
  </div>`;
}

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}
