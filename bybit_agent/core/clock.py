"""Injectable clock — ports src/core/clock.ts. Lets tests freeze/advance time.

``now()`` returns epoch milliseconds (matching JS ``Date.now()``).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> int: ...
    def now_date(self) -> datetime: ...


class RealClock:
    def now(self) -> int:
        return int(time.time() * 1000)

    def now_date(self) -> datetime:
        return datetime.now(timezone.utc)


real_clock = RealClock()


class MockClock:
    def __init__(self, start_ms: int) -> None:
        self._t = start_ms

    def now(self) -> int:
        return self._t

    def now_date(self) -> datetime:
        return datetime.fromtimestamp(self._t / 1000, tz=timezone.utc)

    def advance(self, ms: int) -> None:
        self._t += ms


def make_mock_clock(start_ms: int) -> MockClock:
    return MockClock(start_ms)
