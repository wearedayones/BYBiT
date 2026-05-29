import { request } from 'undici';
import { childLogger } from '../core/logger';

const log = childLogger({ module: 'news-research' });

const CACHE_TTL_MS = 15 * 60 * 1000; // 15 minutes

export interface ResearchData {
  fearGreedIndex: number;
  fearGreedLabel: string;
  globalSentimentScore: number;    // -1 (bearish) to +1 (bullish)
  symbolSentimentScore: number;    // headline score for this symbol
  trendingRank?: number;           // CoinGecko rank (1–7) if trending
  sources: string[];
}

interface CachedResearch {
  global: GlobalResearch;
  expiresAt: number;
}

interface GlobalResearch {
  fearGreedIndex: number;
  fearGreedLabel: string;
  globalSentimentScore: number;
  trendingCoins: Array<{ id: string; symbol: string; rank: number }>;
  headlines: string[];
}

const POSITIVE_KEYWORDS = [
  'bull', 'surge', 'rally', 'gain', 'rise', 'breakout', 'bullish', 'ath',
  'adoption', 'milestone', 'upgrade', 'launch', 'partnership', 'growth',
  'profit', 'recover', 'buy', 'pump', 'support', 'accumulate', 'inflow',
];

const NEGATIVE_KEYWORDS = [
  'bear', 'crash', 'drop', 'fall', 'decline', 'bearish', 'hack', 'exploit',
  'sec', 'ban', 'regulation', 'lawsuit', 'fraud', 'collapse', 'plunge',
  'fear', 'warning', 'sell', 'dump', 'outflow', 'liquidation', 'vulnerable',
];

const SYMBOL_NAMES: Record<string, string[]> = {
  BTCUSDT: ['bitcoin', 'btc'],
  ETHUSDT: ['ethereum', 'eth', 'ether'],
  SOLUSDT: ['solana', 'sol'],
};

export class NewsResearchService {
  private cache: CachedResearch | null = null;
  private readonly newsApiKey?: string;

  constructor(newsApiKey?: string) {
    this.newsApiKey = newsApiKey;
  }

  async getResearch(symbol: string): Promise<ResearchData> {
    const global = await this.getGlobal();
    const names = SYMBOL_NAMES[symbol] ?? [symbol.replace('USDT', '').toLowerCase()];

    const symbolHeadlines = global.headlines.filter(h =>
      names.some(n => h.toLowerCase().includes(n))
    );

    const symbolSentimentScore = symbolHeadlines.length > 0
      ? this.scoreHeadlines(symbolHeadlines)
      : global.globalSentimentScore;

    const trendingEntry = global.trendingCoins.find(c =>
      names.some(n => c.symbol.toLowerCase() === n || c.id.toLowerCase().includes(n))
    );

    return {
      fearGreedIndex: global.fearGreedIndex,
      fearGreedLabel: global.fearGreedLabel,
      globalSentimentScore: global.globalSentimentScore,
      symbolSentimentScore,
      trendingRank: trendingEntry?.rank,
      sources: [],
    };
  }

  private async getGlobal(): Promise<GlobalResearch> {
    const now = Date.now();
    if (this.cache && now < this.cache.expiresAt) return this.cache.global;

    const [fearGreed, trending, headlines] = await Promise.all([
      this.fetchFearGreed().catch(() => ({ index: 50, label: 'Neutral' })),
      this.fetchTrending().catch(() => []),
      this.fetchHeadlines().catch(() => []),
    ]);

    const globalSentimentScore = this.scoreHeadlines(headlines);

    const global: GlobalResearch = {
      fearGreedIndex: fearGreed.index,
      fearGreedLabel: fearGreed.label,
      globalSentimentScore,
      trendingCoins: trending,
      headlines,
    };

    this.cache = { global, expiresAt: now + CACHE_TTL_MS };
    log.debug({
      fearGreed: fearGreed.index,
      label: fearGreed.label,
      sentiment: globalSentimentScore.toFixed(2),
      trending: trending.map(t => t.symbol),
    }, 'Research refreshed');

    return global;
  }

  private async fetchFearGreed(): Promise<{ index: number; label: string }> {
    const res = await request('https://api.alternative.me/fng/?limit=1', {
      method: 'GET',
      headersTimeout: 8_000,
      bodyTimeout: 8_000,
    });
    const body = await res.body.json() as {
      data: Array<{ value: string; value_classification: string }>;
    };
    const item = body.data?.[0];
    if (!item) return { index: 50, label: 'Neutral' };
    return {
      index: parseInt(item.value, 10),
      label: item.value_classification,
    };
  }

  private async fetchTrending(): Promise<Array<{ id: string; symbol: string; rank: number }>> {
    const res = await request('https://api.coingecko.com/api/v3/search/trending', {
      method: 'GET',
      headers: { 'Accept': 'application/json' },
      headersTimeout: 8_000,
      bodyTimeout: 8_000,
    });
    const body = await res.body.json() as {
      coins: Array<{ item: { id: string; symbol: string; score: number } }>;
    };
    return (body.coins ?? []).map((c, i) => ({
      id: c.item.id,
      symbol: c.item.symbol,
      rank: i + 1,
    }));
  }

  private async fetchHeadlines(): Promise<string[]> {
    if (this.newsApiKey) {
      return this.fetchNewsApi(this.newsApiKey);
    }
    return this.fetchRss();
  }

  private async fetchNewsApi(apiKey: string): Promise<string[]> {
    const url = `https://newsapi.org/v2/everything?q=bitcoin+OR+ethereum+OR+crypto&language=en&sortBy=publishedAt&pageSize=30&apiKey=${apiKey}`;
    const res = await request(url, {
      method: 'GET',
      headersTimeout: 8_000,
      bodyTimeout: 8_000,
    });
    const body = await res.body.json() as { articles: Array<{ title: string; description: string }> };
    return (body.articles ?? []).map(a => `${a.title ?? ''} ${a.description ?? ''}`);
  }

  private async fetchRss(): Promise<string[]> {
    const rssUrl = 'https://www.coindesk.com/arc/outboundfeeds/rss/';
    try {
      const res = await request(rssUrl, {
        method: 'GET',
        headersTimeout: 8_000,
        bodyTimeout: 10_000,
      });
      const xml = await res.body.text();
      const matches = xml.match(/<title><!\[CDATA\[([^\]]+)\]\]><\/title>|<title>([^<]+)<\/title>/g) ?? [];
      return matches
        .map(m => m.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim())
        .filter(t => t.length > 10 && !t.toLowerCase().includes('coindesk'));
    } catch {
      return [];
    }
  }

  private scoreHeadlines(headlines: string[]): number {
    if (headlines.length === 0) return 0;
    let pos = 0;
    let neg = 0;
    for (const h of headlines) {
      const lower = h.toLowerCase();
      pos += POSITIVE_KEYWORDS.filter(k => lower.includes(k)).length;
      neg += NEGATIVE_KEYWORDS.filter(k => lower.includes(k)).length;
    }
    const total = pos + neg;
    if (total === 0) return 0;
    return Math.max(-1, Math.min(1, (pos - neg) / total));
  }
}

export function computeSentimentMultiplier(
  research: ResearchData | undefined,
  action: 'enter_long' | 'enter_short',
): number {
  if (!research) return 1.0;

  const { fearGreedIndex, symbolSentimentScore } = research;

  // Fear & Greed component: contrarian (fear = buy opportunity, greed = sell opportunity)
  let fgMultiplier = 1.0;
  if (fearGreedIndex < 20) fgMultiplier = action === 'enter_long' ? 1.15 : 0.85;
  else if (fearGreedIndex < 40) fgMultiplier = action === 'enter_long' ? 1.05 : 0.95;
  else if (fearGreedIndex > 80) fgMultiplier = action === 'enter_short' ? 1.15 : 0.85;
  else if (fearGreedIndex > 60) fgMultiplier = action === 'enter_short' ? 1.05 : 0.95;

  // News sentiment component: confirmatory (positive sentiment = buy, negative = sell)
  let newsMultiplier = 1.0;
  if (symbolSentimentScore > 0.3) newsMultiplier = action === 'enter_long' ? 1.08 : 0.92;
  else if (symbolSentimentScore < -0.3) newsMultiplier = action === 'enter_short' ? 1.08 : 0.92;

  // Blend: fear/greed carries 60% weight, news 40%
  return fgMultiplier * 0.6 + newsMultiplier * 0.4 + 0.0; // weighted average keeps range ~0.85–1.15
}
