"""Correctness + effectiveness of the SQL pre-filter on synthetic data.

1. Soundness: the pipeline's matches must equal a brute-force evaluation of
   every user-product pair (the pre-filter may only remove pairs the
   deterministic engine would reject anyway).
2. Effectiveness: report how many pairs SQL removed (printed with -s).
"""

import uuid

import pytest

from app.matching.engine import evaluate
from app.matching.service import PRODUCT_COLUMNS, run_matching
from app.products.service import seed_dev_fixtures
from scripts.benchmark_prefilter import synthetic_csv
from tests.helpers import import_csv

pytestmark = pytest.mark.db


def test_prefilter_is_sound_and_reduces_pairs(db, settings):
    seed_dev_fixtures(db)
    batch_id = uuid.UUID(import_csv(settings, synthetic_csv(300, seed=7)))
    stats = run_matching(settings, batch_id)

    users = db.execute(
        "SELECT id, monthly_income, credit_score, age, employment_status FROM users WHERE upload_batch_id = %s",
        (batch_id,),
    ).fetchall()
    products = db.execute(f"SELECT {PRODUCT_COLUMNS} FROM loan_products WHERE is_active").fetchall()
    brute = {(u["id"], p["id"]) for u in users for p in products if evaluate(u, p).status != "REJECTED"}
    stored = {
        (r["user_id"], r["loan_product_id"])
        for r in db.execute("SELECT user_id, loan_product_id FROM matches WHERE upload_batch_id = %s", (batch_id,))
    }
    assert stored == brute

    assert stats["total_possible_pairs"] == len(users) * len(products)
    assert stats["sql_candidate_pairs"] == len(brute) + stats["deterministic_rejections"]
    assert stats["sql_eliminated_pairs"] > 0
    print(
        f"\n[pre-filter] users={len(users)} products={len(products)} pairs={stats['total_possible_pairs']} "
        f"sql_candidates={stats['sql_candidate_pairs']} eliminated={stats['sql_eliminated_pairs']} "
        f"matches={len(brute)} llm_calls={stats['llm_invocations']}"
    )
