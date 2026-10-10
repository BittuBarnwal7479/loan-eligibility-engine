# Testing

## How to run

```bash
# backend: unit + PostgreSQL integration tests (throwaway database loan_engine_test)
docker compose up -d postgres
docker compose --profile test run --rm api-tests                       # pytest -q
docker compose --profile test run --rm api-tests sh -c "ruff check . && ruff format --check ."

# frontend
cd frontend && npm ci && npm test && npm run build

# full local pipeline (needs the stack up and n8n set up)
python scripts/setup_n8n.py
python scripts/e2e_local.py sample-data/users_sample.csv
python scripts/e2e_local.py sample-data/users_with_errors.csv
python scripts/e2e_local.py sample-data/invalid_header.csv

# optimization benchmark (synthetic users, no emails)
docker compose exec -e N8N_MATCHING_WEBHOOK_URL= api python -m scripts.benchmark_prefilter --users 10000
```

## Results recorded on 2026-10-10 (Windows 11, Docker Desktop)

Raw outputs are in [`test-results/`](test-results/):

| What | Result | File |
|---|---|---|
| Backend pytest (unit + DB integration + mocked AWS + workflow JSON checks) | **120 passed** (final audit run) | `pytest-and-frontend-2026-10-10.txt` |
| Ruff lint + format check | passed | (command above) |
| Frontend vitest | **6 passed**; `vite build` succeeded | `pytest-and-frontend-2026-10-10.txt` |
| Local E2E through n8n B + C → Mailpit (60 users) | import COMPLETED; 480 pairs → 156 SQL candidates; 156 matches; 58 emails, all to the test inbox | `e2e-local-2026-10-10.txt` |
| E2E with invalid rows | `COMPLETED_WITH_ERRORS`, 5 imported / 7 rejected, 5 emails | same |
| E2E with a bad header | `FAILED`, 0 rows, no matching, no emails | same |
| Retry of B for an already-notified batch | 0 new emails (55 → 55); a new matching run with identical results | verified interactively |
| Workflow A executed by n8n against live lender pages | 3/3 sources stored | `workflow-a-live-run-2026-10-10.txt` |
| Pre-filter benchmark, 10,000 synthetic users × 8 products | 80,000 pairs → 22,285 candidates (72.1% eliminated by SQL), 0 LLM calls | `benchmark-prefilter-2026-10-10.txt` |
| Upload UI in headless Chrome | header check blocks a bad file; `users_with_errors.csv` → batch id shown, `COMPLETED_WITH_ERRORS`, 7 rejected rows listed, matching and notifications `COMPLETED`, no console errors | `ui-smoke-2026-10-10.txt`, `.png` |
| Webhook / internal auth | n8n webhook without token → 403; `/internal/*` without token → 401 | verified with curl |
| CORS | allowed origin gets ACAO header; other origin preflight → 400 | verified with curl |
| n8n import | all 3 workflows and 3 credentials imported and published with the n8n 2.43.3 CLI | `scripts/setup_n8n.py` |
| `serverless.yml` | `serverless package` succeeded with stubbed SSM values and the requirements plugin disabled; `cfn-lint` showed no errors (only notices caused by the stub values). The Docker-based dependency build was **not** run | offline only |

## Coverage map (requirement → test)

| Requirement | Tests |
|---|---|
| Valid CSV import | `test_ingestion_db::test_valid_csv_import`, `test_api_db::test_end_to_end_local_upload_flow` |
| Invalid headers / rows | `test_csv_parser::*`, `test_ingestion_db::test_invalid_header_fails_batch_without_inserting`, `test_all_rows_invalid_fails_batch`, `test_partial_errors_are_recorded_and_valid_rows_committed` |
| Duplicate uploads / S3 events | `test_duplicate_s3_event_is_ignored`, `test_reupload_updates_users_and_reassigns_batch`, `test_upload_url_cannot_be_reused`, `test_aws_mocked::test_s3_object_processed_end_to_end_with_mocked_s3` |
| Batch isolation | `test_matching_db::test_batch_isolation` |
| Numerical rules, boundaries | `test_engine::*` |
| Unknown / missing criteria | `test_engine::test_missing_criteria_are_unknown_not_passed`, `test_unknown_never_overrides_a_failure`, `test_unevaluable_published_condition_downgrades_to_potential` |
| No-match / multiple matches | `test_matching_db::test_user_with_no_match`, `test_pipeline_matches_and_stats` |
| Idempotent matching retries | `test_retry_is_idempotent`, `test_rerun_removes_matches_no_longer_valid`, `test_concurrent_run_is_rejected` |
| Pre-filter soundness and reduction | `test_prefilter_benchmark_db::test_prefilter_is_sound_and_reduces_pairs` |
| Notification dry-run / duplicates / retries | `test_notifications_db::*` |
| Never email CSV addresses | `test_dry_run_groups_one_message_per_user_and_never_uses_csv_addresses`, `test_real_delivery_requires_test_recipient` |
| Webhook auth (outbound) | `test_webhook_db::*` |
| Internal endpoint auth | `test_api_db::test_internal_endpoints_require_token` |
| Failure handling / recovery | `test_transient_error_returns_batch_to_pending_then_retry_succeeds`, `test_failure_on_last_attempt_marks_failed`, `test_stale_processing_lease_can_be_reclaimed`, `test_failure_marks_run_failed`, `test_rejected_webhook_is_recorded_and_import_kept`, `test_llm_exception_leaves_unknown` |
| Migrations | `conftest.database` applies migrations to a fresh DB and asserts a second run is a no-op |
| SQL injection resistance | `test_sql_metacharacters_are_stored_literally` (plus parameterized queries throughout) |
| Product extraction / robots | `test_extract::*`, `test_api_db::test_product_ingest_success_and_failure_paths`, `test_robots_check` |
| LLM guardrails | `test_llm_normalizer_db::*` (LLM faked) |
| Workflow JSON validity | `test_workflows::*` + real CLI import |
| Frontend | `frontend/src/csv.test.ts`, `npm run build` |

## Mocked vs. real

* **Real:** PostgreSQL, n8n (workflow execution, webhooks, Header Auth, SMTP node), Mailpit delivery, live HTTP fetches of 3 lender pages, CORS, Docker build.
* **Mocked:** S3 (`moto`): presigned POST policy contents and object reads/events. The LLM is faked in tests, and no Anthropic API call was made. The `boto3`/SES path was not exercised.
* **Not tested at all (UNVERIFIED):** real AWS deployment (S3 events, Lambda in VPC, RDS, HTTP API, IAM), real SES delivery, n8n's AWS SES node, the real LLM call, and the `serverless-python-requirements` Docker build.
* **UI:** driven in headless Chrome against the local stack (`test-results/ui-smoke-2026-10-10.txt` + `.png`). The scripted browser run is not part of the repo test suite, because it relies on a puppeteer install outside the project.

## Final audit (2026-10-10)

An independent pass re-ran every check above and found the following. Evidence: `test-results/audit-2026-10-10.txt`.

| Finding | Severity | Evidence | Resolution |
|---|---|---|---|
| A re-run of matching that changed a user's match set while that user's email was still undelivered caused two emails on retry (old and new rows both claimable). An orphaned row could also stay `PENDING` forever, blocking `notification_status`. | Bug | `test_changed_match_set_supersedes_unsent_notification` failed on the original code: 2 messages instead of 1 | Migration `002_notification_superseded.sql`; old undelivered rows are marked `SUPERSEDED` in `prepare_outbox` |
| Local upload URL could be reused before processing started (a second POST overwrote the file with 204) | Bug | `test_local_upload_url_is_single_use_even_before_processing` failed (204 ≠ 409) | Exclusive write in `storage.write_local_object`; API returns 409 |
| Services started with the `.env.example` placeholder `change-me` as a token or DB password | Unsafe default | Compose `:?` only checks that the value is set | `config._reject_placeholders` fails startup; `test_config.py` |
| Workflow C note and docs claimed failed emails "are retried up to 3 times"; nothing retries automatically | Inaccurate claim | no scheduler or loop exists for retries | Wording corrected in the workflow sticky note and N8N_WORKFLOWS.md |
| Docs claimed `osls` compatibility, attributed time to upserts without profiling, and called major-version image tags "pinned" | Inaccurate claims | not tested / not measured | Reworded |
| `frontend` service had no Docker healthcheck | Minor | `docker compose ps` | Healthcheck added; all 5 services report healthy |
| n8n workflow JSON was written with CRLF on Windows | Minor | byte check | Generator writes LF |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` exposes n8n's env (incl. the encryption key) to workflow editors | Accepted risk | compose | Documented in SECURITY_AND_COST.md |

Verified with no change needed: all SQL interpolation is constants or generated `%s` placeholders; no secrets in tracked files; every port binds to 127.0.0.1; live n8n workflows are identical to the repo exports and published; retrying B sent 0 additional emails (176 → 176) and created a second matching run with identical results; matches for a batch reference only that batch's users; no `notification_logs` row or Mailpit message was ever addressed to a CSV address; all Markdown links and anchors resolve; `serverless package` (stubbed SSM, requirements plugin disabled) includes migrations 001 and 002 and no tests or `.env`, and the role has the VPC execution policy.

Still unverified: the `serverless-python-requirements` Docker build (`dockerizePip`) and anything that needs a real AWS account, SES, or an Anthropic key.
