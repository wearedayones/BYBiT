/**
 * ExecutionRouter — maker-first order placement with a taker fallback.
 *
 * Entries are not usually time-critical, so we first rest a PostOnly limit order
 * just inside the touch to pay the maker fee (0.020% on perps) instead of the
 * taker fee (0.055%). If it does not fill within MAKER_WAIT_MS we cancel and
 * cross the spread with a market order. Halving the entry fee is the single
 * largest controllable cost lever for a frequently-trading agent.
 */
import { childLogger } from '../core/logger';
import { EXECUTION_DEFAULTS } from '../config/constants';
import type { PlaceOrderRequest, OrderResult, Side, Category } from '../exchange/types';

const log = childLogger({ module: 'execution' });

/** Minimal surface the router needs — satisfied by BybitClient and the sim client. */
export interface OrderClient {
  placeOrder(req: PlaceOrderRequest): Promise<OrderResult>;
  cancelOrder(category: Category, symbol: string, orderId: string): Promise<void>;
  getOpenOrders(category: Category, symbol?: string): Promise<unknown[]>;
}

export type FillType = 'maker' | 'taker';

export interface EnterParams {
  category: Category;
  symbol: string;
  side: Side;
  qty: number;
  refPrice: number;          // current/last price used to derive the limit
  stopLoss?: number;
  takeProfit?: number;
  orderLinkId: string;
}

export interface EnterResult {
  orderId: string;
  orderLinkId: string;
  fillType: FillType;
  limitPrice: number;
}

export class ExecutionRouter {
  /**
   * @param isSim   In simulation the PostOnly limit is assumed to fill as maker
   *                immediately (the synthetic spread is negligible); the live
   *                path waits and falls back to a market order.
   * @param waitMs  Override the maker wait window (tests / sim use 0).
   */
  constructor(
    private readonly client: OrderClient,
    private readonly isSim: boolean,
    private readonly waitMs: number = EXECUTION_DEFAULTS.MAKER_WAIT_MS,
  ) {}

  async enter(p: EnterParams): Promise<EnterResult> {
    const limitPrice = this.makerLimitPrice(p.side, p.refPrice);
    const base = {
      category: p.category, symbol: p.symbol, side: p.side,
      qty: String(p.qty),
      stopLoss: p.stopLoss != null ? String(p.stopLoss) : undefined,
      takeProfit: p.takeProfit != null ? String(p.takeProfit) : undefined,
    };

    // 1) Try maker — PostOnly limit just inside the touch.
    const makerOrder = await this.client.placeOrder({
      ...base,
      orderType: 'Limit',
      timeInForce: 'PostOnly',
      price: String(limitPrice),
      orderLinkId: `${p.orderLinkId}-m`,
    });

    if (this.isSim) {
      return { ...makerOrder, fillType: 'maker', limitPrice };
    }

    // 2) Live: give the limit a chance to fill, then check if it's still resting.
    if (this.waitMs > 0) await sleep(this.waitMs);
    const stillOpen = await this.client
      .getOpenOrders(p.category, p.symbol)
      .then(list => list.some(o => orderLinkIdOf(o) === `${p.orderLinkId}-m`))
      .catch(() => false);

    if (!stillOpen) {
      return { ...makerOrder, fillType: 'maker', limitPrice };
    }

    // 3) Taker fallback — cancel the resting limit and cross with a market order.
    await this.client.cancelOrder(p.category, p.symbol, makerOrder.orderId).catch(e =>
      log.warn({ e, symbol: p.symbol }, 'Failed to cancel unfilled maker order before fallback'),
    );
    const takerOrder = await this.client.placeOrder({
      ...base,
      orderType: 'Market',
      orderLinkId: `${p.orderLinkId}-t`,
    });
    log.info({ symbol: p.symbol, side: p.side }, 'Maker order unfilled — fell back to taker');
    return { ...takerOrder, fillType: 'taker', limitPrice: p.refPrice };
  }

  private makerLimitPrice(side: Side, refPrice: number): number {
    const off = EXECUTION_DEFAULTS.MAKER_OFFSET_PCT;
    // Rest below the market to buy, above to sell — keeps the order passive (maker).
    const raw = side === 'Buy' ? refPrice * (1 - off) : refPrice * (1 + off);
    return Number(raw.toFixed(2));
  }
}

function orderLinkIdOf(o: unknown): string | undefined {
  if (o && typeof o === 'object' && 'orderLinkId' in o) {
    return String((o as { orderLinkId: unknown }).orderLinkId);
  }
  return undefined;
}

function sleep(ms: number): Promise<void> { return new Promise(r => setTimeout(r, ms)); }
