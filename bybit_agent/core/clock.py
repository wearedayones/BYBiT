"""Injectable clock — ports src/core/clock.ts. Lets tests freeze time."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...


class RealClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)


real_clock = RealClock()
