import { readFileSync, existsSync } from 'fs';
import { join } from 'path';
import { childLogger } from '../core/logger';

const log = childLogger({ module: 'skill-loader' });

export interface SkillConfig {
  version: string;
  recvWindow: number;
  userAgent: string;
  rateLimits: { getMinMs: number; postMinMs: number; maxRetries: number };
  errorCodes: Record<number, string>;
}

export function loadSkill(): SkillConfig {
  const skillDir = join(process.cwd(), 'skills');
  const skillPath = join(skillDir, 'SKILL.md');

  if (!existsSync(skillPath)) {
    log.warn('skills/SKILL.md not found — using defaults. Run: npm run fetch-skill');
    return defaultSkillConfig();
  }

  const content = readFileSync(skillPath, 'utf8');
  const versionMatch = content.match(/\*\*Version\*\*:\s*([\d.]+)/);
  const version = versionMatch?.[1] ?? '1.4.1';

  log.info({ version }, 'Skill loaded');
  return {
    version,
    recvWindow: 5000,
    userAgent: `bybit-skill/${version}`,
    rateLimits: { getMinMs: 100, postMinMs: 300, maxRetries: 3 },
    errorCodes: {
      10001: 'REQUEST_PARAM_ERROR',
      10002: 'REQUEST_EXPIRED',
      10003: 'INVALID_API_KEY',
      10004: 'INVALID_SIGNATURE',
      10005: 'PERMISSION_DENIED',
      10006: 'RATE_LIMITED',
      10010: 'IP_NOT_WHITELISTED',
      110001: 'ORDER_NOT_EXIST',
      110003: 'PRICE_OUT_OF_RANGE',
      110004: 'INSUFFICIENT_BALANCE',
      110020: 'TOO_MANY_ACTIVE_ORDERS',
      110040: 'LIQUIDATION_RISK',
      110072: 'DUPLICATE_ORDER_LINK_ID',
      170005: 'SPOT_RATE_LIMITED',
    },
  };
}

function defaultSkillConfig(): SkillConfig {
  return {
    version: '1.4.1',
    recvWindow: 5000,
    userAgent: 'bybit-skill/1.4.1',
    rateLimits: { getMinMs: 100, postMinMs: 300, maxRetries: 3 },
    errorCodes: {},
  };
}
