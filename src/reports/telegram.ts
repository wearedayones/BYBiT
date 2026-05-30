import { request } from 'undici';
import { env } from '../config/env';
import { childLogger } from '../core/logger';

const log = childLogger({ module: 'telegram' });

// Telegram Bot API is HTTPS on api.telegram.org (port 443), so it works in
// restricted cloud environments with no proxy — unlike SMTP. Both the bot
// token and chat id come from the environment; nothing is hardcoded.

const TELEGRAM_API = 'https://api.telegram.org';
const MAX_LEN = 4096; // Telegram hard limit per message.

export function telegramEnabled(): boolean {
  return !!(env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID);
}

/**
 * Send a message to the configured Telegram chat. Uses HTML parse mode
 * (supports <b>, <i>, <code>, <pre>, <a>). Returns true on delivery.
 * No-op (returns false) when Telegram isn't configured.
 */
export async function sendTelegram(text: string): Promise<boolean> {
  if (!telegramEnabled()) return false;

  const body = text.length > MAX_LEN ? text.slice(0, MAX_LEN - 1) + '…' : text;
  const url = `${TELEGRAM_API}/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`;

  try {
    const res = await request(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        chat_id: env.TELEGRAM_CHAT_ID,
        text: body,
        parse_mode: 'HTML',
        disable_web_page_preview: true,
      }),
      headersTimeout: 10_000,
      bodyTimeout: 10_000,
    });
    const json = await res.body.json() as { ok: boolean; description?: string };
    if (res.statusCode !== 200 || !json.ok) {
      log.error({ status: res.statusCode, desc: json.description }, 'Telegram send failed');
      return false;
    }
    return true;
  } catch (e) {
    log.error({ e }, 'Telegram send failed');
    return false;
  }
}
