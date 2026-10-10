"""One-off maintenance Lambda (RDS is private, so migrations run inside the VPC).

serverless invoke -f migrate --stage dev
serverless invoke -f migrate --stage dev --data '{"seed_dev_fixtures": true}'   # demo only: synthetic products
"""

from __future__ import annotations

from typing import Any

from app.db import connection
from app.logging_setup import configure_logging
from app.migrate import migrate
from app.products.service import seed_dev_fixtures

configure_logging()


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    applied = migrate()
    seeded = 0
    if (event or {}).get("seed_dev_fixtures") is True:
        with connection() as conn:
            seeded = seed_dev_fixtures(conn)
    return {"applied_migrations": applied, "dev_fixtures_seeded": seeded}
