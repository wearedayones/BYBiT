"""Phase 1: rate limiter — ports test/rateLimiter.test.ts + header parsing."""

from __future__ import annotations

import pytest

from bybit_agent.core.clock import make_mock_clock
from bybit_agent.core.errors import BybitApiError
from bybit_agent.exchange.rate_limiter import RateLimiter


def test_builds_without_error():
    assert RateLimiter(make_mock_clock(0)) is not None


async def test_schedule_get_resolves_with_result():
    rl = RateLimiter(make_mock_clock(0))
    assert await rl.schedule_get(_const(42)) == 42


async def test_schedule_post_resolves_with_result():
    rl = RateLimiter(make_mock_clock(0))
    assert await rl.schedule_post(_const("done")) == "done"


async def test_propagates_non_rate_limit_errors():
    rl = RateLimiter(make_mock_clock(0))
    with pytest.raises(RuntimeError, match="network error"):
        await rl.schedule_get(_raise(RuntimeError("network error")))


def test_header_parsing_tracks_bucket():
    rl = RateLimiter(make_mock_clock(1_000))
    rl.update_from_headers(
        {"x-bapi-limit": "100", "x-bapi-limit-status": "40", "x-bapi-limit-reset-timestamp": "2000"},
        "/v5/market/tickers",
    )
    bucket = rl.get_bucket("/v5/market/tickers")
    assert bucket is not None
    assert bucket.limit == 100 and bucket.remaining == 40
    assert rl.get_worst_bucket().endpoint == "/v5/market/tickers"


def test_malformed_headers_ignored():
    rl = RateLimiter(make_mock_clock(0))
    rl.update_from_headers({"x-bapi-limit": "notanumber"}, "/x")
    assert rl.get_bucket("/x") is None


def test_rate_limit_error_is_recognised():
    err = BybitApiError(10006, "rate limit", "/x")
    err.ret_code = 10006
    from bybit_agent.exchange.rate_limiter import _is_rate_limit_error

    assert _is_rate_limit_error(err) is True
    assert _is_rate_limit_error(BybitApiError(0, "ok", "/x")) is False


def _const(value):
    async def f():
        return value

    return f


def _raise(exc):
    async def f():
        raise exc

    return f
