import type { Strategy, Signal, StrategyContext } from '../Strategy';
import type { MarketSnapshot } from '../../market/MarketDataService';

export const Breakout: Strategy = {
  name: 'breakout',
  suitableRegimes: ['ranging', 'high_volatility'],
  evaluate(snap: MarketSnapshot, _ctx: StrategyContext): Signal {
    const { atr14, atrPct, adxValue, rsi14 } = snap.indicators;
    const { close, high, low } = snap.ohlcv;
    const price = snap.lastPrice;

    if (close.length < 20) {
      return { action: 'hold', symbol: snap.symbol, strategy: 'breakout', confidence: 0, rationale: 'Not enough data' };
    }

    const lookback = 20;
    const recentHighs = high.slice(-lookback);
    const recentLows = low.slice(-lookback);
    const rangeHigh = Math.max(...recentHighs.slice(0, -1));
    const rangeLow = Math.min(...recentLows.slice(0, -1));

    const breakoutUp = price > rangeHigh && adxValue < 30 && rsi14 > 55;
    const breakoutDown = price < rangeLow && adxValue < 30 && rsi14 < 45;

    if (breakoutUp) {
      return {
        action: 'enter_long', symbol: snap.symbol, strategy: 'breakout',
        confidence: 0.65,
        suggestedEntry: price,
        suggestedStop: rangeLow,
        suggestedTp: price + (price - rangeLow) * 1.5,
        rationale: `Breakout above ${rangeHigh.toFixed(2)}, ATRpct=${(atrPct * 100).toFixed(2)}%`,
      };
    }
    if (breakoutDown) {
      return {
        action: 'enter_short', symbol: snap.symbol, strategy: 'breakout',
        confidence: 0.65,
        suggestedEntry: price,
        suggestedStop: rangeHigh,
        suggestedTp: price - (rangeHigh - price) * 1.5,
        rationale: `Breakdown below ${rangeLow.toFixed(2)}, ATR14=${atr14.toFixed(2)}`,
      };
    }

    return { action: 'hold', symbol: snap.symbol, strategy: 'breakout', confidence: 0, rationale: 'No breakout' };
  },
};
