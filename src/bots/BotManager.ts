import { BybitClient } from '../exchange/BybitClient';
import { getDb } from '../persistence/db';
import { childLogger } from '../core/logger';
import type { MarketSnapshot, Regime } from '../market/MarketDataService';
import type { PortfolioState } from '../risk/RiskManager';

const log = childLogger({ module: 'bot-manager' });

export class BotManager {
  constructor(
    private readonly client: BybitClient,
    private readonly isPaper: boolean,
  ) {}

  async tick(snapshots: MarketSnapshot[], portfolio: PortfolioState): Promise<void> {
    await this.monitorActiveBots();
    await this.considerNewBots(snapshots, portfolio);
  }

  private async monitorActiveBots(): Promise<void> {
    const sql = getDb();
    const bots = await sql`SELECT * FROM bot_instances WHERE status = 'active' AND is_paper = ${this.isPaper}`;

    for (const bot of bots) {
      try {
        if (this.isPaper) {
          // Paper mode: update virtual state
          await sql`UPDATE bot_instances SET updated_at = now() WHERE id = ${bot.id}`;
          continue;
        }

        let detail: Record<string, unknown> | null = null;
        if (bot.bot_type === 'spot_grid' && bot.exchange_bot_id) {
          detail = await this.client.getSpotGridDetail(bot.exchange_bot_id) as Record<string, unknown>;
        } else if (bot.bot_type === 'futures_grid' && bot.exchange_bot_id) {
          detail = { bot_id: bot.exchange_bot_id };
        }

        if (detail) {
          const pnl = parseFloat(String(detail?.profit ?? detail?.realized_pnl ?? '0'));
          await sql`
            INSERT INTO bot_performance (bot_instance_id, realized_pnl, unrealized_pnl, roi)
            VALUES (${bot.id}, ${pnl}, 0, 0)
          `;
        }
      } catch (e) {
        log.warn({ e, botId: bot.id }, 'Error monitoring bot');
      }
    }
  }

  private async considerNewBots(snapshots: MarketSnapshot[], portfolio: PortfolioState): Promise<void> {
    for (const snap of snapshots) {
      const regime = classifyRegime(snap);

      // Only launch grid bots in ranging markets
      if (regime !== 'ranging') continue;

      const sql = getDb();
      const existing = await sql`
        SELECT id FROM bot_instances
        WHERE symbol = ${snap.symbol} AND status = 'active'
        LIMIT 1
      `;
      if (existing.length > 0) continue;

      const investment = portfolio.equity * 0.05; // 5% equity per bot
      if (investment < 100) continue;

      try {
        await this.createGridBot(snap, investment);
      } catch (e) {
        log.warn({ e, symbol: snap.symbol }, 'Failed to create grid bot');
      }
    }
  }

  private async createGridBot(snap: MarketSnapshot, investment: number): Promise<void> {
    const sql = getDb();
    const atr = snap.indicators.atr14;
    const price = snap.lastPrice;
    const minPrice = Math.floor((price - atr * 3) * 100) / 100;
    const maxPrice = Math.ceil((price + atr * 3) * 100) / 100;
    const cellNumber = 10;

    const config = {
      symbol: snap.symbol, minPrice, maxPrice,
      cellNumber, totalInvestment: investment,
    };

    if (this.isPaper) {
      await sql`
        INSERT INTO bot_instances (bot_type, symbol, category, status, config, state, is_paper)
        VALUES ('spot_grid', ${snap.symbol}, 'spot', 'active', ${JSON.stringify(config)}::jsonb, '{}'::jsonb, true)
      `;
      log.info({ symbol: snap.symbol }, '[PAPER] Created spot grid bot');
      return;
    }

    // Validate first (skill rule)
    await this.client.validateSpotGrid({
      symbol: snap.symbol,
      min_price: String(minPrice),
      max_price: String(maxPrice),
      cell_number: cellNumber,
      total_investment: String(investment),
    });

    const result = await this.client.createSpotGrid({
      symbol: snap.symbol,
      min_price: String(minPrice),
      max_price: String(maxPrice),
      cell_number: cellNumber,
      total_investment: String(investment),
    });

    await sql`
      INSERT INTO bot_instances (bot_type, exchange_bot_id, symbol, category, status, config, state, is_paper)
      VALUES ('spot_grid', ${result.grid_id}, ${snap.symbol}, 'spot', 'active',
        ${JSON.stringify(config)}::jsonb, '{}'::jsonb, false)
    `;
    log.info({ symbol: snap.symbol, gridId: result.grid_id }, 'Created spot grid bot');
  }

  async stopAll(): Promise<void> {
    if (this.isPaper) return;
    const sql = getDb();
    const bots = await sql`SELECT * FROM bot_instances WHERE status = 'active' AND is_paper = false`;
    for (const bot of bots) {
      try {
        if (bot.bot_type === 'spot_grid') {
          await this.client.closeSpotGrid(bot.exchange_bot_id, 1);
        } else if (bot.bot_type === 'futures_grid') {
          await this.client.closeFuturesGrid(bot.exchange_bot_id);
        }
        await sql`UPDATE bot_instances SET status = 'stopped', updated_at = now() WHERE id = ${bot.id}`;
      } catch (e) {
        log.error({ e, botId: bot.id }, 'Error stopping bot');
      }
    }
  }
}

function classifyRegime(snap: MarketSnapshot): Regime {
  const { adxValue, atrPct } = snap.indicators;
  if (atrPct > 0.05 || Math.abs(snap.fundingRate) > 0.002) return 'crisis';
  if (adxValue > 25) return 'trending';
  if (atrPct > 0.025) return 'high_volatility';
  return 'ranging';
}
