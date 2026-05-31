"""Exchange types — ports src/exchange/types.ts.

Runtime values are plain dicts (the JSON Bybit returns); these TypedDicts document
the shapes and give editors/type-checkers something to hold onto. Literal aliases
mirror the TS string-union types.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

Category = Literal["spot", "linear", "inverse", "option"]
Side = Literal["Buy", "Sell"]
OrderType = Literal["Market", "Limit"]
TimeInForce = Literal["GTC", "IOC", "FOK", "PostOnly"]
PositionIdx = Literal[0, 1, 2]


class KlineItem(TypedDict):
    startTime: str
    openPrice: str
    highPrice: str
    lowPrice: str
    closePrice: str
    volume: str
    turnover: str


class OrderbookEntry(TypedDict):
    price: str
    size: str


class Orderbook(TypedDict):
    symbol: str
    bids: list[OrderbookEntry]
    asks: list[OrderbookEntry]
    ts: int
    seq: int


class PlaceOrderRequest(TypedDict, total=False):
    category: Category
    symbol: str
    side: Side
    orderType: OrderType
    qty: str
    price: str
    timeInForce: TimeInForce
    positionIdx: PositionIdx
    reduceOnly: bool
    stopLoss: str
    takeProfit: str
    orderLinkId: str
    marketUnit: Literal["baseCoin", "quoteCoin"]


class OrderResult(TypedDict):
    orderId: str
    orderLinkId: str


# The remaining shapes (Ticker, Position, WalletBalance, InstrumentInfo, ClosedPnlItem,
# CopyLeaderInfo, FundingRateItem) are consumed as plain dicts; alias them for clarity.
Ticker = dict[str, Any]
Position = dict[str, Any]
WalletBalance = dict[str, Any]
InstrumentInfo = dict[str, Any]
ClosedPnlItem = dict[str, Any]
CopyLeaderInfo = dict[str, Any]
FundingRateItem = dict[str, Any]
