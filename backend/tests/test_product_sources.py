"""Product discovery endpoints for sources with a separate criteria page (no database needed)."""

from contextlib import nullcontext
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tests.test_extract import ICICI_EXCERPT, KOTAK_ELIGIBILITY_EXCERPT

TOKEN = {"X-Internal-Token": "test-internal-token"}
# Kotak personal-loan main page, captured 2026-10-12: the rate is here, the criteria are on the eligibility page.
KOTAK_MAIN_EXCERPT = (
    "<title>Apply for Personal Loan Online up to ₹1 crore</title>"
    "<p>Personal Loan Apply for Personal Loan Online Starting at 10.99% p.a.* Loan Amount Interest Rate</p>"
)


@pytest.fixture()
def calls(monkeypatch, settings):
    """Replace the database calls made by the product endpoints and record them."""
    from app.api import main

    recorded = {"failures": [], "upserts": []}
    monkeypatch.setattr(main, "connection", lambda: nullcontext(object()))
    monkeypatch.setattr(
        main.products, "record_crawl_failure", lambda conn, source, reason: recorded["failures"].append(reason)
    )

    def upsert(conn, source, product):
        recorded["upserts"].append((source["source_key"], product))
        return {"id": 3, "inserted": False}

    monkeypatch.setattr(main.products, "upsert_crawled_product", upsert)
    recorded["client"] = TestClient(main.app)
    return recorded


def _ingest(client, **body):
    r = client.post("/internal/products/ingest", headers=TOKEN, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_kotak_main_and_criteria_pages_become_one_product(calls):
    out = _ingest(
        calls["client"],
        source_key="kotak-personal-loan",
        status_code=200,
        html=KOTAK_MAIN_EXCERPT,
        criteria_status_code=200,
        criteria_html=KOTAK_ELIGIBILITY_EXCERPT,
    )
    assert out["stored"] is True and out["product_id"] == 3 and out["created"] is False
    assert calls["failures"] == []
    [(source_key, p)] = calls["upserts"]  # exactly one upsert for the two pages
    assert source_key == "kotak-personal-loan"
    assert p.interest_rate_min == Decimal("10.99")  # from the main page
    assert p.min_monthly_income == Decimal("30000")  # strictest tier: customer relationship unknown
    assert (p.min_age, p.max_age) == (21, 60)
    assert p.min_credit_score is None  # 750 is guidance and 685 conflicts: no threshold
    assert p.employment_types == ["salaried"]
    assert "Work experience: 1 year (as published)" in p.other_criteria


@pytest.mark.parametrize(
    ("criteria", "reason"),
    [
        ({"criteria_status_code": 503, "criteria_html": None}, "criteria page: HTTP 503"),
        ({"criteria_status_code": None, "criteria_error": "timeout"}, "criteria page: timeout"),
        ({"criteria_status_code": 200, "criteria_html": ""}, "criteria page: empty response body"),
        ({}, "criteria page: HTTP None"),  # caller did not fetch the criteria page at all
    ],
)
def test_failed_criteria_page_records_failure_and_stores_nothing(calls, criteria, reason):
    out = _ingest(
        calls["client"], source_key="kotak-personal-loan", status_code=200, html=KOTAK_MAIN_EXCERPT, **criteria
    )
    assert out == {"source_key": "kotak-personal-loan", "stored": False, "reason": reason}
    assert calls["failures"] == [reason]
    assert calls["upserts"] == []


def _robots(client, source_key, robots_txt):
    r = client.post(
        "/internal/products/robots-check",
        headers=TOKEN,
        json={"source_key": source_key, "status_code": 200, "robots_txt": robots_txt},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_robots_must_allow_both_kotak_pages(calls):
    out = _robots(calls["client"], "kotak-personal-loan", "User-agent: *\nAllow: /\n")
    assert out["allowed"] is True and calls["failures"] == []


def test_robots_denial_of_criteria_page_blocks_and_records_failure(calls):
    robots = "User-agent: *\nDisallow: /en/personal-banking/loans/personal-loan/eligibility.html\n"
    out = _robots(calls["client"], "kotak-personal-loan", robots)
    assert out == {
        "source_key": "kotak-personal-loan",
        "allowed": False,
        "reason": "criteria page disallowed by robots.txt",
    }
    assert calls["failures"] == ["criteria page disallowed by robots.txt"]


def test_criteria_page_on_another_host_is_not_crawled(calls, monkeypatch):
    from app.api import main

    source = {**main.products.get_source("kotak-personal-loan"), "criteria_url": "https://other.example/eligibility"}
    monkeypatch.setattr(main.products, "get_source", lambda key: source)
    out = _robots(calls["client"], "kotak-personal-loan", "User-agent: *\nAllow: /\n")
    assert out["allowed"] is False and "different host" in out["reason"]


def test_sources_without_criteria_url_are_unchanged(calls):
    client = calls["client"]
    assert _robots(client, "icici-personal-loan", "User-agent: *\nDisallow: /\n") == {
        "source_key": "icici-personal-loan",
        "allowed": False,
        "reason": "robots.txt evaluated",
    }
    assert calls["failures"] == []  # a main-page denial is still only a skip, as before
    # Criteria fields are ignored for sources that do not configure a criteria page.
    out = _ingest(
        client,
        source_key="icici-personal-loan",
        status_code=200,
        html=ICICI_EXCERPT,
        criteria_status_code=503,
        criteria_html=KOTAK_ELIGIBILITY_EXCERPT,
    )
    assert out["stored"] is True
    [(_, p)] = calls["upserts"]
    assert p.interest_rate_min == Decimal("9.99") and (p.min_age, p.max_age) == (23, 58)
    assert p.employment_types == ["salaried", "self_employed"]


def test_kotak_source_config():
    from app.products.service import get_source

    kotak = get_source("kotak-personal-loan")
    assert kotak["lender_name"] == "Kotak Mahindra Bank"  # identity (upsert key) unchanged
    assert kotak["product_name"] == "Kotak Mahindra Bank Personal Loan"
    assert kotak["url"] == "https://www.kotak.bank.in/en/personal-banking/loans/personal-loan.html"
    assert kotak["criteria_url"] == "https://www.kotak.bank.in/en/personal-banking/loans/personal-loan/eligibility.html"
    assert "criteria_url" not in get_source("icici-personal-loan")
    assert "criteria_url" not in get_source("axis-personal-loan")
