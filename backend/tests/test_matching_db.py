import uuid

import pytest

from app.matching import service as matching
from tests.helpers import HEADER, add_product, import_csv

pytestmark = pytest.mark.db

USERS = HEADER + (
    "A,a@example.com,30000,710,salaried,30\n"  # salaried mid
    "B,b@example.com,150000,800,salaried,40\n"  # high income, high score
    "C,c@example.com,10000,600,student,19\n"  # matches nothing with known thresholds
    "D,d@example.com,60000,740,self_employed,35\n"  # self-employed
    "E,e@example.com,25000,690,retired,66\n"  # retired, older
)


def _products(db):
    return {
        "salaried": add_product(
            db,
            product_name="salaried",
            min_monthly_income=25000,
            min_credit_score=700,
            min_age=21,
            max_age=60,
            employment_types=["salaried"],
        ),
        "premium": add_product(
            db,
            product_name="premium",
            min_monthly_income=100000,
            min_credit_score=780,
            min_age=25,
            max_age=58,
            employment_types=["salaried", "self_employed"],
        ),
        "self": add_product(
            db,
            product_name="self",
            min_monthly_income=40000,
            min_credit_score=720,
            min_age=25,
            max_age=65,
            employment_types=["self_employed"],
        ),
        "partial": add_product(db, product_name="partial", min_monthly_income=20000, min_age=21),
        "inactive": add_product(db, product_name="inactive", is_active=False),
    }


def _matches(db, batch_id):
    rows = db.execute(
        "SELECT u.user_id, p.product_name, m.match_status, m.criteria_coverage, m.unknown_criteria"
        " FROM matches m JOIN users u ON u.id = m.user_id JOIN loan_products p ON p.id = m.loan_product_id"
        " WHERE m.upload_batch_id = %s ORDER BY u.user_id, p.product_name",
        (batch_id,),
    ).fetchall()
    return {(r["user_id"], r["product_name"].removeprefix("Product ")): r for r in rows}


def test_pipeline_matches_and_stats(db, settings):
    _products(db)
    batch_id = import_csv(settings, USERS)
    stats = matching.run_matching(settings, uuid.UUID(batch_id))
    found = _matches(db, batch_id)
    assert set(found) == {
        ("A", "salaried"),
        ("A", "partial"),
        ("B", "salaried"),
        ("B", "premium"),
        ("B", "partial"),  # multiple matches for one user
        ("D", "self"),
        ("D", "partial"),
        ("E", "partial"),
    }
    assert found[("A", "salaried")]["match_status"] == "ELIGIBLE"
    assert found[("A", "partial")]["match_status"] == "POTENTIAL"
    unknown = {u["criterion"] for u in found[("A", "partial")]["unknown_criteria"]}
    assert unknown == {"min_credit_score", "max_age", "employment_types"}
    assert float(found[("A", "partial")]["criteria_coverage"]) == 0.4

    assert stats["users_considered"] == 5 and stats["active_products"] == 4
    assert stats["total_possible_pairs"] == 20
    assert stats["sql_candidate_pairs"] == 8  # SQL alone removed every impossible pair here
    assert stats["sql_eliminated_pairs"] == 12
    assert stats["deterministic_rejections"] == 0
    assert stats["eligible_matches"] + stats["potential_matches"] == 8
    assert stats["llm_invocations"] == 0
    assert stats["chunks_processed"] == 1
    batch = db.execute("SELECT matching_status FROM upload_batches WHERE id = %s", (batch_id,)).fetchone()
    assert batch["matching_status"] == "COMPLETED"


def test_user_with_no_match(db, settings):
    _products(db)
    batch_id = import_csv(settings, HEADER + "C,c@example.com,10000,600,student,19\n")
    stats = matching.run_matching(settings, uuid.UUID(batch_id))
    assert stats["eligible_matches"] + stats["potential_matches"] == 0
    assert _matches(db, batch_id) == {}


def test_retry_is_idempotent(db, settings):
    _products(db)
    batch_id = import_csv(settings, USERS)
    first = matching.run_matching(settings, uuid.UUID(batch_id))
    second = matching.run_matching(settings, uuid.UUID(batch_id))
    assert db.execute("SELECT count(*) AS n FROM matches").fetchone()["n"] == 8
    assert {k: v for k, v in first.items() if k != "matching_run_id"} == {
        k: v for k, v in second.items() if k != "matching_run_id"
    }
    assert db.execute("SELECT count(DISTINCT matching_run_id) AS n FROM matches").fetchone()["n"] == 1


def test_rerun_removes_matches_no_longer_valid(db, settings):
    ids = _products(db)
    batch_id = import_csv(settings, USERS)
    matching.run_matching(settings, uuid.UUID(batch_id))
    db.execute("UPDATE loan_products SET min_monthly_income = 200000 WHERE id = %s", (ids["partial"],))
    matching.run_matching(settings, uuid.UUID(batch_id))
    assert not any(p == "partial" for _, p in _matches(db, batch_id))


def test_batch_isolation(db, settings):
    _products(db)
    first = import_csv(settings, HEADER + "A,a@example.com,30000,710,salaried,30\n")
    second = import_csv(settings, HEADER + "B,b@example.com,150000,800,salaried,40\n")
    stats = matching.run_matching(settings, uuid.UUID(second))
    assert stats["users_considered"] == 1
    assert _matches(db, first) == {}
    assert {u for u, _ in _matches(db, second)} == {"B"}


def test_chunking_processes_every_user(db, settings):
    add_product(db, product_name="open", min_monthly_income=1000)
    rows = "".join(f"U{i:03d},u{i}@example.com,{500 + i * 100},700,salaried,30\n" for i in range(20))
    batch_id = import_csv(settings, HEADER + rows)
    stats = matching.run_matching(settings, uuid.UUID(batch_id))  # MATCHING_CHUNK_SIZE=7 in tests
    assert stats["chunks_processed"] == 3 and stats["users_considered"] == 20
    assert stats["sql_candidate_pairs"] == 15  # incomes 500..2400; >= 1000 from U005


def test_batch_not_ready_or_missing(db, settings):
    with pytest.raises(matching.BatchNotReady):
        matching.run_matching(settings, uuid.uuid4())
    db.execute(
        "INSERT INTO upload_batches (id, original_filename, storage_key) VALUES (%s, 'x.csv', 'k')", (uuid.UUID(int=1),)
    )
    with pytest.raises(matching.BatchNotReady):
        matching.run_matching(settings, uuid.UUID(int=1))


def test_concurrent_run_is_rejected(db, settings, database):
    import psycopg

    _products(db)
    batch_id = uuid.UUID(import_csv(settings, USERS))
    with psycopg.connect(database, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (matching._lock_key(batch_id),))
        with pytest.raises(matching.MatchingAlreadyRunning):
            matching.run_matching(settings, batch_id)


def test_failure_marks_run_failed(db, settings, monkeypatch):
    _products(db)
    batch_id = import_csv(settings, USERS)
    monkeypatch.setattr(matching, "evaluate", lambda *a: (_ for _ in ()).throw(RuntimeError("bug")))
    with pytest.raises(RuntimeError):
        matching.run_matching(settings, uuid.UUID(batch_id))
    assert db.execute("SELECT status FROM matching_runs").fetchone()["status"] == "FAILED"
    assert (
        db.execute("SELECT matching_status FROM upload_batches WHERE id = %s", (batch_id,)).fetchone()[
            "matching_status"
        ]
        == "FAILED"
    )
