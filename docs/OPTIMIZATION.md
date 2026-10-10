# Matching optimization

Results are **potential matches based on published criteria**, not approvals or financial advice.

## Pipeline

| Stage | Where | What it does | Cost driver |
|---|---|---|---|
| 0. Criteria normalization (optional) | `app/matching/llm_normalizer.py`, Workflow B "Stage 0" | For active products whose employment requirement is free text that keyword rules could not map, ask an LLM to map the text onto the 5 fixed categories. The answer is schema-constrained, must quote verbatim evidence from the text, and is stored. Runs **once per product**, never per pair, and never sees user data. Off unless `LLM_ENABLED=true`. | ≤ `LLM_MAX_CALLS_PER_RUN` calls per run; 0 once products are normalized |
| 1. SQL pre-filter | `PREFILTER_SQL` in `app/matching/service.py` | One parameterized join per 1,000-user chunk. Drops every pair that fails a **known** threshold (income, min/max score, min/max age, employment list). A NULL threshold never eliminates. | one indexed query per chunk |
| 2. Deterministic evaluation | `app/matching/engine.py` | Re-checks every criterion for survivors and records per-criterion pass/unknown reasons. Classifies the pair `ELIGIBLE` (all known and passed) or `POTENTIAL` (known passed, something unknown, including published conditions with no user data such as work experience). A failure always rejects. | O(survivors) in Python |
| 3. Persist | same | Idempotent `INSERT ... ON CONFLICT (user, product, batch) DO UPDATE`; at the end, removes this batch's matches that the current run didn't re-confirm | O(matches) |

Counters for every run are stored in `matching_runs` and shown in the UI: `total_possible_pairs`, `sql_candidate_pairs`, `sql_eliminated_pairs`, `deterministic_rejections`, `eligible_matches`, `potential_matches` and `llm_invocations`.

### Why the deterministic stage still exists after SQL

SQL removes impossible pairs cheaply, but it cannot explain *why* a pair passed or what is unknown. Python produces the stored explanation. It is also a second check: `tests/test_prefilter_benchmark_db.py` asserts that the pipeline's matches equal a brute-force evaluation of **every** pair. That proves the pre-filter never drops a pair the engine would accept. With the current criteria set, the SQL predicates mirror the engine exactly, so `deterministic_rejections` is 0. It becomes non-zero only if a criterion is added in Python without a SQL equivalent.

## Complexity

Let U = users in the batch, P = active products, K = surviving candidates (K ≤ U·P), C = chunk size.

* Naive approach: U·P evaluations, or U·P LLM calls (the anti-pattern the assignment warns about).
* This pipeline: ⌈U/C⌉ indexed queries; the database evaluates U·P cheap predicate checks in C-sized pieces, Python evaluates K pairs, and the LLM is called at most min(P_ambiguous, LLM_MAX_CALLS_PER_RUN) times **independent of U**.
* Memory: O(C·P) per chunk, independent of batch size.

## Measured results (synthetic data)

All numbers below come from real runs saved in [`test-results/`](test-results/). They depend entirely on the synthetic distribution and on which products were active (5 synthetic dev fixtures + 3 crawled products), so treat them as a demonstration of the mechanism, not a prediction for real data.

| Run | Users | Products | Possible pairs | SQL candidates | Eliminated by SQL | Matches (E / P) | LLM calls | Time |
|---|---|---|---|---|---|---|---|---|
| `scripts.benchmark_prefilter --users 10000 --seed 42` | 10,000 | 8 | 80,000 | 22,285 (27.9%) | 57,715 (72.1%) | 2,833 / 19,452 | 0 | 12.87 s matching, 2.29 s import |
| E2E `users_sample.csv` via n8n | 60 | 8 | 480 | 156 | 324 | 20 / 136 | 0 | – |

Most matches are POTENTIAL because two products have unknown criteria. Kotak's crawled page only exposed a starting rate and "salaried", and one dev fixture is deliberately partial. This is the intended behaviour: unknown is reported as unknown.

Reproduce:

```bash
docker compose exec -e N8N_MATCHING_WEBHOOK_URL= api python -m scripts.benchmark_prefilter --users 10000 --seed 42
```

(Unsetting the webhook URL stops the benchmark batch from triggering emails.)

## Cost implications

* **Database work** grows linearly with U·P, but each pair costs a few comparisons inside one indexed query per chunk. 80,000 pairs took about 13 s end-to-end on a laptop. The time split between the pre-filter queries and the ~22k match upserts (`executemany`) was not profiled. If it needs to be faster, profile first; switching the match insert to `COPY` or multi-row VALUES is the likely next step.
* **LLM cost** is bounded by the number of products with ambiguous free text, cached after the first run, and zero by default. Per-pair LLM evaluation would have meant 80,000 calls for the benchmark above.
* **Email cost** is one message per user with matches, not one per product. SES charges about US$0.10 per 1,000 emails.
* **AWS limit to be aware of:** the HTTP API integration timeout is 30 s. Batches large enough to push matching past ~25 s (roughly >15k users × 8 products at the measured speed) should be run by invoking the matching function asynchronously (a dedicated Lambda or Step Functions) instead of through the HTTP API. This is not implemented; locally there is no such limit.
