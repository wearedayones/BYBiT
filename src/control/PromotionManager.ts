import { getDb } from '../persistence/db';
import { childLogger } from '../core/logger';

const log = childLogger({ module: 'promotion' });

export interface PromotionCriteria {
  min_cycles: number;
  min_sharpe: number;
  min_win_rate: number;
  max_drawdown_pct: number;
}

export class PromotionManager {
  async evaluate(): Promise<boolean> {
    const sql = getDb();
    const ag = await sql`SELECT * FROM agent_state WHERE id = 'singleton' LIMIT 1`.then(r => r[0]);
    if (!ag || ag.env === 'mainnet') return false;

    const criteria: PromotionCriteria = ag.promotion_criteria ?? {
      min_cycles: 72, min_sharpe: 0.5, min_win_rate: 0.50, max_drawdown_pct: 0.05,
    };

    const cycleCount = parseInt(ag.promotion_cycle_count ?? '0');
    if (cycleCount < criteria.min_cycles) {
      await sql`UPDATE agent_state SET promotion_cycle_count = promotion_cycle_count + 1 WHERE id = 'singleton'`;
      return false;
    }

    // Check performance metrics from equity_snapshots and trades
    const [tradeStats, equityStats] = await Promise.all([
      sql<{ win_rate: number; total_pnl: number; count: number }[]>`
        SELECT AVG(CASE WHEN realized_pnl > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
               SUM(realized_pnl) as total_pnl, COUNT(*) as count
        FROM trades WHERE is_paper = false AND closed_at > now() - INTERVAL '3 days'
      `,
      sql<{ min_drawdown: number; avg_equity: number; stddev_equity: number }[]>`
        SELECT MIN(drawdown_pct) as min_drawdown,
               AVG(total_equity) as avg_equity,
               STDDEV(total_equity) as stddev_equity
        FROM equity_snapshots WHERE ts > now() - INTERVAL '3 days' AND env = 'testnet'
      `,
    ]);

    const trades = tradeStats[0];
    const equity = equityStats[0];
    if (!trades || trades.count < 10) {
      log.debug('Not enough trades for promotion evaluation');
      await sql`UPDATE agent_state SET promotion_cycle_count = promotion_cycle_count + 1 WHERE id = 'singleton'`;
      return false;
    }

    const maxDrawdown = Math.abs(equity?.min_drawdown ?? 0);
    const avgEquity = equity?.avg_equity ?? 1;
    const stddev = equity?.stddev_equity ?? 0;
    const dailyReturn = trades.total_pnl / (avgEquity || 1) / 3;
    const sharpe = stddev > 0 ? (dailyReturn / (stddev / (avgEquity || 1))) : 0;

    log.info({ winRate: trades.win_rate, sharpe, maxDrawdown, criteria }, 'Promotion evaluation');

    if (
      trades.win_rate >= criteria.min_win_rate &&
      sharpe >= criteria.min_sharpe &&
      maxDrawdown <= criteria.max_drawdown_pct
    ) {
      await sql`
        UPDATE agent_state SET env = 'mainnet', last_promoted_at = now(), updated_at = now()
        WHERE id = 'singleton'
      `;
      log.info({ winRate: trades.win_rate, sharpe }, '🚀 PROMOTED TO MAINNET');
      return true;
    }

    await sql`UPDATE agent_state SET promotion_cycle_count = promotion_cycle_count + 1 WHERE id = 'singleton'`;
    return false;
  }
}
