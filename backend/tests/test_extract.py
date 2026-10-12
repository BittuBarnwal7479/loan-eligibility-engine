"""Extraction rules tested against short text excerpts captured from public lender pages on 2026-10-10.

The excerpts are a few sentences each, kept only to pin down the extraction rules;
they are not used as product data anywhere in the application.
"""

from decimal import Decimal

import pytest

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
# Kotak personal-loan eligibility page, captured 2026-10-12.
KOTAK_ELIGIBILITY_EXCERPT = (
    "<h2>Personal Loan Eligibility Criteria</h2><p>To qualify for a Personal Loan from Kotak Mahindra Bank, "
    "salaried individuals must meet specific eligibility criteria, which include:</p>"
    "<p>Nature of employment Employed with an MNC, public limited company, or private limited company</p>"
    "<p>Age Individuals aged between 21 years and 60 years are eligible.</p>"
    "<p>Age determines Personal Loan eligibility. You must be between 21 to 60 years of age.</p>"
    "<p>Minimum monthly income ● For a Kotak Mahindra Bank Salary Account holder: Minimum net monthly income of "
    "₹ 25,000 ● For a non Kotak Mahindra Bank Salary Account holder: Minimum net monthly income of ₹ 30,000 "
    "● For Kotak Mahindra Bank Employees: Minimum net monthly income of ₹ 20,000</p>"
    "<p>Work experience Applicants must possess at least one year of work experience.</p>"
    "<p>CIBIL Score A CIBIL score of 750 or above is generally considered good for approving a Personal Loan.</p>"
    "<p>Other factors impacting your eligibility include your occupation type and employer's history.</p>"
    "<h3>How can I get a Personal Loan without a salary slip?</h3><p>Kotak Mahindra Bank requires minimal "
    "documentation such as income proof, ID proof, and address proof. Salary slips are usually required for "
    "salaried employees as proof of income, but if you are a non-salaried employee or self-employed, you can "
    "provide an alternative proof of income such as Bank statements or taxation details.</p>"
    "<h3>What is the eligibility for a Personal Loan?</h3><p>To qualify for a Personal Loan from Kotak Mahindra "
    "Bank, you must meet the following criteria: Age: 21-80 years* Residency: India Employment: Working for an "
    "MNC, private or a public limited company. Cibil Score: 685 or higher.</p>"
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


def test_document_proof_faq_does_not_make_product_accept_self_employed():
    p = extract_product(KOTAK_ELIGIBILITY_EXCERPT)
    assert p.employment_types == ["salaried"]


def test_self_employed_stated_in_eligibility_survives_document_sentence():
    p = extract_product(
        "<h2>Eligibility</h2><p>Self-employed individuals should be at least 23 years old.</p>"
        "<p>Salaried and self-employed applicants must submit income proof such as bank statements.</p>"
    )
    assert p.employment_types == ["salaried", "self_employed"]


def test_documentation_tab_label_does_not_hide_employment_criteria():
    # Axis lists criteria without full stops, right after the "Documentation" tab label.
    p = extract_product(
        "<p>View More Eligibility Documentation Personal Loan eligibility Salaried Employees Salaried doctors "
        "Employees of public and private limited companies Minimum age of 21 years</p>"
    )
    assert p.employment_types == ["salaried"]


@pytest.mark.parametrize(
    "sentence",
    [
        "Applicants must possess at least one year of work experience.",
        "A salaried person should have at least one year or more work experience.",
        "An Employment period of at least one year is another requirement.",
        "Work Experience: 1 year",
    ],
)
def test_one_year_work_experience_variants(sentence):
    p = extract_product(f"<p>{sentence}</p>")
    assert p.other_criteria == ["Work experience: 1 year (as published)"]


def test_work_experience_is_normalized_and_deduplicated():
    p = extract_product(
        "<p>Applicants must possess at least one year of work experience. A salaried person should have at least "
        "one year or more work experience. An Employment period of at least 1 year is another requirement. "
        "Work Experience: 3 years</p>"
    )
    assert p.other_criteria == ["Work experience: 1 year (as published)", "Work experience: 3 years (as published)"]


def test_conflicting_score_and_age_wording_is_not_turned_into_hard_criteria():
    p = extract_product(KOTAK_ELIGIBILITY_EXCERPT)
    assert p.min_credit_score is None
    assert any("not a stated minimum" in c and "750" in c for c in p.other_criteria)
    assert not any("685" in c for c in p.other_criteria)
    assert any("not applied" in n and "685" in n for n in p.notes)
    assert (p.min_age, p.max_age) == (21, 60)  # FAQ "21-80 years*" does not widen the stated range
    assert "Work experience: 1 year (as published)" in p.other_criteria


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
