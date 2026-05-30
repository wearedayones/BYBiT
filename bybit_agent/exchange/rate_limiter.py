"""Async rate limiter — ports src/exchange/rateLimiter.ts.

Two lanes (GET / POST) each enforce a minimum interval between request starts plus a
max-concurrency cap (replicating Bottleneck's ``minTime`` + ``maxConcurrent``). Bybit's
rate-limit response headers drive a pre-emptive pause before the exchange hard-blocks,
and retCode 10006 triggers bounded backoff with a consecutive-limit cooldown.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from ..config.constants import RATE_LIMIT
from ..core.clock import Clock, real_clock
from ..core.logger import child_logger

log = child_logger(module="rate-limiter")

HDR_LIMIT = "x-bapi-limit"
HDR_STATUS = "x-bapi-limit-status"           # remaining requests in window
HDR_RESET = "x-bapi-limit-reset-timestamp"   # epoch ms when window resets

PREEMPTIVE_SLOW_THRESHOLD = 0.80
PREEMPTIVE_STOP_THRESHOLD = 0.96

T = TypeVar("T")


@dataclass
class RateLimitBucket:
    limit: int
    remaining: int
    reset_at: int
    endpoint: str


class _Lane:
    """Min-interval + max-concurrency gate."""

    def __init__(self, min_interval_ms: int, max_concurrent: int, clock: Clock) -> None:
        self._min_interval = min_interval_ms / 1000.0
        self._sem = asyncio.Semaphore(max_concurrent)
        self._gate = asyncio.Lock()
        self._next_start = 0.0
        self._clock = clock

    async def schedule(self, fn: Callable[[], Awaitable[T]]) -> T:
        async with self._sem:
            async with self._gate:
                now = self._clock.now() / 1000.0
                wait = self._next_start - now
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = self._clock.now() / 1000.0
                self._next_start = max(now, self._next_start) + self._min_interval
            return await fn()


class RateLimiter:
    def __init__(self, clock: Clock = real_clock) -> None:
        self._clock = clock
        self._get_lane = _Lane(RATE_LIMIT["GET_MIN_MS"], 10, clock)
        self._post_lane = _Lane(RATE_LIMIT["POST_MIN_MS"], 5, clock)
        self._consecutive_rate_limits = 0
        self._paused_until = 0
        self._buckets: dict[str, RateLimitBucket] = {}

    async def schedule_get(self, fn: Callable[[], Awaitable[T]]) -> T:
        return await self._get_lane.schedule(lambda: self._with_backoff(fn))

    async def schedule_post(self, fn: Callable[[], Awaitable[T]]) -> T:
        return await self._post_lane.schedule(lambda: self._with_backoff(fn))

    def update_from_headers(self, headers: dict[str, str], endpoint: str) -> None:
        def get(k: str) -> str:
            v = headers.get(k) or headers.get(k.lower()) or ""
            return v[0] if isinstance(v, list) else v

        try:
            limit = int(get(HDR_LIMIT))
            remaining = int(get(HDR_STATUS))
            reset_at = int(get(HDR_RESET))
        except (ValueError, TypeError):
            return
        if not limit:
            return

        self._buckets[endpoint] = RateLimitBucket(limit, remaining, reset_at, endpoint)
        usage = (limit - remaining) / limit

        if usage >= PREEMPTIVE_STOP_THRESHOLD:
            if self._clock.now() < reset_at:
                self._paused_until = max(self._paused_until, reset_at + 50)
                log.warning(
                    "Rate limit critical — pausing until window reset",
                    endpoint=endpoint, remaining=remaining, limit=limit, reset_at=reset_at,
                )
        elif usage >= PREEMPTIVE_SLOW_THRESHOLD:
            log.debug(
                "Rate limit approaching — traffic will slow naturally",
                endpoint=endpoint, remaining=remaining, limit=limit, usage_pct=round(usage * 100),
            )

    def get_bucket(self, endpoint: str) -> RateLimitBucket | None:
        return self._buckets.get(endpoint)

    def get_worst_bucket(self) -> RateLimitBucket | None:
        worst: RateLimitBucket | None = None
        for b in self._buckets.values():
            if worst is None or (b.limit - b.remaining) / b.limit > (worst.limit - worst.remaining) / worst.limit:
                worst = b
        return worst

    def get_consecutive_rate_limits(self) -> int:
        return self._consecutive_rate_limits

    async def _with_backoff(self, fn: Callable[[], Awaitable[T]]) -> T:
        now = self._clock.now()
        if now < self._paused_until:
            await asyncio.sleep((self._paused_until - now) / 1000.0)

        attempts = 0
        while True:
            try:
                result = await fn()
                self._consecutive_rate_limits = 0
                return result
            except Exception as err:  # noqa: BLE001
                if _is_rate_limit_error(err) and attempts < RATE_LIMIT["RATE_LIMIT_MAX_RETRIES"]:
                    attempts += 1
                    self._consecutive_rate_limits += 1
                    if self._consecutive_rate_limits >= RATE_LIMIT["RATE_LIMIT_MAX_RETRIES"]:
                        pause = RATE_LIMIT["CONSECUTIVE_LIMIT_PAUSE_MS"]
                        self._paused_until = self._clock.now() + pause
                        log.warning("3 consecutive rate limits — pausing all requests", pause_ms=pause)
                        self._consecutive_rate_limits = 0
                        await asyncio.sleep(pause / 1000.0)
                    else:
                        backoff = random.randint(
                            RATE_LIMIT["RATE_LIMIT_BACKOFF_MIN_MS"],
                            RATE_LIMIT["RATE_LIMIT_BACKOFF_MAX_MS"],
                        )
                        log.warning("Rate limit hit — backing off", attempt=attempts, backoff_ms=backoff)
                        await asyncio.sleep(backoff / 1000.0)
                else:
                    raise


def _is_rate_limit_error(err: object) -> bool:
    return getattr(err, "ret_code", None) == 10006


rate_limiter = RateLimiter()
