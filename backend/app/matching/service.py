"""Multi-stage matching pipeline for one upload batch.

Stage 1  SQL pre-filter: a parameterized join eliminates user-product pairs
         that violate any *known* numeric/categorical threshold. Unknown (NULL)
         thresholds never eliminate a pair.
Stage 2  Deterministic evaluation of each surviving pair, producing per-criterion
         explanations and ELIGIBLE / POTENTIAL / REJECTED.
Stage 3  Persist matches idempotently (unique per user, product, batch).

Users are processed in keyset-paginated chunks so memory stays bounded and
each chunk commits independently. A retried run replaces the previous run's
results for the batch, so retries are idempotent.

The optional LLM step (app.matching.llm_normalizer) runs *before* this and only
normalizes free-text product criteria, once per product - never per pair.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import psycopg

from app.config import Settings
from app.db import connection
from app.matching.engine import evaluate

log = logging.getLogger(__name__)

PRODUCT_COLUMNS = (
    "id, lender_name, product_name, min_monthly_income, min_credit_score, max_credit_score,"
    " min_age, max_age, employment_types, other_criteria"
)

# Stage 1. Every value is a bound parameter; the user chunk is passed as an array.
PREFILTER_SQL = """
SELECT u.id AS user_pk, u.monthly_income, u.credit_score, u.age, u.employment_status,
       p.id AS product_id
  FROM users u
  JOIN loan_products p ON p.is_active
 WHERE u.upload_batch_id = %(batch_id)s
   AND u.id = ANY(%(user_ids)s)
   AND (p.min_monthly_income IS NULL OR u.monthly_income >= p.min_monthly_income)
   AND (p.min_credit_score  IS NULL OR u.credit_score   >= p.min_credit_score)
   AND (p.max_credit_score  IS NULL OR u.credit_score   <= p.max_credit_score)
   AND (p.min_age           IS NULL OR u.age            >= p.min_age)
   AND (p.max_age           IS NULL OR u.age            <= p.max_age)
   AND (p.employment_types  IS NULL OR cardinality(p.employment_types) = 0
        OR u.employment_status = ANY(p.employment_types))
 ORDER BY u.id, p.id
"""

UPSERT_MATCH_SQL = """
INSERT INTO matches (user_id, loan_product_id, upload_batch_id, matching_run_id, match_status,
                     criteria_coverage, passed_criteria, unknown_criteria, decided_by, matched_at)
VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, now())
ON CONFLICT (user_id, loan_product_id, upload_batch_id) DO UPDATE SET
    matching_run_id = EXCLUDED.matching_run_id,
    match_status = EXCLUDED.match_status,
    criteria_coverage = EXCLUDED.criteria_coverage,
    passed_criteria = EXCLUDED.passed_criteria,
    unknown_criteria = EXCLUDED.unknown_criteria,
    decided_by = EXCLUDED.decided_by,
    matched_at = now()
"""


class BatchNotReady(RuntimeError):
    pass


class MatchingAlreadyRunning(RuntimeError):
    pass


def _lock_key(batch_id: uuid.UUID) -> int:
    # Stable signed 64-bit key derived from the UUID.
    return int.from_bytes(batch_id.bytes[:8], "big", signed=True)


def run_matching(settings: Settings, batch_id: uuid.UUID, llm_invocations: int = 0) -> dict[str, Any]:
    run_id = uuid.uuid4()
    with connection() as conn:
        batch = conn.execute("SELECT id, status FROM upload_batches WHERE id = %s", (batch_id,)).fetchone()
        if batch is None:
            raise BatchNotReady("upload batch not found")
        if batch["status"] not in ("COMPLETED", "COMPLETED_WITH_ERRORS"):
            raise BatchNotReady(f"upload batch status is {batch['status']}")
        # Session-level lock: held across the per-chunk commits below.
        if not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (_lock_key(batch_id),)).fetchone()["ok"]:
            raise MatchingAlreadyRunning("matching is already running for this batch")
        try:
            return _run_locked(settings, conn, batch_id, run_id, llm_invocations)
        finally:
            conn.rollback()
            conn.execute("SELECT pg_advisory_unlock(%s)", (_lock_key(batch_id),))
            conn.commit()


def _run_locked(
    settings: Settings, conn: psycopg.Connection, batch_id: uuid.UUID, run_id: uuid.UUID, llm_invocations: int
) -> dict[str, Any]:
    products = {
        p["id"]: p
        for p in conn.execute(f"SELECT {PRODUCT_COLUMNS} FROM loan_products WHERE is_active ORDER BY id").fetchall()
    }
    conn.execute(
        "INSERT INTO matching_runs (id, upload_batch_id, active_products, llm_invocations) VALUES (%s, %s, %s, %s)",
        (run_id, batch_id, len(products), llm_invocations),
    )
    conn.execute(
        "UPDATE upload_batches SET matching_status = 'RUNNING', matching_error = NULL, updated_at = now() WHERE id = %s",
        (batch_id,),
    )
    conn.commit()

    stats = {
        "users_considered": 0,
        "sql_candidate_pairs": 0,
        "deterministic_rejections": 0,
        "eligible_matches": 0,
        "potential_matches": 0,
        "chunks_processed": 0,
    }
    try:
        last_id = 0
        while True:
            user_ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM users WHERE upload_batch_id = %s AND id > %s ORDER BY id LIMIT %s",
                    (batch_id, last_id, settings.matching_chunk_size),
                ).fetchall()
            ]
            if not user_ids:
                break
            last_id = user_ids[-1]
            stats["users_considered"] += len(user_ids)
            candidates = conn.execute(PREFILTER_SQL, {"batch_id": batch_id, "user_ids": user_ids}).fetchall()
            stats["sql_candidate_pairs"] += len(candidates)

            rows: list[tuple[Any, ...]] = []
            for pair in candidates:
                evaluation = evaluate(pair, products[pair["product_id"]])
                if evaluation.status == "REJECTED":
                    stats["deterministic_rejections"] += 1
                    continue
                stats["eligible_matches" if evaluation.status == "ELIGIBLE" else "potential_matches"] += 1
                rows.append(
                    (
                        pair["user_pk"],
                        pair["product_id"],
                        batch_id,
                        run_id,
                        evaluation.status,
                        evaluation.coverage,
                        json.dumps([r.as_dict() for r in evaluation.passed]),
                        json.dumps([r.as_dict() for r in evaluation.unknown]),
                        "deterministic",
                    )
                )
            if rows:
                with conn.cursor() as cur:
                    cur.executemany(UPSERT_MATCH_SQL, rows)
            stats["chunks_processed"] += 1
            conn.commit()

        # Drop results from earlier runs of this batch that this run did not re-confirm
        # (e.g. product criteria changed between a failed run and its retry).
        conn.execute(
            "DELETE FROM matches WHERE upload_batch_id = %s AND matching_run_id IS DISTINCT FROM %s",
            (batch_id, run_id),
        )
        total_pairs = stats["users_considered"] * len(products)
        summary = {
            **stats,
            "active_products": len(products),
            "total_possible_pairs": total_pairs,
            "sql_eliminated_pairs": total_pairs - stats["sql_candidate_pairs"],
            "llm_invocations": llm_invocations,
        }
        conn.execute(
            """
            UPDATE matching_runs SET status = 'COMPLETED', finished_at = now(),
                   users_considered = %(users_considered)s, total_possible_pairs = %(total_possible_pairs)s,
                   sql_candidate_pairs = %(sql_candidate_pairs)s, sql_eliminated_pairs = %(sql_eliminated_pairs)s,
                   deterministic_rejections = %(deterministic_rejections)s,
                   eligible_matches = %(eligible_matches)s, potential_matches = %(potential_matches)s,
                   chunks_processed = %(chunks_processed)s
             WHERE id = %(run_id)s
            """,
            {**summary, "run_id": run_id},
        )
        conn.execute(
            "UPDATE upload_batches SET matching_status = 'COMPLETED', updated_at = now() WHERE id = %s", (batch_id,)
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.execute(
            "UPDATE matching_runs SET status = 'FAILED', finished_at = now(), error_message = %s WHERE id = %s",
            (type(exc).__name__, run_id),
        )
        conn.execute(
            "UPDATE upload_batches SET matching_status = 'FAILED', matching_error = %s, updated_at = now() WHERE id = %s",
            (type(exc).__name__, batch_id),
        )
        conn.commit()
        log.exception("matching run failed", extra={"batch_id": str(batch_id), "run_id": str(run_id)})
        raise

    log.info("matching run completed", extra={"batch_id": str(batch_id), "run_id": str(run_id), **summary})
    return {"matching_run_id": str(run_id), "upload_batch_id": str(batch_id), **summary}


def latest_run(conn: psycopg.Connection, batch_id: uuid.UUID) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT * FROM matching_runs WHERE upload_batch_id = %s ORDER BY started_at DESC LIMIT 1", (batch_id,)
    ).fetchone()
