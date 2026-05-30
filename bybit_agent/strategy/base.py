"""Strategy interface + Signal — ports src/strategy/Strategy.ts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Action = Literal["enter_long", "enter_short", "exit", "hold"]


@dataclass
class Signal:
    action: Action
    symbol: str
    strategy: str
    confidence: float
    rationale: str
    suggestedEntry: float | None = None
    suggestedStop: float | None = None
    suggestedTp: float | None = None


@dataclass
class StrategyContext:
    strategyWeight: float = 1.0
    learnedPrior: float | None = None
    currentPositionSide: str | None = None
    currentPositionSize: float | None = None
    currentEntryPrice: float | None = None
    recencyDecay: float | None = None


class Strategy(Protocol):
    name: str
    suitable_regimes: list[str]

    def evaluate(self, snap, ctx: StrategyContext) -> Signal: ...
