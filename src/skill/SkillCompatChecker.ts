import { readFileSync, existsSync } from 'fs';
import { join } from 'path';
import { childLogger } from '../core/logger';
import { getDb } from '../persistence/db';

const log = childLogger({ module: 'skill-compat' });
const SKILL_DIR = join(process.cwd(), 'skills');

interface EndpointChange {
  type: 'endpoint_removed' | 'endpoint_added' | 'param_changed';
  path: string;
  severity: 'CRITICAL' | 'WARNING' | 'INFO';
  detail?: string;
  usedIn?: string[];
}

interface CompatReport {
  breaking: boolean;
  changes: EndpointChange[];
  impactedFiles: string[];
  agentInstruction: string;
}

export async function runCompatCheck(
  oldVersion: string,
  newVersion: string,
  oldSkillContent: string,
  newSkillContent: string,
): Promise<void> {
  try {
    const oldEndpoints = extractEndpointRegistry(oldSkillContent);
    const newEndpoints = extractEndpointRegistry(newSkillContent);

    // Also check all module files for endpoint references
    const moduleFiles = ['market', 'spot', 'derivatives', 'account', 'advanced', 'copy-trading', 'trading-bot', 'strategy'];
    for (const mod of moduleFiles) {
      const path = join(SKILL_DIR, 'modules', `${mod}.md`);
      if (existsSync(path)) {
        const content = readFileSync(path, 'utf8');
        for (const ep of extractEndpointRegistry(content)) {
          if (!newEndpoints.includes(ep)) newEndpoints.push(ep);
        }
      }
    }

    const removed = oldEndpoints.filter(e => !newEndpoints.includes(e));
    const added = newEndpoints.filter(e => !oldEndpoints.includes(e));

    if (removed.length === 0 && added.length === 0) {
      log.debug({ oldVersion, newVersion }, 'Skill compat: no endpoint changes');
      return;
    }

    const changes: EndpointChange[] = [];
    const impactedFiles: Set<string> = new Set();

    // Check removed endpoints against src/ files
    for (const ep of removed) {
      const usedIn = findUsagesInSrc(ep);
      const severity = usedIn.length > 0 ? 'CRITICAL' : 'WARNING';
      changes.push({ type: 'endpoint_removed', path: ep, severity, usedIn });
      usedIn.forEach(f => impactedFiles.add(f));
    }

    // Non-breaking additions
    for (const ep of added) {
      changes.push({ type: 'endpoint_added', path: ep, severity: 'INFO' });
    }

    const breaking = changes.some(c => c.severity === 'CRITICAL');
    const report: CompatReport = {
      breaking,
      changes,
      impactedFiles: Array.from(impactedFiles),
      agentInstruction: buildAgentInstruction(oldVersion, newVersion, changes, breaking),
    };

    await persistReport(oldVersion, newVersion, report);

    if (breaking) {
      log.error({ impactedFiles: report.impactedFiles }, '⚠️  Breaking Bybit skill change — new entries paused');
      log.error(report.agentInstruction);
    } else {
      log.info({ added: added.length, removed: removed.length }, 'Skill compat: non-breaking changes adopted');
    }
  } catch (err) {
    log.warn({ err }, 'Skill compat check failed');
  }
}

function extractEndpointRegistry(content: string): string[] {
  const seen = new Set<string>();
  const results: string[] = [];
  // Match backtick-quoted /v5/... paths and plain /v5/... in headers
  const matches = content.match(/`?(\/v5\/[a-zA-Z0-9/._-]+)`?/g) ?? [];
  for (const m of matches) {
    const ep = m.replace(/`/g, '').split(/[?#\s]/)[0].trim();
    if (ep.startsWith('/v5/') && !seen.has(ep)) {
      seen.add(ep);
      results.push(ep);
    }
  }
  return results;
}

function findUsagesInSrc(endpoint: string): string[] {
  const srcDir = join(process.cwd(), 'src');
  const results: string[] = [];
  try {
    const { execSync } = require('child_process');
    const output: string = execSync(
      `grep -rn "${endpoint}" "${srcDir}" --include="*.ts" 2>/dev/null || true`,
      { encoding: 'utf8', timeout: 5_000 },
    );
    for (const line of output.split('\n')) {
      if (!line.trim()) continue;
      const [filePath, lineNum] = line.split(':');
      const rel = filePath.replace(process.cwd() + '/', '');
      results.push(`${rel}:${lineNum}`);
    }
  } catch { /* grep failed or no results */ }
  return results;
}

function buildAgentInstruction(
  oldVersion: string,
  newVersion: string,
  changes: EndpointChange[],
  breaking: boolean,
): string {
  if (!breaking) {
    const added = changes.filter(c => c.type === 'endpoint_added');
    return `Bybit Skill updated from ${oldVersion} → ${newVersion}. Non-breaking: ${added.length} new endpoint(s) available: ${added.map(c => c.path).join(', ')}. No code changes required.`;
  }

  const critical = changes.filter(c => c.severity === 'CRITICAL');
  const lines: string[] = [
    `Bybit Skill updated from ${oldVersion} → ${newVersion}. BREAKING CHANGES DETECTED — new order entries are paused until fixed.`,
    '',
  ];

  for (const c of critical) {
    lines.push(`CRITICAL: Endpoint "${c.path}" was removed from the Bybit API.`);
    if (c.usedIn && c.usedIn.length > 0) {
      lines.push(`  Used in: ${c.usedIn.join(', ')}`);
      lines.push(`  Action: Find the replacement endpoint in the new skills/modules/*.md files and update the above files.`);
    }
    lines.push('');
  }

  lines.push('To fix: update the listed files, commit, and push to GitHub. The agent will pull the fix within 15 minutes.');
  return lines.join('\n');
}

async function persistReport(
  oldVersion: string,
  newVersion: string,
  report: CompatReport,
): Promise<void> {
  const sql = getDb();
  await sql`
    INSERT INTO skill_update_events
      (old_version, new_version, endpoint_diffs, breaking, description, agent_instruction)
    VALUES (
      ${oldVersion}, ${newVersion},
      ${JSON.stringify({ changes: report.changes, impactedFiles: report.impactedFiles })}::jsonb,
      ${report.breaking},
      ${report.breaking
        ? `Breaking: ${report.changes.filter(c => c.severity === 'CRITICAL').length} critical endpoint(s) removed, affecting: ${report.impactedFiles.join(', ')}`
        : `Non-breaking: ${report.changes.filter(c => c.type === 'endpoint_added').length} endpoint(s) added`
      },
      ${report.agentInstruction}
    )
  `;
}
