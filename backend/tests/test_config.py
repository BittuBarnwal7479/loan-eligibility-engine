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


@pytest.mark.parametrize(("raw", "expected"), [(None, 200), ("1", 1), ("200", 200)])
def test_notification_max_per_run_default_and_bounds(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("NOTIFICATION_MAX_PER_RUN", raising=False)
    else:
        monkeypatch.setenv("NOTIFICATION_MAX_PER_RUN", raw)
    get_settings.cache_clear()
    try:
        assert get_settings().notification_max_per_run == expected
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("raw", ["0", "-1", "201"])
def test_notification_max_per_run_out_of_range_is_rejected(monkeypatch, raw):
    monkeypatch.setenv("NOTIFICATION_MAX_PER_RUN", raw)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="NOTIFICATION_MAX_PER_RUN"):
            get_settings()
    finally:
        get_settings.cache_clear()
