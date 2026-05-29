import { BybitClient } from '../exchange/BybitClient';
import { childLogger } from '../core/logger';
import { INSTRUMENT_CACHE_TTL_MS, MAINNET_REST } from '../config/constants';
import type { KlineItem, Ticker, Orderbook, InstrumentInfo, FundingRateItem } from '../exchange/types';
import { ema, rsi, macd, atr, bollinger, adx, last } from './indicators';
import type { OHLCVData } from './indicators';
import { request } from 'undici';

const log = childLogger({ module: 'market' });

export interface MarketSnapshot {
  symbol: string;
  lastPrice: number;
  markPrice: number;
  fundingRate: number;
  nextFundingMs: number;
  ohlcv: OHLCVData;
  indicators: {
    ema9: number; ema21: number; ema50: number;
    rsi14: number;
    macdValue: number; macdSignal: number; macdHistogram: number;
    atr14: number; atrPct: number;
    boll: { upper: number; middle: number; lower: number };
    adxValue: number; pdi: number; mdi: number;
  };
  orderbook: { bidDepth: number; askDepth: number; imbalance: number };
  openInterest?: number;
}

export type Regime = 'trending' | 'ranging' | 'high_volatility' | 'crisis';

export class MarketDataService {
  private readonly snapshotCache = new Map<string, { data: MarketSnapshot; expiresAt: number }>();
  private readonly instrumentCache = new Map<string, { data: InstrumentInfo; expiresAt: number }>();

  constructor(private readonly client: BybitClient) {}

  async getSnapshot(symbol: string, category: 'spot' | 'linear' = 'linear'): Promise<MarketSnapshot> {
    const cached = this.snapshotCache.get(symbol);
    if (cached && Date.now() < cached.expiresAt) return cached.data;

    const [klines, ticker, orderbook, funding] = await Promise.all([
      this.client.getKline(category, symbol, '15', 200),
      this.client.getTicker(category, symbol),
      this.client.getOrderbook(category, symbol, 50),
      this.getFundingRatePublic(symbol).catch(() => 0),
    ]);

    const ohlcv = klinesToOhlcv(klines);
    const indicators = computeIndicators(ohlcv);
    const bidDepth = orderbook.bids.reduce((s, b) => s + parseFloat(b.size), 0);
    const askDepth = orderbook.asks.reduce((s, a) => s + parseFloat(a.size), 0);
    const imbalance = (bidDepth - askDepth) / (bidDepth + askDepth + 1e-9);

    const snap: MarketSnapshot = {
      symbol,
      lastPrice: parseFloat(ticker?.lastPrice ?? '0'),
      markPrice: parseFloat(ticker?.markPrice ?? ticker?.lastPrice ?? '0'),
      fundingRate: typeof funding === 'number' ? funding : parseFloat((funding as FundingRateItem).fundingRate ?? '0'),
      nextFundingMs: parseInt(ticker?.nextFundingTime ?? '0'),
      ohlcv,
      indicators,
      orderbook: { bidDepth, askDepth, imbalance },
      openInterest: parseFloat(ticker?.openInterest ?? '0') || undefined,
    };

    this.snapshotCache.set(symbol, { data: snap, expiresAt: Date.now() + 30_000 });
    return snap;
  }

  async getInstrument(category: 'spot' | 'linear', symbol: string): Promise<InstrumentInfo | null> {
    const cached = this.instrumentCache.get(`${category}:${symbol}`);
    if (cached && Date.now() < cached.expiresAt) return cached.data;
    const list = await this.client.getInstrumentsInfo(category, symbol);
    const info = list[0] ?? null;
    if (info) {
      this.instrumentCache.set(`${category}:${symbol}`, {
        data: info,
        expiresAt: Date.now() + INSTRUMENT_CACHE_TTL_MS,
      });
    }
    return info;
  }

  classifyRegime(snap: MarketSnapshot): Regime {
    const { adxValue, atrPct } = snap.indicators;
    const fundingAbs = Math.abs(snap.fundingRate);

    if (atrPct > 0.05 || fundingAbs > 0.002) return 'crisis';
    if (adxValue > 25) return 'trending';
    if (atrPct > 0.025) return 'high_volatility';
    return 'ranging';
  }

  private async getFundingRatePublic(symbol: string): Promise<number> {
    try {
      const url = `${MAINNET_REST}/v5/market/tickers?category=linear&symbol=${symbol}`;
      const res = await request(url, { method: 'GET' });
      const body = await res.body.json() as { retCode: number; result: { list: Array<{ fundingRate: string }> } };
      if (body.retCode === 0 && body.result.list[0]) {
        return parseFloat(body.result.list[0].fundingRate ?? '0');
      }
    } catch { /* ignore */ }
    return 0;
  }
}

function klinesToOhlcv(klines: KlineItem[]): OHLCVData {
  const sorted = [...klines].reverse();
  return {
    open: sorted.map(k => parseFloat(k.openPrice)),
    high: sorted.map(k => parseFloat(k.highPrice)),
    low: sorted.map(k => parseFloat(k.lowPrice)),
    close: sorted.map(k => parseFloat(k.closePrice)),
    volume: sorted.map(k => parseFloat(k.volume)),
  };
}

function computeIndicators(ohlcv: OHLCVData) {
  const closes = ohlcv.close;
  const e9 = ema(closes, 9);
  const e21 = ema(closes, 21);
  const e50 = ema(closes, 50);
  const r = rsi(closes, 14);
  const m = macd(closes);
  const a = atr(ohlcv, 14);
  const b = bollinger(closes, 20, 2);
  const d = adx(ohlcv, 14);

  const lastClose = last(closes) ?? 0;
  const lastAtr = last(a) ?? 0;
  const lastMacd = last(m);
  const lastBoll = last(b);
  const lastAdx = last(d);

  return {
    ema9: last(e9) ?? 0,
    ema21: last(e21) ?? 0,
    ema50: last(e50) ?? 0,
    rsi14: last(r) ?? 50,
    macdValue: lastMacd?.MACD ?? 0,
    macdSignal: lastMacd?.signal ?? 0,
    macdHistogram: lastMacd?.histogram ?? 0,
    atr14: lastAtr,
    atrPct: lastClose > 0 ? lastAtr / lastClose : 0,
    boll: lastBoll ?? { upper: 0, middle: 0, lower: 0 },
    adxValue: lastAdx?.adx ?? 0,
    pdi: lastAdx?.pdi ?? 0,
    mdi: lastAdx?.mdi ?? 0,
  };
}
