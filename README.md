# Loan Eligibility Engine

Upload a CSV of user profiles. The system matches each user against loan products discovered from public lender pages and emails each user (routed to a **test inbox**) a summary of products whose *published* criteria they appear to meet.

> Results are preliminary matches based on published criteria. They are not loan offers, approvals or financial advice.

* **Backend:** Python 3.12, FastAPI (Mangum on Lambda), PostgreSQL, psycopg 3
* **Automation:** self-hosted n8n 2.43 — three workflows (discovery, matching, notifications)
* **Frontend:** React + Vite upload UI
* **Cloud (IaC):** Serverless Framework v3 — S3, Lambda, HTTP API, RDS PostgreSQL, SES policy
* **Local:** one `docker compose` stack; no AWS account, domain or paid service needed

## Contents

1. [Problem and solution](#1-problem-and-solution)
2. [Architecture and data flow](#2-architecture-and-data-flow)
3. [Why the CSV bypasses API Gateway/Lambda](#3-why-the-csv-bypasses-api-gatewaylambda)
4. [Database design](#4-database-design)
5. [The n8n workflows](#5-the-n8n-workflows)
6. [Multi-stage optimization](#6-multi-stage-optimization)
7. [Local setup and run](#7-local-setup-and-run)
8. [Importing and configuring n8n](#8-importing-and-configuring-n8n)
9. [AWS setup and deployment](#9-aws-setup-and-deployment)
10. [SES sender verification and test recipient](#10-ses-sender-verification-and-test-recipient)
11. [Environment variables and secrets](#11-environment-variables-and-secrets)
12. [Security and privacy](#12-security-and-privacy)
13. [Cost-sensitive resources and cleanup](#13-cost-sensitive-resources-and-cleanup)
14. [Troubleshooting](#14-troubleshooting)
15. [Test results and known limitations](#15-test-results-and-known-limitations)
16. [Recording the demo video](#16-recording-the-demo-video)

---

## 1. Problem and solution

Given users (`user_id, email, monthly_income, credit_score, employment_status, age`) and lender products with eligibility rules, find plausible matches cheaply and explainably, then notify users.

* **Products** are refreshed weekly (or on demand) by n8n **Workflow A**. It checks robots.txt, fetches three public personal-loan pages (ICICI Bank, Axis Bank, Kotak Mahindra Bank), and the backend extracts rates and criteria with deterministic rules. Nothing is invented. A criterion the page doesn't state is stored as **unknown**.
* **Users** arrive as CSV through an S3 presigned upload. An S3-triggered Lambda validates and imports them.
* **Matching** (**Workflow B**) uses a SQL pre-filter to remove impossible pairs, then a deterministic evaluator that records why each pair passed and which criteria are unknown. An LLM step is optional, off by default, and limited to normalizing free-text product criteria once per product.
* **Notifications** (**Workflow C**) send one grouped email per user via SES (cloud) or Mailpit (local). They are always addressed to `NOTIFICATION_TEST_RECIPIENT`. Delivery is idempotent and every outcome is logged.

## 2. Architecture and data flow

![architecture](docs/architecture.svg)

Full description, state machines and networking: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** (Mermaid source: [docs/architecture.mmd](docs/architecture.mmd)).

1. UI → `POST /api/uploads` → batch `PENDING` + 5-minute presigned POST (fixed key, ≤10 MB, SSE).
2. Browser → S3 directly.
3. S3 `ObjectCreated` → `processCsv` Lambda → atomic claim (`PROCESSING`) → stream + validate → upsert rows **and** final status in one transaction.
4. Lambda → n8n Workflow B webhook (`X-Webhook-Token`, batch id only).
5. B → backend: validate batch → (optional) normalize criteria → SQL pre-filter + deterministic evaluation + persist (chunked) → trigger C.
6. C → backend: grouped outbox → send each message (SES / SMTP / dry-run) → record outcome → finalize.
7. UI polls `GET /api/uploads/{id}` for import, matching and notification progress.

## 3. Why the CSV bypasses API Gateway/Lambda

API Gateway caps payloads at 10 MB and synchronous Lambda invocations at 6 MB. Multipart and binary bodies are base64-inflated. Proxying the file would also keep a Lambda running for the whole network transfer. With a presigned **POST**, S3 enforces size, key and encryption itself. The processor runs off an event with retries, and the request path stays fast. Locally, an HMAC-signed, expiring, single-use URL mimics the same contract, and the same `process_object()` runs afterwards.

## 4. Database design

[`backend/migrations/`](backend/migrations/) (`001_initial_schema.sql`, `002_notification_superseded.sql`): `upload_batches`, `users`, `loan_products`, `matching_runs`, `matches`, `notification_logs`, with FKs, CHECK constraints, unique keys for idempotency, and indexes for keyset pagination and the pre-filter. Key rules:

* `loan_products` criterion columns: **NULL = unknown**, never "no requirement". Each product keeps `data_origin` (`crawled` / `dev_fixture` / `manual`), source URL + attribution, extraction notes, `last_verified_at`, crawl-failure count, `is_stale` and `is_active`.
* `matches.match_status`: `ELIGIBLE` means every standard criterion was known and passed. `POTENTIAL` means every known criterion passed and something is unknown. `criteria_coverage` is the share of the 5 standard criteria the lender published; it is **not** an approval probability.
* `notification_logs.idempotency_key` = sha256(batch, user, product set) prevents duplicate sends.

Details: [docs/ARCHITECTURE.md#database-design](docs/ARCHITECTURE.md#database-design).

## 5. The n8n workflows

| File | Trigger | Summary |
|---|---|---|
| [`workflow-a-loan-discovery.json`](n8n/workflows/workflow-a-loan-discovery.json) | weekly schedule + manual | robots.txt → polite fetch → deterministic extract/validate/upsert → staleness flags |
| [`workflow-b-user-matching.json`](n8n/workflows/workflow-b-user-matching.json) | webhook `loan-matching` (header auth) | validate batch → optional LLM criteria normalization → SQL pre-filter + deterministic evaluation + persist → trigger C |
| [`workflow-c-notification.json`](n8n/workflows/workflow-c-notification.json) | webhook `loan-notify` (header auth) | grouped outbox → dry-run / SES / SMTP → record SENT/FAILED/DRY_RUN → finalize |

Node-by-node description: **[docs/N8N_WORKFLOWS.md](docs/N8N_WORKFLOWS.md)**.

## 6. Multi-stage optimization

| Stage | Work |
|---|---|
| 0 (optional) | LLM maps free-text employment requirements to fixed categories, **once per product**, evidence-checked, cached |
| 1 | Parameterized SQL join per 1,000-user chunk drops pairs that fail any *known* threshold |
| 2 | Deterministic per-criterion evaluation and explanations for survivors |
| 3 | Idempotent upsert; counters stored in `matching_runs` |

Measured on synthetic data (10,000 users × 8 products): **80,000 possible pairs → 22,285 SQL candidates (72.1% eliminated), 0 LLM calls, 12.9 s.** A test proves the pre-filter is sound: the pipeline's matches equal a brute-force evaluation of every pair. Complexity, cost and limits are covered in **[docs/OPTIMIZATION.md](docs/OPTIMIZATION.md)**.

## 7. Local setup and run

Requirements: Docker (Compose v2), Python 3.10+ (for the helper scripts), Node 20+ (only to run the frontend tests and build outside Docker). On Windows, keep Docker data and npm caches on a drive with space (`npm config set cache D:/npm-cache`).

```bash
git clone <your-repo-url> loan-eligibility-engine && cd loan-eligibility-engine
cp .env.example .env
# replace every "change-me" (generate values with: python -c "import secrets; print(secrets.token_urlsafe(32))")
# set NOTIFICATION_TEST_RECIPIENT to an inbox you control (locally Mailpit catches everything anyway)

docker compose up -d --build            # postgres, api (runs migrations), n8n, mailpit, frontend
python scripts/setup_n8n.py             # import credentials + 3 workflows, publish them
docker compose exec api python -m app.seed   # OPTIONAL: synthetic, clearly labelled dev products
```

| Service | URL |
|---|---|
| Upload UI | http://localhost:5173 |
| API (+ `/docs`) | http://localhost:8081 |
| n8n editor | http://localhost:5678 (create the local owner account on first visit) |
| Mailpit (caught emails) | http://localhost:8026 |
| PostgreSQL | `localhost:5433` (user/db `loan_engine`) |

Load real products: in n8n, open **A - Loan Product Discovery → Execute workflow**. Then upload `sample-data/users_sample.csv` in the UI, or run:

```bash
python scripts/e2e_local.py sample-data/users_sample.csv
```

Stop with `docker compose down`. Add `-v` to delete all local data (DB, n8n, uploads).

## 8. Importing and configuring n8n

`python scripts/setup_n8n.py` does everything (credentials with fixed ids, workflow import, publish, restart). The manual UI steps, the credential list and the hosting variables (`N8N_HOST`, `WEBHOOK_URL`, `N8N_EDITOR_BASE_URL`, `LOAN_API_BASE_URL`) are in [docs/N8N_WORKFLOWS.md](docs/N8N_WORKFLOWS.md). No domain, public HTTPS or OAuth is required; the only webhook caller is the backend on the same network.

## 9. AWS setup and deployment

**Not deployed.** The configuration was validated offline (`serverless package` + `cfn-lint`). Step-by-step instructions (SSM secrets, VPC parameters, deploy, migrate, n8n on EC2, verification, teardown) are in **[docs/AWS_DEPLOYMENT.md](docs/AWS_DEPLOYMENT.md)**.

```bash
npm install
npx serverless deploy --stage dev --param="vpcId=..." --param="subnetIds=a,b" --param="routeTableIds=..." \
  --param="frontendOrigin=..." --param="sesFromAddress=..." --param="n8nSecurityGroupId=..."
npx serverless invoke -f migrate --stage dev
```

## 10. SES sender verification and test recipient

Verify a sender identity in SES (same region as the n8n AWS credential). While the account is in the **sandbox**, also verify the **test recipient**: sandbox accounts can only send to verified addresses, up to 200/day at 1/s. Workflow C throttles to 1/s. Give n8n an IAM principal with only the stack's `N8nSesSendPolicy` (`ses:SendEmail`/`SendRawEmail` restricted to your `FromAddress`). Set `NOTIFICATION_MODE=ses`, `NOTIFICATION_FROM_EMAIL` and `NOTIFICATION_TEST_RECIPIENT`. Details: [docs/AWS_DEPLOYMENT.md#ses](docs/AWS_DEPLOYMENT.md#ses).

## 11. Environment variables and secrets

| Variable | Used by | Notes |
|---|---|---|
| `POSTGRES_PASSWORD` / `POSTGRES_USER` / `POSTGRES_DB` | compose | URL-safe password |
| `INTERNAL_API_TOKEN` | API + n8n credential | n8n → `/internal/*` (`X-Internal-Token`) |
| `N8N_WEBHOOK_TOKEN` | API + n8n credential | backend → n8n webhooks (`X-Webhook-Token`) |
| `UPLOAD_SIGNING_SECRET` | API | local-mode upload URL HMAC |
| `N8N_ENCRYPTION_KEY` | n8n | keep stable or saved credentials become unreadable |
| `NOTIFICATION_MODE` | API | `dry_run` \| `smtp` \| `ses` |
| `NOTIFICATION_TEST_RECIPIENT` | API | **the only address that ever receives email** |
| `NOTIFICATION_FROM_EMAIL` | API | SES-verified sender in the cloud |
| `LLM_ENABLED`, `LLM_MODEL`, `ANTHROPIC_API_KEY` | API | optional; default off |
| `AWS_SES_ACCESS_KEY_ID`, `AWS_SES_SECRET_ACCESS_KEY`, `AWS_SES_REGION` | `setup_n8n.py` | optional; creates the n8n SES credential |
| `STORAGE_MODE`, `UPLOAD_BUCKET`, `DATABASE_URL`/`DB_SECRET_ARN`, `MATCHING_CHUNK_SIZE`, `INSERT_CHUNK_SIZE`, `MAX_UPLOAD_BYTES`, `UPLOAD_URL_TTL_SECONDS`, `CORS_ORIGINS`, `PUBLIC_API_BASE_URL` | API | see `backend/app/config.py` (defaults in code) |

Where to get them: generate the tokens yourself (`secrets.token_urlsafe`). AWS values come from your account (SSM on AWS, see the deployment doc). The Anthropic key comes from console.anthropic.com, and is only needed if you enable the LLM step. Never commit `.env`.

## 12. Security and privacy

Header-authenticated webhooks and internal API, presigned uploads with size/key/SSE limits, a private bucket with TLS-only access and 7-day expiry, private RDS, least-privilege IAM, parameterized SQL, localhost-only ports, and no secrets in the repo. CSV addresses are never emailed, and error reports never echo cell values. Full table and known gaps: **[docs/SECURITY_AND_COST.md](docs/SECURITY_AND_COST.md)**.

## 13. Cost-sensitive resources and cleanup

RDS and the n8n EC2 host bill hourly even when idle. NAT gateways, RDS Proxy and VPC interface endpoints are deliberately avoided. Teardown (`aws s3 rm …`, `serverless remove`, delete the final RDS snapshot, terminate EC2, delete the IAM user and SSM params) is in [docs/AWS_DEPLOYMENT.md#teardown-stop-all-charges](docs/AWS_DEPLOYMENT.md#teardown-stop-all-charges). The full assignment runs locally at US$0.

## 14. Troubleshooting

See **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** (ports, `.env`, n8n webhooks/credentials, SES sandbox errors, layout changes on lender pages, CORS).

## 15. Test results and known limitations

Recorded 2026-10-10 (raw output in [docs/test-results/](docs/test-results/)):

* Backend: **120 pytest tests passed** (unit + PostgreSQL integration + mocked S3 + workflow JSON checks); ruff lint/format clean. (113 at first delivery; the final audit added 7 tests along with its fixes. See [docs/TESTING.md](docs/TESTING.md#final-audit-2026-10-10).)
* Frontend: **6 vitest tests passed**; production build OK.
* Local end-to-end through n8n: 60-user CSV → 156 matches → 58 grouped emails in Mailpit, all to the test inbox. Re-running B sent **0 duplicates**. The error CSV gave `COMPLETED_WITH_ERRORS` (5/7); the bad-header CSV gave `FAILED`.
* Workflow A executed by n8n against the live ICICI, Axis and Kotak pages: 3/3 stored.

* Upload UI driven in headless Chrome: client-side header check, upload, progress, batch id, status badges, rejected-row table ([screenshot](docs/test-results/ui-smoke-2026-10-10.png)).

**Not verified:** real AWS deployment, real SES sending, and the real LLM call (faked in tests). See [docs/TESTING.md](docs/TESTING.md) for the mocked-vs-real breakdown and [docs/REQUIREMENTS_CHECKLIST.md](docs/REQUIREMENTS_CHECKLIST.md) for limitations.

## 16. Recording the demo video

A timed 8-minute script with exact commands is in **[docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)**.

## Repository layout

```
backend/            FastAPI app, Lambda handlers, migrations, seeds, tests
  app/              config, db, ingestion/, matching/, notifications/, products/, api/
  handlers/         api.py (Mangum), process_csv.py (S3 event), migrate.py
  migrations/       versioned SQL
  config/           loan_sources.json (Workflow A sources)
  seeds/            dev_loan_products.json (SYNTHETIC fixtures)
  scripts/          benchmark_prefilter.py
frontend/           React + Vite upload UI
n8n/                build_workflows.py + workflows/*.json
scripts/            setup_n8n.py, e2e_local.py
sample-data/        synthetic CSVs
docs/               architecture, API, AWS, n8n, optimization, security/cost, testing, troubleshooting, demo
docker-compose.yml  local stack
serverless.yml      AWS stack (not deployed)
```
