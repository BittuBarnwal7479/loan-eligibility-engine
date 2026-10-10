import pytest

from app.ingestion import service as ingestion
from app.storage import LOCAL_BUCKET, local_path
from tests.helpers import HEADER, import_csv, upload_csv

pytestmark = pytest.mark.db

VALID = HEADER + "U1,a@example.com,50000,720,salaried,30\nU2,b@example.com,20000,650,self-employed,45\n"


def test_valid_csv_import(db, settings):
    batch_id = import_csv(settings, VALID)
    batch = db.execute("SELECT * FROM upload_batches WHERE id = %s", (batch_id,)).fetchone()
    assert batch["status"] == "COMPLETED"
    assert (batch["total_rows"], batch["processed_rows"], batch["inserted_rows"], batch["failed_rows"]) == (2, 2, 2, 0)
    assert batch["content_sha256"] and batch["attempt_count"] == 1
    users = db.execute("SELECT user_id, employment_status, upload_batch_id FROM users ORDER BY user_id").fetchall()
    assert [(u["user_id"], u["employment_status"]) for u in users] == [("U1", "salaried"), ("U2", "self_employed")]
    assert all(str(u["upload_batch_id"]) == batch_id for u in users)


def test_partial_errors_are_recorded_and_valid_rows_committed(db, settings):
    batch_id = import_csv(settings, VALID + "U3,bad-email,1,700,salaried,30\n")
    batch = db.execute("SELECT * FROM upload_batches WHERE id = %s", (batch_id,)).fetchone()
    assert batch["status"] == "COMPLETED_WITH_ERRORS"
    assert (batch["processed_rows"], batch["failed_rows"]) == (2, 1)
    assert batch["row_errors"][0]["field"] == "email"
    assert "bad-email" not in str(batch["row_errors"])


def test_invalid_header_fails_batch_without_inserting(db, settings):
    batch_id, key = upload_csv(settings, "id,mail\n1,a@example.com\n")
    outcome = ingestion.process_object(settings, LOCAL_BUCKET, key)
    assert outcome.status == "FAILED"
    batch = db.execute("SELECT status, error_message FROM upload_batches WHERE id = %s", (batch_id,)).fetchone()
    assert batch["status"] == "FAILED" and "missing required column" in batch["error_message"]
    assert db.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 0


def test_all_rows_invalid_fails_batch(db, settings):
    batch_id, key = upload_csv(settings, HEADER + "U1,bad,1,1,salaried,1\n")
    assert ingestion.process_object(settings, LOCAL_BUCKET, key).status == "FAILED"
    batch = db.execute("SELECT failed_rows, row_errors FROM upload_batches WHERE id = %s", (batch_id,)).fetchone()
    assert batch["failed_rows"] == 1 and batch["row_errors"]


def test_duplicate_s3_event_is_ignored(db, settings):
    batch_id, key = upload_csv(settings, VALID)
    first = ingestion.process_object(settings, LOCAL_BUCKET, key)
    second = ingestion.process_object(settings, LOCAL_BUCKET, key)
    assert first.action == "processed" and second.action == "skipped_duplicate"
    batch = db.execute("SELECT attempt_count, inserted_rows FROM upload_batches WHERE id = %s", (batch_id,)).fetchone()
    assert batch["attempt_count"] == 1 and batch["inserted_rows"] == 2


def test_unknown_or_foreign_keys_are_ignored(db, settings):
    assert ingestion.process_object(settings, LOCAL_BUCKET, "something/else.csv").action == "unknown_key"
    assert (
        ingestion.process_object(
            settings, LOCAL_BUCKET, "uploads/00000000-0000-0000-0000-000000000000/source.csv"
        ).action
        == "unknown_key"
    )


def test_reupload_updates_users_and_reassigns_batch(db, settings):
    first = import_csv(settings, VALID)
    second = import_csv(
        settings, HEADER + "U1,a@example.com,99000,800,salaried,31\nU9,z@example.com,1000,600,student,19\n"
    )
    b2 = db.execute("SELECT inserted_rows, updated_rows FROM upload_batches WHERE id = %s", (second,)).fetchone()
    assert (b2["inserted_rows"], b2["updated_rows"]) == (1, 1)
    u1 = db.execute("SELECT monthly_income, upload_batch_id FROM users WHERE user_id = 'U1'").fetchone()
    assert u1["monthly_income"] == 99000 and str(u1["upload_batch_id"]) == second
    u2 = db.execute("SELECT upload_batch_id FROM users WHERE user_id = 'U2'").fetchone()
    assert str(u2["upload_batch_id"]) == first
    assert db.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 3


def test_transient_error_returns_batch_to_pending_then_retry_succeeds(db, settings, monkeypatch):
    batch_id, key = upload_csv(settings, VALID)
    real = ingestion.upsert_users

    def boom(*args, **kwargs):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(ingestion, "upsert_users", boom)
    with pytest.raises(ingestion.TransientProcessingError):
        ingestion.process_object(settings, LOCAL_BUCKET, key)
    batch = db.execute("SELECT status, error_message FROM upload_batches WHERE id = %s", (batch_id,)).fetchone()
    assert batch["status"] == "PENDING" and "RuntimeError" in batch["error_message"]
    assert db.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 0  # nothing partially committed

    monkeypatch.setattr(ingestion, "upsert_users", real)
    assert ingestion.process_object(settings, LOCAL_BUCKET, key).status == "COMPLETED"


def test_failure_on_last_attempt_marks_failed(db, settings, monkeypatch):
    batch_id, key = upload_csv(settings, VALID)
    db.execute(
        "UPDATE upload_batches SET attempt_count = %s WHERE id = %s", (ingestion.MAX_PROCESSING_ATTEMPTS - 1, batch_id)
    )
    monkeypatch.setattr(ingestion, "upsert_users", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert ingestion.process_object(settings, LOCAL_BUCKET, key).status == "FAILED"


def test_stale_processing_lease_can_be_reclaimed(db, settings):
    batch_id, key = upload_csv(settings, VALID)
    db.execute(
        "UPDATE upload_batches SET status = 'PROCESSING', processing_started_at = now() WHERE id = %s", (batch_id,)
    )
    assert ingestion.process_object(settings, LOCAL_BUCKET, key).action == "skipped_duplicate"
    db.execute("UPDATE upload_batches SET processing_started_at = now() - interval '1 day' WHERE id = %s", (batch_id,))
    assert ingestion.process_object(settings, LOCAL_BUCKET, key).status == "COMPLETED"


def test_oversized_object_fails(db, settings, monkeypatch):
    batch_id, key = upload_csv(settings, VALID)
    path = local_path(settings, key)
    path.write_bytes(path.read_bytes() + b"U3,c@example.com,1,700,salaried,30\n" * 400_000)
    assert ingestion.process_object(settings, LOCAL_BUCKET, key).status == "FAILED"


def test_sql_metacharacters_are_stored_literally(db, settings):
    nasty = "x');DROP/**/TABLE/**/users;--@example.com"
    import_csv(settings, HEADER + f'U1,"{nasty}",1000,700,salaried,30\n')
    assert db.execute("SELECT email FROM users WHERE user_id = 'U1'").fetchone()["email"] == nasty.lower()
    assert db.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 1


def test_bounded_chunked_insert_handles_many_rows(db, settings):
    rows = "".join(f"U{i},u{i}@example.com,{1000 + i},700,salaried,30\n" for i in range(23))  # chunk size 5
    batch_id = import_csv(settings, HEADER + rows)
    assert db.execute("SELECT count(*) AS n FROM users WHERE upload_batch_id = %s", (batch_id,)).fetchone()["n"] == 23
