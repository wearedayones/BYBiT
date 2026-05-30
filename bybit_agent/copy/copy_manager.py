"""Copy trading manager — ports src/copy/CopyTradingManager.ts.

Reviews leader performance every 4h. Drops underperformers, follows new top leaders
up to MAX_LEADERS. In paper mode, records but doesn't call followLeader().
"""
from __future__ import annotations

import json
import time
from typing import Any

from bybit_agent.config.constants import LEADER_REVIEW_INTERVAL_MS
from bybit_agent.copy.leader_scoring import score_leaders
from bybit_agent.core.logger import get_logger
from bybit_agent.persistence.db import NeonHttpClient
from bybit_agent.risk.risk_manager import PortfolioState

log = get_logger().bind(module="copy-trading")

MAX_LEADERS = 3
DROP_THRESHOLD_SCORE = -0.1


def _investment_e8(usdt_amount: float) -> str:
    return str(int(usdt_amount * 1e8))


class CopyTradingManager:
    def __init__(self, client: Any, db: NeonHttpClient, is_paper: bool) -> None:
        self._client = client
        self._db = db
        self._is_paper = is_paper
        self._last_review = 0.0

    async def tick(self, portfolio: PortfolioState) -> None:
        now = time.time() * 1000
        if now - self._last_review < LEADER_REVIEW_INTERVAL_MS:
            return
        self._last_review = now
        try:
            await self._review_leaders(portfolio)
        except Exception as e:
            log.error("Copy trading review failed", error=str(e))

    async def _review_leaders(self, portfolio: PortfolioState) -> None:
        raw_leaders = await self._client.get_copy_leader_list()
        scored = score_leaders(raw_leaders)
        log.info("Leaders discovered", count=len(scored))

        for l in scored:
            await self._db.execute(
                """INSERT INTO copy_leaders (leader_mark, nickname, score, is_paper)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (leader_mark) DO UPDATE
                     SET score = $3, nickname = $2""",
                l.leader_mark, l.nickname, l.score, self._is_paper,
            )
            await self._db.execute(
                """INSERT INTO copy_leader_performance
                     (copy_leader_id, roi, max_drawdown, sharpe, score)
                   SELECT id, $1, $2, $3, $4
                   FROM copy_leaders WHERE leader_mark = $5""",
                l.roi, l.max_drawdown, l.sharpe, l.score, l.leader_mark,
            )

        # Drop underperformers.
        following = await self._db.fetch(
            "SELECT id, leader_mark, score FROM copy_leaders WHERE status = 'following' AND is_paper = $1",
            self._is_paper,
        )
        for ldr in following:
            fresh = next((s for s in scored if s.leader_mark == ldr["leader_mark"]), None)
            if not fresh or fresh.score < DROP_THRESHOLD_SCORE:
                await self._db.execute(
                    "UPDATE copy_leaders SET status = 'dropped', dropped_at = now(), drop_reason = 'score_decay' WHERE id = $1",
                    ldr["id"],
                )
                log.info("Dropped underperforming leader", leader_mark=ldr["leader_mark"])

        # Follow new top leaders if slots remain.
        current = await self._db.fetch(
            "SELECT id FROM copy_leaders WHERE status = 'following' AND is_paper = $1",
            self._is_paper,
        )
        slots = MAX_LEADERS - len(current)
        if slots <= 0:
            return

        per_leader = portfolio.equity * 0.05
        if per_leader < 1:
            return

        following_marks = {r["leader_mark"] for r in following}
        for l in scored[:MAX_LEADERS]:
            if slots <= 0:
                break
            if l.leader_mark in following_marks:
                continue
            if not self._is_paper:
                await self._client.follow_leader(l.leader_mark, _investment_e8(per_leader))
            await self._db.execute(
                "UPDATE copy_leaders SET status = 'following', followed_at = now() WHERE leader_mark = $1",
                l.leader_mark,
            )
            log.info("Following new leader", leader_mark=l.leader_mark, score=round(l.score, 3))
            slots -= 1
