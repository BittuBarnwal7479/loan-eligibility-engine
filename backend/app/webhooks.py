"""Outbound call that starts n8n Workflow B after a batch is committed."""

from __future__ import annotations

import logging
import time
import uuid

import httpx

from app.config import Settings
from app.db import connection

log = logging.getLogger(__name__)


def trigger_matching_webhook(settings: Settings, batch_id: uuid.UUID, attempts: int = 3) -> str:
    """POST {upload_batch_id} to the n8n matching webhook with a shared-secret header.

    A delivery failure does not undo the import: the batch keeps its import
    status and ``matching_status`` becomes TRIGGER_FAILED so it can be re-triggered.
    Returns the resulting matching_status.
    """
    if not settings.n8n_matching_webhook_url or not settings.n8n_webhook_token:
        log.info("matching webhook not configured; skipping trigger", extra={"batch_id": str(batch_id)})
        return "NOT_STARTED"

    payload = {"event": "upload.completed", "upload_batch_id": str(batch_id)}
    headers = {"X-Webhook-Token": settings.n8n_webhook_token}
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            response = httpx.post(settings.n8n_matching_webhook_url, json=payload, headers=headers, timeout=10)
            if response.status_code < 300:
                _record(batch_id, "TRIGGERED", None)
                log.info("matching workflow triggered", extra={"batch_id": str(batch_id)})
                return "TRIGGERED"
            last_error = f"HTTP {response.status_code}"
            if 400 <= response.status_code < 500 and response.status_code != 429:
                break  # auth/config problem: retrying will not help
        except httpx.HTTPError as exc:
            last_error = type(exc).__name__
        time.sleep(min(2 ** (attempt - 1), 4))
    _record(batch_id, "TRIGGER_FAILED", last_error)
    log.warning("matching webhook failed", extra={"batch_id": str(batch_id), "error": last_error})
    return "TRIGGER_FAILED"


def _record(batch_id: uuid.UUID, status: str, error: str | None) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE upload_batches SET matching_status = %s, matching_error = %s,"
            " matching_triggered_at = now(), updated_at = now()"
            " WHERE id = %s AND matching_status IN ('NOT_STARTED', 'TRIGGER_FAILED', 'TRIGGERED')",
            (status, error, batch_id),
        )
