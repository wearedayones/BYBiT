import type { Strategy, Signal, StrategyContext } from '../Strategy';
import type { MarketSnapshot } from '../../market/MarketDataService';

export const TrendMomentum: Strategy = {
  name: 'trend_momentum',
  suitableRegimes: ['trending', 'high_volatility'],
  evaluate(snap: MarketSnapshot, _ctx: StrategyContext): Signal {
    const { ema9, ema21, ema50, rsi14, macdHistogram, adxValue } = snap.indicators;
    const price = snap.lastPrice;

    const bullish = ema9 > ema21 && ema21 > ema50 && macdHistogram > 0 && adxValue > 20;
    const bearish = ema9 < ema21 && ema21 < ema50 && macdHistogram < 0 && adxValue > 20;

    const rsiOk = rsi14 > 45 && rsi14 < 75;
    const rsiBearOk = rsi14 < 55 && rsi14 > 25;

    if (bullish && rsiOk) {
      const atr = snap.indicators.atr14;
      return {
        action: 'enter_long', symbol: snap.symbol, strategy: 'trend_momentum',
        confidence: Math.min(0.9, 0.5 + adxValue / 100 + (macdHistogram > 0 ? 0.1 : 0)),
        suggestedEntry: price,
        suggestedStop: price - atr * 2,
        suggestedTp: price + atr * 3,
        rationale: `Bullish EMA stack, ADX=${adxValue.toFixed(1)}, RSI=${rsi14.toFixed(1)}`,
      };
    }
    if (bearish && rsiBearOk) {
      const atr = snap.indicators.atr14;
      return {
        action: 'enter_short', symbol: snap.symbol, strategy: 'trend_momentum',
        confidence: Math.min(0.9, 0.5 + adxValue / 100 + (macdHistogram < 0 ? 0.1 : 0)),
        suggestedEntry: price,
        suggestedStop: price + atr * 2,
        suggestedTp: price - atr * 3,
        rationale: `Bearish EMA stack, ADX=${adxValue.toFixed(1)}, RSI=${rsi14.toFixed(1)}`,
      };
    }

    return { action: 'hold', symbol: snap.symbol, strategy: 'trend_momentum', confidence: 0, rationale: 'No trend signal' };
  },
};
