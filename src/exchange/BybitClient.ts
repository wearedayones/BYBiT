import { request } from 'undici';
import {
  MAINNET_REST, TESTNET_REST, USER_AGENT, X_REFERER, RECV_WINDOW,
} from '../config/constants';
import {
  buildGetParamStr, buildPostParamStr, buildHeaders,
} from './signer';
import type { BybitAuth } from './signer';
import { rateLimiter } from './rateLimiter';
import { BybitApiError, BotApiError } from '../core/errors';
import { childLogger } from '../core/logger';
import type {
  BybitResponse, BotResponse, Category, KlineItem, Ticker, Orderbook,
  WalletBalance, Position, PlaceOrderRequest, OrderResult, ClosedPnlItem,
  InstrumentInfo, CopyLeaderInfo, FundingRateItem,
} from './types';

const log = childLogger({ module: 'bybit-client' });

export class BybitClient {
  private baseUrl: string;

  constructor(
    private readonly auth: BybitAuth,
    isTestnet: boolean,
  ) {
    this.baseUrl = isTestnet ? TESTNET_REST : MAINNET_REST;
  }

  private get apiKey(): string { return this.auth.apiKey; }

  setTestnet(isTestnet: boolean) {
    this.baseUrl = isTestnet ? TESTNET_REST : MAINNET_REST;
  }

  // ─── Public (no auth) ────────────────────────────────────────────────────────

  async getServerTime(): Promise<number> {
    const res = await this.publicGet<{ timeSecond: string; timeNano: string }>('/v5/market/time');
    return parseInt(res.timeSecond) * 1000;
  }

  async getKline(category: Category, symbol: string, interval: string, limit = 200): Promise<KlineItem[]> {
    const res = await this.publicGet<{ list: string[][] }>(`/v5/market/kline?category=${category}&symbol=${symbol}&interval=${interval}&limit=${limit}`);
    return res.list.map(k => ({
      startTime: k[0], openPrice: k[1], highPrice: k[2], lowPrice: k[3],
      closePrice: k[4], volume: k[5], turnover: k[6],
    }));
  }

  async getTicker(category: Category, symbol: string): Promise<Ticker | null> {
    const res = await this.publicGet<{ list: Ticker[] }>(`/v5/market/tickers?category=${category}&symbol=${symbol}`);
    return res.list[0] ?? null;
  }

  async getOrderbook(category: Category, symbol: string, limit = 50): Promise<Orderbook> {
    return this.publicGet<Orderbook>(`/v5/market/orderbook?category=${category}&symbol=${symbol}&limit=${limit}`);
  }

  async getFundingHistory(symbol: string, limit = 10): Promise<FundingRateItem[]> {
    const res = await this.publicGet<{ list: FundingRateItem[] }>(`/v5/market/funding/history?category=linear&symbol=${symbol}&limit=${limit}`);
    return res.list;
  }

  async getInstrumentsInfo(category: Category, symbol?: string): Promise<InstrumentInfo[]> {
    const qs = symbol ? `?category=${category}&symbol=${symbol}` : `?category=${category}&limit=500`;
    const res = await this.publicGet<{ list: InstrumentInfo[] }>(`/v5/market/instruments-info${qs}`);
    return res.list;
  }

  // ─── Private (authenticated) ─────────────────────────────────────────────────

  async getWalletBalance(): Promise<WalletBalance> {
    const res = await this.privateGet<{ list: WalletBalance[] }>('/v5/account/wallet-balance?accountType=UNIFIED');
    return res.list[0];
  }

  async getPositions(category: Category, symbol?: string): Promise<Position[]> {
    const qs = symbol ? `category=${category}&symbol=${symbol}` : `category=${category}&settleCoin=USDT`;
    const res = await this.privateGet<{ list: Position[] }>(`/v5/position/list?${qs}`);
    return res.list;
  }

  async getClosedPnl(category: Category, limit = 50): Promise<ClosedPnlItem[]> {
    const res = await this.privateGet<{ list: ClosedPnlItem[] }>(`/v5/position/closed-pnl?category=${category}&limit=${limit}`);
    return res.list;
  }

  async placeOrder(req: PlaceOrderRequest): Promise<OrderResult> {
    return this.privatePost<OrderResult>('/v5/order/create', req as unknown as Record<string, unknown>);
  }

  async cancelOrder(category: Category, symbol: string, orderId: string): Promise<void> {
    await this.privatePost('/v5/order/cancel', { category, symbol, orderId });
  }

  async cancelAllOrders(category: Category, symbol?: string): Promise<void> {
    const body: Record<string, unknown> = { category };
    if (symbol) body.symbol = symbol;
    await this.privatePost('/v5/order/cancel-all', body);
  }

  async setLeverage(category: Category, symbol: string, leverage: number): Promise<void> {
    await this.privatePost('/v5/position/set-leverage', {
      category, symbol,
      buyLeverage: String(leverage),
      sellLeverage: String(leverage),
    });
  }

  async setTradingStop(category: Category, symbol: string, opts: {
    stopLoss?: string; takeProfit?: string; trailingStop?: string;
    positionIdx?: number;
  }): Promise<void> {
    await this.privatePost('/v5/position/trading-stop', { category, symbol, ...opts });
  }

  async getOpenOrders(category: Category, symbol?: string): Promise<unknown[]> {
    const qs = symbol ? `category=${category}&symbol=${symbol}` : `category=${category}`;
    const res = await this.privateGet<{ list: unknown[] }>(`/v5/order/realtime?${qs}`);
    return res.list;
  }

  async switchPositionMode(category: Category, symbol: string, mode: 0 | 3): Promise<void> {
    await this.privatePost('/v5/position/switch-mode', { category, symbol, mode });
  }

  // ─── Copy Trading ─────────────────────────────────────────────────────────────

  async getCopyLeaderList(): Promise<CopyLeaderInfo[]> {
    const res = await this.publicGet<{ list: CopyLeaderInfo[] }>('/v5/copy-trade/recommend-leader-list');
    return res.list ?? [];
  }

  async followLeader(leaderMark: string, investmentE8: string): Promise<void> {
    await this.privatePost('/v5/copy-trade/private/follower/trade-setting/create', {
      leaderMark,
      investmentE8,
    });
  }

  // ─── Trading Bots (mainnet only) ──────────────────────────────────────────────

  async validateSpotGrid(params: Record<string, unknown>): Promise<unknown> {
    return this.botPost('/v5/grid/validate-input', params);
  }

  async createSpotGrid(params: Record<string, unknown>): Promise<{ grid_id: string }> {
    return this.botPost<{ grid_id: string }>('/v5/grid/create-grid', params);
  }

  async closeSpotGrid(gridId: string, closeMode: 1 | 2 | 3 | 4): Promise<void> {
    await this.botPost('/v5/grid/close-grid', { grid_id: gridId, close_mode: closeMode });
  }

  async getSpotGridDetail(gridId: string): Promise<unknown> {
    return this.botPost('/v5/grid/query-grid-detail', { grid_id: gridId });
  }

  async validateFuturesGrid(params: Record<string, unknown>): Promise<unknown> {
    return this.botPost('/v5/fgridbot/validate', params);
  }

  async createFuturesGrid(params: Record<string, unknown>): Promise<{ bot_id: string }> {
    return this.botPost<{ bot_id: string }>('/v5/fgridbot/create', params);
  }

  async closeFuturesGrid(botId: string, stopType?: string): Promise<void> {
    const body: Record<string, unknown> = { bot_id: botId };
    if (stopType) body.stop_type = stopType;
    await this.botPost('/v5/fgridbot/close', body);
  }

  async createDcaBot(params: Record<string, unknown>): Promise<{ bot_id: string }> {
    return this.botPost<{ bot_id: string }>('/v5/dca/create-bot', params);
  }

  async closeDcaBot(botId: string, closeMode: 1 | 2 | 3): Promise<void> {
    await this.botPost('/v5/dca/close-bot', { bot_id: botId, close_mode: closeMode });
  }

  async getMartingaleLimit(params: Record<string, unknown>): Promise<unknown> {
    return this.botPost('/v5/fmartingalebot/getlimit', params);
  }

  async createMartingaleBot(params: Record<string, unknown>): Promise<{ bot_id: string }> {
    return this.botPost<{ bot_id: string }>('/v5/fmartingalebot/create', params);
  }

  async closeMartingaleBot(botId: string, stopType?: string): Promise<void> {
    const body: Record<string, unknown> = { bot_id: botId };
    if (stopType) body.stop_type = stopType;
    await this.botPost('/v5/fmartingalebot/close', body);
  }

  async createComboBot(params: Record<string, unknown>): Promise<{ bot_id: string }> {
    return this.botPost<{ bot_id: string }>('/v5/fcombobot/create', params);
  }

  async closeComboBot(botId: string, stopType?: string): Promise<void> {
    const body: Record<string, unknown> = { bot_id: botId };
    if (stopType) body.stop_type = stopType;
    await this.botPost('/v5/fcombobot/close', body);
  }

  // ─── Algo Strategy Orders ──────────────────────────────────────────────────────

  async createStrategyOrder(params: Record<string, unknown>): Promise<unknown> {
    return this.privatePost('/v5/strategy/create', params);
  }

  async stopStrategyOrder(strategyId: string, category: string): Promise<void> {
    await this.privatePost('/v5/strategy/stop', { strategyId, category });
  }

  // ─── Internal helpers ─────────────────────────────────────────────────────────

  private async publicGet<T>(path: string): Promise<T> {
    return rateLimiter.scheduleGet(async () => {
      const url = `${this.baseUrl}${path}`;
      log.debug({ url }, 'GET');
      const res = await request(url, {
        method: 'GET',
        headers: { 'User-Agent': USER_AGENT },
      });
      rateLimiter.updateFromHeaders(res.headers as Record<string, string | string[]>, path.split('?')[0]);
      const body = await res.body.json() as BybitResponse<T>;
      if (body.retCode !== 0) {
        throw new BybitApiError(body.retCode, body.retMsg, path);
      }
      return body.result;
    });
  }

  private async privateGet<T>(pathWithQs: string): Promise<T> {
    return rateLimiter.scheduleGet(async () => {
      const ts = Date.now();
      const qsStart = pathWithQs.indexOf('?');
      const basePath = qsStart >= 0 ? pathWithQs.slice(0, qsStart) : pathWithQs;
      const qs = qsStart >= 0 ? pathWithQs.slice(qsStart + 1) : '';
      const paramStr = buildGetParamStr(ts, this.apiKey, qs);
      const reqHeaders = buildHeaders(this.auth, ts, paramStr, USER_AGENT, X_REFERER);
      const url = `${this.baseUrl}${pathWithQs}`;
      log.debug({ url }, 'GET (auth)');
      const res = await request(url, { method: 'GET', headers: reqHeaders });
      rateLimiter.updateFromHeaders(res.headers as Record<string, string | string[]>, basePath);
      const body = await res.body.json() as BybitResponse<T>;
      if (body.retCode !== 0) {
        const err = new BybitApiError(body.retCode, body.retMsg, basePath);
        Object.assign(err, { retCode: body.retCode });
        throw err;
      }
      return body.result;
    });
  }

  private async privatePost<T>(path: string, payload: Record<string, unknown>): Promise<T> {
    return rateLimiter.schedulePost(async () => {
      const ts = Date.now();
      const jsonBody = JSON.stringify(payload);
      const paramStr = buildPostParamStr(ts, this.apiKey, jsonBody);
      const reqHeaders = buildHeaders(this.auth, ts, paramStr, USER_AGENT, X_REFERER);
      const url = `${this.baseUrl}${path}`;
      log.debug({ url, payload }, 'POST (auth)');
      const res = await request(url, { method: 'POST', headers: reqHeaders, body: jsonBody });
      rateLimiter.updateFromHeaders(res.headers as Record<string, string | string[]>, path);
      const body = await res.body.json() as BybitResponse<T>;
      if (body.retCode !== 0) {
        const err = new BybitApiError(body.retCode, body.retMsg, path);
        Object.assign(err, { retCode: body.retCode });
        throw err;
      }
      return body.result;
    });
  }

  private async botPost<T>(path: string, payload: Record<string, unknown>): Promise<T> {
    return rateLimiter.schedulePost(async () => {
      const ts = Date.now();
      const jsonBody = JSON.stringify(payload);
      const paramStr = buildPostParamStr(ts, this.apiKey, jsonBody);
      const reqHeaders = buildHeaders(this.auth, ts, paramStr, USER_AGENT, X_REFERER);
      const url = `${this.baseUrl}${path}`;
      log.debug({ url, payload }, 'BOT POST');
      const res = await request(url, { method: 'POST', headers: reqHeaders, body: jsonBody });
      rateLimiter.updateFromHeaders(res.headers as Record<string, string | string[]>, path);
      const body = await res.body.json() as BotResponse<T>;
      if (body.status_code === 503) {
        throw new BotApiError(503, 'Active investment cycle — retry later', path);
      }
      if (body.status_code === 421) {
        throw new BotApiError(421, 'Account ban status', path);
      }
      if (body.status_code !== 200) {
        throw new BotApiError(body.status_code, body.debug_msg, path);
      }
      return body.result;
    });
  }
}
