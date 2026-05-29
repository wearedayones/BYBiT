export type Category = 'spot' | 'linear' | 'inverse' | 'option';
export type Side = 'Buy' | 'Sell';
export type OrderType = 'Market' | 'Limit';
export type TimeInForce = 'GTC' | 'IOC' | 'FOK' | 'PostOnly';
export type PositionIdx = 0 | 1 | 2;
export type OrderStatus = 'New' | 'PartiallyFilled' | 'Untriggered' | 'Filled' | 'PartiallyFilledCanceled' | 'Cancelled' | 'Rejected' | 'Triggered' | 'Deactivated';

export interface BybitResponse<T = unknown> {
  retCode: number;
  retMsg: string;
  result: T;
  time: number;
}

export interface BotResponse<T = unknown> {
  status_code: number;
  debug_msg: string;
  result: T;
}

export interface KlineItem {
  startTime: string;
  openPrice: string;
  highPrice: string;
  lowPrice: string;
  closePrice: string;
  volume: string;
  turnover: string;
}

export interface Ticker {
  symbol: string;
  lastPrice: string;
  markPrice?: string;
  indexPrice?: string;
  prevPrice24h: string;
  price24hPcnt: string;
  highPrice24h: string;
  lowPrice24h: string;
  volume24h: string;
  turnover24h: string;
  bid1Price?: string;
  ask1Price?: string;
  fundingRate?: string;
  nextFundingTime?: string;
  openInterest?: string;
}

export interface OrderbookEntry {
  price: string;
  size: string;
}

export interface Orderbook {
  symbol: string;
  bids: OrderbookEntry[];
  asks: OrderbookEntry[];
  ts: number;
  seq: number;
}

export interface WalletBalance {
  accountType: string;
  totalEquity: string;
  totalAvailableBalance: string;
  totalPerpUPL: string;
  coin: Array<{
    coin: string;
    equity: string;
    availableToWithdraw: string;
    walletBalance: string;
    unrealisedPnl: string;
    cumRealisedPnl: string;
  }>;
}

export interface Position {
  symbol: string;
  side: Side;
  size: string;
  avgPrice: string;
  markPrice: string;
  liqPrice: string;
  unrealisedPnl: string;
  leverage: string;
  positionIdx: PositionIdx;
  positionStatus: string;
  stopLoss: string;
  takeProfit: string;
  trailingStop: string;
}

export interface PlaceOrderRequest {
  category: Category;
  symbol: string;
  side: Side;
  orderType: OrderType;
  qty: string;
  price?: string;
  timeInForce?: TimeInForce;
  positionIdx?: PositionIdx;
  reduceOnly?: boolean;
  stopLoss?: string;
  takeProfit?: string;
  orderLinkId?: string;
  marketUnit?: 'baseCoin' | 'quoteCoin';
}

export interface OrderResult {
  orderId: string;
  orderLinkId: string;
}

export interface ClosedPnlItem {
  symbol: string;
  side: Side;
  qty: string;
  orderPrice: string;
  orderType: OrderType;
  execType: string;
  closedSize: string;
  cumEntryValue: string;
  avgEntryPrice: string;
  cumExitValue: string;
  avgExitPrice: string;
  closedPnl: string;
  fillCount: string;
  createdTime: string;
  updatedTime: string;
}

export interface InstrumentInfo {
  symbol: string;
  baseCoin: string;
  quoteCoin: string;
  status: string;
  lotSizeFilter: {
    minOrderQty: string;
    maxOrderQty: string;
    qtyStep: string;
    basePrecision?: string;
    quotePrecision?: string;
  };
  priceFilter: {
    minPrice: string;
    maxPrice: string;
    tickSize: string;
  };
  leverageFilter?: {
    minLeverage: string;
    maxLeverage: string;
    leverageStep: string;
  };
  copyTrading?: string;
}

export interface CopyLeaderInfo {
  leaderMark: string;
  nickName: string;
  roi: string;
  maxDrawdown: string;
  sharpeRatio?: string;
}

export interface FundingRateItem {
  symbol: string;
  fundingRate: string;
  fundingRateTimestamp: string;
}
