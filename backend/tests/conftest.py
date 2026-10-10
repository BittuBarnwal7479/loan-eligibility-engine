"""Test configuration.

Unit tests need nothing external. Tests marked ``db`` need PostgreSQL:
set TEST_DATABASE_URL to a server URL whose user may CREATE DATABASE, e.g.
postgresql://loan_engine:<password>@localhost:5433/postgres . A throwaway
database ``loan_engine_test`` is (re)created for the session.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import pytest

_UPLOAD_DIR = tempfile.mkdtemp(prefix="loan-engine-uploads-")
_TEST_DB_NAME = "loan_engine_test"
_ADMIN_URL = os.environ.get("TEST_DATABASE_URL")


def _with_db(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


# Settings are read from the environment; set safe test values before app imports.
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": _with_db(_ADMIN_URL, _TEST_DB_NAME) if _ADMIN_URL else "postgresql://invalid/none",
        "STORAGE_MODE": "local",
        "LOCAL_UPLOAD_DIR": _UPLOAD_DIR,
        "UPLOAD_SIGNING_SECRET": "test-upload-secret",
        "INTERNAL_API_TOKEN": "test-internal-token",
        "PUBLIC_API_BASE_URL": "http://testserver",
        "NOTIFICATION_MODE": "dry_run",
        "LLM_ENABLED": "false",
        "MATCHING_CHUNK_SIZE": "7",  # small on purpose so tests cross chunk boundaries
        "INSERT_CHUNK_SIZE": "5",
    }
)
os.environ.pop("N8N_MATCHING_WEBHOOK_URL", None)
os.environ.pop("NOTIFICATION_TEST_RECIPIENT", None)


@pytest.fixture(scope="session")
def database() -> Iterator[str]:
    if not _ADMIN_URL:
        pytest.skip("TEST_DATABASE_URL not set; skipping database tests")
    import psycopg

    with psycopg.connect(_ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB_NAME}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{_TEST_DB_NAME}"')
    from app.migrate import migrate

    applied = migrate()
    assert applied, "expected migrations to be applied to a fresh database"
    assert migrate() == [], "migrations must be idempotent"
    yield os.environ["DATABASE_URL"]


@pytest.fixture()
def db(database: str):
    """Clean tables before each db test and yield an autocommit connection for assertions."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(
            "TRUNCATE notification_logs, matches, matching_runs, users, loan_products, upload_batches RESTART IDENTITY CASCADE"
        )
        yield conn


@pytest.fixture()
def settings(monkeypatch):
    from app.config import get_settings

    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()
