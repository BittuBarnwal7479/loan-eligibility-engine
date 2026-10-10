"""Extraction rules tested against short text excerpts captured from public lender pages on 2026-10-10.

The excerpts are a few sentences each, kept only to pin down the extraction rules;
they are not used as product data anywhere in the application.
"""

from decimal import Decimal

from app.products.extract import extract_product, html_to_text, robots_allows
from app.products.service import validate_extracted

ICICI_EXCERPT = (
    "<title>Personal Loan - Apply for Personal Loan Online Starting @ 9.99% p.a*</title>"
    "<p>Salaried individuals should be between 18 and 58 years of age with a minimum monthly income of "
    "&#8377;30,000. Self-employed individuals should be at least 23 years old.</p>"
    "<h3>Eligibility</h3><p>For Salaried Individuals: Age: 20–58 years Income: minimum ₹30,000 or month "
    "Work Experience: 2 years For Self-Employed Individuals: Age: 23–65 years Loan limit: Up to ₹ 50 lakh</p>"
)
AXIS_EXCERPT = (
    "<p>Rate of interest starting from 9.99% p.a. basis loans disbursed</p>"
    "<h2>Eligibility</h2><ul><li>Salaried employees of private limited companies</li>"
    "<li>Minimum age of 21 years</li><li>Maximum age of 60 years at the time of maturity of the Personal Loan</li>"
    "<li>Minimum net monthly income - ₹15,000 for Axis Bank customers and ₹25,000 for non Axis Bank customers</li></ul>"
)
UNION_EXCERPT = (
    "<p>In case of default/delayed payment, penal interest should be recovered @2% p.a.</p>"
    "<p>CIBIL Score – A CIBIL score above 700 is ideal for Personal Loans.</p>"
)


def test_icici_style_page_uses_strictest_segment_values():
    p = extract_product(ICICI_EXCERPT)
    validate_extracted(p)
    assert p.interest_rate_min == Decimal("9.99") and p.interest_rate_max is None
    assert p.min_monthly_income == Decimal("30000")
    assert (p.min_age, p.max_age) == (23, 58)  # strictest of 18-58, 20-58, 23-65
    assert p.employment_types == ["salaried", "self_employed"]
    assert any("Work experience: 2 years" in c for c in p.other_criteria)
    assert any("strictest" in n for n in p.notes)


def test_axis_style_page_takes_higher_income_for_unknown_customer_relationship():
    p = extract_product(AXIS_EXCERPT)
    assert p.interest_rate_min == Decimal("9.99")
    assert p.min_monthly_income == Decimal("25000")
    assert (p.min_age, p.max_age) == (21, 60)
    assert p.employment_types == ["salaried"]


def test_penal_rates_and_advisory_scores_are_not_treated_as_criteria():
    p = extract_product(UNION_EXCERPT)
    assert p.interest_rate_min is None
    assert p.min_credit_score is None
    assert any("not a stated minimum" in c for c in p.other_criteria)


def test_explicit_rate_range_and_minimum_score():
    p = extract_product("<p>Interest rate: 10.5% - 21% p.a.</p><p>A minimum credit score of 750 is required.</p>")
    assert (p.interest_rate_min, p.interest_rate_max) == (Decimal("10.5"), Decimal("21"))
    assert p.min_credit_score == 750


def test_emi_calculator_slider_values_are_ignored():
    p = extract_product("<div>EMI calculator Interest Rate % 9.99% PA 24% PA</div>")
    assert p.interest_rate_max is None


def test_empty_or_changed_layout_extracts_nothing():
    p = extract_product("<html><body><h1>Page moved</h1></body></html>")
    assert p.extracted_field_count() == 0


def test_html_to_text_drops_scripts_and_styles():
    assert html_to_text("<script>var x='9% p.a.';</script><style>p{}</style><p>Hi &amp; bye</p>") == "Hi & bye"


def test_robots_rules():
    robots = "User-agent: *\nDisallow: /private\n"
    assert robots_allows(robots, "Bot", "https://bank.example/personal-loan")
    assert not robots_allows(robots, "Bot", "https://bank.example/private/x")
