import { getDb } from '../persistence/db';
import { childLogger } from '../core/logger';

const log = childLogger({ module: 'self-review' });

const WEIGHT_ALPHA = 0.2;
const MIN_WEIGHT = 0.2;
const MAX_WEIGHT = 2.5;
const MIN_TRADES_FOR_REVIEW = 10;
const DRAWDOWN_DISABLE_THRESHOLD = -0.05;

export class SelfReview {
  private lastDeepReview = 0;

  async tick(cycleId: string): Promise<void> {
    await this.quickWeightUpdate();
    const now = Date.now();
    if (now - this.lastDeepReview > 24 * 60 * 60 * 1000) {
      await this.deepReview();
      this.lastDeepReview = now;
    }
  }

  private async quickWeightUpdate(): Promise<void> {
    const sql = getDb();
    try {
      const stats = await sql<{
        strategy: string; realized_pnl: number; count: number; win_rate: number;
      }[]>`
        SELECT strategy,
          SUM(realized_pnl) as realized_pnl,
          COUNT(*) as count,
          AVG(CASE WHEN realized_pnl > 0 THEN 1.0 ELSE 0.0 END) as win_rate
        FROM trades
        WHERE closed_at > now() - INTERVAL '7 days'
          AND strategy IS NOT NULL AND is_paper = false
        GROUP BY strategy
      `;

      if (stats.length < 1) return;

      const totalPnl = stats.reduce((s, r) => s + (r.realized_pnl ?? 0), 0);

      for (const row of stats) {
        if (row.count < MIN_TRADES_FOR_REVIEW) continue;

        const currentWeight = await sql<{ weight: number }[]>`
          SELECT weight FROM strategy_weights WHERE strategy = ${row.strategy}
        `.then(r => r[0]?.weight ?? 1.0);

        // Softmax-like update: good pnl relative to total → increase weight
        const relativePerf = totalPnl !== 0 ? (row.realized_pnl ?? 0) / Math.abs(totalPnl) : 0;
        const normalizedScore = 0.5 + relativePerf * 0.5;
        const newWeight = Math.max(MIN_WEIGHT, Math.min(MAX_WEIGHT,
          currentWeight * (1 - WEIGHT_ALPHA) + normalizedScore * WEIGHT_ALPHA * 2,
        ));

        await sql`
          UPDATE strategy_weights
          SET weight = ${newWeight},
              realized_pnl = ${row.realized_pnl ?? 0},
              trades_count = ${row.count},
              win_rate = ${row.win_rate},
              last_adjusted_at = now()
          WHERE strategy = ${row.strategy}
        `;

        // Disable strategy if pnl is deeply negative
        if ((row.realized_pnl ?? 0) / Math.abs(totalPnl || 1) < DRAWDOWN_DISABLE_THRESHOLD) {
          await sql`
            UPDATE strategy_weights
            SET enabled = false, cooldown_until = now() + INTERVAL '24 hours'
            WHERE strategy = ${row.strategy}
          `;
          log.warn({ strategy: row.strategy }, 'Strategy disabled due to negative expectancy');
        }
      }

      // Re-enable strategies past their cooldown
      await sql`
        UPDATE strategy_weights
        SET enabled = true, cooldown_until = null
        WHERE cooldown_until IS NOT NULL AND cooldown_until < now()
      `;
    } catch (e) {
      log.error({ e }, 'Weight update failed');
    }
  }

  private async deepReview(): Promise<void> {
    log.info('Running deep review');
    const sql = getDb();
    try {
      // Update learned signals from completed decision_log + trades
      const rows = await sql<{
        strategy: string; regime: string; action: string;
        win_rate: number; avg_pnl: number; count: number;
      }[]>`
        SELECT d.strategy, d.regime, d.action,
          AVG(CASE WHEN t.realized_pnl > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
          AVG(t.realized_pnl) as avg_pnl,
          COUNT(*) as count
        FROM decision_log d
        JOIN trades t ON t.decision_id = d.id
        WHERE d.ts > now() - INTERVAL '30 days'
          AND d.strategy IS NOT NULL AND d.regime IS NOT NULL
          AND t.closed_at IS NOT NULL AND t.is_paper = false
        GROUP BY d.strategy, d.regime, d.action
        HAVING COUNT(*) >= 5
      `;

      for (const row of rows) {
        const hash = `${row.strategy}:${row.regime}:${row.action}`;
        await sql`
          INSERT INTO learned_signals (signal_hash, strategy, regime, n_trades, win_rate, avg_pnl, last_seen)
          VALUES (${hash}, ${row.strategy}, ${row.regime}, ${row.count}, ${row.win_rate}, ${row.avg_pnl}, now())
          ON CONFLICT (signal_hash, strategy) DO UPDATE SET
            n_trades = ${row.count}, win_rate = ${row.win_rate},
            avg_pnl = ${row.avg_pnl}, last_seen = now(), last_updated = now()
        `;
      }
      log.info({ rows: rows.length }, 'Learned signals updated');
    } catch (e) {
      log.error({ e }, 'Deep review failed');
    }
  }
}
