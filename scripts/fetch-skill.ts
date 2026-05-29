import { mkdirSync, writeFileSync } from 'fs';
import { join } from 'path';

const RAW_BASE = 'https://raw.githubusercontent.com/bybit-exchange/skills/main';
const SKILL_DIR = join(__dirname, '..', 'skills');
const MODULES_DIR = join(SKILL_DIR, 'modules');

const FILES = [
  'SKILL.md',
  'modules/market.md',
  'modules/spot.md',
  'modules/derivatives.md',
  'modules/account.md',
  'modules/advanced.md',
  'modules/copy-trading.md',
  'modules/trading-bot.md',
  'modules/strategy.md',
];

async function fetchSkill() {
  mkdirSync(SKILL_DIR, { recursive: true });
  mkdirSync(MODULES_DIR, { recursive: true });

  for (const file of FILES) {
    const url = `${RAW_BASE}/${file}`;
    const dest = join(SKILL_DIR, file);
    try {
      const res = await fetch(url);
      if (!res.ok) { console.warn(`⚠️  Could not fetch ${file}: ${res.status}`); continue; }
      const text = await res.text();
      writeFileSync(dest, text, 'utf8');
      console.log(`✅ ${file}`);
    } catch (e) {
      console.warn(`⚠️  Failed to fetch ${file}: ${e}`);
    }
  }
}

fetchSkill().catch(console.error);
