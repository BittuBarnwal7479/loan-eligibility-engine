# Security, privacy and cost

## Security controls

| Area | Control | Where |
|---|---|---|
| Secrets | None in code, workflows or docs. `.env` is gitignored; `.env.example` has placeholders only. n8n credentials are imported from `.env` via stdin and stored encrypted with `N8N_ENCRYPTION_KEY`. On AWS: SSM SecureString. | `.gitignore`, `scripts/setup_n8n.py`, `serverless.yml` |
| Internal API | `/internal/*` requires `X-Internal-Token`, compared in constant time | `app/api/main.py`, `app/security.py` |
| Webhooks | n8n webhooks use Header Auth (`X-Webhook-Token`); requests without it get 403. The payload is a batch UUID only (validated as a UUID in n8n). | workflows B, C |
| Uploads | Presigned POST: fixed key, 1 B–10 MB size range, SSE required, 5-minute expiry. The local mode uses an HMAC-signed, expiring URL that accepts exactly one file per batch (exclusive write; tested). Startup fails if any secret still has the `.env.example` placeholder `change-me` (tested). Only `.csv` is accepted. | `app/storage.py` |
| S3 | All public access blocked, BucketOwnerEnforced, SSE-S3, TLS-only bucket policy, uploads expire after 7 days | `serverless.yml` |
| Database | Private RDS, SG allows only the Lambda SG, TLS required, encrypted storage. Every query is parameterized; the only dynamic SQL is generated placeholder lists. A test proves SQL metacharacters are stored literally. | `app/*`, `tests/test_ingestion_db.py` |
| IAM | Lambda: `s3:GetObject`/`PutObject` on `uploads/*` only (+ standard logs/VPC). n8n SES: send-only, restricted to one `FromAddress`. | `serverless.yml` |
| Network (local) | Every port bound to `127.0.0.1`; Mailpit SMTP is internal only | `docker-compose.yml` |
| CORS | API and bucket allow only the configured frontend origin; other origins get 400 on preflight (verified locally) | `app/api/main.py`, `serverless.yml` |
| Containers | API runs as a non-root user. n8n (`2.43.3`) and Mailpit (`v1.31.4`) are pinned to exact versions; `postgres:16-alpine`, `python:3.12-slim` and `node:22-alpine` are pinned to a major version only (pin digests for reproducible builds) | `backend/Dockerfile`, compose |
| Crawling | robots.txt checked on every run (unreachable/forbidden ⇒ skip), identifying User-Agent, one page per lender per week, 5 s between sources | Workflow A |

## Privacy

* **No email is ever sent to an address from a CSV.** Recipients are always `NOTIFICATION_TEST_RECIPIENT`. In smtp/ses mode the backend refuses to build an outbox without it. Tests assert that CSV addresses never appear as recipients or unmasked in message bodies.
* Row-level errors list line, field and an error code only; cell values are never echoed (tested).
* Logs contain batch ids, counts and error types, not row contents. The n8n trigger payload contains no personal data.
* `notification_logs` stores a masked original address (`a***@example.com`).
* Raw uploads expire from S3 after 7 days. Locally they stay in the `uploads` volume; `docker compose down -v` removes them.
* Sample data is synthetic (`example.com`).
* The optional LLM step sends **product** text only, never user data.

## Known gaps (acceptable for an assignment, fix before real use)

* `POST /api/uploads` is unauthenticated, so anyone who can reach the API can create batches. Add auth (Cognito/JWT or an API key plus a WAF rate limit) before exposing it publicly.
* Lambda environment variables hold the resolved secrets (encrypted at rest, but readable by principals with `lambda:GetFunctionConfiguration`).
* n8n runs on plain HTTP inside the VPC/compose network. The editor should only be reached through an SSH tunnel.
* `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` (needed for `$env.LOAN_API_BASE_URL` in the workflows) lets anyone who can edit n8n workflows read the n8n container's environment, including `N8N_ENCRYPTION_KEY`. Locally only the owner can edit. In a shared deployment, hard-code the API URL in the workflows and re-enable the block.
* There is no malware scanning of uploads (they are parsed as text only and never executed or served back).

## Cost-sensitive resources

| Resource | Bills when idle? | How to avoid |
|---|---|---|
| RDS instance | **Yes, hourly** | local mode; `aws rds stop-db-instance` (up to 7 days); `serverless remove` |
| RDS final snapshot after removal | Yes (storage) | delete the snapshot |
| n8n EC2 host | **Yes, hourly** while running (+ EBS) | stop or terminate; local n8n instead |
| NAT gateway | Yes, hourly | not used by design |
| RDS Proxy / Secrets Manager endpoints | Yes | not used by design |
| Lambda, HTTP API, S3, SES | No (per use) | — |
| LLM API | Per call | disabled by default; at most `LLM_MAX_CALLS_PER_RUN` per matching run, once per product |

The whole assignment can be run and recorded locally for US$0.
