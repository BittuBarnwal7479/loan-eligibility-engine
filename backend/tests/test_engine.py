from decimal import Decimal

from app.matching.engine import evaluate

USER = {"monthly_income": Decimal("50000"), "credit_score": 720, "age": 30, "employment_status": "salaried"}
FULL = {
    "min_monthly_income": Decimal("25000"),
    "min_credit_score": 700,
    "max_credit_score": None,
    "min_age": 21,
    "max_age": 60,
    "employment_types": ["salaried"],
    "other_criteria": [],
}


def test_all_known_criteria_pass_is_eligible_with_full_coverage():
    result = evaluate(USER, FULL)
    assert result.status == "ELIGIBLE"
    assert result.coverage == Decimal("1.000")
    assert {r.criterion for r in result.passed} == {
        "min_monthly_income",
        "min_credit_score",
        "min_age",
        "max_age",
        "employment_types",
    }
    assert "50000 >= required minimum 25000" in result.passed[0].detail


def test_boundary_values_pass():
    user = {**USER, "monthly_income": Decimal("25000"), "credit_score": 700, "age": 60}
    assert evaluate(user, FULL).status == "ELIGIBLE"


def test_any_failed_criterion_rejects():
    for change in (
        {"monthly_income": Decimal("24999.99")},
        {"credit_score": 699},
        {"age": 20},
        {"age": 61},
        {"employment_status": "student"},
    ):
        result = evaluate({**USER, **change}, FULL)
        assert result.status == "REJECTED", change
        assert len(result.failed) == 1


def test_missing_criteria_are_unknown_not_passed():
    product = {**FULL, "min_credit_score": None, "employment_types": None}
    result = evaluate(USER, product)
    assert result.status == "POTENTIAL"
    assert {r.criterion for r in result.unknown} == {"min_credit_score", "employment_types"}
    assert result.coverage == Decimal("0.600")


def test_product_with_no_known_criteria_is_potential_with_zero_coverage():
    product = {k: None for k in FULL} | {"other_criteria": []}
    result = evaluate(USER, product)
    assert result.status == "POTENTIAL" and result.coverage == Decimal("0.000") and not result.passed


def test_unknown_never_overrides_a_failure():
    product = {**FULL, "min_credit_score": None, "min_monthly_income": Decimal("90000")}
    assert evaluate(USER, product).status == "REJECTED"


def test_unevaluable_published_condition_downgrades_to_potential():
    product = {**FULL, "other_criteria": ["Work experience: 2 years (as published)"]}
    result = evaluate(USER, product)
    assert result.status == "POTENTIAL"
    assert result.coverage == Decimal("1.000")
    assert any("Work experience" in r.detail for r in result.unknown)


def test_max_credit_score_checked_only_when_published():
    assert evaluate({**USER, "credit_score": 800}, {**FULL, "max_credit_score": 750}).status == "REJECTED"
    assert evaluate({**USER, "credit_score": 800}, FULL).status == "ELIGIBLE"
