import type { CopyLeaderInfo } from '../exchange/types';

export interface LeaderScore {
  leaderMark: string;
  nickname: string;
  score: number;
  roi: number;
  maxDrawdown: number;
  sharpe: number;
}

export function scoreLeaders(leaders: CopyLeaderInfo[]): LeaderScore[] {
  return leaders
    .map(l => {
      const roi = parseFloat(l.roi ?? '0');
      const maxDD = parseFloat(l.maxDrawdown ?? '0');
      const sharpe = parseFloat(l.sharpeRatio ?? '0');

      // Score formula: weight ROI positively, max drawdown and sharpe
      // Penalize high drawdown heavily
      const score = roi * 0.4 + sharpe * 0.4 - maxDD * 0.2;

      return { leaderMark: l.leaderMark, nickname: l.nickName, score, roi, maxDrawdown: maxDD, sharpe };
    })
    .filter(l => l.maxDrawdown < 0.30) // never follow leaders with >30% max DD
    .sort((a, b) => b.score - a.score);
}
