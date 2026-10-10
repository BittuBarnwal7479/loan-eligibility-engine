import uuid

import pytest

from app.config import get_settings
from app.matching import service as matching
from app.notifications import service as notifications
from tests.helpers import HEADER, add_product, import_csv

pytestmark = pytest.mark.db

CSV = HEADER + (
    "A,alice.real@example.com,150000,800,salaried,40\n"
    "B,bob.real@example.com,30000,710,salaried,30\n"
    "C,carol.real@example.com,1000,600,student,19\n"
)


def _setup(db, settings):
    add_product(
        db,
        product_name="one",
        min_monthly_income=25000,
        min_credit_score=700,
        min_age=21,
        max_age=60,
        employment_types=["salaried"],
    )
    add_product(
        db,
        product_name="two",
        min_monthly_income=100000,
        data_origin="dev_fixture",
        other_criteria=["Work experience: 2 years (as published)"],
    )
    batch_id = uuid.UUID(import_csv(settings, CSV))
    matching.run_matching(settings, batch_id)
    return batch_id


def _settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    return get_settings()


def test_dry_run_groups_one_message_per_user_and_never_uses_csv_addresses(db, settings, monkeypatch):
    batch_id = _setup(db, settings)
    s = _settings(monkeypatch, NOTIFICATION_TEST_RECIPIENT="tester@inbox.test")
    out = notifications.prepare_outbox(s, db, batch_id)
    assert out["delivery_mode"] == "dry_run" and out["users_with_matches"] == 2
    assert len(out["messages"]) == 2
    for message in out["messages"]:
        assert message["to"] == "tester@inbox.test"
        assert "real@example.com" not in message["html"]  # only the masked form appears
        assert "not loan offers" in message["html"] and "not loan offers" in message["text"]
    alice = next(m for m in out["messages"] if "user A " in m["text"])
    assert alice["subject"].startswith("2 potential loan matches")
    assert "a***@example.com" in alice["html"] and "TEST FIXTURE" in alice["html"]
    assert "Work experience" in alice["html"]
    logs = db.execute("SELECT recipient_email FROM notification_logs").fetchall()
    assert {r["recipient_email"] for r in logs} == {"tester@inbox.test"}


def test_duplicate_prepare_and_results_do_not_resend(db, settings):
    batch_id = _setup(db, settings)
    first = notifications.prepare_outbox(settings, db, batch_id)
    assert {m["to"] for m in first["messages"]} == {notifications.DRY_RUN_RECIPIENT}
    # A retry while the first claim is still fresh returns nothing new.
    assert notifications.prepare_outbox(settings, db, batch_id)["messages"] == []
    for m in first["messages"]:
        assert (
            notifications.record_result(settings, db, m["notification_id"], "DRY_RUN", None, None)["status"]
            == "DRY_RUN"
        )
        assert notifications.record_result(settings, db, m["notification_id"], "DRY_RUN", None, None)["duplicate"]
    db.execute("UPDATE notification_logs SET claimed_at = now() - interval '1 hour'")
    again = notifications.prepare_outbox(settings, db, batch_id)
    assert again["messages"] == [] and again["new_notifications"] == 0
    # An empty retry settles the batch status itself (n8n has no items to finalize with).
    status = db.execute("SELECT notification_status FROM upload_batches WHERE id = %s", (batch_id,)).fetchone()
    assert status["notification_status"] == "COMPLETED"
    assert notifications.finalize(db, batch_id)["notification_status"] == "COMPLETED"
    assert db.execute("SELECT count(*) AS n FROM notification_logs").fetchone()["n"] == 2


def test_failed_delivery_is_retried_then_gives_up(db, settings, monkeypatch):
    batch_id = _setup(db, settings)
    s = _settings(
        monkeypatch,
        NOTIFICATION_MODE="smtp",
        NOTIFICATION_TEST_RECIPIENT="tester@inbox.test",
        NOTIFICATION_MAX_ATTEMPTS="2",
    )
    msgs = notifications.prepare_outbox(s, db, batch_id)["messages"]
    target = msgs[0]["notification_id"]
    for m in msgs:
        failed = m["notification_id"] == target
        notifications.record_result(
            s,
            db,
            m["notification_id"],
            "FAILED" if failed else "SENT",
            None if failed else "msg-1",
            "smtp timeout" if failed else None,
        )
    assert notifications.finalize(db, batch_id)["notification_status"] == "IN_PROGRESS"

    retry = notifications.prepare_outbox(s, db, batch_id)["messages"]
    assert [m["notification_id"] for m in retry] == [target] and retry[0]["attempt"] == 2
    assert notifications.record_result(s, db, target, "FAILED", None, "smtp timeout")["status"] == "GAVE_UP"
    assert notifications.prepare_outbox(s, db, batch_id)["messages"] == []
    assert notifications.finalize(db, batch_id)["notification_status"] == "COMPLETED_WITH_ERRORS"


def test_real_delivery_requires_test_recipient(db, settings, monkeypatch):
    batch_id = _setup(db, settings)
    s = _settings(monkeypatch, NOTIFICATION_MODE="ses")
    with pytest.raises(notifications.NotificationConfigError):
        notifications.prepare_outbox(s, db, batch_id)


def test_status_must_match_delivery_mode(db, settings):
    batch_id = _setup(db, settings)
    msg = notifications.prepare_outbox(settings, db, batch_id)["messages"][0]
    with pytest.raises(ValueError):
        notifications.record_result(settings, db, msg["notification_id"], "SENT", "x", None)


def test_batch_without_matches_completes_notifications(db, settings):
    add_product(db, product_name="none", min_monthly_income=10**7)
    batch_id = uuid.UUID(import_csv(settings, CSV))
    matching.run_matching(settings, batch_id)
    out = notifications.prepare_outbox(settings, db, batch_id)
    assert out["users_with_matches"] == 0 and out["messages"] == []
    status = db.execute("SELECT notification_status FROM upload_batches WHERE id = %s", (batch_id,)).fetchone()
    assert status["notification_status"] == "COMPLETED"


def test_notifications_wait_for_completed_matching(db, settings):
    batch_id = uuid.UUID(import_csv(settings, CSV))
    with pytest.raises(notifications.NotificationConfigError):
        notifications.prepare_outbox(settings, db, batch_id)


def test_changed_match_set_supersedes_unsent_notification(db, settings, monkeypatch):
    batch_id = _setup(db, settings)
    s = _settings(monkeypatch, NOTIFICATION_MODE="smtp", NOTIFICATION_TEST_RECIPIENT="tester@inbox.test")
    first = notifications.prepare_outbox(s, db, batch_id)["messages"]
    for m in first:
        notifications.record_result(s, db, m["notification_id"], "FAILED", None, "smtp down")
    # Product criteria change and matching is re-run before the retry: user B no longer matches,
    # user A's match set changes, so the old unsent rows must not be sent or block completion.
    db.execute("UPDATE loan_products SET min_monthly_income = 200000 WHERE product_name = 'one'")
    matching.run_matching(s, batch_id)
    retry = notifications.prepare_outbox(s, db, batch_id)["messages"]
    assert len(retry) == 1, [m["subject"] for m in retry]  # only A's new grouped message
    for m in retry:
        notifications.record_result(s, db, m["notification_id"], "SENT", "id-1", None)
    assert notifications.finalize(db, batch_id)["notification_status"] == "COMPLETED"
    statuses = sorted(r["status"] for r in db.execute("SELECT status FROM notification_logs").fetchall())
    assert statuses == ["SENT", "SUPERSEDED", "SUPERSEDED"]
