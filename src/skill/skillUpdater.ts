import { writeFileSync, readFileSync, existsSync, mkdirSync } from 'fs';
import { join } from 'path';
import { childLogger } from '../core/logger';
import { getDb } from '../persistence/db';
import { runCompatCheck } from './SkillCompatChecker';

const log = childLogger({ module: 'skill-updater' });
const RAW_BASE = 'https://raw.githubusercontent.com/bybit-exchange/skills/main';
const SKILL_DIR = join(process.cwd(), 'skills');
const FILES = ['SKILL.md','modules/market.md','modules/spot.md','modules/derivatives.md','modules/account.md','modules/advanced.md','modules/copy-trading.md','modules/trading-bot.md','modules/strategy.md'];

function semverGt(a: string, b: string): boolean {
  const parse = (v: string) => v.split('.').map(Number);
  const [aMaj, aMin, aPatch] = parse(a);
  const [bMaj, bMin, bPatch] = parse(b);
  if (aMaj !== bMaj) return aMaj > bMaj;
  if (aMin !== bMin) return aMin > bMin;
  return aPatch > bPatch;
}

function extractVersion(content: string): string | null {
  const m = content.match(/\*\*Version\*\*:\s*([\d.]+)/);
  return m?.[1] ?? null;
}

function extractEndpoints(content: string): string[] {
  return (content.match(/`(\/v5\/[^`]+)`/g) ?? []).map(s => s.replace(/`/g, ''));
}

export async function checkAndUpdateSkill(currentVersion: string): Promise<void> {
  try {
    const skillPath = join(SKILL_DIR, 'SKILL.md');
    const remoteUrl = `${RAW_BASE}/SKILL.md`;
    const res = await fetch(remoteUrl, { signal: AbortSignal.timeout(10_000) });
    if (!res.ok) { log.warn('Skill manifest fetch failed'); return; }

    const remoteContent = await res.text();
    const remoteVersion = extractVersion(remoteContent);
    if (!remoteVersion) { log.warn('Could not parse remote skill version'); return; }

    if (!semverGt(remoteVersion, currentVersion)) {
      log.debug({ currentVersion, remoteVersion }, 'Skill is up to date');
      return;
    }

    log.info({ currentVersion, remoteVersion }, 'New skill version found — downloading');

    const oldEndpoints = existsSync(skillPath)
      ? extractEndpoints(readFileSync(skillPath, 'utf8'))
      : [];

    mkdirSync(join(SKILL_DIR, 'modules'), { recursive: true });
    for (const file of FILES) {
      const url = `${RAW_BASE}/${file}`;
      const dest = join(SKILL_DIR, file);
      try {
        const r = await fetch(url, { signal: AbortSignal.timeout(10_000) });
        if (!r.ok) { log.warn({ file }, 'Failed to download skill file'); continue; }
        writeFileSync(dest, await r.text(), 'utf8');
      } catch { log.warn({ file }, 'Skill file download error'); }
    }

    const newContent = readFileSync(skillPath, 'utf8');

    // Run full compatibility check — diffs endpoints, finds impacted files, writes agentInstruction
    await runCompatCheck(currentVersion, remoteVersion, oldEndpoints.join('\n'), newContent);

    const newEndpoints = extractEndpoints(newContent);
    const added = newEndpoints.filter(e => !oldEndpoints.includes(e));
    log.info({ version: remoteVersion, added: added.length }, 'Skill update complete');
  } catch (err) {
    log.warn({ err }, 'Skill update check failed — using current version');
  }
}
