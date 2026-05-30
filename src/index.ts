import './config/env';
import { readFileSync, existsSync, readdirSync } from 'fs';
import { join } from 'path';
import { loadSkill } from './skill/skillLoader';
import { checkAndUpdateSkill } from './skill/skillUpdater';
import { getDb, closeDb } from './persistence/db';
import { BybitClient } from './exchange/BybitClient';
import { resolveBybitAuth } from './exchange/credentials';
import { AgentLoop } from './control/AgentLoop';
import { logger } from './core/logger';

async function main() {
  logger.info('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
  logger.info('  BYBiT Autonomous Trading Agent');
  logger.info('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━');

  // ── 1. Load + validate skill ──────────────────────────────────────────────
  const skill = loadSkill();
  logger.info({ version: skill.version }, `[SKILL] Loaded v${skill.version}`);

  // ── 2. Self-update skill in background (never blocks) ────────────────────
  checkAndUpdateSkill(skill.version).catch(() => {});

  // ── 3. Run migrations ─────────────────────────────────────────────────────
  logger.info('Running database migrations…');
  const sql = getDb();
  const migrationsDir = join(process.cwd(), 'migrations');
  if (existsSync(migrationsDir)) {
    const files = readdirSync(migrationsDir).filter(f => f.endsWith('.sql')).sort();
    for (const f of files) {
      await sql.unsafe(readFileSync(join(migrationsDir, f), 'utf8'));
    }
    logger.info({ files: files.length }, '✅ Migrations complete');
  }

  // ── 4. Verify clock sync (skill rule: halt if >5s off) ───────────────────
  const env = (await import('./config/env')).env;
  const ag = await sql`SELECT env, kill_engaged FROM agent_state WHERE id = 'singleton' LIMIT 1`.then(r => r[0]);
  const isTestnet = (ag?.env ?? 'testnet') === 'testnet';
  logger.info({ env: isTestnet ? 'TESTNET' : 'MAINNET' }, `[${isTestnet ? 'TESTNET' : 'MAINNET'}] Starting`);

  // Quick clock check via public endpoint
  try {
    const client = new BybitClient(resolveBybitAuth(env), isTestnet);
    const serverTime = await client.getServerTime();
    const diff = Math.abs(Date.now() - serverTime);
    if (diff > 5000) {
      logger.fatal({ diff }, 'System clock is more than 5 seconds off — sync your clock and restart');
      process.exit(1);
    }
    logger.info({ diff: `${diff}ms` }, 'Clock sync OK');

    // ── 5. Start agent loop ────────────────────────────────────────────────
    const loop = new AgentLoop(client, isTestnet);

    process.on('SIGINT', async () => {
      logger.info('Shutting down gracefully…');
      loop.stop();
      await closeDb();
      process.exit(0);
    });

    process.on('SIGTERM', async () => {
      logger.info('SIGTERM received — shutting down…');
      loop.stop();
      await closeDb();
      process.exit(0);
    });

    await loop.start();
  } catch (e) {
    logger.fatal({ e }, 'Fatal startup error');
    await closeDb();
    process.exit(1);
  }
}

main();
