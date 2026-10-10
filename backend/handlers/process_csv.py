"""S3 ObjectCreated -> CSV import.

S3 invokes Lambda asynchronously; if this handler raises, Lambda retries the
event (up to 2 more times). Duplicate deliveries are harmless because
``process_object`` atomically claims the batch before doing any work.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import unquote_plus

from app.config import get_settings
from app.ingestion.service import TransientProcessingError, process_object
from app.logging_setup import configure_logging

configure_logging()
log = logging.getLogger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    settings = get_settings()
    outcomes = []
    retry_needed = False
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        try:
            outcome = process_object(settings, bucket, key)
            outcomes.append(outcome.__dict__)
        except TransientProcessingError:
            retry_needed = True
    if retry_needed:
        # Re-raise so Lambda's async retry re-delivers the event; completed records are skipped as duplicates.
        raise TransientProcessingError("one or more records need a retry")
    log.info("s3 event processed", extra={"outcomes": outcomes})
    return {"outcomes": outcomes}
