"""PostgreSQL access helpers (psycopg 3).

All SQL in this project is parameterized; values are never interpolated into
query strings.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings

_lambda_conn: psycopg.Connection | None = None


def _new_connection() -> psycopg.Connection:
    return psycopg.connect(
        get_settings().database_url,
        row_factory=dict_row,
        connect_timeout=10,
        application_name="loan-eligibility-engine",
    )


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """Yield a connection. Commits on success, rolls back on error.

    On Lambda a single connection is reused across warm invocations to avoid
    exhausting RDS connections; locally a fresh connection is used per call.
    """
    global _lambda_conn
    on_lambda = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    if on_lambda:
        if _lambda_conn is None or _lambda_conn.closed or _lambda_conn.broken:
            _lambda_conn = _new_connection()
        conn = _lambda_conn
    else:
        conn = _new_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if not on_lambda:
            conn.close()
