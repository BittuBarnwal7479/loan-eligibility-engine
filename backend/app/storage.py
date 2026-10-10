"""Upload storage abstraction.

* ``s3`` mode: the browser POSTs the file straight to S3 using a presigned POST
  policy (which enforces a byte-size limit and a fixed key). S3 then emits an
  ObjectCreated event that invokes the CSV-processing Lambda.
* ``local`` mode: the backend issues an HMAC-signed, short-lived upload URL that
  mimics the presigned POST shape. After the file is written to disk the API
  dispatches the same ``process_object`` call an S3 event would.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.security import sign_upload

_KEY_RE = re.compile(r"^uploads/(?P<batch>[0-9a-f-]{36})/source\.csv$")
LOCAL_BUCKET = "local"


class UploadTooLarge(ValueError):
    pass


@dataclass(frozen=True)
class UploadTarget:
    method: str
    url: str
    fields: dict[str, str]
    expires_at: int
    storage_mode: str


def object_key(batch_id: uuid.UUID | str) -> str:
    return f"uploads/{batch_id}/source.csv"


def batch_id_from_key(key: str) -> uuid.UUID | None:
    match = _KEY_RE.match(key)
    return uuid.UUID(match.group("batch")) if match else None


def create_upload_target(settings: Settings, batch_id: uuid.UUID) -> UploadTarget:
    expires_at = int(time.time()) + settings.upload_url_ttl_seconds
    key = object_key(batch_id)
    if settings.storage_mode == "s3":
        import boto3

        if not settings.s3_bucket:
            raise RuntimeError("UPLOAD_BUCKET is not configured")
        client = boto3.client("s3", region_name=settings.aws_region)
        post = client.generate_presigned_post(
            Bucket=settings.s3_bucket,
            Key=key,
            Fields={"x-amz-server-side-encryption": "AES256"},
            Conditions=[
                ["content-length-range", 1, settings.max_upload_bytes],
                {"x-amz-server-side-encryption": "AES256"},
            ],
            ExpiresIn=settings.upload_url_ttl_seconds,
        )
        return UploadTarget("POST", post["url"], dict(post["fields"]), expires_at, "s3")

    if not settings.upload_signing_secret:
        raise RuntimeError("UPLOAD_SIGNING_SECRET is not configured")
    signature = sign_upload(settings.upload_signing_secret, str(batch_id), expires_at)
    url = f"{settings.public_api_base_url}/local-uploads/{batch_id}?expires={expires_at}&signature={signature}"
    return UploadTarget("POST", url, {}, expires_at, "local")


def local_path(settings: Settings, key: str) -> Path:
    if batch_id_from_key(key) is None:
        raise ValueError("unexpected object key")
    return Path(settings.local_upload_dir) / key


def write_local_object(settings: Settings, key: str, chunks: Iterator[bytes]) -> int:
    """Write an upload once. Raises FileExistsError if the key already has (or is receiving) a file,
    which makes local upload URLs single-use like a fixed S3 key per batch."""
    path = local_path(settings, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError("an object already exists for this upload")
    size = 0
    tmp = path.with_suffix(".part")
    with tmp.open("xb") as handle:  # exclusive: a concurrent upload to the same key fails here
        for chunk in chunks:
            size += len(chunk)
            if size > settings.max_upload_bytes:
                handle.close()
                tmp.unlink(missing_ok=True)
                raise UploadTooLarge(f"file exceeds {settings.max_upload_bytes} bytes")
            handle.write(chunk)
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise ValueError("file is empty")
    tmp.replace(path)
    return size


def read_object(settings: Settings, bucket: str, key: str) -> Iterator[bytes]:
    """Stream an uploaded object, enforcing the size limit while reading."""
    if bucket == LOCAL_BUCKET:
        path = local_path(settings, key)
        if path.stat().st_size > settings.max_upload_bytes:
            raise UploadTooLarge("object exceeds size limit")
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                yield chunk
        return

    import boto3

    client = boto3.client("s3", region_name=settings.aws_region)
    obj = client.get_object(Bucket=bucket, Key=key)
    if int(obj.get("ContentLength", 0)) > settings.max_upload_bytes:
        raise UploadTooLarge("object exceeds size limit")
    yield from obj["Body"].iter_chunks(64 * 1024)
