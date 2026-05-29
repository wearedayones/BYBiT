/**
 * Simulated Bybit client — no real API calls.
 * Uses MarketSimulator for price data and tracks virtual positions/balance.
 */
import type { MarketSimulator, SimMarket } from './MarketSimulator';
import type {
  KlineItem, Ticker, Orderbook, WalletBalance, Position,
  PlaceOrderRequest, OrderResult, ClosedPnlItem, InstrumentInfo, FundingRateItem,
} from '../exchange/types';
import { childLogger } from '../core/logger';

const log = childLogger({ module: 'sim-client' });

interface SimPosition {
  symbol: string;
  side: 'Buy' | 'Sell';
  size: number;
  avgEntry: number;
  stopLoss?: number;
  takeProfit?: number;
}

interface VirtualAccount {
  balance: number;
  positions: Map<string, SimPosition>;
  closedTrades: Array<{ symbol: string; pnl: number; closedAt: Date }>;
}

export class SimulatedBybitClient {
  private orderCounter = 1;
  private account: VirtualAccount = {
    balance: 10_000,
    positions: new Map(),
    closedTrades: [],
  };

  constructor(private readonly sim: MarketSimulator) {}

  setTestnet(_v: boolean) {}

  async getServerTime(): Promise<number> {
    return Date.now();
  }

  async getKline(_category: string, symbol: string, _interval: string, limit = 200): Promise<KlineItem[]> {
    const market = this.sim.getMarket(symbol);
    if (!market) return [];
    return market.bars.slice(-limit).map(b => ({
      startTime: String(b.ts),
      openPrice: String(b.open.toFixed(2)),
      highPrice: String(b.high.toFixed(2)),
      lowPrice: String(b.low.toFixed(2)),
      closePrice: String(b.close.toFixed(2)),
      volume: String(b.volume.toFixed(4)),
      turnover: String((b.close * b.volume).toFixed(2)),
    }));
  }

  async getTicker(_category: string, symbol: string): Promise<Ticker | null> {
    const market = this.sim.getMarket(symbol);
    if (!market) return null;
    const price = market.price.toFixed(2);
    return {
      symbol,
      lastPrice: price,
      markPrice: price,
      indexPrice: price,
      prevPrice24h: String((market.price * 0.99).toFixed(2)),
      price24hPcnt: '0.01',
      highPrice24h: String((market.price * 1.02).toFixed(2)),
      lowPrice24h: String((market.price * 0.98).toFixed(2)),
      volume24h: '1000000',
      turnover24h: String((market.price * 1_000_000).toFixed(2)),
      bid1Price: String((market.price * 0.9999).toFixed(2)),
      ask1Price: String((market.price * 1.0001).toFixed(2)),
      fundingRate: String(market.fundingRate.toFixed(6)),
      nextFundingTime: String(Date.now() + 8 * 3_600_000),
      openInterest: String((Math.random() * 50_000_000).toFixed(0)),
    };
  }

  async getOrderbook(_category: string, symbol: string): Promise<Orderbook> {
    const market = this.sim.getMarket(symbol);
    const price = market?.price ?? 50_000;
    const bids = Array.from({ length: 10 }, (_, i) => ({
      price: String((price * (1 - (i + 1) * 0.0001)).toFixed(2)),
      size: String((Math.random() * 2).toFixed(4)),
    }));
    const asks = Array.from({ length: 10 }, (_, i) => ({
      price: String((price * (1 + (i + 1) * 0.0001)).toFixed(2)),
      size: String((Math.random() * 2).toFixed(4)),
    }));
    return { symbol, bids, asks, ts: Date.now(), seq: 1 };
  }

  async getFundingHistory(symbol: string): Promise<FundingRateItem[]> {
    const market = this.sim.getMarket(symbol);
    return [{ symbol, fundingRate: String(market?.fundingRate ?? 0.0001), fundingRateTimestamp: String(Date.now()) }];
  }

  async getInstrumentsInfo(_category: string, symbol?: string): Promise<InstrumentInfo[]> {
    const symbols = symbol ? [symbol] : ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'];
    return symbols.map(s => ({
      symbol: s, baseCoin: s.replace('USDT', ''), quoteCoin: 'USDT', status: 'Trading',
      lotSizeFilter: { minOrderQty: '0.001', maxOrderQty: '1000', qtyStep: '0.001' },
      priceFilter: { minPrice: '0.01', maxPrice: '9999999', tickSize: '0.01' },
      leverageFilter: { minLeverage: '1', maxLeverage: '100', leverageStep: '1' },
      copyTrading: 'both',
    }));
  }

  async getWalletBalance(): Promise<WalletBalance> {
    const unrealizedPnl = this.computeUnrealizedPnl();
    return {
      accountType: 'UNIFIED',
      totalEquity: String((this.account.balance + unrealizedPnl).toFixed(2)),
      totalAvailableBalance: String((this.account.balance * 0.9).toFixed(2)),
      totalPerpUPL: String(unrealizedPnl.toFixed(2)),
      coin: [{
        coin: 'USDT',
        equity: String((this.account.balance + unrealizedPnl).toFixed(2)),
        availableToWithdraw: String((this.account.balance * 0.9).toFixed(2)),
        walletBalance: String(this.account.balance.toFixed(2)),
        unrealisedPnl: String(unrealizedPnl.toFixed(2)),
        cumRealisedPnl: String(this.account.closedTrades.reduce((s, t) => s + t.pnl, 0).toFixed(2)),
      }],
    };
  }

  async getPositions(_category: string, symbol?: string): Promise<Position[]> {
    const positions = [...this.account.positions.values()];
    const filtered = symbol ? positions.filter(p => p.symbol === symbol) : positions;
    return filtered.map(p => {
      const market = this.sim.getMarket(p.symbol);
      const price = market?.price ?? p.avgEntry;
      const multiplier = p.side === 'Buy' ? 1 : -1;
      const unrealizedPnl = multiplier * p.size * (price - p.avgEntry);
      return {
        symbol: p.symbol, side: p.side,
        size: p.size.toFixed(4), avgPrice: p.avgEntry.toFixed(2),
        markPrice: price.toFixed(2), liqPrice: '0',
        unrealisedPnl: unrealizedPnl.toFixed(2), leverage: '5',
        positionIdx: 0,
        positionStatus: 'Normal',
        stopLoss: p.stopLoss?.toFixed(2) ?? '',
        takeProfit: p.takeProfit?.toFixed(2) ?? '',
        trailingStop: '',
      };
    });
  }

  async getClosedPnl(): Promise<ClosedPnlItem[]> {
    return this.account.closedTrades.slice(-20).map(t => ({
      symbol: t.symbol, side: 'Buy', qty: '0.01',
      orderPrice: '0', orderType: 'Market', execType: 'Trade',
      closedSize: '0.01', cumEntryValue: '0', avgEntryPrice: '0',
      cumExitValue: '0', avgExitPrice: '0',
      closedPnl: t.pnl.toFixed(4),
      fillCount: '1', createdTime: String(t.closedAt.getTime()),
      updatedTime: String(t.closedAt.getTime()),
    }));
  }

  async placeOrder(req: PlaceOrderRequest): Promise<OrderResult> {
    const market = this.sim.getMarket(req.symbol);
    const price = market?.price ?? 50_000;
    const qty = parseFloat(req.qty);
    const orderId = `sim-${this.orderCounter++}`;
    const orderLinkId = req.orderLinkId ?? orderId;

    // Check for existing position to close
    const existing = this.account.positions.get(`${req.symbol}-${req.side === 'Buy' ? 'Sell' : 'Buy'}`);
    if (req.reduceOnly && existing) {
      const pnl = (req.side === 'Sell' ? 1 : -1) * qty * (price - existing.avgEntry);
      this.account.balance += pnl;
      this.account.closedTrades.push({ symbol: req.symbol, pnl, closedAt: new Date() });
      this.account.positions.delete(`${req.symbol}-${existing.side}`);
      log.info({ symbol: req.symbol, side: req.side, qty, pnl: pnl.toFixed(2) }, '[SIM] Position closed');
      return { orderId, orderLinkId };
    }

    // Open/add to position (with 0.05% simulated slippage)
    const slippage = req.side === 'Buy' ? 1.0005 : 0.9995;
    const fillPrice = price * slippage;
    const posKey = `${req.symbol}-${req.side}`;
    const existing2 = this.account.positions.get(posKey);

    if (existing2) {
      const totalQty = existing2.size + qty;
      existing2.avgEntry = (existing2.avgEntry * existing2.size + fillPrice * qty) / totalQty;
      existing2.size = totalQty;
    } else {
      this.account.positions.set(posKey, {
        symbol: req.symbol, side: req.side, size: qty, avgEntry: fillPrice,
        stopLoss: req.stopLoss ? parseFloat(req.stopLoss) : undefined,
        takeProfit: req.takeProfit ? parseFloat(req.takeProfit) : undefined,
      });
    }

    log.info({
      symbol: req.symbol, side: req.side, qty, fillPrice: fillPrice.toFixed(2),
      sl: req.stopLoss, tp: req.takeProfit,
    }, '[SIM] Order filled');
    return { orderId, orderLinkId };
  }

  async cancelOrder(): Promise<void> {}
  async cancelAllOrders(): Promise<void> {
    log.info('[SIM] All orders cancelled');
  }

  async setLeverage(): Promise<void> {}
  async setTradingStop(): Promise<void> {}
  async getOpenOrders(): Promise<unknown[]> { return []; }
  async switchPositionMode(): Promise<void> {}

  async getCopyLeaderList() {
    return [
      { leaderMark: 'sim-leader-1', nickName: 'AlphaTrader', roi: '0.45', maxDrawdown: '0.08', sharpeRatio: '2.1' },
      { leaderMark: 'sim-leader-2', nickName: 'BetaQuant', roi: '0.30', maxDrawdown: '0.12', sharpeRatio: '1.5' },
      { leaderMark: 'sim-leader-3', nickName: 'GammaFund', roi: '0.22', maxDrawdown: '0.18', sharpeRatio: '1.1' },
    ];
  }

  async followLeader(leaderMark: string): Promise<void> {
    log.info({ leaderMark }, '[SIM] Following copy leader');
  }

  async validateSpotGrid(): Promise<unknown> { return { check_code: 'ok' }; }
  async createSpotGrid(params: Record<string, unknown>): Promise<{ grid_id: string }> {
    const id = `sim-grid-${Date.now()}`;
    log.info({ symbol: params.symbol, id }, '[SIM] Spot grid bot created');
    return { grid_id: id };
  }
  async closeSpotGrid(): Promise<void> {}
  async getSpotGridDetail(): Promise<unknown> { return { profit: (Math.random() * 10).toFixed(2) }; }
  async validateFuturesGrid(): Promise<unknown> { return {}; }
  async createFuturesGrid(): Promise<{ bot_id: string }> { return { bot_id: `sim-fgrid-${Date.now()}` }; }
  async closeFuturesGrid(): Promise<void> {}
  async createDcaBot(): Promise<{ bot_id: string }> { return { bot_id: `sim-dca-${Date.now()}` }; }
  async closeDcaBot(): Promise<void> {}
  async getMartingaleLimit(): Promise<unknown> { return {}; }
  async createMartingaleBot(): Promise<{ bot_id: string }> { return { bot_id: `sim-mart-${Date.now()}` }; }
  async closeMartingaleBot(): Promise<void> {}
  async createComboBot(): Promise<{ bot_id: string }> { return { bot_id: `sim-combo-${Date.now()}` }; }
  async closeComboBot(): Promise<void> {}
  async createStrategyOrder(): Promise<unknown> { return {}; }
  async stopStrategyOrder(): Promise<void> {}

  private computeUnrealizedPnl(): number {
    let total = 0;
    for (const pos of this.account.positions.values()) {
      const market = this.sim.getMarket(pos.symbol);
      const price = market?.price ?? pos.avgEntry;
      const multiplier = pos.side === 'Buy' ? 1 : -1;
      total += multiplier * pos.size * (price - pos.avgEntry);
    }
    return total;
  }

  // Check stop-loss / take-profit on every tick
  checkStopsAndTakeProfit(): void {
    for (const [key, pos] of this.account.positions) {
      const market = this.sim.getMarket(pos.symbol);
      if (!market) continue;
      const price = market.price;

      let hit = false;
      let hitType = '';
      if (pos.stopLoss && pos.side === 'Buy' && price <= pos.stopLoss) { hit = true; hitType = 'SL'; }
      if (pos.stopLoss && pos.side === 'Sell' && price >= pos.stopLoss) { hit = true; hitType = 'SL'; }
      if (pos.takeProfit && pos.side === 'Buy' && price >= pos.takeProfit) { hit = true; hitType = 'TP'; }
      if (pos.takeProfit && pos.side === 'Sell' && price <= pos.takeProfit) { hit = true; hitType = 'TP'; }

      if (hit) {
        const multiplier = pos.side === 'Buy' ? 1 : -1;
        const pnl = multiplier * pos.size * (price - pos.avgEntry);
        this.account.balance += pnl;
        this.account.closedTrades.push({ symbol: pos.symbol, pnl, closedAt: new Date() });
        this.account.positions.delete(key);
        log.info({ symbol: pos.symbol, hitType, pnl: pnl.toFixed(2), price: price.toFixed(2) }, `[SIM] ${hitType} hit`);
      }
    }
  }

  getBalance(): number { return this.account.balance; }
  getVirtualPositions() { return [...this.account.positions.values()]; }
}
