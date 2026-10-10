# Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `docker compose up` fails with `set POSTGRES_PASSWORD in .env` | `.env` missing | `cp .env.example .env` and replace every `change-me` |
| API restarts / `password authentication failed` | `POSTGRES_PASSWORD` changed after the volume was created | Put the old password back, or wipe local data with `docker compose down -v` (destroys local DB and n8n data) |
| `connection refused` / DSN errors with odd passwords | Password contains URL-reserved characters (`@ : / ? #`) | Use `secrets.token_urlsafe()` values |
| Port already in use (8081, 5173, 5433, 5678, 8026) | Another local service | Change `API_HOST_PORT`, `FRONTEND_HOST_PORT`, `POSTGRES_HOST_PORT`, `MAILPIT_UI_HOST_PORT` in `.env`; 5678 is fixed in compose, so edit it there if needed |
| C: drive full during installs (Windows) | npm/puppeteer caches | `npm config set cache D:/npm-cache`, `PUPPETEER_CACHE_DIR=D:/puppeteer`; move Docker's disk image to D: |
| Upload succeeds but status stays `PENDING` | Processing raised a transient error (see `error_message`) | `docker compose logs api`; local mode has no automatic retry. Fix the cause, then re-upload |
| Status `FAILED: missing required column(s)` | Header mismatch | Download the template from the UI; header matching ignores case, spaces and dashes |
| `matching_status: TRIGGER_FAILED` | n8n not running, workflows not published, or token mismatch (`HTTP 403`) | `python scripts/setup_n8n.py`, then `curl -X POST -H "X-Internal-Token: $INTERNAL_API_TOKEN" localhost:8081/internal/batches/<id>/trigger-matching` |
| n8n webhook returns 404 `webhook not registered` | Workflow not published or n8n not restarted after import | `python scripts/setup_n8n.py` (publishes and restarts) |
| n8n node error "credentials not found" after a manual UI import | Credential ids differ from the export | Open the node and pick the credential, or use `scripts/setup_n8n.py`, which uses the fixed ids |
| Workflow C stays on "Send via AWS SES" errors locally | `NOTIFICATION_MODE=ses` without AWS credentials | Use `smtp` (Mailpit) or `dry_run` locally |
| `NOTIFICATION_TEST_RECIPIENT must be set` (409 in n8n) | smtp/ses mode without a recipient | Set it in `.env`, `docker compose up -d api` |
| SES `MessageRejected: Email address is not verified` | Sandbox: both sender and recipient must be verified | Verify both in the SES console (same region as the credential) |
| SES `Throttling` | Sandbox: 1 msg/s | Workflow C already throttles; reduce parallel runs |
| `n8n execute` fails: "Task Broker's port 5679 is already in use" | The CLI starts a second runner inside the running container | Add `-e N8N_RUNNERS_BROKER_PORT=5690 -e N8N_PORT=5699` (see N8N_WORKFLOWS.md) or use the editor's Execute button |
| Workflow A stores nothing for a source; reason `only N field(s) extracted` | The lender changed the page layout or blocked the request | Look at the HTML, adjust the regexes in `app/products/extract.py`, add a test excerpt; existing data is kept and flagged stale after 3 failures |
| Workflow A: `skipped: robots.txt unavailable (403)` | The site forbids or blocks robots.txt | Respect it: remove or disable the source in `backend/config/loan_sources.json` |
| Matching run 409 "already running" | A previous run still holds the advisory lock (or crashed mid-run, in which case the lock is released when its connection closes) | Wait and retry; n8n retries automatically |
| Frontend "Network error during upload" | CORS origin mismatch or wrong `VITE_API_BASE_URL` | `CORS_ORIGINS` must contain the exact frontend origin; in S3 mode the bucket CORS must too (`frontendOrigin` param) |
| `serverless deploy` warns about `python3.12` | v3 schema predates the runtime | Harmless schema warning; CloudFormation accepts the runtime |
| Tests skip DB tests | `TEST_DATABASE_URL` not set | Run `docker compose --profile test run --rm api-tests` |
