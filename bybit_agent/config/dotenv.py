"""Minimal .env loader — mirrors the manual loader in src/config/env.ts.

Populates ``os.environ`` so modules that read os.environ directly (e.g. constants.py
computing proxy-aware REST URLs) see the values, exactly as the TS loader sets
``process.env``. ``.env`` always wins over stale shell exports.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        eq = line.find("=")
        if eq < 0:
            continue
        key = line[:eq].strip()
        value = line[eq + 1:].strip()
        os.environ[key] = value  # .env always wins
