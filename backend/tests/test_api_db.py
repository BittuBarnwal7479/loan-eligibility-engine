"""HTTP-level tests through FastAPI's TestClient (local storage mode)."""

import pytest
from fastapi.testclient import TestClient

from tests.helpers import HEADER, add_product

pytestmark = pytest.mark.db
TOKEN = {"X-Internal-Token": "test-internal-token"}
CSV = HEADER + "U1,a@example.com,50000,720,salaried,30\nU2,bad,1,1,salaried,1\n"


@pytest.fixture()
def client(db, settings):
    from app.api.main import app

    return TestClient(app)


def _upload(client, content: str = CSV, filename: str = "users.csv"):
    created = client.post("/api/uploads", json={"filename": filename, "size_bytes": len(content)})
    assert created.status_code == 201, created.text
    body = created.json()
    target = body["upload"]
    url = target["url"].replace("http://testserver", "")
    response = client.post(url, data=target["fields"], files={"file": (filename, content, "text/csv")})
    return body, response


def test_end_to_end_local_upload_flow(client):
    body, response = _upload(client)
    assert response.status_code == 204
    assert body["storage_mode"] == "local"
    status = client.get(f"/api/uploads/{body['upload_batch_id']}").json()
    assert status["status"] == "COMPLETED_WITH_ERRORS"
    assert (status["processed_rows"], status["failed_rows"]) == (1, 1)
    assert status["row_errors"][0]["field"] in {"email", "credit_score", "age", "monthly_income"}


def test_upload_url_cannot_be_reused(client):
    body, _ = _upload(client)
    url = body["upload"]["url"].replace("http://testserver", "")
    again = client.post(url, files={"file": ("users.csv", CSV, "text/csv")})
    assert again.status_code == 409


def test_tampered_upload_signature_rejected(client):
    created = client.post("/api/uploads", json={"filename": "u.csv", "size_bytes": 10}).json()
    url = created["upload"]["url"].replace("http://testserver", "").replace("signature=", "signature=00")
    assert client.post(url, files={"file": ("u.csv", CSV, "text/csv")}).status_code == 403


def test_upload_request_validation(client):
    assert client.post("/api/uploads", json={"filename": "users.xlsx", "size_bytes": 10}).status_code == 400
    assert client.post("/api/uploads", json={"filename": "users.csv", "size_bytes": 10**9}).status_code == 413
    assert client.post("/api/uploads", json={"filename": "users.csv", "size_bytes": 0}).status_code == 422


def test_unknown_batch_and_bad_uuid(client):
    assert client.get("/api/uploads/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/api/uploads/not-a-uuid").status_code == 404


def test_csv_template(client):
    response = client.get("/api/csv-template")
    assert response.status_code == 200
    assert response.text.splitlines()[0] == HEADER.strip()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/internal/batches/00000000-0000-0000-0000-000000000000"),
        ("post", "/internal/matching/00000000-0000-0000-0000-000000000000/run"),
        ("post", "/internal/notifications/00000000-0000-0000-0000-000000000000/prepare"),
        ("get", "/internal/products/sources"),
        ("post", "/internal/products/refresh-staleness"),
    ],
)
def test_internal_endpoints_require_token(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"X-Internal-Token": "wrong"}).status_code == 401


def test_internal_pipeline_over_http(client, db):
    add_product(
        db,
        product_name="p",
        min_monthly_income=1000,
        min_credit_score=600,
        min_age=18,
        max_age=70,
        employment_types=["salaried"],
    )
    body, _ = _upload(client)
    batch_id = body["upload_batch_id"]
    assert client.get(f"/internal/batches/{batch_id}", headers=TOKEN).json()["ready_for_matching"] is True
    normalize = client.post(f"/internal/matching/{batch_id}/normalize-criteria", headers=TOKEN).json()
    assert normalize["llm_invocations"] == 0
    run = client.post(f"/internal/matching/{batch_id}/run", headers=TOKEN, json={"llm_invocations": 0}).json()
    assert run["eligible_matches"] == 1 and run["total_possible_pairs"] == 1
    outbox = client.post(f"/internal/notifications/{batch_id}/prepare", headers=TOKEN).json()
    assert len(outbox["messages"]) == 1
    nid = outbox["messages"][0]["notification_id"]
    result = client.post(f"/internal/notifications/results/{nid}", headers=TOKEN, json={"status": "DRY_RUN"})
    assert result.json()["status"] == "DRY_RUN"
    bad = client.post(f"/internal/notifications/results/{nid}", headers=TOKEN, json={"status": "BOGUS"})
    assert bad.status_code in (200, 422)  # already final -> reported as duplicate before validation matters
    final = client.post(f"/internal/notifications/{batch_id}/finalize", headers=TOKEN).json()
    assert final["notification_status"] == "COMPLETED"
    status = client.get(f"/api/uploads/{batch_id}").json()
    assert status["matching"]["eligible_matches"] == 1 and status["notifications"] == {"DRY_RUN": 1}


def test_matching_rejects_unprocessed_batch(client):
    created = client.post("/api/uploads", json={"filename": "u.csv", "size_bytes": 10}).json()
    response = client.post(f"/internal/matching/{created['upload_batch_id']}/run", headers=TOKEN)
    assert response.status_code == 409


def test_product_ingest_success_and_failure_paths(client, db):
    page = (
        "<p>Interest rates starting from 10.5% p.a.</p><h2>Eligibility</h2>"
        "<p>Salaried individuals aged between 21 and 60 years with a minimum monthly income of Rs. 25,000.</p>"
    )
    ok = client.post(
        "/internal/products/ingest",
        headers=TOKEN,
        json={"source_key": "axis-personal-loan", "status_code": 200, "html": page},
    ).json()
    assert ok["stored"] and ok["created"]
    assert ok["extracted"]["min_monthly_income"] in ("25000", 25000, "25000.00")
    row = db.execute("SELECT * FROM loan_products WHERE id = %s", (ok["product_id"],)).fetchone()
    assert row["data_origin"] == "crawled" and row["min_age"] == 21 and row["employment_types"] == ["salaried"]

    # A broken crawl must not delete or overwrite existing data.
    failed = client.post(
        "/internal/products/ingest",
        headers=TOKEN,
        json={"source_key": "axis-personal-loan", "status_code": 503, "html": None},
    ).json()
    assert failed["stored"] is False
    layout = client.post(
        "/internal/products/ingest",
        headers=TOKEN,
        json={"source_key": "axis-personal-loan", "status_code": 200, "html": "<p>moved</p>"},
    ).json()
    assert layout["stored"] is False and "layout" in layout["reason"]
    row = db.execute("SELECT * FROM loan_products WHERE id = %s", (ok["product_id"],)).fetchone()
    assert row["min_age"] == 21 and row["consecutive_crawl_failures"] == 2 and row["is_active"]

    assert client.post("/internal/products/refresh-staleness", headers=TOKEN).json() == {
        "flagged_stale": 0,
        "deactivated": 0,
    }
    db.execute("UPDATE loan_products SET consecutive_crawl_failures = 3")
    assert client.post("/internal/products/refresh-staleness", headers=TOKEN).json()["flagged_stale"] == 1
    db.execute("UPDATE loan_products SET last_verified_at = now() - interval '40 days'")
    assert client.post("/internal/products/refresh-staleness", headers=TOKEN).json()["deactivated"] == 1
    assert db.execute("SELECT count(*) AS n FROM loan_products").fetchone()["n"] == 1  # never deleted


def test_unknown_source_rejected(client):
    r = client.post(
        "/internal/products/ingest", headers=TOKEN, json={"source_key": "nope", "status_code": 200, "html": "x"}
    )
    assert r.status_code == 404


@pytest.mark.parametrize(
    ("status", "robots", "allowed"),
    [
        (200, "User-agent: *\nDisallow:\n", True),
        (200, "User-agent: *\nDisallow: /\n", False),
        (404, None, True),
        (403, None, False),
        (503, None, False),
        (None, None, False),
    ],
)
def test_robots_check(client, status, robots, allowed):
    r = client.post(
        "/internal/products/robots-check",
        headers=TOKEN,
        json={"source_key": "icici-personal-loan", "status_code": status, "robots_txt": robots},
    )
    assert r.json()["allowed"] is allowed


def test_sources_listing(client):
    sources = client.get("/internal/products/sources", headers=TOKEN).json()
    assert len(sources["sources"]) == 3
    assert sources["sources"][0]["robots_url"] == "https://www.icicibank.com/robots.txt"


def test_local_upload_url_is_single_use_even_before_processing(client, monkeypatch):
    import app.api.main as main

    monkeypatch.setattr(main, "_simulated_s3_event", lambda *a: None)  # processing has not started yet
    created = client.post("/api/uploads", json={"filename": "u.csv", "size_bytes": len(CSV)}).json()
    url = created["upload"]["url"].replace("http://testserver", "")
    assert client.post(url, files={"file": ("u.csv", CSV, "text/csv")}).status_code == 204
    second = client.post(url, files={"file": ("u.csv", "user_id\nX\n", "text/csv")})
    assert second.status_code == 409
