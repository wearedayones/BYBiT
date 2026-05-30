"""DecisionEngine — ports src/strategy/DecisionEngine.ts.

Runs every enabled strategy on each snapshot, blends a composite score
(confidence × weight × regime fit × learned prior × sentiment × trending), filters by
a confidence floor, ranks, and persists to decision_log. In the Python build these are
shadow rows (is_paper=true) until cutover.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..core.logger import child_logger
from ..market.market_data import classify_regime
from ..market.news import compute_sentiment_multiplier
from ..persistence.db import get_db
from .base import Signal
from .impl.breakout import Breakout
from .impl.funding_harvest import FundingHarvest
from .impl.mean_reversion import MeanReversion
from .impl.trend_momentum import TrendMomentum

log = child_logger(module="decision-engine")

CONFIDENCE_FLOOR = 0.40
RECENCY_DECAY = 0.85


@dataclass
class WeightedSignal:
    action: str
    symbol: str
    strategy: str
    confidence: float
    rationale: str
    suggestedEntry: float | None
    suggestedStop: float | None
    suggestedTp: float | None
    compositeScore: float
    weight: float
    learnedPrior: float
    sentimentMultiplier: float
    trendingBoost: float


class DecisionEngine:
    def __init__(self, paper: bool = True) -> None:
        self._strategies = [TrendMomentum(), MeanReversion(), Breakout(), FundingHarvest()]
        self._paper = paper

    @property
    def paper(self) -> bool:
        return self._paper

    async def run(self, snapshots, cycle_id: str) -> list[WeightedSignal]:
        db = get_db()
        rows = await db.fetch("SELECT strategy, weight, enabled FROM strategy_weights")
        weight_map = {r["strategy"]: r for r in rows}

        results: list[WeightedSignal] = []
        for snap in snapshots:
            regime = classify_regime(snap)
            for strategy in self._strategies:
                w_row = weight_map.get(strategy.name)
                if not w_row or not w_row["enabled"]:
                    continue

                learned_prior = await self._get_learned_prior(strategy.name, regime, snap.symbol)
                signal: Signal = strategy.evaluate(snap, _ctx(w_row, learned_prior))
                if signal.action == "hold":
                    continue

                regime_mult = 1.0 if regime in strategy.suitable_regimes else 0.3
                weight = float(w_row["weight"]) if w_row["weight"] is not None else 1.0

                if signal.action in ("enter_long", "enter_short"):
                    sentiment_mult = compute_sentiment_multiplier(snap.research, signal.action)
                else:
                    sentiment_mult = 1.0
                trending_boost = 1.10 if (snap.research and snap.research.trendingRank is not None) else 1.0

                composite = (
                    signal.confidence * weight * regime_mult
                    * (learned_prior * RECENCY_DECAY + (1 - RECENCY_DECAY))
                    * sentiment_mult * trending_boost
                )
                if composite < CONFIDENCE_FLOOR:
                    continue

                results.append(WeightedSignal(
                    action=signal.action, symbol=signal.symbol, strategy=signal.strategy,
                    confidence=signal.confidence, rationale=signal.rationale,
                    suggestedEntry=signal.suggestedEntry, suggestedStop=signal.suggestedStop,
                    suggestedTp=signal.suggestedTp, compositeScore=composite, weight=weight,
                    learnedPrior=learned_prior, sentimentMultiplier=sentiment_mult,
                    trendingBoost=trending_boost,
                ))

        results.sort(key=lambda s: s.compositeScore, reverse=True)
        await self._persist(results, snapshots, cycle_id)
        log.info("Decisions computed", signals=len(results), cycle_id=cycle_id)
        return results

    async def _persist(self, results, snapshots, cycle_id: str) -> None:
        db = get_db()
        by_symbol = {s.symbol: s for s in snapshots}
        for sig in results:
            snap = by_symbol.get(sig.symbol)
            inputs = None
            if snap:
                inputs = {
                    **asdict(snap.indicators),
                    "sentimentMultiplier": sig.sentimentMultiplier,
                    "trendingBoost": sig.trendingBoost,
                    "fearGreedIndex": snap.research.fearGreedIndex if snap.research else None,
                    "fearGreedLabel": snap.research.fearGreedLabel if snap.research else None,
                    "symbolSentimentScore": snap.research.symbolSentimentScore if snap.research else None,
                    "trendingRank": snap.research.trendingRank if snap.research else None,
                }
            regime = classify_regime(snap) if snap else None
            try:
                await db.execute(
                    """
                    INSERT INTO decision_log (cycle_id, symbol, action, strategy, confidence, regime,
                      rationale, inputs, composite_score, approved, outcome, is_paper)
                    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, null, null, $10)
                    """,
                    cycle_id, sig.symbol, sig.action, sig.strategy, sig.confidence, regime,
                    sig.rationale, db.json(inputs) if inputs else None, sig.compositeScore, self._paper,
                )
            except Exception as e:  # noqa: BLE001
                log.error("Failed to persist decision", error=str(e))

    async def _get_learned_prior(self, strategy: str, regime: str, symbol: str) -> float:
        try:
            db = get_db()
            rows = await db.fetch(
                """
                SELECT win_rate, n_trades FROM learned_signals
                WHERE strategy = $1 AND regime = $2 AND n_trades >= 10 LIMIT 1
                """,
                strategy, regime,
            )
            if rows and rows[0].get("win_rate"):
                return max(0.5, float(rows[0]["win_rate"]))
        except Exception:  # noqa: BLE001
            pass
        return 1.0


def _ctx(w_row, learned_prior):
    from .base import StrategyContext

    return StrategyContext(
        strategyWeight=float(w_row["weight"]) if w_row["weight"] is not None else 1.0,
        learnedPrior=learned_prior,
    )
