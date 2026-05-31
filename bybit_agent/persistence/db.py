"""Database access over Neon's HTTP SQL endpoint (port 443).

Ports src/persistence/db.ts. The Neon serverless driver speaks to an HTTPS endpoint
(``https://<host>/sql``) instead of the Postgres wire protocol, so it works in cloud
environments where TCP 5432/6543 is blocked. We replicate that here with httpx.

Query style is asyncpg-like: ``await db.fetch("SELECT ... WHERE x = $1", x)``.
For JSONB columns, wrap the value in ``Jsonb(obj)`` and write ``$1::jsonb`` in the SQL;
the client serialises it to a JSON string (mirrors the ``$__JSONB__`` sentinel in db.ts).

A 3× retry handles ``CERT_NOT_YET_VALID`` from TLS-inspecting proxies with clock skew.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
from typing import Any, Sequence
from urllib.parse import urlparse

import httpx

from ..config.env import get_env
from ..core.logger import child_logger

log = child_logger(module="db")


class Jsonb:
    """Marker wrapper: serialise as a JSON string param for a ``$n::jsonb`` placeholder."""

    __slots__ = ("data",)

    def __init__(self, data: Any) -> None:
        self.data = data


def _neon_sql_url(database_url: str) -> str:
    parsed = urlparse(database_url)
    host = parsed.hostname
    if not host:
        raise ValueError("DATABASE_URL has no host")
    return f"https://{host}/sql"


def _encode_params(params: Sequence[Any]) -> list[Any]:
    out: list[Any] = []
    for p in params:
        if isinstance(p, Jsonb):
            out.append(json.dumps(p.data))
        else:
            out.append(p)
    return out


class NeonHttpClient:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._endpoint = _neon_sql_url(database_url)
        # TLS context tolerant of proxy clock skew is handled via retry, not verify-off.
        self._client = httpx.AsyncClient(timeout=30.0)
        self._headers = {
            "Neon-Connection-String": database_url,
            "Neon-Raw-Text-Output": "false",
            "Neon-Array-Mode": "false",
            "Content-Type": "application/json",
        }

    async def _post(self, query: str, params: Sequence[Any]) -> dict[str, Any]:
        body = {"query": query, "params": _encode_params(params)}
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.post(self._endpoint, json=body, headers=self._headers)
                if resp.status_code >= 400:
                    raise RuntimeError(f"Neon HTTP {resp.status_code}: {resp.text[:300]}")
                return resp.json()
            except (ssl.SSLError, httpx.ConnectError) as exc:  # TLS / connect skew
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise
        raise last_exc or RuntimeError("unreachable")

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        data = await self._post(query, params)
        return data.get("rows", [])

    async def fetchrow(self, query: str, *params: Any) -> dict[str, Any] | None:
        rows = await self.fetch(query, *params)
        return rows[0] if rows else None

    async def fetchval(self, query: str, *params: Any) -> Any:
        row = await self.fetchrow(query, *params)
        if not row:
            return None
        return next(iter(row.values()), None)

    async def execute(self, query: str, *params: Any) -> None:
        await self._post(query, params)

    async def execute_script(self, raw_sql: str) -> int:
        """Run a multi-statement SQL file (migrations).

        Strips ``--`` line comments first (so a ';' inside a comment doesn't split a
        statement), then splits on ';'. Sufficient for our migrations, which contain
        no dollar-quoted blocks or '--' inside string literals.
        """
        no_comments = re.sub(r"--[^\n]*", "", raw_sql)
        stmts = [s.strip() for s in no_comments.split(";") if s.strip()]
        for stmt in stmts:
            await self._post(stmt, [])
        return len(stmts)

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def json(value: Any) -> Jsonb:
        return Jsonb(value)


_db: NeonHttpClient | None = None


def get_db() -> NeonHttpClient:
    global _db
    if _db is None:
        _db = NeonHttpClient(get_env().DATABASE_URL)
        log.info("DB connected via Neon HTTP API (port 443)")
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.aclose()
        _db = None
