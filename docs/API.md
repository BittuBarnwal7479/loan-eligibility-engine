# API reference

Base URL: `http://localhost:8081` locally; the HTTP API URL printed by `serverless deploy` on AWS.
FastAPI also serves interactive docs at `/docs` and the schema at `/openapi.json`.

## Public endpoints (frontend)

### `GET /health`
`200 {"status":"ok"}` when the database is reachable.

### `POST /api/uploads`
Create an upload batch and get a short-lived upload target.

```json
// request
{"filename": "users.csv", "size_bytes": 2048}
// 201 response
{
  "upload_batch_id": "722863c2-cff1-409a-ba30-454f3837a6c2",
  "status": "PENDING",
  "storage_mode": "s3",
  "max_upload_bytes": 10485760,
  "upload": {"method": "POST", "url": "https://<bucket>.s3.amazonaws.com/", "fields": {"key": "uploads/<id>/source.csv", "policy": "...", "x-amz-signature": "..."}, "expires_at": 1791600000}
}
```
Errors: `400` not `.csv`, `413` larger than `MAX_UPLOAD_BYTES`, `422` invalid body.

Upload by sending `multipart/form-data` to `upload.url` with every entry of `upload.fields` followed by `file` (it must be the last field). In local mode `url` points to `/local-uploads/{id}?expires=…&signature=…` and `fields` is empty.

### `POST /local-uploads/{batch_id}?expires=&signature=` (local mode only)
Stand-in for S3. Verifies the HMAC signature and expiry, stores the file, then starts processing exactly as an S3 event would. `204` on success, `403` invalid or expired URL, `409` batch already received a file, `413` too large.

### `GET /api/uploads/{batch_id}`
Batch status for polling.

```json
{
  "upload_batch_id": "…", "original_filename": "users_with_errors.csv",
  "status": "COMPLETED_WITH_ERRORS", "total_rows": 12, "processed_rows": 5,
  "inserted_rows": 5, "updated_rows": 0, "failed_rows": 7,
  "row_errors": [{"row": 7, "field": "email", "code": "invalid_format", "message": "email is not a valid address"}],
  "error_message": null,
  "matching_status": "COMPLETED", "notification_status": "COMPLETED",
  "matching": {"status": "COMPLETED", "users_considered": 5, "active_products": 8, "total_possible_pairs": 40,
               "sql_candidate_pairs": 11, "sql_eliminated_pairs": 29, "deterministic_rejections": 0,
               "eligible_matches": 1, "potential_matches": 10, "llm_invocations": 0, "chunks_processed": 1,
               "matching_run_id": "…", "finished_at": "…"},
  "notifications": {"SENT": 5}
}
```
`row_errors` is capped at `MAX_REPORTED_ROW_ERRORS` (100) and never includes cell values. `row` is the line number in the file (the header is line 1).

### `GET /api/csv-template`
CSV header and one example row.

## Internal endpoints (n8n only)

All require the header `X-Internal-Token: <INTERNAL_API_TOKEN>` and return `401` otherwise (the comparison is constant-time).

| Method & path | Purpose |
|---|---|
| `GET /internal/batches/{id}` | Batch summary plus `ready_for_matching` |
| `POST /internal/batches/{id}/trigger-matching` | Re-send the Workflow B webhook (after `TRIGGER_FAILED`) |
| `POST /internal/matching/{id}/normalize-criteria` | Stage 0: optional LLM normalization of ambiguous product criteria → `{llm_enabled, ambiguous_products, llm_invocations, normalized_product_ids, left_unknown_product_ids}` |
| `POST /internal/matching/{id}/run` body `{"llm_invocations": 0}` | Stages 1-3. Returns the run counters. `409` if the batch isn't imported or a run is already in progress |
| `GET /internal/matching/{id}/summary` | Latest run counters |
| `POST /internal/notifications/{id}/prepare` | Group matches per user, create/claim log rows, return `messages[]` (`notification_id, to, from, subject, html, text, delivery_mode, attempt, idempotency_key`). `409` if matching isn't complete or the test recipient is missing for smtp/ses |
| `POST /internal/notifications/results/{notification_id}` body `{"status":"SENT"\|"FAILED"\|"DRY_RUN","provider_message_id":…,"error":…}` | Record a delivery outcome. Re-reporting a final row returns `duplicate: true`; a row superseded by a newer match set only accepts `SENT` |
| `POST /internal/notifications/{id}/finalize` | Settle `notification_status` |
| `GET /internal/products/sources` | Crawl configuration plus each source's `robots_url` |
| `POST /internal/products/robots-check` body `{source_key, status_code, robots_txt}` | `{allowed, reason}` |
| `POST /internal/products/ingest` body `{source_key, status_code, html, error}` | Deterministic extraction → validation → upsert, or a recorded crawl failure |
| `POST /internal/products/refresh-staleness` | `{flagged_stale, deactivated}` |
| `GET /internal/products` | List products and criteria |

## n8n webhooks (called by the backend / Workflow B)

| URL | Header | Body |
|---|---|---|
| `POST {n8n}/webhook/loan-matching` | `X-Webhook-Token` | `{"event":"upload.completed","upload_batch_id":"<uuid>"}` |
| `POST {n8n}/webhook/loan-notify` | `X-Webhook-Token` | `{"event":"matching.completed","upload_batch_id":"<uuid>"}` |

Requests without the header get `403` from n8n (verified locally).
