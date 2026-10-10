import pytest

import handlers.process_csv as lambda_module
from app.ingestion.service import ProcessOutcome, TransientProcessingError


def _event(*keys):
    return {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": k}}} for k in keys]}


def test_keys_are_url_decoded_and_each_record_processed(monkeypatch):
    seen = []
    monkeypatch.setattr(
        lambda_module,
        "process_object",
        lambda s, b, k: seen.append((b, k)) or ProcessOutcome("x", "processed", "COMPLETED"),
    )
    result = lambda_module.handler(_event("uploads/a+b%2Fc.csv", "uploads/x.csv"), None)
    assert seen == [("b", "uploads/a b/c.csv"), ("b", "uploads/x.csv")]
    assert len(result["outcomes"]) == 2


def test_transient_error_reraised_for_lambda_retry(monkeypatch):
    def flaky(settings, bucket, key):
        if key == "bad":
            raise TransientProcessingError("retry me")
        return ProcessOutcome("x", "processed", "COMPLETED")

    monkeypatch.setattr(lambda_module, "process_object", flaky)
    with pytest.raises(TransientProcessingError):
        lambda_module.handler(_event("good", "bad"), None)
