import type { Strategy, Signal, StrategyContext } from '../Strategy';
import type { MarketSnapshot } from '../../market/MarketDataService';

export const MeanReversion: Strategy = {
  name: 'mean_reversion',
  suitableRegimes: ['ranging'],
  evaluate(snap: MarketSnapshot, _ctx: StrategyContext): Signal {
    const { rsi14, boll, atrPct } = snap.indicators;
    const price = snap.lastPrice;

    if (atrPct > 0.03) {
      return { action: 'hold', symbol: snap.symbol, strategy: 'mean_reversion', confidence: 0, rationale: 'Too volatile for mean reversion' };
    }

    const oversold = price <= boll.lower && rsi14 < 35;
    const overbought = price >= boll.upper && rsi14 > 65;

    if (oversold) {
      return {
        action: 'enter_long', symbol: snap.symbol, strategy: 'mean_reversion',
        confidence: Math.min(0.85, 0.4 + (35 - rsi14) / 50),
        suggestedEntry: price,
        suggestedStop: price - snap.indicators.atr14 * 1.5,
        suggestedTp: boll.middle,
        rationale: `Price at lower Bollinger, RSI=${rsi14.toFixed(1)}`,
      };
    }
    if (overbought) {
      return {
        action: 'enter_short', symbol: snap.symbol, strategy: 'mean_reversion',
        confidence: Math.min(0.85, 0.4 + (rsi14 - 65) / 50),
        suggestedEntry: price,
        suggestedStop: price + snap.indicators.atr14 * 1.5,
        suggestedTp: boll.middle,
        rationale: `Price at upper Bollinger, RSI=${rsi14.toFixed(1)}`,
      };
    }

    return { action: 'hold', symbol: snap.symbol, strategy: 'mean_reversion', confidence: 0, rationale: 'No mean-reversion signal' };
  },
};
