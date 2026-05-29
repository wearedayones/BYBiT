/**
 * Market data generator — produces realistic OHLCV candles
 * with regime transitions: trending → ranging → high_volatility → crisis
 */

export type SimRegime = 'trending_up' | 'trending_down' | 'ranging' | 'high_volatility' | 'crisis';

export interface SimBar {
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  ts: number;
}

export interface SimMarket {
  symbol: string;
  price: number;
  regime: SimRegime;
  bars: SimBar[];
  fundingRate: number;
}

const REGIME_DURATIONS: Record<SimRegime, number> = {
  trending_up: 30,
  trending_down: 20,
  ranging: 40,
  high_volatility: 15,
  crisis: 8,
};

const REGIME_TRANSITIONS: Record<SimRegime, SimRegime[]> = {
  trending_up: ['ranging', 'high_volatility', 'trending_down'],
  trending_down: ['ranging', 'crisis', 'trending_up'],
  ranging: ['trending_up', 'trending_down', 'high_volatility'],
  high_volatility: ['ranging', 'crisis', 'trending_up'],
  crisis: ['ranging', 'high_volatility'],
};

export class MarketSimulator {
  private markets: Map<string, SimMarket> = new Map();
  private regimeTick: Map<string, number> = new Map();

  constructor(
    private readonly seeds: Array<{ symbol: string; startPrice: number; regime: SimRegime }>,
  ) {
    for (const s of seeds) {
      const bars = this.generateInitialBars(s.startPrice, 200);
      this.markets.set(s.symbol, {
        symbol: s.symbol,
        price: bars[bars.length - 1].close,
        regime: s.regime,
        bars,
        fundingRate: 0.0001,
      });
      this.regimeTick.set(s.symbol, 0);
    }
  }

  tick(): void {
    for (const [symbol, market] of this.markets) {
      const tick = (this.regimeTick.get(symbol) ?? 0) + 1;
      this.regimeTick.set(symbol, tick);

      // Regime transition
      const duration = REGIME_DURATIONS[market.regime];
      if (tick >= duration) {
        const nexts = REGIME_TRANSITIONS[market.regime];
        market.regime = nexts[Math.floor(Math.random() * nexts.length)];
        this.regimeTick.set(symbol, 0);
        console.log(`  [SIM] ${symbol} regime → ${market.regime.toUpperCase()}`);
      }

      const newBar = this.nextBar(market);
      market.price = newBar.close;
      market.bars.push(newBar);
      if (market.bars.length > 300) market.bars.shift();

      // Funding rate: high in crisis/trending
      market.fundingRate = this.simulateFunding(market.regime);
    }
  }

  getMarket(symbol: string): SimMarket | undefined {
    return this.markets.get(symbol);
  }

  getAllMarkets(): SimMarket[] {
    return [...this.markets.values()];
  }

  private nextBar(market: SimMarket): SimBar {
    const price = market.price;
    const regime = market.regime;

    let drift = 0;
    let volatility = 0;

    switch (regime) {
      case 'trending_up':    drift = 0.0015;  volatility = 0.008; break;
      case 'trending_down':  drift = -0.0015; volatility = 0.008; break;
      case 'ranging':        drift = (Math.random() - 0.5) * 0.001; volatility = 0.005; break;
      case 'high_volatility': drift = (Math.random() - 0.5) * 0.002; volatility = 0.020; break;
      case 'crisis':         drift = -0.003; volatility = 0.035; break;
    }

    const noise = (Math.random() - 0.5) * 2;
    const change = drift + volatility * noise;
    const close = Math.max(price * (1 + change), price * 0.5);
    const open = price;
    const range = price * volatility * Math.abs(noise + 0.5);
    const high = Math.max(open, close) + range * 0.5;
    const low = Math.min(open, close) - range * 0.5;
    const volume = 1000 + Math.random() * 5000;

    return { open, high: Math.max(high, open, close), low: Math.min(low, open, close), close, volume, ts: Date.now() };
  }

  private generateInitialBars(startPrice: number, count: number): SimBar[] {
    const bars: SimBar[] = [];
    let price = startPrice;
    const ts = Date.now() - count * 60_000;
    for (let i = 0; i < count; i++) {
      const change = (Math.random() - 0.5) * 0.012;
      const close = Math.max(price * (1 + change), 100);
      const open = price;
      const range = price * 0.008;
      bars.push({
        open, high: Math.max(open, close) + range, low: Math.min(open, close) - range,
        close, volume: 500 + Math.random() * 2000, ts: ts + i * 60_000,
      });
      price = close;
    }
    return bars;
  }

  private simulateFunding(regime: SimRegime): number {
    switch (regime) {
      case 'trending_up':    return 0.0003 + Math.random() * 0.0005;
      case 'trending_down':  return -0.0003 - Math.random() * 0.0005;
      case 'crisis':         return -0.002 - Math.random() * 0.003;
      case 'high_volatility': return (Math.random() - 0.5) * 0.002;
      default:               return (Math.random() - 0.5) * 0.0002;
    }
  }
}
