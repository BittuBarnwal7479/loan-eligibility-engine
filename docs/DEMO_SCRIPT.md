# Demo video script (about 8 minutes)

**Before recording**

```bash
docker compose up -d --build
python scripts/setup_n8n.py
docker compose exec api python -m app.seed      # synthetic fixtures (labelled), optional
```

Open these tabs: the UI (http://localhost:5173), n8n (http://localhost:5678), Mailpit (http://localhost:8026), and this repo in an editor. Use a fresh terminal with a large font. If you use your own CSV, make sure it contains no real people's addresses. Emails only go to `NOTIFICATION_TEST_RECIPIENT` anyway, and Mailpit keeps them on your machine.

| Time | Show | Say |
|---|---|---|
| 0:00–0:45 | README architecture diagram | "Users upload a CSV; we match them against loan products discovered from public lender pages and email a preliminary summary. Three n8n workflows: discovery, matching, notification. The CSV goes straight to S3 through a presigned POST; an S3 event drives a Lambda that imports it." |
| 0:45–1:30 | `backend/migrations/001_initial_schema.sql` | Tables, the batch state machine, "NULL criterion means unknown", unique keys that make retries idempotent (`matches`, `notification_logs.idempotency_key`). |
| 1:30–2:45 | n8n → Workflow A, click **Execute workflow** | Robots.txt check, polite fetch, backend deterministic extraction, upsert, staleness. Open the "Extract, validate and upsert" output: ICICI/Axis values, Kotak only rate + employment (unknown stays unknown). "No LLM here, nothing invented." |
| 2:45–4:15 | UI: download template, upload `sample-data/users_with_errors.csv` | Progress bar, batch id, `COMPLETED_WITH_ERRORS`, the rejected-rows table (line/field/problem; no raw values). Then upload `users_sample.csv` and let it run. |
| 4:15–5:30 | n8n → Executions → Workflow B run | Webhook with header auth → batch check → Stage 0 (LLM off: 0 calls) → Stages 1-3. Open "Optimization summary": possible pairs vs SQL candidates vs matches. Mention the 10k-user benchmark: 80,000 pairs → 22,285 candidates, 0 LLM calls (`docs/OPTIMIZATION.md`). |
| 5:30–6:30 | Mailpit | One email per user, all addressed to the test inbox; product, lender, published rate, source link, "not checked" list, disclaimer, [TEST FIXTURE] labels. |
| 6:30–7:15 | Terminal: re-trigger B for the same batch (`POST /internal/batches/<id>/trigger-matching` with the internal token) | Mailpit count unchanged: idempotent retry. Show `curl` without the token → 401 / 403. |
| 7:15–8:00 | `serverless.yml`, `docs/AWS_DEPLOYMENT.md` | S3 (private, SSE, 7-day expiry), Lambdas in VPC, private RDS, no NAT, SES least-privilege policy, teardown. "Not deployed to avoid cost; validated with `serverless package` + cfn-lint." Close with `docs/TESTING.md`: 120 backend tests, what's mocked vs real. |

**Commands used on camera**

```bash
TOKEN=$(grep ^INTERNAL_API_TOKEN .env | cut -d= -f2)
curl -s -X POST -H "X-Internal-Token: $TOKEN" http://localhost:8081/internal/batches/<batch-id>/trigger-matching
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8081/internal/products/sources        # 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:5678/webhook/loan-matching    # 403
```

**Recording tips:** hide `.env` and terminal history, record at 1080p, keep n8n's execution side panel open so the node outputs are readable, and pre-run Workflow A once so the products exist if the network is slow.
