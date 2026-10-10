"""Loan product persistence: source config, validated upserts, staleness."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from app.products.extract import ExtractedProduct

log = logging.getLogger(__name__)

SOURCES_FILE = Path(__file__).resolve().parents[2] / "config" / "loan_sources.json"
FIXTURES_FILE = Path(__file__).resolve().parents[2] / "seeds" / "dev_loan_products.json"
MIN_FIELDS_FOR_VALID_CRAWL = 2
STALE_AFTER_DAYS = 7
DEACTIVATE_AFTER_DAYS = 30
EMPLOYMENT_TYPES = {"salaried", "self_employed", "unemployed", "student", "retired"}


def load_sources() -> dict[str, Any]:
    config = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    config["sources"] = [s for s in config["sources"] if s.get("enabled", True)]
    return config


def get_source(source_key: str) -> dict[str, Any] | None:
    return next((s for s in load_sources()["sources"] if s["source_key"] == source_key), None)


def validate_extracted(product: ExtractedProduct) -> list[str]:
    """Drop out-of-range values (recording why) instead of storing bad data."""
    problems: list[str] = []
    if product.interest_rate_min is not None and not Decimal(0) < product.interest_rate_min <= Decimal(60):
        problems.append("interest_rate_min out of range; dropped")
        product.interest_rate_min = None
    if product.interest_rate_max is not None and (
        not Decimal(0) < product.interest_rate_max <= Decimal(60)
        or (product.interest_rate_min is not None and product.interest_rate_max < product.interest_rate_min)
    ):
        problems.append("interest_rate_max invalid; dropped")
        product.interest_rate_max = None
    if product.min_monthly_income is not None and not Decimal(0) < product.min_monthly_income <= Decimal(10_000_000):
        problems.append("min_monthly_income out of range; dropped")
        product.min_monthly_income = None
    if product.min_credit_score is not None and not 300 <= product.min_credit_score <= 900:
        problems.append("min_credit_score out of range; dropped")
        product.min_credit_score = None
    for name in ("min_age", "max_age"):
        value = getattr(product, name)
        if value is not None and not 18 <= value <= 100:
            problems.append(f"{name} out of range; dropped")
            setattr(product, name, None)
    if product.employment_types is not None:
        product.employment_types = [t for t in product.employment_types if t in EMPLOYMENT_TYPES] or None
    product.notes.extend(problems)
    return problems


UPSERT_SQL = """
INSERT INTO loan_products (
    lender_name, product_name, source_key, source_url, source_attribution, data_origin,
    interest_rate_min, interest_rate_max, interest_rate_text, min_monthly_income, min_credit_score,
    min_age, max_age, employment_types, employment_types_source, employment_criteria_text,
    other_criteria, extraction_notes, last_verified_at, last_crawl_attempt_at, consecutive_crawl_failures,
    is_stale, is_active)
VALUES (%(lender_name)s, %(product_name)s, %(source_key)s, %(source_url)s, %(source_attribution)s, 'crawled',
    %(interest_rate_min)s, %(interest_rate_max)s, %(interest_rate_text)s, %(min_monthly_income)s, %(min_credit_score)s,
    %(min_age)s, %(max_age)s, %(employment_types)s, %(employment_types_source)s, %(employment_criteria_text)s,
    %(other_criteria)s::jsonb, %(extraction_notes)s::jsonb, now(), now(), 0, false, true)
ON CONFLICT (lender_name, product_name) DO UPDATE SET
    source_key = EXCLUDED.source_key,
    source_url = EXCLUDED.source_url,
    source_attribution = EXCLUDED.source_attribution,
    interest_rate_min = EXCLUDED.interest_rate_min,
    interest_rate_max = EXCLUDED.interest_rate_max,
    interest_rate_text = EXCLUDED.interest_rate_text,
    min_monthly_income = EXCLUDED.min_monthly_income,
    min_credit_score = EXCLUDED.min_credit_score,
    min_age = EXCLUDED.min_age,
    max_age = EXCLUDED.max_age,
    -- Keep an earlier LLM normalization when the free-text requirement is unchanged.
    employment_types = COALESCE(EXCLUDED.employment_types,
        CASE WHEN loan_products.employment_criteria_text IS NOT DISTINCT FROM EXCLUDED.employment_criteria_text
             THEN loan_products.employment_types END),
    employment_types_source = CASE
        WHEN EXCLUDED.employment_types IS NOT NULL THEN EXCLUDED.employment_types_source
        WHEN loan_products.employment_criteria_text IS NOT DISTINCT FROM EXCLUDED.employment_criteria_text
             THEN loan_products.employment_types_source END,
    employment_criteria_text = EXCLUDED.employment_criteria_text,
    other_criteria = EXCLUDED.other_criteria,
    extraction_notes = EXCLUDED.extraction_notes,
    last_verified_at = now(),
    last_crawl_attempt_at = now(),
    consecutive_crawl_failures = 0,
    is_stale = false,
    updated_at = now()
RETURNING id, (xmax = 0) AS inserted
"""


def upsert_crawled_product(
    conn: psycopg.Connection, source: dict[str, Any], product: ExtractedProduct
) -> dict[str, Any]:
    params = {
        "lender_name": source["lender_name"],
        "product_name": source["product_name"],
        "source_key": source["source_key"],
        "source_url": source["url"],
        "source_attribution": f"Extracted from the public product page of {source['lender_name']}",
        "interest_rate_min": product.interest_rate_min,
        "interest_rate_max": product.interest_rate_max,
        "interest_rate_text": product.interest_rate_text,
        "min_monthly_income": product.min_monthly_income,
        "min_credit_score": product.min_credit_score,
        "min_age": product.min_age,
        "max_age": product.max_age,
        "employment_types": product.employment_types,
        "employment_types_source": "extracted" if product.employment_types else None,
        "employment_criteria_text": product.employment_criteria_text,
        "other_criteria": json.dumps(product.other_criteria),
        "extraction_notes": json.dumps(product.notes),
    }
    return conn.execute(UPSERT_SQL, params).fetchone()


def record_crawl_failure(conn: psycopg.Connection, source: dict[str, Any], reason: str) -> None:
    """Never deletes or overwrites criteria; only counts the failure."""
    conn.execute(
        "UPDATE loan_products SET consecutive_crawl_failures = consecutive_crawl_failures + 1,"
        " last_crawl_attempt_at = now(),"
        " extraction_notes = extraction_notes || %s::jsonb, updated_at = now()"
        " WHERE lender_name = %s AND product_name = %s",
        (json.dumps([f"crawl failed: {reason}"[:200]]), source["lender_name"], source["product_name"]),
    )


def refresh_staleness(conn: psycopg.Connection) -> dict[str, int]:
    """Flag crawled products not verified recently; deactivate (never delete) very old ones."""
    stale = conn.execute(
        "UPDATE loan_products SET is_stale = true, updated_at = now()"
        " WHERE data_origin = 'crawled' AND NOT is_stale"
        " AND (last_verified_at < now() - make_interval(days => %s) OR consecutive_crawl_failures >= 3)",
        (STALE_AFTER_DAYS,),
    ).rowcount
    deactivated = conn.execute(
        "UPDATE loan_products SET is_active = false, updated_at = now()"
        " WHERE data_origin = 'crawled' AND is_active AND last_verified_at < now() - make_interval(days => %s)",
        (DEACTIVATE_AFTER_DAYS,),
    ).rowcount
    return {"flagged_stale": stale, "deactivated": deactivated}


def seed_dev_fixtures(conn: psycopg.Connection) -> int:
    """Load clearly-labelled synthetic products for local testing only."""
    fixtures = json.loads(FIXTURES_FILE.read_text(encoding="utf-8"))["products"]
    for f in fixtures:
        conn.execute(
            """
            INSERT INTO loan_products (lender_name, product_name, source_url, source_attribution, data_origin,
                interest_rate_min, interest_rate_max, interest_rate_text, min_monthly_income, min_credit_score,
                max_credit_score, min_age, max_age, employment_types, employment_types_source,
                employment_criteria_text, other_criteria, extraction_notes, last_verified_at)
            VALUES (%(lender_name)s, %(product_name)s, %(source_url)s, %(source_attribution)s, 'dev_fixture',
                %(interest_rate_min)s, %(interest_rate_max)s, %(interest_rate_text)s, %(min_monthly_income)s,
                %(min_credit_score)s, %(max_credit_score)s, %(min_age)s, %(max_age)s, %(employment_types)s,
                %(employment_types_source)s, %(employment_criteria_text)s, %(other_criteria)s::jsonb,
                '["synthetic development fixture - not a real product"]'::jsonb, NULL)
            ON CONFLICT (lender_name, product_name) DO NOTHING
            """,
            {
                **f,
                "employment_types": f.get("employment_types"),
                "employment_types_source": "fixture" if f.get("employment_types") else None,
                "employment_criteria_text": f.get("employment_criteria_text"),
                "max_credit_score": f.get("max_credit_score"),
                "other_criteria": json.dumps(f.get("other_criteria", [])),
            },
        )
    return len(fixtures)
