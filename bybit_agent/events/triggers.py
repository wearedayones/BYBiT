"""Trigger sources — the four kinds of events that wake the installing AI agent.

Each trigger function is called from the event detector or from the trading loop
at the appropriate moment. They write to pending_events via enqueue(); the loop
never blocks on the result.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from bybit_agent.events.queue import enqueue
from bybit_agent.persistence.db import NeonHttpClient


async def trigger_scheduled_review(db: NeonHttpClient) -> str | None:
    """Fire once every 24 h — the AI's recurring strategy-review session entry."""
    return await enqueue(
        db,
        kind="scheduled_review",
        severity="info",
        title="Daily strategy review due",
        summary=(
            "24 hours have elapsed. Run `bybit report` to pull the performance digest, "
            "then use `bybit tune` / `bybit weights` to adjust parameters within bounds. "
            "Resolve this event with action='reviewed' when done."
        ),
        default_action="acknowledge",
        options=[
            {"action": "reviewed", "label": "Review complete — parameters updated"},
            {"action": "acknowledge", "label": "Acknowledge without changes"},
        ],
        dedupe_key="scheduled_review:daily",
        ttl=timedelta(hours=48),
    )


async def trigger_risk_escalation(
    db: NeonHttpClient,
    *,
    reason: str,
    details: dict[str, Any],
    cycle_id: str | None = None,
) -> str | None:
    """Fire after the loop has already taken a deterministic risk action (informational)."""
    return await enqueue(
        db,
        kind="risk_escalation",
        severity="critical",
        title=f"Risk action taken: {reason}",
        summary=(
            f"The loop triggered a hard-limit response: {reason}. "
            "The action has already been applied deterministically. "
            "Review the details and acknowledge or adjust parameters."
        ),
        default_action="acknowledge",
        context=details,
        cycle_id=cycle_id,
        options=[
            {"action": "acknowledge", "label": "Acknowledge — review complete"},
            {"action": "pause", "label": "Pause the agent for manual inspection"},
        ],
        dedupe_key=f"risk_escalation:{reason[:60]}",
        ttl=timedelta(hours=4),
    )


async def trigger_market_event(
    db: NeonHttpClient,
    *,
    symbol: str,
    event_type: str,
    details: dict[str, Any],
    cycle_id: str | None = None,
) -> str | None:
    """Fire when the detector spots a significant market condition (price spike, vol, funding)."""
    title = f"Market event: {event_type} on {symbol}"
    return await enqueue(
        db,
        kind="market_event",
        severity="warning",
        symbol=symbol,
        title=title,
        summary=(
            f"The detector observed '{event_type}' on {symbol}. "
            "Review the context and decide whether to adjust strategy weights, "
            "pause trading on this symbol, or hold the current configuration."
        ),
        default_action="hold",
        context=details,
        cycle_id=cycle_id,
        options=[
            {"action": "hold", "label": "Hold — current config is appropriate"},
            {"action": "reduce_weight", "label": "Reduce exposure (use bybit weights)"},
            {"action": "pause_symbol", "label": "Stop trading this symbol until reviewed"},
        ],
        dedupe_key=f"market_event:{symbol}:{event_type}",
        ttl=timedelta(hours=2),
    )


async def trigger_ambiguous_decision(
    db: NeonHttpClient,
    *,
    symbol: str,
    strategy: str,
    signal: dict[str, Any],
    score: float,
    cycle_id: str | None = None,
) -> str | None:
    """Fire when a signal score sits in the ambiguous zone — too low to auto-execute."""
    return await enqueue(
        db,
        kind="ambiguous_decision",
        severity="info",
        symbol=symbol,
        title=f"Ambiguous signal: {strategy} on {symbol} (score={score:.2f})",
        summary=(
            f"The decision engine scored {strategy} on {symbol} at {score:.2f} — "
            "above the noise floor but below the auto-execute threshold. "
            "Use `bybit event <id>` to review the full signal context, then "
            "`bybit decide <id> --action approve` or `--action reject`."
        ),
        default_action="reject",
        context={"signal": signal, "score": score, "strategy": strategy},
        cycle_id=cycle_id,
        options=[
            {"action": "approve", "label": "Approve — execute this signal next cycle"},
            {"action": "reject", "label": "Reject — discard this signal"},
            {"action": "hold", "label": "Hold — re-evaluate next cycle"},
        ],
        dedupe_key=f"ambiguous:{symbol}:{strategy}",
        ttl=timedelta(minutes=30),
    )


async def trigger_signal_drought(
    db: NeonHttpClient,
    *,
    signals_fired: int,
    cycles_blocked: int,
    rejection_summary: dict[str, Any],
    cycle_id: str | None = None,
) -> str | None:
    """Fire when signals are generated but every one is rejected for an extended period.

    The loop fires this after SIGNAL_DROUGHT_THRESHOLD consecutive cycles where signals
    exist but none pass the risk gate. The AI should inspect rejection reasons via
    `bybit report` and adjust risk parameters with `bybit tune`.
    """
    top = rejection_summary.get("top_rejections", [])
    top_str = "; ".join(
        f"{r['strategy']}/{r['reason']} ×{r['count']}" for r in top[:5]
    ) or "none recorded"
    return await enqueue(
        db,
        kind="risk_escalation",
        severity="warning",
        title=f"Signal drought: {signals_fired} signals blocked over {cycles_blocked} cycles",
        summary=(
            f"The bot generated signals for {cycles_blocked} consecutive cycles but no trade "
            f"passed the risk gate. Total blocked signals: {signals_fired}. "
            f"Top rejection reasons: {top_str}. "
            "Run `bybit report --period daily --json` then `bybit tune --list --json` "
            "to review risk parameters. Common fixes: raise maxRiskPct, check equity, "
            "review circuit breaker state."
        ),
        default_action="acknowledge",
        context={
            "signals_fired": signals_fired,
            "cycles_blocked": cycles_blocked,
            **rejection_summary,
        },
        cycle_id=cycle_id,
        options=[
            {"action": "acknowledge", "label": "Acknowledge — will review and tune"},
            {"action": "tune", "label": "Adjust risk params (use bybit tune)"},
            {"action": "pause", "label": "Pause for manual inspection"},
        ],
        dedupe_key="signal_drought",
        ttl=timedelta(hours=4),
    )
