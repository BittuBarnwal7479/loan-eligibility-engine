"""Measure how much the SQL pre-filter reduces work, on synthetic data.

Generates N synthetic users (seeded RNG, example.invalid emails) and imports
them through the real ingestion path, loads the synthetic dev-fixture products
if no products exist, then runs the real matching pipeline and prints the
counters stored in matching_runs. Numbers depend entirely on the synthetic
distribution and the active products; they are a demonstration, not a claim
about real-world data.

Usage (inside the api container or with DATABASE_URL set):
    python -m scripts.benchmark_prefilter --users 10000 --seed 42
"""

from __future__ import annotations

import argparse
import io
import random
import time
import uuid

from app.config import get_settings
from app.db import connection
from app.ingestion import service as ingestion
from app.matching.service import run_matching
from app.products.service import seed_dev_fixtures
from app.storage import LOCAL_BUCKET, write_local_object

EMPLOYMENT = ["salaried"] * 55 + ["self_employed"] * 25 + ["unemployed"] * 8 + ["student"] * 7 + ["retired"] * 5


def synthetic_csv(n: int, seed: int) -> str:
    rng = random.Random(seed)  # noqa: S311 - reproducible synthetic data, not security-sensitive
    out = io.StringIO()
    out.write("user_id,email,monthly_income,credit_score,employment_status,age\n")
    run = uuid.uuid4().hex[:6]
    for i in range(n):
        income = int(rng.lognormvariate(10.4, 0.6))  # median ~33k
        score = max(300, min(900, int(rng.gauss(700, 70))))
        age = rng.randint(18, 75)
        out.write(f"BENCH{run}{i:07d},user{i}@example.invalid,{income},{score},{rng.choice(EMPLOYMENT)},{age}\n")
    return out.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    settings = get_settings()

    with connection() as conn:
        if conn.execute("SELECT count(*) AS n FROM loan_products WHERE is_active").fetchone()["n"] == 0:
            seed_dev_fixtures(conn)
        batch = ingestion.create_batch(conn, "benchmark.csv", LOCAL_BUCKET)
    write_local_object(settings, batch["storage_key"], iter([synthetic_csv(args.users, args.seed).encode()]))

    t0 = time.perf_counter()
    outcome = ingestion.process_object(settings, LOCAL_BUCKET, batch["storage_key"])
    t1 = time.perf_counter()
    stats = run_matching(settings, batch["id"])
    t2 = time.perf_counter()

    pairs = stats["total_possible_pairs"] or 1
    print(f"import: {outcome.status} in {t1 - t0:.2f}s")
    print(f"users considered          {stats['users_considered']:>12,}")
    print(f"active products           {stats['active_products']:>12,}")
    print(f"possible user-product pairs {stats['total_possible_pairs']:>10,}")
    print(
        f"SQL candidates            {stats['sql_candidate_pairs']:>12,}  ({stats['sql_candidate_pairs'] / pairs:.1%} of pairs)"
    )
    print(
        f"eliminated by SQL         {stats['sql_eliminated_pairs']:>12,}  ({stats['sql_eliminated_pairs'] / pairs:.1%})"
    )
    print(f"rejected deterministically{stats['deterministic_rejections']:>12,}")
    print(f"ELIGIBLE matches          {stats['eligible_matches']:>12,}")
    print(f"POTENTIAL matches         {stats['potential_matches']:>12,}")
    print(f"LLM invocations           {stats['llm_invocations']:>12,}")
    print(f"matching time             {t2 - t1:>11.2f}s  ({stats['chunks_processed']} chunks)")
    print(f"upload_batch_id {batch['id']}")


if __name__ == "__main__":
    main()
