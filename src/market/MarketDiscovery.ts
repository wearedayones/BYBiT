/**
 * MarketDiscovery — the agent's eyes on the whole exchange.
 *
 * Instead of a hardcoded symbol list, the agent surveys every linear perp,
 * then keeps only the markets that are (a) liquid enough to trade without
 * bleeding to spread, (b) moving enough to have an edge to capture, and
 * (c) AFFORDABLE at the account's current balance — a $10 account and a
 * $10,000 account see different universes. The result is a ranked watch
 * list that adapts as the balance grows or shrinks, so the decision engine
 * always has something tradable in front of it.
 */
import { BybitClient } from '../exchange/BybitClient';
import { childLogger } from '../core/logger';
import { getDb } from '../persistence/db';
import type { Ticker, InstrumentInfo } from '../exchange/types';

const log = childLogger({ module: 'market-discovery' });

export interface DiscoveryConfig {
  maxSymbols: number;        // how many markets to actively watch
  minTurnover24h: number;    // USDT 24h turnover floor — liquidity gate
  minVolatility: number;     // |24h %| floor — need movement to have an edge
  maxVolatility: number;     // |24h %| ceiling — avoid blow-off / illiquid spikes
  affordabilityLeverage: number; // assumed leverage when testing min-lot affordability
  maxMarginFraction: number; // a min-lot entry may use at most this fraction of equity as margin
}

export const DISCOVERY_DEFAULTS: DiscoveryConfig = {
  maxSymbols: 6,
  minTurnover24h: 1_000,       // tiny "is it actually trading" floor; liquidity
                               // is otherwise a *scoring* preference so the list
                               // adapts to thin testnet and deep mainnet alike
  minVolatility: 0,            // no hard floor — folded into score instead
  maxVolatility: 0.40,         // ≤40% daily — hard safety filter, skip the chaos
  affordabilityLeverage: 5,
  maxMarginFraction: 0.5,      // one min-lot position ≤ 50% of equity in margin
};

export interface ScoredMarket {
  symbol: string;
  turnover24h: number;
  volatility: number;
  price: number;
  minOrderQty: number;
  minNotional: number;   // minOrderQty × price
  score: number;
  affordable: boolean;
}

export class MarketDiscovery {
  private instruments = new Map<string, InstrumentInfo>();
  private lastInstrumentFetch = 0;
  private readonly INSTRUMENT_TTL = 2 * 60 * 60 * 1000; // 2h per skill rule

  constructor(
    private readonly client: BybitClient,
    private readonly cfg: DiscoveryConfig = DISCOVERY_DEFAULTS,
  ) {}

  /**
   * Survey the linear universe and return the symbols worth watching at this
   * equity level, best first. Falls back to majors if the survey fails.
   */
  async discover(equity: number): Promise<string[]> {
    try {
      const [tickers] = await Promise.all([this.client.getTickers('linear')]);
      await this.refreshInstruments();

      const scored: ScoredMarket[] = [];
      for (const t of tickers) {
        const inst = this.instruments.get(t.symbol);
        if (!inst || inst.status !== 'Trading') continue;
        if (inst.quoteCoin !== 'USDT') continue;

        const m = this.scoreMarket(t, inst, equity);
        if (!m) continue;
        scored.push(m);
      }

      // Affordable + within liquidity/volatility band, ranked by score.
      const tradable = scored
        .filter(m => m.affordable)
        .sort((a, b) => b.score - a.score)
        .slice(0, this.cfg.maxSymbols);

      if (tradable.length === 0) {
        log.warn({ equity }, 'No affordable markets found — falling back to majors');
        return this.fallback();
      }

      await this.persist(tradable, equity);

      const symbols = tradable.map(m => m.symbol);
      log.info(
        { equity, count: symbols.length, symbols, cheapest: tradable[tradable.length - 1]?.minNotional },
        'Market discovery complete',
      );
      return symbols;
    } catch (e) {
      log.error({ e }, 'Discovery failed — using fallback symbols');
      return this.fallback();
    }
  }

  private scoreMarket(t: Ticker, inst: InstrumentInfo, equity: number): ScoredMarket | null {
    const price = parseFloat(t.lastPrice);
    const turnover24h = parseFloat(t.turnover24h ?? '0');
    const volatility = Math.abs(parseFloat(t.price24hPcnt ?? '0'));
    const minOrderQty = parseFloat(inst.lotSizeFilter.minOrderQty);
    if (!(price > 0) || !(minOrderQty > 0)) return null;

    const minNotional = minOrderQty * price;

    // Hard gates: must actually be trading, and not in a blow-off. Liquidity
    // and minimum volatility are handled by scoring, not hard cutoffs, so the
    // agent still finds a tradable universe on a thin exchange.
    if (turnover24h < this.cfg.minTurnover24h) return null;
    if (this.cfg.minVolatility > 0 && volatility < this.cfg.minVolatility) return null;
    if (volatility > this.cfg.maxVolatility) return null;

    // Affordability: can we open one min-lot using ≤ maxMarginFraction of equity
    // as margin (at the assumed leverage)? This is what lets a $10 account trade
    // cheap coins while a big account can also touch BTC.
    const requiredMargin = minNotional / this.cfg.affordabilityLeverage;
    const affordable = equity > 0 && requiredMargin <= equity * this.cfg.maxMarginFraction;

    // Score: liquidity (log turnover) rewarded, volatility rewarded mildly,
    // and we prefer markets whose min-lot leaves headroom (smaller margin
    // footprint relative to equity = more room to size and manage risk).
    const liquidityScore = Math.log10(turnover24h + 1) / 10;          // ~0.6–1.0 for $1M–$10B
    const volScore = Math.min(volatility, 0.15) * 2;                   // cap contribution
    const headroom = equity > 0 ? 1 - Math.min(1, requiredMargin / (equity * this.cfg.maxMarginFraction)) : 0;
    const score = liquidityScore + volScore + headroom * 0.3;

    return { symbol: t.symbol, turnover24h, volatility, price, minOrderQty, minNotional, score, affordable };
  }

  private async refreshInstruments(): Promise<void> {
    if (Date.now() - this.lastInstrumentFetch < this.INSTRUMENT_TTL && this.instruments.size > 0) return;
    const list = await this.client.getInstrumentsInfo('linear');
    this.instruments = new Map(list.map(i => [i.symbol, i]));
    this.lastInstrumentFetch = Date.now();
    log.debug({ count: this.instruments.size }, 'Instrument universe refreshed');
  }

  private async persist(markets: ScoredMarket[], equity: number): Promise<void> {
    const sql = getDb();
    for (const m of markets) {
      await sql`
        INSERT INTO discovered_markets (symbol, turnover_24h, volatility, price, min_notional, score, equity_at_discovery)
        VALUES (${m.symbol}, ${m.turnover24h}, ${m.volatility}, ${m.price}, ${m.minNotional}, ${m.score}, ${equity})
      `.catch(() => {});
    }
  }

  private fallback(): string[] {
    return ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'];
  }
}
