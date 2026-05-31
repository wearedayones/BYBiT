"""Event-driven backtester — the acceptance gate for every strategy.

A strategy is never enabled for live/paper trading until it has *passed* a
backtest here. The engine replays historical klines bar-by-bar, calling the
strategy's own ``evaluate()`` (the exact code that runs live), opens/closes
simulated positions with risk-based sizing and round-trip costs, and reports a
verdict against acceptance thresholds.

Design notes:
  • Reuses ``compute_indicators`` so the indicators the strategy sees in the
    backtest are byte-identical to live — no separate, drifting backtest path.
  • Risk-based sizing: each trade risks ``risk_pct`` of equity; position
    fraction = risk_pct / stop_distance, capped, so the equity curve reflects
    real position scaling rather than fixed notional.
  • Conservative intrabar fills: if a bar's range spans both stop and target,
    the stop is assumed hit first (worst case) — backtests never flatter.
  • Pure and synchronous: takes an OHLCV dict, returns a dataclass. Fully unit
    testable with synthetic price paths, no network or DB.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..market.indicators import OHLCV
from ..market.market_data import MarketSnapshot, compute_indicators
from ..strategy.base import StrategyContext

# Per-trade leverage cap on equity (a 1%-stop signal won't deploy 100× equity).
MAX_POSITION_FRACTION = 2.0


@dataclass
class BacktestConfig:
    starting_equity: float = 1000.0
    risk_pct: float = 0.015           # fraction of equity risked per trade
    cost_pct: float = 0.00175         # round-trip cost (taker both sides + slippage)
    warmup: int = 60                  # bars before the strategy may trade
    window: int = 200                 # trailing bars fed to compute_indicators
    max_hold_bars: int = 48           # force-exit a stale position
    funding_rate: float = 0.0         # constant funding assumption for the run
    # Acceptance thresholds:
    min_trades: int = 10
    min_win_rate: float = 0.40
    min_profit_factor: float = 1.10
    max_drawdown_pct: float = 0.25
    require_positive_return: bool = True


@dataclass
class BTTrade:
    side: str
    entry_idx: int
    entry: float
    exit_idx: int
    exit: float
    r_multiple: float
    net_return: float   # return on equity for this trade (after cost)
    reason: str         # tp | stop | time | end


@dataclass
class BacktestResult:
    strategy: str
    n_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_return_pct: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_like: float = 0.0
    avg_r: float = 0.0
    bars_tested: int = 0
    passed: bool = False
    fail_reasons: list[str] = field(default_factory=list)
    trades: list[BTTrade] = field(default_factory=list)

    def metrics(self) -> dict[str, Any]:
        """Compact JSON-able summary for storage in strategy_registry.backtest."""
        return {
            "n_trades": self.n_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "total_return_pct": round(self.total_return_pct, 3),
            "profit_factor": round(self.profit_factor, 3),
            "max_drawdown_pct": round(self.max_drawdown_pct, 3),
            "sharpe_like": round(self.sharpe_like, 3),
            "avg_r": round(self.avg_r, 3),
            "bars_tested": self.bars_tested,
            "passed": self.passed,
            "fail_reasons": self.fail_reasons,
        }


def _slice_ohlcv(ohlcv: OHLCV, lo: int, hi: int) -> OHLCV:
    return {
        "open": ohlcv["open"][lo:hi],
        "high": ohlcv["high"][lo:hi],
        "low": ohlcv["low"][lo:hi],
        "close": ohlcv["close"][lo:hi],
        "volume": ohlcv["volume"][lo:hi],
    }


def backtest_strategy(
    strategy,
    ohlcv: OHLCV,
    *,
    symbol: str = "BACKTEST",
    cfg: BacktestConfig | None = None,
) -> BacktestResult:
    """Replay ``ohlcv`` bar-by-bar through ``strategy.evaluate`` and score it."""
    cfg = cfg or BacktestConfig()
    res = BacktestResult(strategy=getattr(strategy, "name", "unknown"))

    closes = ohlcv["close"]
    highs = ohlcv["high"]
    lows = ohlcv["low"]
    n = len(closes)
    if n <= cfg.warmup + 5:
        res.fail_reasons.append(f"insufficient data: {n} bars (need > {cfg.warmup + 5})")
        return res

    equity = cfg.starting_equity
    peak = equity
    max_dd = 0.0
    nets: list[float] = []

    # Open position state.
    pos_side: str | None = None
    entry = stop = tp = 0.0
    entry_idx = 0

    res.bars_tested = n - cfg.warmup

    for i in range(cfg.warmup, n):
        # ── manage an open position first (check this bar's range) ──────────
        if pos_side is not None:
            hi_p, lo_p, close_p = highs[i], lows[i], closes[i]
            exit_price: float | None = None
            reason = ""
            if pos_side == "long":
                if lo_p <= stop:                 # worst-case: stop before tp
                    exit_price, reason = stop, "stop"
                elif hi_p >= tp:
                    exit_price, reason = tp, "tp"
            else:  # short
                if hi_p >= stop:
                    exit_price, reason = stop, "stop"
                elif lo_p <= tp:
                    exit_price, reason = tp, "tp"
            if exit_price is None and (i - entry_idx) >= cfg.max_hold_bars:
                exit_price, reason = close_p, "time"

            if exit_price is not None:
                equity, net, r = _close_trade(
                    res, equity, cfg, pos_side, entry, stop, exit_price, entry_idx, i, reason
                )
                nets.append(net)
                peak = max(peak, equity)
                if peak > 0:
                    max_dd = max(max_dd, (peak - equity) / peak)
                pos_side = None
                continue  # one action per bar

        # ── no position: ask the strategy ──────────────────────────────────
        if pos_side is None:
            lo = max(0, i - cfg.window + 1)
            window = _slice_ohlcv(ohlcv, lo, i + 1)
            try:
                indicators = compute_indicators(window)
            except Exception:  # noqa: BLE001 — degenerate window; skip bar
                continue
            snap = MarketSnapshot(
                symbol=symbol, lastPrice=closes[i], markPrice=closes[i],
                fundingRate=cfg.funding_rate, nextFundingMs=0, ohlcv=window,
                indicators=indicators,
                orderbook={"bidDepth": 0.0, "askDepth": 0.0, "imbalance": 0.0},
                research=None,
            )
            try:
                sig = strategy.evaluate(snap, StrategyContext())
            except Exception:  # noqa: BLE001 — a buggy strategy fails its backtest, not the run
                continue
            if sig.action in ("enter_long", "enter_short") and sig.suggestedStop:
                entry = sig.suggestedEntry or closes[i]
                stop = sig.suggestedStop
                # Default TP = 1.5R if the strategy didn't supply one.
                if sig.suggestedTp:
                    tp = sig.suggestedTp
                else:
                    r_dist = abs(entry - stop)
                    tp = entry + r_dist * 1.5 if sig.action == "enter_long" else entry - r_dist * 1.5
                if abs(entry - stop) <= 0:
                    continue
                pos_side = "long" if sig.action == "enter_long" else "short"
                entry_idx = i

    # Close any position still open at the end.
    if pos_side is not None:
        equity, net, _ = _close_trade(
            res, equity, cfg, pos_side, entry, stop, closes[-1], entry_idx, n - 1, "end"
        )
        nets.append(net)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)

    _finalize(res, cfg, equity, nets, max_dd)
    return res


def _close_trade(res, equity, cfg, side, entry, stop, exit_price, entry_idx, exit_idx, reason):
    stop_dist_pct = abs(entry - stop) / entry if entry else 0.0
    if stop_dist_pct <= 0:
        return equity, 0.0, 0.0
    fraction = min(cfg.risk_pct / stop_dist_pct, MAX_POSITION_FRACTION)
    move_pct = (exit_price - entry) / entry
    if side == "short":
        move_pct = -move_pct
    gross = fraction * move_pct
    net = gross - cfg.cost_pct * fraction
    r_multiple = (abs(exit_price - entry) / abs(entry - stop)) * (1 if move_pct > 0 else -1)

    equity *= (1 + net)
    res.trades.append(BTTrade(
        side=side, entry_idx=entry_idx, entry=entry, exit_idx=exit_idx, exit=exit_price,
        r_multiple=r_multiple, net_return=net, reason=reason,
    ))
    return equity, net, r_multiple


def _finalize(res: BacktestResult, cfg: BacktestConfig, equity: float, nets: list[float], max_dd: float) -> None:
    res.n_trades = len(res.trades)
    res.wins = sum(1 for t in res.trades if t.net_return > 0)
    res.losses = res.n_trades - res.wins
    res.win_rate = (res.wins / res.n_trades) if res.n_trades else 0.0
    res.total_return_pct = (equity / cfg.starting_equity - 1) * 100
    res.max_drawdown_pct = max_dd * 100

    gains = sum(n for n in nets if n > 0)
    losses = abs(sum(n for n in nets if n < 0))
    res.profit_factor = (gains / losses) if losses > 0 else (math.inf if gains > 0 else 0.0)

    if len(nets) > 1:
        mean = sum(nets) / len(nets)
        var = sum((x - mean) ** 2 for x in nets) / len(nets)
        sd = math.sqrt(var)
        res.sharpe_like = (mean / sd * math.sqrt(len(nets))) if sd > 0 else 0.0
    res.avg_r = (sum(t.r_multiple for t in res.trades) / res.n_trades) if res.n_trades else 0.0

    # ── acceptance verdict ───────────────────────────────────────────────────
    fails: list[str] = []
    if res.n_trades < cfg.min_trades:
        fails.append(f"too few trades: {res.n_trades} < {cfg.min_trades}")
    if res.win_rate < cfg.min_win_rate:
        fails.append(f"win rate {res.win_rate:.2f} < {cfg.min_win_rate}")
    if res.profit_factor < cfg.min_profit_factor:
        pf = "inf" if res.profit_factor == math.inf else f"{res.profit_factor:.2f}"
        fails.append(f"profit factor {pf} < {cfg.min_profit_factor}")
    if res.max_drawdown_pct > cfg.max_drawdown_pct * 100:
        fails.append(f"max drawdown {res.max_drawdown_pct:.1f}% > {cfg.max_drawdown_pct * 100:.0f}%")
    if cfg.require_positive_return and res.total_return_pct <= 0:
        fails.append(f"non-positive return {res.total_return_pct:.2f}%")

    res.fail_reasons = fails
    res.passed = len(fails) == 0
