"""Shared helpers for database tests."""

from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.db import connection
from app.ingestion import service as ingestion
from app.storage import LOCAL_BUCKET, write_local_object

HEADER = "user_id,email,monthly_income,credit_score,employment_status,age\n"


def upload_csv(settings: Settings, content: str, filename: str = "users.csv") -> tuple[str, str]:
    """Create a batch, store its file locally; returns (batch_id, key). Does not process."""
    with connection() as conn:
        batch = ingestion.create_batch(conn, filename, LOCAL_BUCKET)
    write_local_object(settings, batch["storage_key"], iter([content.encode("utf-8")]))
    return str(batch["id"]), batch["storage_key"]


def import_csv(settings: Settings, content: str) -> str:
    batch_id, key = upload_csv(settings, content)
    outcome = ingestion.process_object(settings, LOCAL_BUCKET, key)
    assert outcome.action == "processed", outcome
    return batch_id


def add_product(conn, **overrides: Any) -> int:
    values: dict[str, Any] = {
        "lender_name": "Test Lender",
        "product_name": f"Product {overrides.get('product_name', 'X')}",
        "source_url": "https://fixtures.invalid/p",
        "source_attribution": "test fixture",
        "data_origin": "dev_fixture",
        "interest_rate_min": 10,
        "interest_rate_max": 15,
        "min_monthly_income": None,
        "min_credit_score": None,
        "max_credit_score": None,
        "min_age": None,
        "max_age": None,
        "employment_types": None,
        "employment_criteria_text": None,
        "other_criteria": [],
        "is_active": True,
    }
    values.update(overrides)
    values["other_criteria"] = json.dumps(values["other_criteria"])
    columns = ", ".join(values)
    placeholders = ", ".join(f"%({k})s" for k in values)
    return conn.execute(
        f"INSERT INTO loan_products ({columns}) VALUES ({placeholders}) RETURNING id", values
    ).fetchone()["id"]
