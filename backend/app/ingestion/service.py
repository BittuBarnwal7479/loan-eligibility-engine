"""Upload batch lifecycle and CSV import.

State machine for ``upload_batches.status``::

    PENDING --claim--> PROCESSING --commit--> COMPLETED | COMPLETED_WITH_ERRORS
                           |                         (rows + status in ONE transaction)
                           +--> FAILED   (bad header / too large / no valid rows / retries exhausted)
                           +--> PENDING  (transient error; S3/Lambda retry may reclaim)

Duplicate S3 events are ignored because only one worker can move a batch out
of PENDING (atomic UPDATE ... WHERE status = 'PENDING'). A PROCESSING batch
whose lease expired (worker crashed) can be reclaimed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg

from app.config import Settings
from app.db import connection
from app.ingestion.csv_parser import CsvHeaderError, ParseResult, UserRecord, iter_text_lines, parse_csv
from app.storage import UploadTooLarge, batch_id_from_key, object_key, read_object
from app.webhooks import trigger_matching_webhook

log = logging.getLogger(__name__)

MAX_PROCESSING_ATTEMPTS = 3


@dataclass(frozen=True)
class ProcessOutcome:
    batch_id: str | None
    action: str  # processed | skipped_duplicate | failed | unknown_key | retry_scheduled
    status: str | None = None
    detail: str | None = None


class TransientProcessingError(RuntimeError):
    """Raised so the Lambda runtime retries the S3 event."""


def create_batch(conn: psycopg.Connection, original_filename: str, bucket: str | None) -> dict[str, Any]:
    batch_id = uuid.uuid4()
    safe_name = original_filename.replace("\\", "/").rsplit("/", 1)[-1][:255] or "upload.csv"
    return conn.execute(
        "INSERT INTO upload_batches (id, original_filename, storage_bucket, storage_key)"
        " VALUES (%s, %s, %s, %s) RETURNING *",
        (batch_id, safe_name, bucket, object_key(batch_id)),
    ).fetchone()


def get_batch(conn: psycopg.Connection, batch_id: uuid.UUID | str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM upload_batches WHERE id = %s", (str(batch_id),)).fetchone()


def list_recent_batches(conn: psycopg.Connection, limit: int) -> list[dict[str, Any]]:
    return conn.execute("SELECT * FROM upload_batches ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()


def _claim(conn: psycopg.Connection, key: str, lease_seconds: int) -> dict[str, Any] | None:
    return conn.execute(
        """
        UPDATE upload_batches
           SET status = 'PROCESSING', attempt_count = attempt_count + 1,
               processing_started_at = now(), error_message = NULL, updated_at = now()
         WHERE storage_key = %s
           AND (status = 'PENDING'
                OR (status = 'PROCESSING' AND processing_started_at < now() - make_interval(secs => %s)))
        RETURNING *
        """,
        (key, lease_seconds),
    ).fetchone()


def _mark(conn: psycopg.Connection, batch_id: uuid.UUID, status: str, message: str | None, **counts: int) -> None:
    conn.execute(
        """
        UPDATE upload_batches
           SET status = %s, error_message = %s, updated_at = now(),
               processing_completed_at = CASE WHEN %s IN ('PENDING') THEN NULL ELSE now() END,
               total_rows = COALESCE(%s, total_rows), failed_rows = COALESCE(%s, failed_rows)
         WHERE id = %s
        """,
        (status, message, status, counts.get("total_rows"), counts.get("failed_rows"), batch_id),
    )


_UPSERT_PREFIX = (
    "INSERT INTO users (user_id, email, monthly_income, credit_score, employment_status, age, upload_batch_id) VALUES "
)
_UPSERT_SUFFIX = """
 ON CONFLICT (user_id) DO UPDATE SET
     email = EXCLUDED.email,
     monthly_income = EXCLUDED.monthly_income,
     credit_score = EXCLUDED.credit_score,
     employment_status = EXCLUDED.employment_status,
     age = EXCLUDED.age,
     upload_batch_id = EXCLUDED.upload_batch_id,
     updated_at = now()
 RETURNING (xmax = 0) AS inserted
"""


def upsert_users(
    conn: psycopg.Connection, batch_id: uuid.UUID, records: list[UserRecord], chunk_size: int
) -> tuple[int, int]:
    """Bounded multi-row upserts. Only placeholders are generated dynamically."""
    inserted = updated = 0
    for start in range(0, len(records), chunk_size):
        chunk = records[start : start + chunk_size]
        placeholders = ",".join(["(%s, %s, %s, %s, %s, %s, %s)"] * len(chunk))
        params: list[Any] = []
        for r in chunk:
            params.extend([r.user_id, r.email, r.monthly_income, r.credit_score, r.employment_status, r.age, batch_id])
        for row in conn.execute(_UPSERT_PREFIX + placeholders + _UPSERT_SUFFIX, params).fetchall():
            if row["inserted"]:
                inserted += 1
            else:
                updated += 1
    return inserted, updated


class _HashingStream:
    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self.sha256 = hashlib.sha256()

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            self.sha256.update(chunk)
            yield chunk


def process_object(settings: Settings, bucket: str, key: str) -> ProcessOutcome:
    """Import one uploaded CSV. Safe to call repeatedly for the same object."""
    batch_uuid = batch_id_from_key(key)
    if batch_uuid is None:
        log.warning("ignoring object with unexpected key", extra={"key": key})
        return ProcessOutcome(None, "unknown_key")
    batch_id = str(batch_uuid)

    with connection() as conn:
        claimed = _claim(conn, key, settings.processing_lease_seconds)
        if claimed is None:
            existing = get_batch(conn, batch_uuid)
    if claimed is None:
        status = existing["status"] if existing else None
        log.info("duplicate or unknown event ignored", extra={"batch_id": batch_id, "status": status})
        return ProcessOutcome(batch_id, "skipped_duplicate" if existing else "unknown_key", status)

    log.info("batch claimed", extra={"batch_id": batch_id, "attempt": claimed["attempt_count"]})

    # 1) Read + validate (no DB transaction held open while streaming the file).
    try:
        stream = _HashingStream(read_object(settings, bucket, key))
        parsed: ParseResult = parse_csv(
            iter_text_lines(stream), settings.max_csv_rows, settings.max_reported_row_errors
        )
    except (CsvHeaderError, UploadTooLarge) as exc:
        with connection() as conn:
            _mark(conn, batch_uuid, "FAILED", str(exc))
        log.warning("batch rejected", extra={"batch_id": batch_id, "reason": str(exc)})
        return ProcessOutcome(batch_id, "failed", "FAILED", str(exc))
    except Exception as exc:  # storage outage etc.
        return _transient_failure(batch_uuid, claimed["attempt_count"], exc)

    if not parsed.records:
        with connection() as conn:
            conn.execute(
                "UPDATE upload_batches SET row_errors = %s::jsonb WHERE id = %s",
                (json.dumps([e.as_dict() for e in parsed.errors]), batch_uuid),
            )
            _mark(
                conn,
                batch_uuid,
                "FAILED",
                "no valid rows",
                total_rows=parsed.total_rows,
                failed_rows=parsed.failed_rows,
            )
        return ProcessOutcome(batch_id, "failed", "FAILED", "no valid rows")

    # 2) Persist all valid rows and the final status atomically.
    final_status = "COMPLETED_WITH_ERRORS" if parsed.failed_rows else "COMPLETED"
    try:
        with connection() as conn:
            inserted, updated = upsert_users(conn, batch_uuid, parsed.records, settings.insert_chunk_size)
            conn.execute(
                """
                UPDATE upload_batches
                   SET status = %s, total_rows = %s, processed_rows = %s, inserted_rows = %s,
                       updated_rows = %s, failed_rows = %s, row_errors = %s::jsonb,
                       content_sha256 = %s, processing_completed_at = now(), updated_at = now()
                 WHERE id = %s AND status = 'PROCESSING'
                """,
                (
                    final_status,
                    parsed.total_rows,
                    len(parsed.records),
                    inserted,
                    updated,
                    parsed.failed_rows,
                    json.dumps([e.as_dict() for e in parsed.errors]),
                    stream.sha256.hexdigest(),
                    batch_uuid,
                ),
            )
    except Exception as exc:
        return _transient_failure(batch_uuid, claimed["attempt_count"], exc)

    log.info(
        "batch imported",
        extra={
            "batch_id": batch_id,
            "status": final_status,
            "inserted": inserted,
            "updated": updated,
            "failed_rows": parsed.failed_rows,
            "total_rows": parsed.total_rows,
        },
    )
    trigger_matching_webhook(settings, batch_uuid)
    return ProcessOutcome(batch_id, "processed", final_status)


def _transient_failure(batch_id: uuid.UUID, attempt: int, exc: Exception) -> ProcessOutcome:
    give_up = attempt >= MAX_PROCESSING_ATTEMPTS
    status = "FAILED" if give_up else "PENDING"
    message = f"processing error ({type(exc).__name__}); attempt {attempt}"
    log.exception("batch processing error", extra={"batch_id": str(batch_id), "will_retry": not give_up})
    try:
        with connection() as conn:
            _mark(conn, batch_id, status, message)
    except Exception:
        log.exception("could not record batch failure", extra={"batch_id": str(batch_id)})
    if give_up:
        return ProcessOutcome(str(batch_id), "failed", "FAILED", message)
    raise TransientProcessingError(message) from exc


def batch_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Public view of a batch (no storage internals beyond the key)."""
    return {
        "upload_batch_id": str(row["id"]),
        "original_filename": row["original_filename"],
        "status": row["status"],
        "total_rows": row["total_rows"],
        "processed_rows": row["processed_rows"],
        "inserted_rows": row["inserted_rows"],
        "updated_rows": row["updated_rows"],
        "failed_rows": row["failed_rows"],
        "row_errors": row["row_errors"],
        "error_message": row["error_message"],
        "matching_status": row["matching_status"],
        "notification_status": row["notification_status"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "processing_completed_at": row["processing_completed_at"].isoformat()
        if row["processing_completed_at"]
        else None,
    }
