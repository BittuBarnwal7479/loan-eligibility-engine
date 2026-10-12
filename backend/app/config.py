"""Runtime configuration loaded from environment variables.

The same settings object is used by the FastAPI app (local / API Gateway via
Mangum) and by the S3-triggered Lambda, so business logic stays portable.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw is not None else default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _database_url() -> str:
    url = _env("DATABASE_URL")
    if url:
        return url
    # On AWS the RDS-managed secret (JSON with username/password) is injected by ARN.
    secret_arn = _env("DB_SECRET_ARN")
    if secret_arn:
        import boto3  # imported lazily: not needed for local runs

        secret = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
        creds = json.loads(secret["SecretString"])
        host = _env("DB_HOST", creds.get("host"))
        port = _env("DB_PORT", str(creds.get("port", 5432)))
        name = _env("DB_NAME", creds.get("dbname", "loan_engine"))
        from urllib.parse import quote

        return (
            f"postgresql://{quote(creds['username'])}:{quote(creds['password'])}"
            f"@{host}:{port}/{name}?sslmode={_env('DB_SSLMODE', 'require')}"
        )
    raise RuntimeError("DATABASE_URL (or DB_SECRET_ARN on AWS) must be set")


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    # "local" stores uploads on disk and simulates the S3 event; "s3" uses presigned POST.
    storage_mode: str
    local_upload_dir: str
    s3_bucket: str | None
    aws_region: str
    upload_url_ttl_seconds: int
    max_upload_bytes: int
    max_csv_rows: int
    insert_chunk_size: int
    max_reported_row_errors: int
    processing_lease_seconds: int
    public_api_base_url: str
    internal_api_token: str | None
    upload_signing_secret: str | None
    n8n_matching_webhook_url: str | None
    n8n_webhook_token: str | None
    matching_chunk_size: int
    notification_mode: str
    notification_test_recipient: str | None
    notification_from_email: str | None
    notification_max_attempts: int
    notification_max_per_run: int
    llm_enabled: bool
    llm_model: str
    llm_max_calls_per_run: int
    cors_origins: list[str] = field(default_factory=list)


MAX_NOTIFICATIONS_PER_RUN = 200  # SES sandbox daily quota; also the per-run cap's default
PLACEHOLDER_SECRET = "change-me"  # noqa: S105 - the placeholder we reject, not a secret


def _reject_placeholders(database_url: str) -> None:
    """Refuse to run with the unedited values from .env.example."""
    names = ("INTERNAL_API_TOKEN", "N8N_WEBHOOK_TOKEN", "UPLOAD_SIGNING_SECRET")
    bad = [n for n in names if _env(n) == PLACEHOLDER_SECRET]
    if f":{PLACEHOLDER_SECRET}@" in database_url:
        bad.append("POSTGRES_PASSWORD")
    if bad:
        raise RuntimeError(f"replace the placeholder value of {', '.join(bad)} in .env before starting")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    storage_mode = (_env("STORAGE_MODE", "local") or "local").lower()
    if storage_mode not in {"local", "s3"}:
        raise RuntimeError("STORAGE_MODE must be 'local' or 's3'")
    notification_mode = (_env("NOTIFICATION_MODE", "dry_run") or "dry_run").lower()
    if notification_mode not in {"dry_run", "smtp", "ses"}:
        raise RuntimeError("NOTIFICATION_MODE must be dry_run, smtp or ses")
    notification_max_per_run = _env_int("NOTIFICATION_MAX_PER_RUN", MAX_NOTIFICATIONS_PER_RUN)
    if not 1 <= notification_max_per_run <= MAX_NOTIFICATIONS_PER_RUN:
        raise RuntimeError(f"NOTIFICATION_MAX_PER_RUN must be between 1 and {MAX_NOTIFICATIONS_PER_RUN}")
    database_url = _database_url()
    _reject_placeholders(database_url)
    return Settings(
        app_env=_env("APP_ENV", "local") or "local",
        database_url=database_url,
        storage_mode=storage_mode,
        local_upload_dir=_env("LOCAL_UPLOAD_DIR", "/data/uploads") or "/data/uploads",
        s3_bucket=_env("UPLOAD_BUCKET"),
        aws_region=_env("AWS_REGION", "ap-southeast-2") or "ap-southeast-2",
        upload_url_ttl_seconds=_env_int("UPLOAD_URL_TTL_SECONDS", 300),
        max_upload_bytes=_env_int("MAX_UPLOAD_BYTES", 10 * 1024 * 1024),
        max_csv_rows=_env_int("MAX_CSV_ROWS", 200_000),
        insert_chunk_size=_env_int("INSERT_CHUNK_SIZE", 500),
        max_reported_row_errors=_env_int("MAX_REPORTED_ROW_ERRORS", 100),
        processing_lease_seconds=_env_int("PROCESSING_LEASE_SECONDS", 900),
        public_api_base_url=(_env("PUBLIC_API_BASE_URL", "http://localhost:8081") or "").rstrip("/"),
        internal_api_token=_env("INTERNAL_API_TOKEN"),
        upload_signing_secret=_env("UPLOAD_SIGNING_SECRET"),
        n8n_matching_webhook_url=_env("N8N_MATCHING_WEBHOOK_URL"),
        n8n_webhook_token=_env("N8N_WEBHOOK_TOKEN"),
        matching_chunk_size=_env_int("MATCHING_CHUNK_SIZE", 1000),
        notification_mode=notification_mode,
        notification_test_recipient=_env("NOTIFICATION_TEST_RECIPIENT"),
        notification_from_email=_env("NOTIFICATION_FROM_EMAIL"),
        notification_max_attempts=_env_int("NOTIFICATION_MAX_ATTEMPTS", 3),
        notification_max_per_run=notification_max_per_run,
        llm_enabled=_env_bool("LLM_ENABLED", False),
        llm_model=_env("LLM_MODEL", "claude-opus-5-5") or "claude-opus-5-5",
        llm_max_calls_per_run=_env_int("LLM_MAX_CALLS_PER_RUN", 10),
        cors_origins=[o.strip() for o in (_env("CORS_ORIGINS", "http://localhost:5173") or "").split(",") if o.strip()],
    )
