import { BybitClient } from '../exchange/BybitClient';
import { getDb } from '../persistence/db';
import { childLogger } from '../core/logger';
import type { PortfolioState } from '../risk/RiskManager';

const log = childLogger({ module: 'portfolio' });

export class PortfolioManager {
  private state: PortfolioState = {
    equity: 0, peakEquity: 0, daySartEquity: 0,
    dailyRealizedPnl: 0, openPositionCount: 0,
    dailyLossLimit: 0.08, killLevelPct: 0.20,
    circuitBreakerPct: 0.10, maxRiskPct: 0.015,
  };
  private openSymbols: Set<string> = new Set();

  constructor(private readonly client: BybitClient) {}

  getState(): PortfolioState { return { ...this.state }; }

  /** Symbols that currently have a non-zero position on the exchange. */
  hasOpenPosition(symbol: string): boolean { return this.openSymbols.has(symbol); }

  async refresh(): Promise<PortfolioState> {
    try {
      const [wallet, positions, agentRow] = await Promise.all([
        this.client.getWalletBalance(),
        this.client.getPositions('linear'),
        getDb()`SELECT * FROM agent_state WHERE id = 'singleton' LIMIT 1`,
      ]);

      const ag = agentRow[0];
      const equity = parseFloat(wallet?.totalEquity ?? '0');
      const unrealizedPnl = parseFloat(wallet?.totalPerpUPL ?? '0');
      const openPos = positions.filter(p => parseFloat(p.size) > 0);
      const openPositionCount = openPos.length;
      this.openSymbols = new Set(openPos.map(p => p.symbol));

      const today = new Date().toISOString().slice(0, 10);
      const tradingDay = ag?.trading_day ? new Date(ag.trading_day).toISOString().slice(0, 10) : '';

      let daySartEquity = parseFloat(ag?.day_start_equity ?? '0') || equity;
      let dailyRealizedPnl = parseFloat(ag?.daily_realized_pnl ?? '0') || 0;

      if (tradingDay !== today) {
        daySartEquity = equity;
        dailyRealizedPnl = 0;
        await getDb()`
          UPDATE agent_state SET
            trading_day = ${today}::date,
            day_start_equity = ${equity},
            daily_realized_pnl = 0,
            updated_at = now()
          WHERE id = 'singleton'
        `;
      }

      const peakEquity = Math.max(parseFloat(ag?.peak_equity ?? '0') || equity, equity);

      this.state = {
        equity,
        peakEquity,
        daySartEquity,
        dailyRealizedPnl,
        openPositionCount,
        dailyLossLimit: parseFloat(ag?.daily_loss_limit ?? '0.08'),
        killLevelPct: parseFloat(ag?.kill_level_pct ?? '0.20'),
        circuitBreakerPct: parseFloat(ag?.circuit_breaker_pct ?? '0.10'),
        maxRiskPct: parseFloat(ag?.max_risk_pct ?? '0.015'),
      };

      const drawdownPct = peakEquity > 0 ? (peakEquity - equity) / peakEquity : 0;

      await getDb()`
        UPDATE agent_state SET equity = ${equity}, peak_equity = ${peakEquity}, updated_at = now()
        WHERE id = 'singleton'
      `;

      await getDb()`
        INSERT INTO equity_snapshots (total_equity, available, unrealized_pnl, realized_pnl_day, open_positions, drawdown_pct, env)
        VALUES (
          ${equity}, ${parseFloat(wallet?.totalAvailableBalance ?? '0')},
          ${unrealizedPnl}, ${dailyRealizedPnl}, ${openPositionCount}, ${drawdownPct},
          ${ag?.env ?? 'testnet'}
        )
      `;

      log.debug({ equity, drawdownPct: (drawdownPct * 100).toFixed(2) + '%', openPositionCount }, 'Portfolio refreshed');
      return this.getState();
    } catch (err) {
      log.error({ err }, 'Portfolio refresh failed');
      return this.getState();
    }
  }
}
