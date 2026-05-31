"""Skill version checker — detects when the official Bybit Exchange skill hub has updates.

Called by the Doctor once per 24 hours (in-memory TTL, no DB writes). Three tiers:
  patch/minor bump  → auto-refresh (Doctor handles it, zero agent involvement)
  major bump        → enqueue market_event so the agent reviews before accepting
  unavailable       → info finding only (network failure, rate-limit, etc.)
"""
from __future__ import annotations

import time
from pathlib import Path

_SKILLS_ROOT = Path(__file__).parent.parent.parent / "skills"

# GitHub endpoints — releases API preferred (authoritative version), raw fallback.
_RELEASES_API = "https://api.github.com/repos/bybit-exchange/skills/releases/latest"
_VERSION_RAW   = "https://raw.githubusercontent.com/bybit-exchange/skills/main/VERSION"
_MODULES_BASE  = "https://raw.githubusercontent.com/bybit-exchange/skills/main/modules"

# 24-hour rate-limit so the 24/7 service doesn't hammer the GitHub API.
_CHECK_INTERVAL_S = 86_400
_cache: dict = {"checked_at": 0.0, "latest": None}

_MODULES = [
    "account", "advanced", "alpha-trade", "copy-trading", "derivatives",
    "earn", "fiat", "market", "spot", "strategy", "tradfi", "trading-bot",
]


# ── semver helpers ────────────────────────────────────────────────────────────

def _parse(v: str) -> tuple[int, int, int]:
    parts = v.lstrip("v").split(".")
    try:
        return (int(parts[0]), int(parts[1] if len(parts) > 1 else 0),
                int(parts[2] if len(parts) > 2 else 0))
    except (ValueError, IndexError):
        return (0, 0, 0)


def _bump_type(embedded: str, latest: str) -> str:
    """Return 'current' | 'patch' | 'minor' | 'major'."""
    e, l = _parse(embedded), _parse(latest)
    if l <= e:
        return "current"
    if l[0] > e[0]:
        return "major"
    if l[1] > e[1]:
        return "minor"
    return "patch"


# ── version fetch ─────────────────────────────────────────────────────────────

async def _fetch_latest() -> str | None:
    """Try GitHub Releases API, then fall back to raw VERSION file."""
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as c:
        try:
            r = await c.get(_RELEASES_API,
                            headers={"Accept": "application/vnd.github+json"})
            if r.status_code == 200:
                tag = r.json().get("tag_name", "")
                if tag:
                    return tag.lstrip("v")
        except Exception:  # noqa: BLE001
            pass
        try:
            r = await c.get(_VERSION_RAW)
            if r.status_code == 200:
                return r.text.strip().lstrip("v")
        except Exception:  # noqa: BLE001
            pass
    return None


def _embedded_version() -> str:
    p = _SKILLS_ROOT / "VERSION"
    return p.read_text().strip() if p.exists() else "unknown"


# ── public API ────────────────────────────────────────────────────────────────

async def check() -> dict:
    """
    Returns::

        {
          "embedded": "1.1.1",
          "latest":   "1.4.1",   # None if GitHub unreachable
          "bump":     "minor",   # "current" | "patch" | "minor" | "major" | "unknown"
        }

    Result is cached for 24 h so the 24/7 service doesn't hammer the GitHub API.
    """
    now = time.monotonic()
    if now - _cache["checked_at"] < _CHECK_INTERVAL_S and _cache.get("latest"):
        latest = _cache["latest"]
    else:
        latest = await _fetch_latest()
        _cache["checked_at"] = now
        _cache["latest"] = latest

    embedded = _embedded_version()
    if not latest:
        return {"embedded": embedded, "latest": None, "bump": "unknown"}

    return {"embedded": embedded, "latest": latest, "bump": _bump_type(embedded, latest)}


async def refresh(latest_version: str | None = None) -> dict:
    """Pull all modules from GitHub, update VERSION + MANIFEST.

    Returns ``{"updated": [...], "errors": [...], "version": "..."}``.
    """
    import hashlib
    import httpx

    updated: list[str] = []
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=30.0) as c:
        if not latest_version:
            latest_version = await _fetch_latest() or "unknown"

        for mod in _MODULES:
            url = f"{_MODULES_BASE}/{mod}.md"
            try:
                r = await c.get(url)
                r.raise_for_status()
                dest = _SKILLS_ROOT / "modules" / f"{mod}.md"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(r.content)
                updated.append(mod)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{mod}: {e}")

    if latest_version and latest_version != "unknown":
        (_SKILLS_ROOT / "VERSION").write_text(latest_version + "\n")
        # Update in-memory cache so next check() sees the new version.
        _cache["latest"] = latest_version
        _cache["checked_at"] = time.monotonic()

    # Rebuild MANIFEST with SHA-256 hashes.
    lines: list[str] = []
    for mod in _MODULES:
        p = _SKILLS_ROOT / "modules" / f"{mod}.md"
        if p.exists():
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            lines.append(f"{h}  modules/{mod}.md")
    (_SKILLS_ROOT / "MANIFEST").write_text("\n".join(lines) + "\n")

    return {"updated": updated, "errors": errors, "version": latest_version}
