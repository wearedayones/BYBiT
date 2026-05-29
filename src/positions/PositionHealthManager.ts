/**
 * PositionHealthManager — active management of open positions, ticked every cycle.
 *
 * Entries previously received a static SL/TP and were then abandoned until they
 * hit or the kill switch fired (a $100 sim run held one position for 40 cycles
 * and never closed). This manager locks in winners and culls dead trades:
 *
 *   • Break-even at +1R  — move the stop to entry (+ a cost buffer) once safe.
 *   • ATR trail at +2R   — trail the stop behind the high-water mark.
 *   • Partial TP at +1R  — bank half the position, let the rest run on the trail.
 *   • Time / regime exit — close stale, ~flat trades or ones the trend turned on.
 *
 * It reuses BybitClient.setTradingStop() (previously dead code) and reduceOnly
 * market orders for partial/full closes.
 */
import { childLogger } from '../core/logger';
import { POSITION_MGMT } from '../config/constants';
import type { MarketSnapshot } from '../market/MarketDataService';
import type { Position, OrderResult, PlaceOrderRequest, Category } from '../exchange/types';

const log = childLogger({ module: 'position-health' });

export interface PositionClient {
  getPositions(category: Category, symbol?: string): Promise<Position[]>;
  setTradingStop(category: Category, symbol: string, opts: {
    stopLoss?: string; takeProfit?: string; trailingStop?: string; positionIdx?: number;
  }): Promise<void>;
  placeOrder(req: PlaceOrderRequest): Promise<OrderResult>;
}

interface PosState {
  entry: number;
  riskPerUnit: number;   // |entry - initial stop|
  hwm: number;           // best favorable price seen (high for long, low for short)
  cyclesHeld: number;
  partialTaken: boolean;
  breakevenSet: boolean;
}

export class PositionHealthManager {
  private readonly state = new Map<string, PosState>();

  constructor(private readonly client: PositionClient) {}

  async tick(snapshots: MarketSnapshot[]): Promise<void> {
    const snapBySymbol = new Map(snapshots.map(s => [s.symbol, s]));
    let positions: Position[];
    try {
      positions = await this.client.getPositions('linear');
    } catch (e) {
      log.error({ e }, 'Failed to fetch positions');
      return;
    }

    const liveKeys = new Set<string>();

    for (const pos of positions) {
      const size = parseFloat(pos.size);
      if (size <= 0) continue;
      const key = `${pos.symbol}-${pos.side}`;
      liveKeys.add(key);

      const snap = snapBySymbol.get(pos.symbol);
      if (!snap) continue;

      const isLong = pos.side === 'Buy';
      const entry = parseFloat(pos.avgPrice) || snap.lastPrice;
      const price = parseFloat(pos.markPrice) || snap.lastPrice;
      const atr = snap.indicators.atr14;

      // Initialize per-position memory on first sight.
      let st = this.state.get(key);
      if (!st) {
        const initialStop = parseFloat(pos.stopLoss) || (isLong ? entry - atr * 2 : entry + atr * 2);
        st = {
          entry,
          riskPerUnit: Math.abs(entry - initialStop) || atr * 2 || entry * 0.01,
          hwm: price,
          cyclesHeld: 0,
          partialTaken: false,
          breakevenSet: false,
        };
        this.state.set(key, st);
      }

      st.cyclesHeld += 1;
      st.hwm = isLong ? Math.max(st.hwm, price) : Math.min(st.hwm, price);

      const currentR = isLong
        ? (price - entry) / st.riskPerUnit
        : (entry - price) / st.riskPerUnit;

      // 1) Partial take-profit at +1R — bank half, let the rest ride.
      if (!st.partialTaken && currentR >= POSITION_MGMT.PARTIAL_TP_AT_R) {
        const closeQty = roundDownToStr(size * POSITION_MGMT.PARTIAL_TP_FRACTION, pos.size);
        if (parseFloat(closeQty) > 0) {
          await this.reduce(pos, closeQty, 'partial_tp', currentR);
          st.partialTaken = true;
        }
      }

      // 2) Break-even at +1R — move stop to entry plus a cost buffer.
      if (!st.breakevenSet && currentR >= POSITION_MGMT.BREAKEVEN_AT_R) {
        const be = isLong
          ? entry * (1 + POSITION_MGMT.BREAKEVEN_BUFFER_PCT)
          : entry * (1 - POSITION_MGMT.BREAKEVEN_BUFFER_PCT);
        await this.moveStop(pos, be, 'breakeven');
        st.breakevenSet = true;
      }

      // 3) ATR trailing at +2R — only ever tightens the stop.
      if (currentR >= POSITION_MGMT.TRAIL_START_R) {
        const trail = isLong
          ? st.hwm - atr * POSITION_MGMT.TRAIL_ATR_MULT
          : st.hwm + atr * POSITION_MGMT.TRAIL_ATR_MULT;
        const cur = parseFloat(pos.stopLoss) || 0;
        const tighter = isLong ? trail > cur : (cur === 0 || trail < cur);
        if (tighter) await this.moveStop(pos, trail, 'atr_trail');
      }

      // 4) Time / regime exit — close stale ~flat trades or ones the trend turned on.
      const { ema9, ema21, ema50 } = snap.indicators;
      const trendFlipped = isLong
        ? (ema9 < ema21 && ema21 < ema50)
        : (ema9 > ema21 && ema21 > ema50);
      const stale = st.cyclesHeld > POSITION_MGMT.MAX_HOLD_CYCLES
        && Math.abs(currentR) < POSITION_MGMT.STALE_PNL_R;
      if (trendFlipped || stale) {
        await this.reduce(pos, pos.size, trendFlipped ? 'regime_flip' : 'time_stop', currentR);
        this.state.delete(key);
      }
    }

    // Forget positions that are no longer open (closed by SL/TP on the exchange).
    for (const key of [...this.state.keys()]) {
      if (!liveKeys.has(key)) this.state.delete(key);
    }
  }

  private async moveStop(pos: Position, stop: number, reason: string): Promise<void> {
    try {
      await this.client.setTradingStop('linear', pos.symbol, {
        stopLoss: String(Number(stop.toFixed(2))),
        positionIdx: pos.positionIdx ?? 0,
      });
      log.info({ symbol: pos.symbol, side: pos.side, stop: stop.toFixed(2), reason }, 'Stop adjusted');
    } catch (e) {
      log.warn({ e, symbol: pos.symbol, reason }, 'Failed to adjust stop');
    }
  }

  private async reduce(pos: Position, qty: string, reason: string, r: number): Promise<void> {
    try {
      const side = pos.side === 'Buy' ? 'Sell' : 'Buy';
      await this.client.placeOrder({
        category: 'linear', symbol: pos.symbol, side, orderType: 'Market',
        qty, reduceOnly: true,
        orderLinkId: `phm-${reason}-${pos.symbol}-${Date.now()}`,
      });
      log.info({ symbol: pos.symbol, side: pos.side, qty, reason, r: r.toFixed(2) }, 'Position reduced');
    } catch (e) {
      log.warn({ e, symbol: pos.symbol, reason }, 'Failed to reduce position');
    }
  }
}

/** Floor `qty` to the same decimal precision the position size string uses. */
function roundDownToStr(qty: number, sizeStr: string): string {
  const decimals = (sizeStr.split('.')[1] ?? '').length;
  const factor = Math.pow(10, decimals);
  return String(Math.floor(qty * factor) / factor);
}
