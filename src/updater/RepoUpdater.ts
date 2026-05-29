import { execSync } from 'child_process';
import { existsSync, writeFileSync, readFileSync } from 'fs';
import { join } from 'path';
import { childLogger } from '../core/logger';
import { getDb } from '../persistence/db';
import { env } from '../config/env';

const log = childLogger({ module: 'repo-updater' });

function getRemoteSlug(): string | null {
  try {
    const url = execSync('git remote get-url origin', { encoding: 'utf8' }).trim();
    const m = url.match(/github\.com[:/](.+?)(\.git)?$/);
    return m?.[1] ?? null;
  } catch { return null; }
}

function getCurrentSha(): string {
  const versionFile = join(process.cwd(), 'dist', '.version');
  if (existsSync(versionFile)) return readFileSync(versionFile, 'utf8').trim();
  try {
    return execSync('git rev-parse HEAD', { encoding: 'utf8' }).trim();
  } catch { return 'unknown'; }
}

function getCurrentBranch(): string {
  try {
    return execSync('git rev-parse --abbrev-ref HEAD', { encoding: 'utf8' }).trim();
  } catch { return 'main'; }
}

export async function checkAndUpdate(): Promise<void> {
  const slug = getRemoteSlug();
  if (!slug) { log.debug('No GitHub remote detected — skipping repo update check'); return; }

  const branch = getCurrentBranch();
  const currentSha = getCurrentSha();

  const apiUrl = `https://api.github.com/repos/${slug}/commits/${branch}`;
  const headers: Record<string, string> = { 'User-Agent': 'bybit-agent' };
  if (env.GITHUB_TOKEN) headers['Authorization'] = `token ${env.GITHUB_TOKEN}`;

  try {
    const res = await fetch(apiUrl, { headers, signal: AbortSignal.timeout(10_000) });
    if (!res.ok) { log.debug({ status: res.status }, 'GitHub API call failed'); return; }

    const data = await res.json() as { sha: string; commit: { message: string } };
    const remoteSha = data.sha;

    if (remoteSha === currentSha) { log.debug('Repo is up to date'); return; }

    log.info({ currentSha, remoteSha }, 'New version detected — pulling update');

    const filesChangedRaw = execSync(`git diff ${currentSha} ${remoteSha} --name-only 2>/dev/null || echo ''`, { encoding: 'utf8' });
    const filesChanged = filesChangedRaw.trim().split('\n').filter(Boolean);
    const commitMessages = [data.commit.message.split('\n')[0]];

    execSync(`git fetch origin ${branch}`, { stdio: 'pipe' });
    execSync(`git reset --hard origin/${branch}`, { stdio: 'pipe' });

    let buildOk = false;
    try {
      execSync('npm install --prefer-offline 2>/dev/null || npm install', { stdio: 'pipe' });
      execSync('npm run build', { stdio: 'pipe' });
      buildOk = true;
    } catch (buildErr) {
      log.error({ buildErr }, 'Build failed after update — rolling back');
      try {
        execSync(`git reset --hard ${currentSha}`, { stdio: 'pipe' });
        execSync('npm run build', { stdio: 'pipe' });
      } catch { /* best effort */ }

      await getDb()`
        INSERT INTO repo_updates (branch, old_sha, new_sha, commit_messages, files_changed, build_ok, restart_ok)
        VALUES (${branch}, ${currentSha}, ${remoteSha}, ${commitMessages}, ${filesChanged}, false, false)
      `;
      return;
    }

    await getDb()`
      INSERT INTO repo_updates (branch, old_sha, new_sha, commit_messages, files_changed, build_ok, restart_ok)
      VALUES (${branch}, ${currentSha}, ${remoteSha}, ${commitMessages}, ${filesChanged}, true, true)
    `;

    log.info('Restarting with new version…');
    // Re-exec the process with new binary
    const { execPath, argv } = process;
    // Give DB a moment to flush
    await new Promise(r => setTimeout(r, 500));
    const { spawn } = await import('child_process');
    spawn(execPath, argv.slice(1), {
      detached: true, stdio: 'inherit',
      env: { ...process.env },
    }).unref();
    process.exit(0);
  } catch (e) {
    log.warn({ e }, 'Repo update check failed');
  }
}
