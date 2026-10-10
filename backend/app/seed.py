"""Load synthetic development loan products (never run this in production).

Usage: python -m app.seed
"""

from __future__ import annotations

import logging

from app.db import connection
from app.logging_setup import configure_logging
from app.products.service import seed_dev_fixtures

if __name__ == "__main__":
    configure_logging()
    with connection() as conn:
        count = seed_dev_fixtures(conn)
    logging.getLogger(__name__).info("dev fixtures loaded", extra={"fixtures": count})
