import { z } from 'zod';
import * as fs from 'fs';
import * as path from 'path';

// Load .env file manually (no dotenv dependency)
function loadEnvFile() {
  const envPath = path.resolve(process.cwd(), '.env');
  if (!fs.existsSync(envPath)) return;
  const lines = fs.readFileSync(envPath, 'utf8').split('\n');
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eqIdx = trimmed.indexOf('=');
    if (eqIdx < 0) continue;
    const key = trimmed.slice(0, eqIdx).trim();
    const value = trimmed.slice(eqIdx + 1).trim();
    if (!(key in process.env)) process.env[key] = value;
  }
}
loadEnvFile();

const schema = z.object({
  BYBIT_API_KEY: z.string().min(1, 'BYBIT_API_KEY is required'),
  // One of these two is required (auto-selected at runtime, see exchange/credentials.ts):
  //   BYBIT_API_SECRET           → HMAC-SHA256 (Bybit-generated key)
  //   BYBIT_API_PRIVATE_KEY_PATH → RSA-SHA256  (self-generated/AI sub-account key)
  BYBIT_API_SECRET: z.string().optional(),
  BYBIT_API_PRIVATE_KEY_PATH: z.string().optional(),
  BYBIT_API_PRIVATE_KEY: z.string().optional(),  // inline PEM (alternative to path)
  DATABASE_URL: z.string().url('DATABASE_URL must be a valid connection URL'),
  REPORT_EMAIL: z.string().email().optional(),
  REPORT_EMAIL_APP_PASSWORD: z.string().optional(),
  GITHUB_TOKEN: z.string().optional(),
  NEWS_API_KEY: z.string().optional(),
  NODE_ENV: z.enum(['development', 'production', 'test']).default('development'),
  LOG_LEVEL: z.string().default('info'),
}).refine(
  (e) => !!e.BYBIT_API_SECRET || !!e.BYBIT_API_PRIVATE_KEY_PATH || !!e.BYBIT_API_PRIVATE_KEY,
  { message: 'Set BYBIT_API_SECRET (HMAC) or BYBIT_API_PRIVATE_KEY_PATH (RSA)', path: ['BYBIT_API_SECRET'] },
);

const parsed = schema.safeParse(process.env);
if (!parsed.success) {
  console.error('❌ Invalid environment configuration:');
  for (const issue of parsed.error.issues) {
    console.error(`  ${issue.path.join('.')}: ${issue.message}`);
  }
  process.exit(1);
}

export const env = parsed.data;
