import { BybitClient } from '../exchange/BybitClient';
import { getDb } from '../persistence/db';
import { childLogger } from '../core/logger';
import { KillSwitchError } from '../core/errors';

const log = childLogger({ module: 'kill-switch' });

export class KillSwitch {
  private engaged = false;

  constructor(private readonly client: BybitClient) {}

  isEngaged(): boolean { return this.engaged; }

  async engage(reason: string): Promise<void> {
    if (this.engaged) return;
    this.engaged = true;
    log.fatal({ reason }, '🔴 KILL SWITCH ENGAGED');

    const sql = getDb();
    try {
      await sql`
        UPDATE agent_state SET kill_engaged = true, kill_reason = ${reason}, status = 'killed', updated_at = now()
        WHERE id = 'singleton'
      `;
    } catch (e) { log.error({ e }, 'Failed to persist kill state'); }

    try {
      await Promise.all([
        this.client.cancelAllOrders('spot').catch(() => {}),
        this.client.cancelAllOrders('linear').catch(() => {}),
        this.client.cancelAllOrders('inverse').catch(() => {}),
      ]);
      log.info('All orders cancelled');
    } catch (e) { log.error({ e }, 'Error cancelling orders'); }

    try {
      const positions = await this.client.getPositions('linear');
      for (const pos of positions) {
        if (parseFloat(pos.size) <= 0) continue;
        const side = pos.side === 'Buy' ? 'Sell' : 'Buy';
        await this.client.placeOrder({
          category: 'linear',
          symbol: pos.symbol,
          side,
          orderType: 'Market',
          qty: pos.size,
          reduceOnly: true,
          orderLinkId: `kill-${pos.symbol}-${Date.now()}`,
        }).catch(e => log.error({ e, symbol: pos.symbol }, 'Error flattening position'));
      }
      log.info('All positions flattened');
    } catch (e) { log.error({ e }, 'Error flattening positions'); }

    try {
      await sql`
        INSERT INTO risk_events (type, severity, detail, action_taken)
        VALUES ('kill_level', 'critical', ${JSON.stringify({ reason })}::jsonb, 'kill_switch_engaged')
      `;
    } catch { /* best effort */ }

    throw new KillSwitchError(reason);
  }
}
