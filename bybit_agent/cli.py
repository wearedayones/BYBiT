"""The ``bybit`` CLI — the universal surface any AI agent uses to operate the skill.

Phase 0 ships ``migrate`` and ``doctor``. Trading/event/tuning commands are added in
later phases (see the plan). All commands talk to Postgres directly, so the CLI works
as a short-lived process independent of the long-running ``bybit run`` service.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer

app = typer.Typer(add_completion=False, help="Agent-operated Bybit trading skill.")


def _migrations_dir() -> Path:
    return Path.cwd() / "migrations"


async def _run_migrations() -> tuple[int, int]:
    from .persistence.db import close_db, get_db

    db = get_db()
    files = sorted(_migrations_dir().glob("*.sql"))
    stmt_count = 0
    for f in files:
        stmt_count += await db.execute_script(f.read_text())
    await close_db()
    return len(files), stmt_count


@app.command()
def migrate() -> None:
    """Apply all migrations/*.sql in order (idempotent)."""
    files, stmts = asyncio.run(_run_migrations())
    typer.echo(f"✅ Applied {files} migration file(s), {stmts} statement(s).")


async def _doctor() -> bool:
    from .config.env import get_env
    from .core.logger import mask_secret
    from .persistence.db import close_db, get_db

    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "✅" if passed else "❌"
        ok = ok and passed
        typer.echo(f"  {mark} {label}{(' — ' + detail) if detail else ''}")

    typer.echo("bybit doctor — pre-flight checks\n")

    # 1. Env validation
    try:
        env = get_env()
        check("env loaded & validated", True)
        signing = (
            "RSA (inline PEM)" if env.BYBIT_API_PRIVATE_KEY
            else "RSA (key file)" if env.BYBIT_API_PRIVATE_KEY_PATH
            else "HMAC"
        )
        typer.echo(f"     API key:  {mask_secret(env.BYBIT_API_KEY)}")
        typer.echo(f"     signing:  {signing}")
        typer.echo(f"     proxy:    {'set' if env.BYBIT_PROXY_URL else 'none'}")
    except Exception as exc:  # noqa: BLE001
        check("env loaded & validated", False, str(exc)[:160])
        return False  # cannot continue without env

    # 2. DB over 443 + JSONB round-trip
    try:
        db = get_db()
        val = await db.fetchval("SELECT 1 AS one")
        check("DB reachable over HTTPS:443", val == 1)
        row = await db.fetchrow(
            "SELECT ($1::jsonb)->>'k' AS v", db.json({"k": "neon-http-ok"})
        )
        check("JSONB round-trip", bool(row) and row.get("v") == "neon-http-ok")
    except Exception as exc:  # noqa: BLE001
        check("DB reachable over HTTPS:443", False, str(exc)[:160])
    finally:
        await close_db()

    return ok


@app.command()
def doctor() -> None:
    """Validate env, DB connectivity over 443, and a JSONB round-trip."""
    ok = asyncio.run(_doctor())
    typer.echo("")
    if ok:
        typer.echo("All checks passed.")
    else:
        typer.echo("Some checks failed — see above.")
        sys.exit(1)


@app.command()
def run() -> None:
    """Launch the 24/7 deterministic trading service (added in Phase 3)."""
    typer.echo("`bybit run` is implemented in Phase 3. Scaffold only for now.")
    sys.exit(2)


if __name__ == "__main__":
    app()
