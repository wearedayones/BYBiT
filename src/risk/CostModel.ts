/**
 * CostModel — the single source of truth for trading friction.
 *
 * Every trade pays fees (entry + exit) and slippage. A strategy can be
 * directionally correct and still lose money if that friction exceeds its edge.
 * This module turns a signal's stop/TP geometry into an expected value NET of
 * costs, so RiskManager can refuse trades that cannot pay for themselves.
 */
import { FEES, COST_DEFAULTS } from '../config/constants';
import type { Signal } from '../strategy/Strategy';

export type Category = 'spot' | 'linear';

/** Per-side fee as a fraction of notional, given category and maker/taker. */
export function feeRate(category: Category, maker: boolean): number {
  if (category === 'spot') return maker ? FEES.SPOT_MAKER : FEES.SPOT_TAKER;
  return maker ? FEES.PERP_MAKER : FEES.PERP_TAKER;
}

/**
 * Round-trip cost as a fraction of notional: entry fee + exit fee + slippage on
 * both fills. `entryMaker`/`exitMaker` reflect how each leg is expected to fill
 * (maker-first entry, taker SL/TP exit by default).
 */
export function roundTripCostPct(
  category: Category,
  entryMaker: boolean,
  exitMaker: boolean,
): number {
  const fees = feeRate(category, entryMaker) + feeRate(category, exitMaker);
  const slippage = COST_DEFAULTS.SLIPPAGE_PCT * 2; // one slip per fill
  return fees + slippage;
}

export interface ExpectancyResult {
  ev: number;          // expected net return as a fraction of entry price
  rewardRisk: number;  // |tp-entry| / |entry-stop|
  winProb: number;     // probability used (from learned prior or 0.5 fallback)
  avgWin: number;      // |tp-entry| / entry
  avgLoss: number;     // |entry-stop| / entry
  costPct: number;     // round-trip cost fraction applied
}

/**
 * Expected value of a signal net of costs.
 *
 *   EV = winProb·avgWin − (1−winProb)·avgLoss − costPct
 *
 * winProb comes from the learned prior (a win-rate in [0,1]); when no prior is
 * available the engine passes ~1.0, which we clamp to a neutral 0.5 so an
 * unproven signal is not assumed to be a guaranteed winner.
 */
export function expectedValue(
  signal: Signal,
  learnedPrior: number,
  costPct: number,
): ExpectancyResult | null {
  const entry = signal.suggestedEntry;
  const stop = signal.suggestedStop;
  const tp = signal.suggestedTp;
  if (!entry || !stop || !tp || entry <= 0) return null;

  const avgWin = Math.abs(tp - entry) / entry;
  const avgLoss = Math.abs(entry - stop) / entry;
  if (avgLoss <= 0) return null;

  // A learned prior arrives as a win-rate already floored at 0.5 (or 1.0 when
  // unknown). Treat the unknown case as a slight positive bias (0.55) — a
  // directional signal that passed strategy and regime filters isn't a pure
  // coin flip. Once enough trades accumulate the learned rate takes over.
  const winProb = learnedPrior >= 0.999 ? 0.55 : Math.min(0.95, Math.max(0.05, learnedPrior));

  const ev = winProb * avgWin - (1 - winProb) * avgLoss - costPct;
  const rewardRisk = avgWin / avgLoss;

  return { ev, rewardRisk, winProb, avgWin, avgLoss, costPct };
}

export const MIN_NET_EDGE_PCT = COST_DEFAULTS.MIN_NET_EDGE_PCT;
export const MIN_REWARD_RISK = COST_DEFAULTS.MIN_REWARD_RISK;
