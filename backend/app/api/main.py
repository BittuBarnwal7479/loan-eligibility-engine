"""HTTP API (FastAPI). Runs under uvicorn locally and under Mangum on Lambda.

Public endpoints (used by the frontend):
    POST /api/uploads                 create a batch + short-lived upload target
    GET  /api/uploads/{batch_id}      batch status, row errors, matching/notification progress
    GET  /api/csv-template            downloadable CSV header template
    POST /local-uploads/{batch_id}    local-mode stand-in for S3 presigned POST

Internal endpoints (n8n only; require the X-Internal-Token header):
    /internal/batches/..., /internal/matching/..., /internal/notifications/..., /internal/products/...
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.db import connection
from app.ingestion import service as ingestion
from app.ingestion.csv_parser import REQUIRED_COLUMNS
from app.logging_setup import configure_logging
from app.matching import service as matching
from app.matching.llm_normalizer import normalize_ambiguous_products
from app.notifications import service as notifications
from app.products import extract as product_extract
from app.products import service as products
from app.security import constant_time_equals, verify_upload_signature
from app.storage import LOCAL_BUCKET, UploadTooLarge, create_upload_target, object_key, write_local_object
from app.webhooks import trigger_matching_webhook

configure_logging()
log = logging.getLogger(__name__)

app = FastAPI(title="Loan Eligibility Engine API", version="1.0.0")
_settings_for_cors = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings_for_cors.cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

SettingsDep = Annotated[Settings, Depends(get_settings)]


def require_internal_token(settings: SettingsDep, x_internal_token: Annotated[str | None, Header()] = None) -> None:
    if not constant_time_equals(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=401, detail="invalid or missing internal token")


Internal = Depends(require_internal_token)


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="upload batch not found") from exc


# --------------------------------------------------------------------------- public


@app.get("/health")
def health() -> dict[str, str]:
    with connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


class CreateUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)


@app.post("/api/uploads", status_code=201)
def create_upload(body: CreateUploadRequest, settings: SettingsDep) -> dict[str, Any]:
    if not body.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="only .csv files are accepted")
    if body.size_bytes > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail=f"file exceeds {settings.max_upload_bytes} bytes")
    bucket = settings.s3_bucket if settings.storage_mode == "s3" else LOCAL_BUCKET
    with connection() as conn:
        batch = ingestion.create_batch(conn, body.filename, bucket)
    target = create_upload_target(settings, batch["id"])
    log.info("upload batch created", extra={"batch_id": str(batch["id"]), "storage_mode": target.storage_mode})
    return {
        "upload_batch_id": str(batch["id"]),
        "status": batch["status"],
        "storage_mode": target.storage_mode,
        "max_upload_bytes": settings.max_upload_bytes,
        "upload": {
            "method": target.method,
            "url": target.url,
            "fields": target.fields,
            "expires_at": target.expires_at,
        },
    }


@app.get("/api/uploads/{batch_id}")
def get_upload(batch_id: str) -> dict[str, Any]:
    bid = _uuid(batch_id)
    with connection() as conn:
        batch = ingestion.get_batch(conn, bid)
        if batch is None:
            raise HTTPException(status_code=404, detail="upload batch not found")
        run = matching.latest_run(conn, bid)
        notif = conn.execute(
            "SELECT status, count(*) AS n FROM notification_logs WHERE upload_batch_id = %s GROUP BY status", (bid,)
        ).fetchall()
    result = ingestion.batch_summary(batch)
    result["matching"] = _run_view(run)
    result["notifications"] = {r["status"]: r["n"] for r in notif}
    return result


def _run_view(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if run is None:
        return None
    keys = (
        "status",
        "users_considered",
        "active_products",
        "total_possible_pairs",
        "sql_candidate_pairs",
        "sql_eliminated_pairs",
        "deterministic_rejections",
        "eligible_matches",
        "potential_matches",
        "llm_invocations",
        "chunks_processed",
    )
    view = {k: run[k] for k in keys}
    view["matching_run_id"] = str(run["id"])
    view["finished_at"] = run["finished_at"].isoformat() if run["finished_at"] else None
    return view


@app.get("/api/csv-template", response_class=PlainTextResponse)
def csv_template() -> PlainTextResponse:
    content = ",".join(REQUIRED_COLUMNS) + "\nU0001,someone@example.com,55000,742,salaried,29\n"
    return PlainTextResponse(
        content, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=users_template.csv"}
    )


@app.post("/local-uploads/{batch_id}", status_code=204, response_class=Response, response_model=None)
async def local_upload(
    batch_id: str,
    background: BackgroundTasks,
    settings: SettingsDep,
    expires: Annotated[int, Query()],
    signature: Annotated[str, Query()],
    file: Annotated[UploadFile, File()],
) -> Response:
    """Local stand-in for S3: verify the signed URL, store the file, emit the 'object created' event."""
    if settings.storage_mode != "local":
        raise HTTPException(status_code=404, detail="not found")
    bid = _uuid(batch_id)
    if not settings.upload_signing_secret or not verify_upload_signature(
        settings.upload_signing_secret, str(bid), expires, signature
    ):
        raise HTTPException(status_code=403, detail="upload URL is invalid or expired")
    with connection() as conn:
        batch = ingestion.get_batch(conn, bid)
    if batch is None or batch["status"] != "PENDING":
        raise HTTPException(status_code=409, detail="upload batch is not awaiting a file")

    async def chunks():
        while chunk := await file.read(64 * 1024):
            yield chunk

    data = [c async for c in chunks()]
    try:
        write_local_object(settings, object_key(bid), iter(data))
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="upload batch already received a file") from exc
    except UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background.add_task(_simulated_s3_event, settings, object_key(bid))
    return Response(status_code=204)


def _simulated_s3_event(settings: Settings, key: str) -> None:
    try:
        ingestion.process_object(settings, LOCAL_BUCKET, key)
    except ingestion.TransientProcessingError:
        log.warning("local processing will need a retry", extra={"key": key})


# --------------------------------------------------------------------------- internal: batches + matching


@app.get("/internal/batches/{batch_id}", dependencies=[Internal])
def internal_batch(batch_id: str) -> dict[str, Any]:
    bid = _uuid(batch_id)
    with connection() as conn:
        batch = ingestion.get_batch(conn, bid)
    if batch is None:
        raise HTTPException(status_code=404, detail="upload batch not found")
    summary = ingestion.batch_summary(batch)
    summary["ready_for_matching"] = batch["status"] in ("COMPLETED", "COMPLETED_WITH_ERRORS")
    return summary


@app.post("/internal/batches/{batch_id}/trigger-matching", dependencies=[Internal])
def internal_retrigger(batch_id: str, settings: SettingsDep) -> dict[str, str]:
    bid = _uuid(batch_id)
    return {"matching_status": trigger_matching_webhook(settings, bid)}


@app.post("/internal/matching/{batch_id}/normalize-criteria", dependencies=[Internal])
def internal_normalize(batch_id: str, settings: SettingsDep) -> dict[str, Any]:
    _uuid(batch_id)
    with connection() as conn:
        return normalize_ambiguous_products(settings, conn)


class RunMatchingRequest(BaseModel):
    llm_invocations: int = Field(default=0, ge=0)


@app.post("/internal/matching/{batch_id}/run", dependencies=[Internal])
def internal_run_matching(
    batch_id: str, settings: SettingsDep, body: RunMatchingRequest | None = None
) -> dict[str, Any]:
    bid = _uuid(batch_id)
    try:
        return matching.run_matching(settings, bid, (body or RunMatchingRequest()).llm_invocations)
    except matching.BatchNotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except matching.MatchingAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/internal/matching/{batch_id}/summary", dependencies=[Internal])
def internal_matching_summary(batch_id: str) -> dict[str, Any]:
    bid = _uuid(batch_id)
    with connection() as conn:
        run = matching.latest_run(conn, bid)
    if run is None:
        raise HTTPException(status_code=404, detail="no matching run for this batch")
    return _run_view(run) or {}


# --------------------------------------------------------------------------- internal: notifications


@app.post("/internal/notifications/{batch_id}/prepare", dependencies=[Internal])
def internal_prepare_notifications(batch_id: str, settings: SettingsDep) -> dict[str, Any]:
    bid = _uuid(batch_id)
    try:
        with connection() as conn:
            return notifications.prepare_outbox(settings, conn, bid)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except notifications.NotificationConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class NotificationResult(BaseModel):
    status: str
    provider_message_id: str | None = Field(default=None, max_length=255)
    error: str | None = Field(default=None, max_length=2000)


@app.post("/internal/notifications/results/{notification_id}", dependencies=[Internal])
def internal_notification_result(
    notification_id: int, body: NotificationResult, settings: SettingsDep
) -> dict[str, Any]:
    try:
        with connection() as conn:
            return notifications.record_result(
                settings, conn, notification_id, body.status, body.provider_message_id, body.error
            )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/internal/notifications/{batch_id}/finalize", dependencies=[Internal])
def internal_finalize_notifications(batch_id: str) -> dict[str, Any]:
    bid = _uuid(batch_id)
    with connection() as conn:
        return notifications.finalize(conn, bid)


# --------------------------------------------------------------------------- internal: product discovery


@app.get("/internal/products/sources", dependencies=[Internal])
def internal_sources() -> dict[str, Any]:
    config = products.load_sources()
    for s in config["sources"]:
        s["robots_url"] = s["url"].split("/", 3)[0] + "//" + s["url"].split("/", 3)[2] + "/robots.txt"
    return config


class RobotsCheckRequest(BaseModel):
    source_key: str
    status_code: int | None = None
    robots_txt: str | None = Field(default=None, max_length=500_000)


@app.post("/internal/products/robots-check", dependencies=[Internal])
def internal_robots_check(body: RobotsCheckRequest) -> dict[str, Any]:
    source = products.get_source(body.source_key)
    if source is None:
        raise HTTPException(status_code=404, detail="unknown source")
    user_agent = products.load_sources()["user_agent"]
    if body.status_code is not None and 400 <= body.status_code < 500 and body.status_code not in (401, 403):
        # RFC 9309: an unavailable (4xx) robots.txt means no restrictions.
        return {"source_key": body.source_key, "allowed": True, "reason": f"robots.txt HTTP {body.status_code}"}
    if body.status_code is None or body.status_code >= 500 or body.status_code in (401, 403) or body.robots_txt is None:
        # Unreachable or forbidden robots.txt: be conservative and do not crawl.
        return {
            "source_key": body.source_key,
            "allowed": False,
            "reason": f"robots.txt unavailable ({body.status_code})",
        }
    allowed = product_extract.robots_allows(body.robots_txt, user_agent, source["url"])
    criteria_url = source.get("criteria_url")
    if allowed and criteria_url:
        # Both pages make one product: if the criteria page may not be crawled, crawl neither.
        reason = None
        if _host(criteria_url) != _host(source["url"]):
            reason = "criteria_url is on a different host; its robots.txt was not checked"
        elif not product_extract.robots_allows(body.robots_txt, user_agent, criteria_url):
            reason = "criteria page disallowed by robots.txt"
        if reason:
            with connection() as conn:
                products.record_crawl_failure(conn, source, reason)
            return {"source_key": body.source_key, "allowed": False, "reason": reason}
    return {"source_key": body.source_key, "allowed": allowed, "reason": "robots.txt evaluated"}


def _host(url: str) -> str:
    return url.split("/", 3)[2].lower()


class IngestPageRequest(BaseModel):
    source_key: str
    status_code: int | None = None
    html: str | None = Field(default=None, max_length=5_000_000)
    error: str | None = Field(default=None, max_length=500)
    # Only read for sources that configure a criteria_url (e.g. a separate eligibility page).
    criteria_status_code: int | None = None
    criteria_html: str | None = Field(default=None, max_length=5_000_000)
    criteria_error: str | None = Field(default=None, max_length=500)


@app.post("/internal/products/ingest", dependencies=[Internal])
def internal_ingest_page(body: IngestPageRequest) -> dict[str, Any]:
    source = products.get_source(body.source_key)
    if source is None:
        raise HTTPException(status_code=404, detail="unknown source")
    failure = None
    if body.error:
        failure = body.error
    elif body.status_code is None or not 200 <= body.status_code < 300:
        failure = f"HTTP {body.status_code}"
    elif not body.html:
        failure = "empty response body"
    html = body.html or ""
    if failure is None and source.get("criteria_url"):
        # Never upsert one page alone: the upsert would overwrite the other page's fields with NULL.
        if body.criteria_error:
            failure = f"criteria page: {body.criteria_error}"
        elif body.criteria_status_code is None or not 200 <= body.criteria_status_code < 300:
            failure = f"criteria page: HTTP {body.criteria_status_code}"
        elif not body.criteria_html:
            failure = "criteria page: empty response body"
        else:
            html = html + "\n" + body.criteria_html

    extracted = None
    if failure is None:
        extracted = product_extract.extract_product(html)
        products.validate_extracted(extracted)
        if extracted.extracted_field_count() < products.MIN_FIELDS_FOR_VALID_CRAWL:
            failure = f"only {extracted.extracted_field_count()} field(s) extracted; page layout may have changed"

    with connection() as conn:
        if failure is not None:
            products.record_crawl_failure(conn, source, failure)
            log.warning("product crawl failed", extra={"source_key": body.source_key, "reason": failure})
            return {"source_key": body.source_key, "stored": False, "reason": failure}
        assert extracted is not None
        row = products.upsert_crawled_product(conn, source, extracted)
    return {
        "source_key": body.source_key,
        "stored": True,
        "product_id": row["id"],
        "created": row["inserted"],
        "extracted": {
            "interest_rate_min": extracted.interest_rate_min,
            "interest_rate_max": extracted.interest_rate_max,
            "min_monthly_income": extracted.min_monthly_income,
            "min_credit_score": extracted.min_credit_score,
            "min_age": extracted.min_age,
            "max_age": extracted.max_age,
            "employment_types": extracted.employment_types,
            "employment_criteria_text": extracted.employment_criteria_text,
            "other_criteria": extracted.other_criteria,
        },
        "notes": extracted.notes,
    }


@app.post("/internal/products/refresh-staleness", dependencies=[Internal])
def internal_refresh_staleness() -> dict[str, int]:
    with connection() as conn:
        return products.refresh_staleness(conn)


@app.get("/internal/products", dependencies=[Internal])
def internal_list_products() -> list[dict[str, Any]]:
    with connection() as conn:
        return conn.execute(
            "SELECT id, lender_name, product_name, data_origin, source_url, interest_rate_min, interest_rate_max,"
            " min_monthly_income, min_credit_score, max_credit_score, min_age, max_age, employment_types,"
            " employment_types_source, other_criteria, last_verified_at, is_active, is_stale"
            " FROM loan_products ORDER BY id"
        ).fetchall()
