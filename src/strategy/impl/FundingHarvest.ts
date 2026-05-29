import type { Strategy, Signal, StrategyContext } from '../Strategy';
import type { MarketSnapshot } from '../../market/MarketDataService';

// Delta-neutral funding rate harvesting:
// High positive funding → short perp + long spot → earn funding every 8h
// High negative funding → long perp + short spot → earn funding every 8h
const FUNDING_THRESHOLD_ANNUALIZED = 0.50; // 50% APR

export const FundingHarvest: Strategy = {
  name: 'funding_harvest',
  suitableRegimes: ['trending', 'ranging', 'high_volatility'],
  evaluate(snap: MarketSnapshot, _ctx: StrategyContext): Signal {
    const fundingRate = snap.fundingRate;
    // Bybit charges funding every 8h → 3x per day → annualize: rate * 3 * 365
    const annualized = fundingRate * 3 * 365;

    if (Math.abs(annualized) < FUNDING_THRESHOLD_ANNUALIZED) {
      return { action: 'hold', symbol: snap.symbol, strategy: 'funding_harvest', confidence: 0, rationale: `Funding ${(annualized * 100).toFixed(1)}% APR below threshold` };
    }

    // Positive funding: longs pay shorts → be short perp
    if (annualized > 0) {
      return {
        action: 'enter_short',
        symbol: snap.symbol,
        strategy: 'funding_harvest',
        confidence: Math.min(0.95, 0.5 + Math.abs(annualized) / 2),
        rationale: `High positive funding ${(annualized * 100).toFixed(1)}% APR — harvest via short perp`,
        suggestedEntry: snap.markPrice,
        suggestedStop: snap.markPrice * 1.05,
      };
    }
    // Negative funding: shorts pay longs → be long perp
    return {
      action: 'enter_long',
      symbol: snap.symbol,
      strategy: 'funding_harvest',
      confidence: Math.min(0.95, 0.5 + Math.abs(annualized) / 2),
      rationale: `High negative funding ${(annualized * 100).toFixed(1)}% APR — harvest via long perp`,
      suggestedEntry: snap.markPrice,
      suggestedStop: snap.markPrice * 0.95,
    };
  },
};
