"""Bybit V5 REST client — ports src/exchange/BybitClient.ts to httpx (async).

Public, private (signed), and trading-bot endpoints. Every call goes through the
rate limiter and a 3× TLS-skew retry. POST bodies are compact JSON (no spaces) so the
signed ``param_str`` exactly equals the bytes sent on the wire.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from typing import Any, Awaitable, Callable, TypeVar
from urllib.parse import quote

import httpx

from ..config.constants import (
    MAINNET_REST,
    TESTNET_REST,
    USER_AGENT,
    X_REFERER,
)
from ..core.errors import BotApiError, BybitApiError
from ..core.logger import child_logger
from .rate_limiter import rate_limiter
from .signer import (
    BybitAuth,
    build_get_param_str,
    build_headers,
    build_post_param_str,
)
from .types import (
    Category,
    InstrumentInfo,
    KlineItem,
    Orderbook,
    PlaceOrderRequest,
    Position,
    Ticker,
    WalletBalance,
)

log = child_logger(module="bybit-client")

T = TypeVar("T")

_COMPACT = (",", ":")  # JSON separators matching JS JSON.stringify


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=_COMPACT)


class BybitClient:
    def __init__(self, auth: BybitAuth, is_testnet: bool) -> None:
        self._auth = auth
        self._base_url = TESTNET_REST if is_testnet else MAINNET_REST
        self._http = httpx.AsyncClient(timeout=30.0)

    @property
    def _api_key(self) -> str:
        return self._auth.api_key

    def set_testnet(self, is_testnet: bool) -> None:
        self._base_url = TESTNET_REST if is_testnet else MAINNET_REST

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── Public ────────────────────────────────────────────────────────────────
    async def get_server_time(self) -> int:
        res = await self._public_get("/v5/market/time")
        return int(res["timeSecond"]) * 1000

    async def get_kline(self, category: Category, symbol: str, interval: str, limit: int = 200) -> list[KlineItem]:
        res = await self._public_get(
            f"/v5/market/kline?category={category}&symbol={symbol}&interval={interval}&limit={limit}"
        )
        return [
            {
                "startTime": k[0], "openPrice": k[1], "highPrice": k[2], "lowPrice": k[3],
                "closePrice": k[4], "volume": k[5], "turnover": k[6],
            }
            for k in res["list"]
        ]

    async def get_ticker(self, category: Category, symbol: str) -> Ticker | None:
        res = await self._public_get(f"/v5/market/tickers?category={category}&symbol={symbol}")
        lst = res.get("list") or []
        return lst[0] if lst else None

    async def get_tickers(self, category: Category) -> list[Ticker]:
        res = await self._public_get(f"/v5/market/tickers?category={category}")
        return res.get("list") or []

    async def get_orderbook(self, category: Category, symbol: str, limit: int = 50) -> Orderbook:
        raw = await self._public_get(f"/v5/market/orderbook?category={category}&symbol={symbol}&limit={limit}")
        return {
            "symbol": raw["s"],
            "bids": [{"price": p, "size": s} for p, s in (raw.get("b") or [])],
            "asks": [{"price": p, "size": s} for p, s in (raw.get("a") or [])],
            "ts": raw["ts"],
            "seq": raw["seq"],
        }

    async def get_funding_history(self, symbol: str, limit: int = 10) -> list[dict[str, Any]]:
        res = await self._public_get(f"/v5/market/funding/history?category=linear&symbol={symbol}&limit={limit}")
        return res["list"]

    async def get_instruments_info(self, category: Category, symbol: str | None = None) -> list[InstrumentInfo]:
        if symbol:
            res = await self._public_get(f"/v5/market/instruments-info?category={category}&symbol={symbol}")
            return res["list"]
        # Page through the full universe via cursor.
        all_items: list[InstrumentInfo] = []
        cursor = ""
        for _ in range(10):
            cur = f"&cursor={quote(cursor)}" if cursor else ""
            res = await self._public_get(f"/v5/market/instruments-info?category={category}&limit=1000{cur}")
            all_items.extend(res["list"])
            nxt = res.get("nextPageCursor")
            if not nxt or len(res["list"]) == 0:
                break
            cursor = nxt
        return all_items

    # ── Private ──────────────────────────────────────────────────────────────
    async def get_wallet_balance(self) -> WalletBalance:
        res = await self._private_get("/v5/account/wallet-balance?accountType=UNIFIED")
        return res["list"][0]

    async def get_positions(self, category: Category, symbol: str | None = None) -> list[Position]:
        qs = f"category={category}&symbol={symbol}" if symbol else f"category={category}&settleCoin=USDT"
        res = await self._private_get(f"/v5/position/list?{qs}")
        return res["list"]

    async def get_closed_pnl(self, category: Category, limit: int = 50) -> list[dict[str, Any]]:
        res = await self._private_get(f"/v5/position/closed-pnl?category={category}&limit={limit}")
        return res["list"]

    async def place_order(self, req: PlaceOrderRequest) -> dict[str, Any]:
        return await self._private_post("/v5/order/create", dict(req))

    async def cancel_order(self, category: Category, symbol: str, order_id: str) -> None:
        await self._private_post("/v5/order/cancel", {"category": category, "symbol": symbol, "orderId": order_id})

    async def cancel_all_orders(self, category: Category, symbol: str | None = None) -> None:
        body: dict[str, Any] = {"category": category}
        if symbol:
            body["symbol"] = symbol
        await self._private_post("/v5/order/cancel-all", body)

    async def set_leverage(self, category: Category, symbol: str, leverage: int) -> None:
        try:
            await self._private_post(
                "/v5/position/set-leverage",
                {"category": category, "symbol": symbol, "buyLeverage": str(leverage), "sellLeverage": str(leverage)},
            )
        except Exception as e:
            # 110043 = leverage already set to the requested value — not an error.
            if "110043" not in str(e):
                raise

    async def set_trading_stop(self, category: Category, symbol: str, **opts: Any) -> None:
        await self._private_post("/v5/position/trading-stop", {"category": category, "symbol": symbol, **opts})

    async def get_open_orders(self, category: Category, symbol: str | None = None) -> list[Any]:
        qs = f"category={category}&symbol={symbol}" if symbol else f"category={category}"
        res = await self._private_get(f"/v5/order/realtime?{qs}")
        return res["list"]

    async def switch_position_mode(self, category: Category, symbol: str, mode: int) -> None:
        await self._private_post("/v5/position/switch-mode", {"category": category, "symbol": symbol, "mode": mode})

    async def amend_order(self, category: Category, symbol: str, order_id: str, **opts: Any) -> dict[str, Any]:
        return await self._private_post(
            "/v5/order/amend", {"category": category, "symbol": symbol, "orderId": order_id, **opts}
        )

    async def get_order_history(self, category: Category, symbol: str | None = None, limit: int = 50) -> list[Any]:
        qs = f"category={category}&limit={limit}"
        if symbol:
            qs += f"&symbol={symbol}"
        res = await self._private_get(f"/v5/order/history?{qs}")
        return res["list"]

    async def get_executions(self, category: Category, symbol: str | None = None, limit: int = 50) -> list[Any]:
        qs = f"category={category}&limit={limit}"
        if symbol:
            qs += f"&symbol={symbol}"
        res = await self._private_get(f"/v5/execution/list?{qs}")
        return res["list"]

    async def create_batch_orders(self, category: Category, requests: list[dict[str, Any]]) -> list[Any]:
        res = await self._private_post("/v5/order/create-batch", {"category": category, "request": requests})
        return (res.get("result") or {}).get("list") or []

    # ── Market signals ────────────────────────────────────────────────────────

    async def get_open_interest(self, category: Category, symbol: str, interval: str = "1h", limit: int = 1) -> list[Any]:
        res = await self._public_get(
            f"/v5/market/open-interest?category={category}&symbol={symbol}&intervalTime={interval}&limit={limit}"
        )
        return res["list"]

    async def get_long_short_ratio(self, category: Category, symbol: str, period: str = "1h", limit: int = 1) -> list[Any]:
        res = await self._public_get(
            f"/v5/market/account-ratio?category={category}&symbol={symbol}&period={period}&limit={limit}"
        )
        return res["list"]

    async def get_historical_volatility(self, category: Category, symbol: str | None = None, period: int = 30) -> list[Any]:
        qs = f"category={category}&period={period}"
        if symbol:
            qs += f"&symbol={symbol}"
        res = await self._public_get(f"/v5/market/historical-volatility?{qs}")
        return res if isinstance(res, list) else res.get("list", [])

    # ── Algo (strategy) orders ────────────────────────────────────────────────

    async def create_algo_order(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._private_post("/v5/strategy/create", params)

    async def stop_algo_order(self, category: str, algo_order_id: str) -> None:
        await self._private_post("/v5/strategy/stop", {"category": category, "algoOrderId": algo_order_id})

    async def list_algo_orders(self, category: str, symbol: str | None = None) -> list[Any]:
        qs = f"category={category}"
        if symbol:
            qs += f"&symbol={symbol}"
        res = await self._private_get(f"/v5/strategy/list?{qs}")
        return res.get("list") or []

    # ── Copy trading ──────────────────────────────────────────────────────────
    async def get_copy_leader_list(self) -> list[dict[str, Any]]:
        res = await self._public_get("/v5/copy-trade/recommend-leader-list")
        return res.get("list") or []

    async def follow_leader(self, leader_mark: str, investment_e8: str) -> None:
        await self._private_post(
            "/v5/copy-trade/private/follower/trade-setting/create",
            {"leaderMark": leader_mark, "investmentE8": investment_e8},
        )

    # ── Trading bots ──────────────────────────────────────────────────────────
    async def validate_spot_grid(self, params: dict[str, Any]) -> Any:
        return await self._bot_post("/v5/grid/validate-input", params)

    async def create_spot_grid(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._bot_post("/v5/grid/create-grid", params)

    async def close_spot_grid(self, grid_id: str, close_mode: int) -> None:
        await self._bot_post("/v5/grid/close-grid", {"grid_id": grid_id, "close_mode": close_mode})

    async def get_spot_grid_detail(self, grid_id: str) -> Any:
        return await self._bot_post("/v5/grid/query-grid-detail", {"grid_id": grid_id})

    async def validate_futures_grid(self, params: dict[str, Any]) -> Any:
        return await self._bot_post("/v5/fgridbot/validate", params)

    async def create_futures_grid(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._bot_post("/v5/fgridbot/create", params)

    async def close_futures_grid(self, bot_id: str) -> None:
        await self._bot_post("/v5/fgridbot/close", {"botId": bot_id})

    async def get_futures_grid_detail(self, bot_id: str) -> Any:
        return await self._bot_post("/v5/fgridbot/detail", {"botId": bot_id})

    async def create_dca_bot(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._bot_post("/v5/dca/create-bot", params)

    async def close_dca_bot(self, bot_id: str, settle_type: int = 1) -> None:
        await self._bot_post("/v5/dca/close-bot", {"botId": bot_id, "settleType": settle_type})

    # ── Internal helpers ──────────────────────────────────────────────────────
    async def _tls_retry(self, fn: Callable[[], Awaitable[T]]) -> T:
        for i in range(3):
            try:
                return await fn()
            except (ssl.SSLError, httpx.ConnectError):
                if i < 2:
                    await asyncio.sleep(1.5 * (i + 1))
                    continue
                raise
        raise RuntimeError("unreachable")

    @staticmethod
    def _headers_to_dict(resp: httpx.Response) -> dict[str, str]:
        return {k.lower(): v for k, v in resp.headers.items()}

    async def _public_get(self, path: str) -> Any:
        async def call() -> Any:
            url = f"{self._base_url}{path}"
            log.debug("GET", url=url)
            resp = await self._http.get(url, headers={"User-Agent": USER_AGENT})
            rate_limiter.update_from_headers(self._headers_to_dict(resp), path.split("?")[0])
            body = resp.json()
            if body["retCode"] != 0:
                raise BybitApiError(body["retCode"], body["retMsg"], path)
            return body["result"]

        return await self._tls_retry(lambda: rate_limiter.schedule_get(call))

    async def _private_get(self, path_with_qs: str) -> Any:
        async def call() -> Any:
            ts = int(time.time() * 1000)
            qs_start = path_with_qs.find("?")
            base_path = path_with_qs[:qs_start] if qs_start >= 0 else path_with_qs
            qs = path_with_qs[qs_start + 1:] if qs_start >= 0 else ""
            param_str = build_get_param_str(ts, self._api_key, qs)
            headers = build_headers(self._auth, ts, param_str, USER_AGENT, X_REFERER)
            url = f"{self._base_url}{path_with_qs}"
            log.debug("GET (auth)", url=url)
            resp = await self._http.get(url, headers=headers)
            rate_limiter.update_from_headers(self._headers_to_dict(resp), base_path)
            body = resp.json()
            if body["retCode"] != 0:
                err = BybitApiError(body["retCode"], body["retMsg"], base_path)
                err.ret_code = body["retCode"]
                raise err
            return body["result"]

        return await self._tls_retry(lambda: rate_limiter.schedule_get(call))

    async def _private_post(self, path: str, payload: dict[str, Any]) -> Any:
        async def call() -> Any:
            ts = int(time.time() * 1000)
            json_body = _dumps(payload)
            param_str = build_post_param_str(ts, self._api_key, json_body)
            headers = build_headers(self._auth, ts, param_str, USER_AGENT, X_REFERER)
            url = f"{self._base_url}{path}"
            log.debug("POST (auth)", url=url)
            resp = await self._http.post(url, headers=headers, content=json_body)
            rate_limiter.update_from_headers(self._headers_to_dict(resp), path)
            body = resp.json()
            if body["retCode"] != 0:
                err = BybitApiError(body["retCode"], body["retMsg"], path)
                err.ret_code = body["retCode"]
                raise err
            return body["result"]

        return await self._tls_retry(lambda: rate_limiter.schedule_post(call))

    async def _bot_post(self, path: str, payload: dict[str, Any]) -> Any:
        async def call() -> Any:
            ts = int(time.time() * 1000)
            json_body = _dumps(payload)
            param_str = build_post_param_str(ts, self._api_key, json_body)
            headers = build_headers(self._auth, ts, param_str, USER_AGENT, X_REFERER)
            url = f"{self._base_url}{path}"
            log.debug("BOT POST", url=url)
            resp = await self._http.post(url, headers=headers, content=json_body)
            rate_limiter.update_from_headers(self._headers_to_dict(resp), path)
            body = resp.json()
            # Grid/bot endpoints return standard V5 retCode, not legacy status_code.
            if "retCode" in body:
                rc = body["retCode"]
                if rc != 0:
                    raise BotApiError(rc, body.get("retMsg", ""), path)
                return body.get("result", {})
            # Legacy bot API format fallback.
            sc = body.get("status_code")
            if sc == 503:
                raise BotApiError(503, "Active investment cycle — retry later", path)
            if sc == 421:
                raise BotApiError(421, "Account ban status", path)
            if sc != 200:
                raise BotApiError(sc, body.get("debug_msg", ""), path)
            return body.get("result", {})

        return await self._tls_retry(lambda: rate_limiter.schedule_post(call))
