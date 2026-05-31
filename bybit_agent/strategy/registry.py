"""Strategy registry — lifecycle management for every strategy, backtest-gated.

This is the data layer behind ``bybit strategy``. It maps DB rows in
``strategy_registry`` to live Python strategy instances and enforces the central
rule: **a strategy cannot be accepted (traded) until it has passed a backtest.**

Lifecycle:  draft → (backtest) → backtested → (accept) → accepted ⇄ paused
                                            ↘ (reject) → rejected

  • create  → draft, disabled
  • edit    → resets to draft, disabled (params changed ⇒ must re-backtest)
  • backtest→ backtested (+ stored metrics); pass/fail recorded
  • accept  → accepted + enabled  (GUARDED: requires backtest.passed)
  • pause   → enabled=false (kept accepted)
  • resume  → enabled=true  (only if accepted)
  • reject  → rejected, disabled
  • delete  → row removed

``base_strategy`` keys a Python class; ``params`` overlays its tunable knobs so
the agent can spin up several instances of one base with different parameters,
each independently backtested.
"""
from __future__ import annotations

import json
from typing import Any

from ..core.logger import child_logger
from ..persistence.db import NeonHttpClient
from .impl.breakout import Breakout
from .impl.funding_harvest import FundingHarvest
from .impl.mean_reversion import MeanReversion
from .impl.trend_momentum import TrendMomentum

log = child_logger(module="strategy-registry")

# The base strategy classes available to instantiate. New base strategies are
# registered here once their class lands in impl/.
BASE_STRATEGIES: dict[str, type] = {
    "trend_momentum": TrendMomentum,
    "mean_reversion": MeanReversion,
    "breakout": Breakout,
    "funding_harvest": FundingHarvest,
}


class StrategyError(Exception):
    """Raised on an invalid lifecycle transition (e.g. accept without a pass)."""


def instantiate(base_strategy: str, params: dict[str, Any] | None = None, *, name: str | None = None):
    """Build a live strategy instance from a base key, overlaying ``params``.

    Params are applied as instance attributes; a strategy that reads them adapts,
    one that doesn't ignores them (forward-compatible parameterization).
    """
    cls = BASE_STRATEGIES.get(base_strategy)
    if cls is None:
        raise StrategyError(f"Unknown base strategy '{base_strategy}'. "
                            f"Known: {', '.join(sorted(BASE_STRATEGIES))}")
    inst = cls()
    for k, v in (params or {}).items():
        setattr(inst, k, v)
    if name:
        inst.name = name  # so decision_log records the instance name, not the base
    return inst


# ── CRUD ────────────────────────────────────────────────────────────────────

async def list_strategies(db: NeonHttpClient, status: str | None = None) -> list[dict]:
    if status:
        return await db.fetch(
            "SELECT * FROM strategy_registry WHERE status = $1 ORDER BY name", status
        )
    return await db.fetch("SELECT * FROM strategy_registry ORDER BY name")


async def get_strategy(db: NeonHttpClient, name: str) -> dict | None:
    rows = await db.fetch("SELECT * FROM strategy_registry WHERE name = $1 LIMIT 1", name)
    return rows[0] if rows else None


async def create_strategy(
    db: NeonHttpClient,
    *,
    name: str,
    base_strategy: str,
    params: dict[str, Any] | None = None,
    weight: float = 1.0,
    notes: str | None = None,
) -> None:
    if base_strategy not in BASE_STRATEGIES:
        raise StrategyError(f"Unknown base strategy '{base_strategy}'. "
                            f"Known: {', '.join(sorted(BASE_STRATEGIES))}")
    existing = await get_strategy(db, name)
    if existing:
        raise StrategyError(f"Strategy '{name}' already exists.")
    await db.execute(
        """INSERT INTO strategy_registry (name, base_strategy, params, status, enabled, weight, notes)
           VALUES ($1, $2, $3::jsonb, 'draft', false, $4, $5)""",
        name, base_strategy, json.dumps(params or {}), weight, notes,
    )
    log.info("Strategy created", name=name, base=base_strategy)


async def update_strategy(
    db: NeonHttpClient,
    name: str,
    *,
    params: dict[str, Any] | None = None,
    weight: float | None = None,
    notes: str | None = None,
) -> None:
    """Edit a strategy. Changing params invalidates any prior backtest: the row
    drops back to 'draft' + disabled and must be re-backtested before re-accept."""
    s = await get_strategy(db, name)
    if not s:
        raise StrategyError(f"Strategy '{name}' not found.")
    if params is not None:
        await db.execute(
            """UPDATE strategy_registry
               SET params = $1::jsonb, status = 'draft', enabled = false,
                   backtest = NULL, backtested_at = NULL, updated_at = now()
               WHERE name = $2""",
            json.dumps(params), name,
        )
    if weight is not None:
        await db.execute(
            "UPDATE strategy_registry SET weight = $1, updated_at = now() WHERE name = $2",
            weight, name,
        )
    if notes is not None:
        await db.execute(
            "UPDATE strategy_registry SET notes = $1, updated_at = now() WHERE name = $2",
            notes, name,
        )
    log.info("Strategy updated", name=name)


async def delete_strategy(db: NeonHttpClient, name: str) -> None:
    s = await get_strategy(db, name)
    if not s:
        raise StrategyError(f"Strategy '{name}' not found.")
    await db.execute("DELETE FROM strategy_registry WHERE name = $1", name)
    log.info("Strategy deleted", name=name)


# ── lifecycle ─────────────────────────────────────────────────────────────────

async def record_backtest(db: NeonHttpClient, name: str, metrics: dict[str, Any], passed: bool) -> None:
    s = await get_strategy(db, name)
    if not s:
        raise StrategyError(f"Strategy '{name}' not found.")
    await db.execute(
        """UPDATE strategy_registry
           SET backtest = $1::jsonb, backtested_at = now(),
               status = CASE WHEN status = 'accepted' THEN 'accepted' ELSE 'backtested' END,
               updated_at = now()
           WHERE name = $2""",
        json.dumps(metrics), name,
    )
    log.info("Backtest recorded", name=name, passed=passed)


async def accept(db: NeonHttpClient, name: str) -> None:
    """Promote a strategy to live/paper trading — GUARDED by a passing backtest."""
    s = await get_strategy(db, name)
    if not s:
        raise StrategyError(f"Strategy '{name}' not found.")
    bt = s.get("backtest")
    if isinstance(bt, str):
        try:
            bt = json.loads(bt)
        except Exception:  # noqa: BLE001
            bt = None
    if not bt or not bt.get("passed"):
        raise StrategyError(
            f"Cannot accept '{name}': no passing backtest on record. "
            f"Run `bybit strategy backtest {name}` first."
        )
    await db.execute(
        "UPDATE strategy_registry SET status = 'accepted', enabled = true, updated_at = now() WHERE name = $1",
        name,
    )
    log.info("Strategy accepted", name=name)


async def reject(db: NeonHttpClient, name: str) -> None:
    s = await get_strategy(db, name)
    if not s:
        raise StrategyError(f"Strategy '{name}' not found.")
    await db.execute(
        "UPDATE strategy_registry SET status = 'rejected', enabled = false, updated_at = now() WHERE name = $1",
        name,
    )
    log.info("Strategy rejected", name=name)


async def pause(db: NeonHttpClient, name: str) -> None:
    s = await get_strategy(db, name)
    if not s:
        raise StrategyError(f"Strategy '{name}' not found.")
    await db.execute(
        "UPDATE strategy_registry SET enabled = false, updated_at = now() WHERE name = $1", name
    )
    log.info("Strategy paused", name=name)


async def resume(db: NeonHttpClient, name: str) -> None:
    s = await get_strategy(db, name)
    if not s:
        raise StrategyError(f"Strategy '{name}' not found.")
    if s.get("status") != "accepted":
        raise StrategyError(
            f"Cannot resume '{name}': status is '{s.get('status')}', not 'accepted'. "
            "Backtest and accept it first."
        )
    await db.execute(
        "UPDATE strategy_registry SET enabled = true, updated_at = now() WHERE name = $1", name
    )
    log.info("Strategy resumed", name=name)


async def load_active_strategies(db: NeonHttpClient) -> list[tuple[Any, float]]:
    """Return (instance, weight) for every accepted+enabled strategy — the set the
    decision engine should run. Falls back to nothing if the table is empty."""
    rows = await db.fetch(
        "SELECT name, base_strategy, params, weight FROM strategy_registry "
        "WHERE status = 'accepted' AND enabled = true"
    )
    out: list[tuple[Any, float]] = []
    for r in rows:
        params = r.get("params") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:  # noqa: BLE001
                params = {}
        try:
            inst = instantiate(r["base_strategy"], params, name=r["name"])
            out.append((inst, float(r.get("weight") or 1.0)))
        except StrategyError as e:
            log.warning("Skipping unloadable strategy", name=r.get("name"), error=str(e))
    return out
