"""SelfReview — classical statistics-based strategy weight adaptation.

No LLM. Runs on closed trade history:
  - quickWeightUpdate (every cycle): exponential blend toward recent 7-day P&L ratio.
  - deepReview (every 24h): update learned_signals from decision_log JOIN trades.

Exact parity with src/review/SelfReview.ts.
"""
from __future__ import annotations

import time
from typing import Any

from bybit_agent.core.logger import get_logger
from bybit_agent.persistence.db import NeonHttpClient

log = get_logger().bind(module="self-review")

WEIGHT_ALPHA = 0.2
MIN_WEIGHT = 0.2
MAX_WEIGHT = 2.5
MIN_TRADES_FOR_REVIEW = 5
DRAWDOWN_DISABLE_THRESHOLD = -0.05
DEEP_REVIEW_INTERVAL_S = 24 * 60 * 60


class SelfReview:
    def __init__(self) -> None:
        self._last_deep_review = 0.0

    async def tick(self, cycle_id: str | None, db: NeonHttpClient) -> None:
        await self._quick_weight_update(db)
        now = time.time()
        if now - self._last_deep_review > DEEP_REVIEW_INTERVAL_S:
            await self._deep_review(db)
            self._last_deep_review = now

    async def _quick_weight_update(self, db: NeonHttpClient) -> None:
        try:
            stats = await db.fetch(
                """SELECT strategy,
                     SUM(realized_pnl)   AS realized_pnl,
                     COUNT(*)            AS count,
                     AVG(CASE WHEN realized_pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate
                   FROM trades
                   WHERE closed_at > now() - INTERVAL '7 days'
                     AND strategy IS NOT NULL AND strategy != 'unknown'
                   GROUP BY strategy"""
            )
            if not stats:
                return

            # Coerce Neon-returned strings to numeric (mirrors the TS parseFloat/parseInt).
            normalised = [
                {
                    "strategy": r["strategy"],
                    "realized_pnl": float(r["realized_pnl"] or 0),
                    "count": int(r["count"] or 0),
                    "win_rate": float(r["win_rate"] or 0),
                }
                for r in stats
            ]

            total_pnl = sum(r["realized_pnl"] for r in normalised)

            for row in normalised:
                if row["count"] < MIN_TRADES_FOR_REVIEW:
                    continue

                w_rows = await db.fetch(
                    "SELECT weight FROM strategy_weights WHERE strategy = $1",
                    row["strategy"],
                )
                raw_w = w_rows[0]["weight"] if w_rows else 1.0
                try:
                    current_weight = float(raw_w)
                    if current_weight != current_weight:  # NaN guard
                        current_weight = 1.0
                except (TypeError, ValueError):
                    current_weight = 1.0

                relative_perf = (row["realized_pnl"] / abs(total_pnl)) if total_pnl != 0 else 0.0
                normalized_score = 0.5 + relative_perf * 0.5
                new_weight = max(
                    MIN_WEIGHT,
                    min(
                        MAX_WEIGHT,
                        current_weight * (1 - WEIGHT_ALPHA) + normalized_score * WEIGHT_ALPHA * 2,
                    ),
                )

                await db.execute(
                    """UPDATE strategy_weights
                       SET weight = $1, realized_pnl = $2, trades_count = $3,
                           win_rate = $4, last_adjusted_at = now()
                       WHERE strategy = $5""",
                    new_weight,
                    row["realized_pnl"],
                    row["count"],
                    row["win_rate"],
                    row["strategy"],
                )
                log.info(
                    "Strategy weight updated",
                    strategy=row["strategy"],
                    new_weight=round(new_weight, 3),
                    win_rate=round(row["win_rate"], 2),
                    trades=row["count"],
                )

                # Disable deeply-negative strategies for 24h cooldown.
                if total_pnl != 0 and row["realized_pnl"] / abs(total_pnl) < DRAWDOWN_DISABLE_THRESHOLD:
                    await db.execute(
                        """UPDATE strategy_weights
                           SET enabled = false, cooldown_until = now() + INTERVAL '24 hours'
                           WHERE strategy = $1""",
                        row["strategy"],
                    )
                    log.warning("Strategy disabled due to negative expectancy", strategy=row["strategy"])

            # Re-enable strategies past their cooldown.
            await db.execute(
                """UPDATE strategy_weights
                   SET enabled = true, cooldown_until = null
                   WHERE cooldown_until IS NOT NULL AND cooldown_until < now()"""
            )

        except Exception as e:
            log.error("Weight update failed", error=str(e))

    async def _deep_review(self, db: NeonHttpClient) -> None:
        log.info("Running deep review")
        try:
            rows = await db.fetch(
                """SELECT d.strategy, d.regime, d.action,
                     AVG(CASE WHEN t.realized_pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                     AVG(t.realized_pnl) AS avg_pnl,
                     COUNT(*) AS count
                   FROM decision_log d
                   JOIN trades t ON t.decision_id = d.id
                   WHERE d.ts > now() - INTERVAL '30 days'
                     AND d.strategy IS NOT NULL AND d.regime IS NOT NULL
                     AND t.closed_at IS NOT NULL
                   GROUP BY d.strategy, d.regime, d.action
                   HAVING COUNT(*) >= 5"""
            )

            for row in rows:
                h = f"{row['strategy']}:{row['regime']}:{row['action']}"
                await db.execute(
                    """INSERT INTO learned_signals
                         (signal_hash, strategy, regime, n_trades, win_rate, avg_pnl, last_seen)
                       VALUES ($1, $2, $3, $4, $5, $6, now())
                       ON CONFLICT (signal_hash, strategy) DO UPDATE SET
                         n_trades = $4, win_rate = $5, avg_pnl = $6,
                         last_seen = now(), last_updated = now()""",
                    h,
                    row["strategy"],
                    row["regime"],
                    int(row["count"] or 0),
                    float(row["win_rate"] or 0),
                    float(row["avg_pnl"] or 0),
                )
            log.info("Learned signals updated", rows=len(rows))
        except Exception as e:
            log.error("Deep review failed", error=str(e))
