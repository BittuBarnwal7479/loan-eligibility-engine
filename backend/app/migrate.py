"""Minimal versioned migration runner.

Applies backend/migrations/NNN_*.sql in order, once each, inside a transaction,
guarded by an advisory lock so concurrent starts cannot double-apply.

Usage: python -m app.migrate
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.db import connection
from app.logging_setup import configure_logging

log = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_LOCK_ID = 727_001


def migrate() -> list[str]:
    applied: list[str] = []
    with connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_ID,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            log.info("applying migration", extra={"migration": path.name})
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied


if __name__ == "__main__":
    configure_logging()
    result = migrate()
    log.info("migrations complete", extra={"applied": result})
