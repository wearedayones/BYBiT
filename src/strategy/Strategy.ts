import type { MarketSnapshot, Regime } from '../market/MarketDataService';

export type Action = 'enter_long' | 'enter_short' | 'exit' | 'hold';

export interface Signal {
  action: Action;
  symbol: string;
  strategy: string;
  confidence: number;   // 0..1
  suggestedEntry?: number;
  suggestedStop?: number;
  suggestedTp?: number;
  rationale: string;
}

export interface StrategyContext {
  currentPositionSide?: 'Buy' | 'Sell';
  currentPositionSize?: number;
  currentEntryPrice?: number;
  strategyWeight: number;
  learnedPrior?: number;   // from LearningEngine
  recencyDecay?: number;
}

export interface Strategy {
  name: string;
  suitableRegimes: Regime[];
  evaluate(snapshot: MarketSnapshot, ctx: StrategyContext): Signal;
}
