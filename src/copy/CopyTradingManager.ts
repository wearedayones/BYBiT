import { BybitClient } from '../exchange/BybitClient';
import { getDb } from '../persistence/db';
import { childLogger } from '../core/logger';
import { scoreLeaders } from './leaderScoring';
import type { PortfolioState } from '../risk/RiskManager';

const log = childLogger({ module: 'copy-trading' });
const MAX_LEADERS = 3;
const DROP_THRESHOLD_SCORE = -0.1;
const investmentE8 = (usdtAmount: number) => String(Math.floor(usdtAmount * 1e8));

export class CopyTradingManager {
  private lastLeaderReview = 0;

  constructor(
    private readonly client: BybitClient,
    private readonly isPaper: boolean,
  ) {}

  async tick(portfolio: PortfolioState): Promise<void> {
    const now = Date.now();
    const LEADER_REVIEW_MS = 4 * 60 * 60 * 1000;
    if (now - this.lastLeaderReview < LEADER_REVIEW_MS) return;
    this.lastLeaderReview = now;

    try {
      await this.reviewLeaders(portfolio);
    } catch (e) {
      log.error({ e }, 'Copy trading review failed');
    }
  }

  private async reviewLeaders(portfolio: PortfolioState): Promise<void> {
    const sql = getDb();
    const rawLeaders = await this.client.getCopyLeaderList();
    const scored = scoreLeaders(rawLeaders);

    log.info({ count: scored.length }, 'Leaders discovered');

    // Persist/update candidates
    for (const l of scored) {
      await sql`
        INSERT INTO copy_leaders (leader_mark, nickname, score, is_paper)
        VALUES (${l.leaderMark}, ${l.nickname}, ${l.score}, ${this.isPaper})
        ON CONFLICT (leader_mark) DO UPDATE SET score = ${l.score}, nickname = ${l.nickname}
      `;
      await sql`
        INSERT INTO copy_leader_performance (copy_leader_id, roi, max_drawdown, sharpe, score)
        SELECT id, ${l.roi}, ${l.maxDrawdown}, ${l.sharpe}, ${l.score}
        FROM copy_leaders WHERE leader_mark = ${l.leaderMark}
      `;
    }

    // Drop underperformers
    const following = await sql<{ id: string; leader_mark: string; score: number }[]>`
      SELECT id, leader_mark, score FROM copy_leaders WHERE status = 'following' AND is_paper = ${this.isPaper}
    `;
    for (const ldr of following) {
      const fresh = scored.find(s => s.leaderMark === ldr.leader_mark);
      if (!fresh || fresh.score < DROP_THRESHOLD_SCORE) {
        await sql`UPDATE copy_leaders SET status = 'dropped', dropped_at = now(), drop_reason = 'score_decay' WHERE id = ${ldr.id}`;
        log.info({ leaderMark: ldr.leader_mark }, 'Dropped underperforming leader');
      }
    }

    // Follow new top leaders if below max
    const currentFollowing = await sql`SELECT id FROM copy_leaders WHERE status = 'following' AND is_paper = ${this.isPaper}`;
    let slots = MAX_LEADERS - currentFollowing.length;
    if (slots <= 0) return;

    const perLeaderInvestment = portfolio.equity * 0.05;
    if (perLeaderInvestment < 1) return;

    for (const l of scored.slice(0, MAX_LEADERS)) {
      if (slots <= 0) break;
      const already = following.find(f => f.leader_mark === l.leaderMark);
      if (already) continue;

      if (!this.isPaper) {
        await this.client.followLeader(l.leaderMark, investmentE8(perLeaderInvestment));
      }

      await sql`
        UPDATE copy_leaders SET status = 'following', followed_at = now(),
          investment_e8 = ${investmentE8(perLeaderInvestment)}
        WHERE leader_mark = ${l.leaderMark}
      `;
      log.info({ leaderMark: l.leaderMark, score: l.score, isPaper: this.isPaper }, 'Now following leader');
      slots--;
    }
  }
}
