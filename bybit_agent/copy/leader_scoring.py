"""Leader scoring — ports src/copy/leaderScoring.ts."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LeaderScore:
    leader_mark: str
    nickname: str
    score: float
    roi: float
    max_drawdown: float
    sharpe: float


def score_leaders(leaders: list[dict]) -> list[LeaderScore]:
    """Score and rank copy-trading leaders. Filters out >30% max drawdown."""
    scored = []
    for l in leaders:
        roi = float(l.get("roi") or 0)
        max_dd = float(l.get("maxDrawdown") or 0)
        sharpe = float(l.get("sharpeRatio") or 0)
        score = roi * 0.4 + sharpe * 0.4 - max_dd * 0.2
        scored.append(LeaderScore(
            leader_mark=l.get("leaderMark", ""),
            nickname=l.get("nickName", ""),
            score=score, roi=roi, max_drawdown=max_dd, sharpe=sharpe,
        ))
    return sorted(
        (s for s in scored if s.max_drawdown < 0.30),
        key=lambda s: s.score, reverse=True,
    )
