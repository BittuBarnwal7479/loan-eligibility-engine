import pytest

from app.config import get_settings


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("INTERNAL_API_TOKEN", "change-me"),
        ("N8N_WEBHOOK_TOKEN", "change-me"),
        ("UPLOAD_SIGNING_SECRET", "change-me"),
        ("DATABASE_URL", "postgresql://loan_engine:change-me@postgres:5432/loan_engine"),
    ],
)
def test_placeholder_secrets_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="placeholder"):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_internal_endpoints_fail_closed_without_token(monkeypatch):
    from app.security import constant_time_equals

    monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().internal_api_token is None
        assert not constant_time_equals("anything", get_settings().internal_api_token)
    finally:
        get_settings.cache_clear()
