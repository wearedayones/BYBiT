"""Repo updater — ports src/updater/RepoUpdater.ts.

Polls GitHub for new commits on the current branch every REPO_POLL_INTERVAL_MS.
On a new SHA:
  1. git fetch + reset --hard
  2. pip install -e . (replaces npm install + build)
  3. INSERT into repo_updates
  4. os.execv(sys.executable, sys.argv) — in-process respawn (replaces spawn+exit)

If pip fails, rolls back to old SHA and records build_ok=false.
The GITHUB_TOKEN env var is optional (avoids GitHub rate limiting).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx

from bybit_agent.core.logger import get_logger
from bybit_agent.persistence.db import get_db

log = get_logger().bind(module="repo-updater")


def _run(cmd: str) -> str:
    return subprocess.check_output(cmd, shell=True, encoding="utf-8", stderr=subprocess.DEVNULL).strip()


def _get_remote_slug() -> str | None:
    try:
        url = _run("git remote get-url origin")
        import re
        m = re.search(r"github\.com[:/](.+?)(\.git)?$", url)
        return m.group(1) if m else None
    except Exception:
        return None


def _current_sha() -> str:
    version_file = Path.cwd() / "dist" / ".version"
    if version_file.exists():
        return version_file.read_text().strip()
    try:
        return _run("git rev-parse HEAD")
    except Exception:
        return "unknown"


def _current_branch() -> str:
    try:
        return _run("git rev-parse --abbrev-ref HEAD")
    except Exception:
        return "main"


async def check_and_update() -> None:
    slug = _get_remote_slug()
    if not slug:
        log.debug("No GitHub remote detected — skipping repo update check")
        return

    branch = _current_branch()
    current_sha = _current_sha()
    api_url = f"https://api.github.com/repos/{slug}/commits/{branch}"
    headers = {"User-Agent": "bybit-agent"}
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(api_url, headers=headers)
        if resp.status_code != 200:
            log.debug("GitHub API call failed", status=resp.status_code)
            return

        data = resp.json()
        remote_sha: str = data["sha"]
        commit_msg: str = data["commit"]["message"].split("\n")[0]

        if remote_sha == current_sha:
            log.debug("Repo is up to date")
            return

        log.info("New version detected — pulling update", current=current_sha, remote=remote_sha)

        try:
            files_raw = _run(f"git diff {current_sha} {remote_sha} --name-only")
            files_changed = [f for f in files_raw.split("\n") if f]
        except Exception:
            files_changed = []

        _run(f"git fetch origin {branch}")
        _run(f"git reset --hard origin/{branch}")

        build_ok = False
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-e", ".", "-q"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            build_ok = True
        except subprocess.CalledProcessError as exc:
            log.error("pip install failed after update — rolling back", error=str(exc))
            try:
                _run(f"git reset --hard {current_sha}")
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", "-e", ".", "-q"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

            db = get_db()
            await db.execute(
                """INSERT INTO repo_updates
                     (branch, old_sha, new_sha, commit_messages, files_changed, build_ok, restart_ok)
                   VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, false, false)""",
                branch, current_sha, remote_sha,
                __import__("json").dumps([commit_msg]),
                __import__("json").dumps(files_changed),
            )
            return

        db = get_db()
        await db.execute(
            """INSERT INTO repo_updates
                 (branch, old_sha, new_sha, commit_messages, files_changed, build_ok, restart_ok)
               VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, true, true)""",
            branch, current_sha, remote_sha,
            __import__("json").dumps([commit_msg]),
            __import__("json").dumps(files_changed),
        )

        log.info("Restarting with new version via os.execv")
        # Brief flush window before respawn.
        import asyncio
        await asyncio.sleep(0.5)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    except Exception as e:
        log.warning("Repo update check failed", error=str(e))
