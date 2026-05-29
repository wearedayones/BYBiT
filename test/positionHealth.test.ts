import { describe, it, expect } from 'vitest';
import { PositionHealthManager, type PositionClient } from '../src/positions/PositionHealthManager';
import type { Position, PlaceOrderRequest, OrderResult, Category } from '../src/exchange/types';
import type { MarketSnapshot } from '../src/market/MarketDataService';

function snapshot(over: Partial<MarketSnapshot['indicators']> = {}): MarketSnapshot {
  return {
    symbol: 'BTCUSDT', lastPrice: 102, markPrice: 102, fundingRate: 0, nextFundingMs: 0,
    ohlcv: { open: [], high: [], low: [], close: [], volume: [] },
    indicators: {
      ema9: 110, ema21: 105, ema50: 100,        // bullish stack → no trend flip for a long
      rsi14: 60, macdValue: 0, macdSignal: 0, macdHistogram: 0,
      atr14: 2, atrPct: 0.02, boll: { upper: 0, middle: 0, lower: 0 },
      adxValue: 30, pdi: 0, mdi: 0, ...over,
    },
    orderbook: { bidDepth: 1, askDepth: 1, imbalance: 0 },
  };
}

class FakeClient implements PositionClient {
  stops: Array<{ symbol: string; stopLoss?: string }> = [];
  orders: PlaceOrderRequest[] = [];
  constructor(private pos: Position[]) {}
  async getPositions(_c: Category): Promise<Position[]> { return this.pos; }
  async setTradingStop(_c: Category, symbol: string, opts: { stopLoss?: string }): Promise<void> {
    this.stops.push({ symbol, stopLoss: opts.stopLoss });
  }
  async placeOrder(req: PlaceOrderRequest): Promise<OrderResult> {
    this.orders.push(req);
    return { orderId: 'x', orderLinkId: req.orderLinkId ?? 'x' };
  }
  setPositions(p: Position[]) { this.pos = p; }
}

const longPos = (over: Partial<Position> = {}): Position => ({
  symbol: 'BTCUSDT', side: 'Buy', size: '1.0', avgPrice: '100', markPrice: '102',
  liqPrice: '0', unrealisedPnl: '2', leverage: '5', positionIdx: 0, positionStatus: 'Normal',
  stopLoss: '98', takeProfit: '', trailingStop: '', ...over,
});

describe('PositionHealthManager', () => {
  it('at +1R moves the stop to break-even and banks a partial', async () => {
    // entry 100, stop 98 → riskPerUnit 2; mark 102 → +1R
    const client = new FakeClient([longPos()]);
    const phm = new PositionHealthManager(client);
    await phm.tick([snapshot()]);

    // Break-even stop set at/just above entry.
    expect(client.stops.length).toBeGreaterThan(0);
    expect(parseFloat(client.stops.at(-1)!.stopLoss!)).toBeGreaterThanOrEqual(100);

    // Partial TP: a reduceOnly Sell for half the size.
    const partial = client.orders.find(o => o.reduceOnly && o.side === 'Sell');
    expect(partial).toBeDefined();
    expect(parseFloat(partial!.qty)).toBeCloseTo(0.5, 6);
  });

  it('closes a stale, ~flat position after the max hold window', async () => {
    // Flat position (mark == entry) so no breakeven/partial/trail fires.
    const client = new FakeClient([longPos({ markPrice: '100' })]);
    const phm = new PositionHealthManager(client);
    for (let i = 0; i < 32; i++) await phm.tick([snapshot()]);

    const fullClose = client.orders.find(o => o.reduceOnly && o.orderLinkId?.includes('time_stop'));
    expect(fullClose).toBeDefined();
    expect(parseFloat(fullClose!.qty)).toBeCloseTo(1.0, 6);
  });

  it('exits a long when the EMA trend flips bearish', async () => {
    const client = new FakeClient([longPos({ markPrice: '100' })]);
    const phm = new PositionHealthManager(client);
    // Bearish stack: ema9 < ema21 < ema50 → trend flipped against the long.
    await phm.tick([snapshot({ ema9: 90, ema21: 95, ema50: 100 })]);

    const flip = client.orders.find(o => o.reduceOnly && o.orderLinkId?.includes('regime_flip'));
    expect(flip).toBeDefined();
  });
});
