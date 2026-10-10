"""AWS code paths exercised against moto (MOCKED S3 - no real AWS calls or credentials).

These verify our use of the S3 API, not real cloud behaviour (bucket policies,
event delivery, IAM). See docs/TESTING.md for what still needs a real deployment test.
"""

import uuid

import boto3
import pytest
from moto import mock_aws

from app.config import get_settings
from app.storage import batch_id_from_key, create_upload_target, object_key, read_object

BUCKET = "test-upload-bucket"


@pytest.fixture()
def s3_settings(monkeypatch):
    monkeypatch.setenv("STORAGE_MODE", "s3")
    monkeypatch.setenv("UPLOAD_BUCKET", BUCKET)
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    get_settings.cache_clear()
    with mock_aws():
        boto3.client("s3", region_name="ap-south-1").create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-south-1"}
        )
        yield get_settings()
    get_settings.cache_clear()


def test_presigned_post_enforces_key_and_size(s3_settings):
    batch_id = uuid.uuid4()
    target = create_upload_target(s3_settings, batch_id)
    assert target.storage_mode == "s3" and target.method == "POST"
    assert target.fields["key"] == f"uploads/{batch_id}/source.csv"
    assert "policy" in target.fields and target.fields["x-amz-server-side-encryption"] == "AES256"
    import base64
    import json

    policy = json.loads(base64.b64decode(target.fields["policy"]))
    assert ["content-length-range", 1, s3_settings.max_upload_bytes] in policy["conditions"]


def test_read_object_streams_and_enforces_size(s3_settings, monkeypatch):
    key = object_key(uuid.uuid4())
    boto3.client("s3", region_name="ap-south-1").put_object(Bucket=BUCKET, Key=key, Body=b"a,b\n1,2\n")
    assert b"".join(read_object(s3_settings, BUCKET, key)) == b"a,b\n1,2\n"
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "3")
    get_settings.cache_clear()
    from app.storage import UploadTooLarge

    with pytest.raises(UploadTooLarge):
        list(read_object(get_settings(), BUCKET, key))


def test_key_parsing_rejects_unexpected_keys():
    bid = uuid.uuid4()
    assert batch_id_from_key(object_key(bid)) == bid
    assert batch_id_from_key(f"uploads/{bid}/../../etc/passwd") is None
    assert batch_id_from_key("other/key.csv") is None


@pytest.mark.db
def test_s3_object_processed_end_to_end_with_mocked_s3(db, s3_settings):
    from app.db import connection
    from app.ingestion import service as ingestion
    from handlers.process_csv import handler

    with connection() as conn:
        batch = ingestion.create_batch(conn, "users.csv", BUCKET)
    body = b"user_id,email,monthly_income,credit_score,employment_status,age\nU1,a@example.com,1000,700,salaried,30\n"
    boto3.client("s3", region_name="ap-south-1").put_object(Bucket=BUCKET, Key=batch["storage_key"], Body=body)
    event = {"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": batch["storage_key"]}}}]}
    first = handler(event, None)
    duplicate = handler(event, None)  # S3 at-least-once delivery
    assert first["outcomes"][0]["status"] == "COMPLETED"
    assert duplicate["outcomes"][0]["action"] == "skipped_duplicate"
