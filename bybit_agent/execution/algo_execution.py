"""Algorithmic execution orders — TWAP, Iceberg, Chase, POV via /v5/strategy/create.

Used by execution_router when position notional exceeds TWAP_NOTIONAL_THRESHOLD to
reduce market impact. Always falls back to regular orders in paper mode.

Note: algo orders use category "UTA_USDT" (not "linear") per Bybit strategy.md spec.
"""
from __future__ import annotations

from typing import Literal

from ..core.logger import child_logger
from ..exchange.bybit_client import BybitClient

log = child_logger(module="algo-execution")

AlgoType = Literal["TWAP", "Iceberg", "Chase", "POV"]

# Maps standard category names to algo-order category format.
_ALGO_CATEGORY = {
    "linear": "UTA_USDT",
    "inverse": "UTA_INVERSE",
    "spot": "UTA_SPOT",
}


async def create_twap(
    client: BybitClient,
    *,
    category: str,
    symbol: str,
    side: str,
    qty: float,
    duration_secs: int = 300,
    price_limit: float | None = None,
    reduce_only: bool = False,
) -> dict:
    """Submit a TWAP strategy order. Splits qty evenly over duration_secs."""
    algo_cat = _ALGO_CATEGORY.get(category, category)
    body: dict = {
        "category": algo_cat,
        "symbol": symbol,
        "side": side,
        "orderType": "TWAP",
        "qty": str(qty),
        "timeDuration": duration_secs,
        "reduceOnly": reduce_only,
    }
    if price_limit:
        body["priceLimit"] = str(price_limit)
    result = await client.create_algo_order(body)
    log.info("TWAP order submitted", symbol=symbol, qty=qty, duration=duration_secs,
             algo_id=result.get("algoOrderId"))
    return result


async def create_iceberg(
    client: BybitClient,
    *,
    category: str,
    symbol: str,
    side: str,
    qty: float,
    child_qty: float,
    price_limit: float | None = None,
    reduce_only: bool = False,
) -> dict:
    """Submit an Iceberg order. Shows child_qty at a time from total qty."""
    algo_cat = _ALGO_CATEGORY.get(category, category)
    body: dict = {
        "category": algo_cat,
        "symbol": symbol,
        "side": side,
        "orderType": "Iceberg",
        "qty": str(qty),
        "subSize": str(child_qty),
        "reduceOnly": reduce_only,
    }
    if price_limit:
        body["priceLimit"] = str(price_limit)
    result = await client.create_algo_order(body)
    log.info("Iceberg order submitted", symbol=symbol, qty=qty, child_qty=child_qty,
             algo_id=result.get("algoOrderId"))
    return result


async def stop_algo(client: BybitClient, category: str, algo_order_id: str) -> None:
    """Permanently stop a running algo order (irreversible)."""
    algo_cat = _ALGO_CATEGORY.get(category, category)
    await client.stop_algo_order(algo_cat, algo_order_id)
    log.info("Algo order stopped", algo_id=algo_order_id)


async def list_algos(client: BybitClient, category: str, symbol: str | None = None) -> list[dict]:
    """List active algo orders for a category (+ optional symbol filter)."""
    algo_cat = _ALGO_CATEGORY.get(category, category)
    return await client.list_algo_orders(algo_cat, symbol)
