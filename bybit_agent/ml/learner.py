"""Continuous self-improvement — the learning loop that runs inside the 24/7 service.

Three layers of learning, each firing at a different cadence:

  1. record_outcome()         — every trade close (online update of win_rate)
  2. adaptive_weight_decay()  — every cycle (no-op until ≥5 48h trades)
  3. retrain_from_history()   — on demand via `bybit train` or after 50 new trades

Layer 1 keeps learned_signals current so DecisionEngine always has fresh priors.
Layer 2 gently steers strategy weights toward what's working without blocking the
agent's manual overrides.
Layer 3 does a full batch recompute — useful after historical backfill or when the
online updates have drifted from the true distribution.
"""
from __future__ import annotations

import hashlib

from bybit_agent.core.logger import get_logger

log = get_logger().bind(module="learner")

# ── adaptive weight decay constants ──────────────────────────────────────────
_MIN_TRADES_FOR_DECAY = 5     # need at least this many 48h trades before touching weights
_DECAY_FACTOR         = 0.90  # weight × this when 48h win rate < LOW_THRESHOLD
_BOOST_FACTOR         = 1.05  # weight × this when 48h win rate > HIGH_THRESHOLD
_LOW_THRESHOLD        = 0.35  # below → decay
_HIGH_THRESHOLD       = 0.60  # above → boost
_WEIGHT_MIN           = 0.10
_WEIGHT_MAX           = 2.00

# Track how many outcomes recorded since last full retrain.
_outcomes_since_retrain: int = 0
_RETRAIN_THRESHOLD = 50


def _hash(strategy: str, regime: str) -> str:
    return hashlib.md5(f"{strategy}:{regime}".encode()).hexdigest()[:16]


# ── layer 1: online outcome recording ─────────────────────────────────────────

async def record_outcome(
    db,
    strategy: str,
    regime: str,
    symbol: str,
    pnl: float,
    *,
    is_paper: bool = False,
) -> None:
    """Upsert a win/loss outcome into learned_signals immediately when a trade closes.

    Uses an incremental formula so win_rate stays accurate without requerying
    the full trade history every time:
        new_win_rate = (old_win_rate × old_n + new_indicator) / (old_n + 1)
    """
    global _outcomes_since_retrain
    win_indicator = 1.0 if pnl > 0 else 0.0
    sig_hash = _hash(strategy, regime)
    try:
        await db.execute(
            """
            INSERT INTO learned_signals
              (signal_hash, strategy, regime, n_trades, win_rate, avg_pnl,
               last_seen, last_updated)
            VALUES ($1, $2, $3, 1, $4, $5, now(), now())
            ON CONFLICT (signal_hash, strategy) DO UPDATE SET
              n_trades     = learned_signals.n_trades + 1,
              win_rate     = (learned_signals.win_rate * learned_signals.n_trades + $4)
                             / (learned_signals.n_trades + 1),
              avg_pnl      = (COALESCE(learned_signals.avg_pnl, 0) * learned_signals.n_trades + $5)
                             / (learned_signals.n_trades + 1),
              last_seen    = now(),
              last_updated = now()
            """,
            sig_hash, strategy, regime, win_indicator, pnl,
        )
        _outcomes_since_retrain += 1
        log.debug("Outcome recorded", strategy=strategy, regime=regime,
                  win=pnl > 0, paper=is_paper)
    except Exception as exc:  # noqa: BLE001
        log.warning("record_outcome failed", strategy=strategy, error=str(exc))


def outcomes_since_retrain() -> int:
    return _outcomes_since_retrain


def reset_retrain_counter() -> None:
    global _outcomes_since_retrain
    _outcomes_since_retrain = 0


# ── layer 2: adaptive weight decay (per-cycle, low overhead) ─────────────────

async def adaptive_weight_decay(db) -> None:
    """Gently steer strategy weights based on 48-hour rolling win rate.

    Runs every cycle. No-ops if there aren't enough recent trades to be
    statistically meaningful. The agent's manual bybit weights --set always
    takes precedence on the next cycle.
    """
    try:
        rows = await db.fetch(
            """
            SELECT dl.strategy,
                   COUNT(*)::int                                                  AS n,
                   SUM(CASE WHEN t.realized_pnl > 0 THEN 1 ELSE 0 END)::int     AS wins
            FROM trades t
            JOIN decision_log dl ON dl.id = t.decision_id
            WHERE t.closed_at > now() - INTERVAL '48 hours'
              AND t.realized_pnl IS NOT NULL
              AND dl.strategy IS NOT NULL
            GROUP BY dl.strategy
            """
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("adaptive_weight_decay query failed", error=str(exc))
        return

    for row in rows:
        strategy = row.get("strategy")
        n = int(row.get("n") or 0)
        wins = int(row.get("wins") or 0)
        if not strategy or n < _MIN_TRADES_FOR_DECAY:
            continue

        win_rate_48h = wins / n
        if win_rate_48h < _LOW_THRESHOLD:
            factor = _DECAY_FACTOR
        elif win_rate_48h > _HIGH_THRESHOLD:
            factor = _BOOST_FACTOR
        else:
            # Store rolling rate even if no adjustment made.
            try:
                await db.execute(
                    "UPDATE strategy_weights SET rolling_win_48h = $1 WHERE strategy = $2",
                    win_rate_48h, strategy,
                )
            except Exception:  # noqa: BLE001
                pass
            continue

        try:
            await db.execute(
                """
                UPDATE strategy_weights
                SET weight          = GREATEST($1, LEAST($2, weight * $3)),
                    rolling_win_48h = $4,
                    last_adapted_at = now()
                WHERE strategy = $5
                """,
                _WEIGHT_MIN, _WEIGHT_MAX, factor, win_rate_48h, strategy,
            )
            log.info("Weight adapted", strategy=strategy, factor=factor,
                     win_rate_48h=round(win_rate_48h, 3), n_trades=n)
        except Exception as exc:  # noqa: BLE001
            log.debug("Weight update failed", strategy=strategy, error=str(exc))


# ── layer 2b: regime-aware weight adjustment (called by auto_manage each cycle) ──

# Maps each strategy to the regimes where it is designed to excel.
# Must stay in sync with each strategy class's `suitable_regimes` attribute.
_STRATEGY_REGIMES: dict[str, set[str]] = {
    "trend_momentum":      {"trending"},
    "mean_reversion":      {"ranging", "crisis", "crowded_long", "crowded_short"},
    "breakout":            {"trending", "high_volatility"},
    "funding_harvest":     {"trending", "ranging", "high_volatility", "crisis",
                            "crowded_long", "crowded_short"},
    "crowded_positioning": {"crowded_long", "crowded_short"},
    "grid":                {"ranging"},
    "dca":                 {"ranging", "high_volatility"},
}

# Small nudges keep the system stable; larger changes come from record_outcome() / retrain.
_REGIME_BOOST_FACTOR = 1.03   # +3% when ≥60% of snapshots fit this strategy's regimes
_REGIME_DECAY_FACTOR = 0.97   # −3% when ≤20% of snapshots fit
_REGIME_ADJUST_COOLDOWN = 50  # cycles between adjustments for the same strategy


async def regime_aware_adjust(db, snapshot_regimes: list[str]) -> None:
    """Nudge strategy weights based on how well each strategy fits the current regime mix.

    Called from AgentLoop._auto_manage() every AUTO_MANAGE_CYCLES. Strategies that are
    a poor fit for the live regime mix are gently decayed; well-fitted ones are gently
    boosted. This lets the bot self-optimise its strategy allocation without user input.
    """
    if not snapshot_regimes:
        return

    from collections import Counter
    regime_counts = Counter(snapshot_regimes)
    total = sum(regime_counts.values())
    dominant = regime_counts.most_common(1)[0][0]

    # Crisis mode: if majority of symbols are in crisis, cut all non-crisis-safe weights.
    crisis_fraction = regime_counts.get("crisis", 0) / total if total else 0.0
    in_crisis = crisis_fraction >= 0.5

    try:
        rows = await db.fetch(
            "SELECT strategy, weight, enabled, last_adapted_at FROM strategy_weights"
        )
    except Exception as exc:
        log.debug("regime_aware_adjust query failed", error=str(exc))
        return

    for row in rows:
        strategy = row.get("strategy") or ""
        if not strategy or not row.get("enabled"):
            continue

        suitable = _STRATEGY_REGIMES.get(strategy, set())
        current_weight = float(row.get("weight") or 1.0)

        # Anti-oscillation: skip if adapted very recently.
        last_adapted = row.get("last_adapted_at")
        if last_adapted is not None:
            import datetime as _dt
            if hasattr(last_adapted, "timestamp"):
                age_s = (
                    _dt.datetime.now(_dt.timezone.utc).timestamp() - last_adapted.timestamp()
                )
                # Skip if adapted within the last 10 minutes.
                if age_s < 600:
                    continue

        # Crisis override: if we're in a crisis regime, strategies not designed for it
        # get their weights pushed toward the minimum to reduce exposure.
        if in_crisis and "crisis" not in suitable:
            factor = _REGIME_DECAY_FACTOR
        else:
            fit_count = sum(cnt for regime, cnt in regime_counts.items() if regime in suitable)
            fit_score = fit_count / total if total > 0 else 0.0

            if fit_score >= 0.60:
                factor = _REGIME_BOOST_FACTOR
            elif fit_score <= 0.20:
                factor = _REGIME_DECAY_FACTOR
            else:
                continue  # Neutral fit — leave weight unchanged.

        new_weight = round(max(_WEIGHT_MIN, min(_WEIGHT_MAX, current_weight * factor)), 4)
        if abs(new_weight - current_weight) < 0.005:
            continue

        try:
            await db.execute(
                """UPDATE strategy_weights
                   SET weight = $1, last_adapted_at = now()
                   WHERE strategy = $2""",
                new_weight, strategy,
            )
            log.info(
                "Regime weight adjusted",
                strategy=strategy,
                old_weight=round(current_weight, 4),
                new_weight=new_weight,
                dominant_regime=dominant,
                in_crisis=in_crisis,
            )
        except Exception as exc:
            log.debug("Regime weight update failed", strategy=strategy, error=str(exc))


# ── layer 3: full batch retrain (on-demand) ───────────────────────────────────

async def retrain_from_history(db, window_days: int = 30) -> dict:
    """Batch recompute win rates from trade history.

    Unlike record_outcome() which does online updates, this rebuilds
    learned_signals from scratch using all trades in the window. Run via
    `bybit train` or automatically when outcomes_since_retrain() >= 50.
    """
    try:
        rows = await db.fetch(
            f"""
            SELECT dl.strategy,
                   dl.regime,
                   COUNT(*)::int                                                  AS n_trades,
                   (SUM(CASE WHEN t.realized_pnl > 0 THEN 1.0 ELSE 0.0 END)
                    / NULLIF(COUNT(*), 0))::numeric                               AS win_rate,
                   AVG(t.realized_pnl)                                            AS avg_pnl
            FROM trades t
            JOIN decision_log dl ON dl.id = t.decision_id
            WHERE t.closed_at > now() - INTERVAL '{window_days} days'
              AND t.realized_pnl IS NOT NULL
              AND dl.strategy IS NOT NULL
              AND dl.regime   IS NOT NULL
            GROUP BY dl.strategy, dl.regime
            HAVING COUNT(*) >= 3
            """
        )
    except Exception as exc:  # noqa: BLE001
        return {"updated": 0, "total_trades": 0, "details": [], "error": str(exc)}

    updated = 0
    total = 0
    details = []

    for row in rows:
        strategy = row["strategy"]
        regime   = row["regime"]
        n        = int(row.get("n_trades") or 0)
        win_rate = float(row.get("win_rate") or 0)
        avg_pnl  = float(row.get("avg_pnl") or 0)
        sig_hash = _hash(strategy, regime)
        total += n

        try:
            await db.execute(
                """
                INSERT INTO learned_signals
                  (signal_hash, strategy, regime, n_trades, win_rate, avg_pnl,
                   last_seen, last_updated)
                VALUES ($1, $2, $3, $4, $5, $6, now(), now())
                ON CONFLICT (signal_hash, strategy) DO UPDATE SET
                  n_trades     = $4,
                  win_rate     = $5,
                  avg_pnl      = $6,
                  last_updated = now()
                """,
                sig_hash, strategy, regime, n, win_rate, avg_pnl,
            )
            updated += 1
            details.append({
                "strategy": strategy, "regime": regime,
                "n_trades": n, "win_rate": win_rate,
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("retrain upsert failed", strategy=strategy, error=str(exc))

    reset_retrain_counter()
    log.info("Retrain complete", updated=updated, total_trades=total)
    return {"updated": updated, "total_trades": total, "details": details}
