import { readFileSync } from 'fs';
import { childLogger } from '../core/logger';
import type { BybitAuth } from './signer';

const log = childLogger({ module: 'credentials' });

/**
 * Resolve Bybit auth from the environment, auto-selecting the signing method
 * exactly as the skill specifies (skills/SKILL.md "Determine sign type"):
 *   - BYBIT_API_PRIVATE_KEY_PATH set  → RSA-SHA256 (sign type 2)
 *   - BYBIT_API_SECRET set            → HMAC-SHA256 (sign type 1)
 *   - both set                        → prefer RSA, warn once
 *   - neither                         → throw (caller halts before any signed call)
 *
 * Security: the private key PEM is read into memory and never logged. Only the
 * file basename is ever surfaced.
 */
export function resolveBybitAuth(env: {
  BYBIT_API_KEY: string;
  BYBIT_API_SECRET?: string;
  BYBIT_API_PRIVATE_KEY_PATH?: string;
}): BybitAuth {
  const hasRsa = !!env.BYBIT_API_PRIVATE_KEY_PATH;
  const hasHmac = !!env.BYBIT_API_SECRET;

  if (hasRsa && hasHmac) {
    log.warn(
      'Both BYBIT_API_SECRET and BYBIT_API_PRIVATE_KEY_PATH are set. Using RSA. ' +
      'To force HMAC, unset BYBIT_API_PRIVATE_KEY_PATH.',
    );
  }

  if (hasRsa) {
    const path = env.BYBIT_API_PRIVATE_KEY_PATH!;
    let privateKey: string;
    try {
      privateKey = readFileSync(path, 'utf8');
    } catch {
      throw new Error(`Could not read RSA private key at BYBIT_API_PRIVATE_KEY_PATH (${basename(path)})`);
    }
    if (!privateKey.includes('PRIVATE KEY')) {
      throw new Error(`File at BYBIT_API_PRIVATE_KEY_PATH (${basename(path)}) is not a PEM private key`);
    }
    log.info({ signing: 'RSA-SHA256', keyFile: basename(path) }, 'Bybit auth: RSA');
    return { apiKey: env.BYBIT_API_KEY, signType: 2, privateKey };
  }

  if (hasHmac) {
    log.info({ signing: 'HMAC-SHA256' }, 'Bybit auth: HMAC');
    return { apiKey: env.BYBIT_API_KEY, signType: 1, secret: env.BYBIT_API_SECRET };
  }

  throw new Error(
    'No Bybit credentials found. Set BYBIT_API_SECRET (HMAC) or BYBIT_API_PRIVATE_KEY_PATH (RSA).',
  );
}

function basename(p: string): string {
  return p.split(/[/\\]/).pop() ?? p;
}
