"""Deterministic, explainable eligibility evaluation for one user-product pair.

Semantics
---------
* A criterion whose product value is NULL is **unknown** - never a pass.
* Any failed known criterion rejects the pair (an LLM cannot override this).
* ELIGIBLE  = every standard criterion known and passed.
* POTENTIAL = every known criterion passed, at least one criterion unknown
  (including published free-text conditions we hold no user data for).
* ``criteria_coverage`` = known standard criteria / total standard criteria.

The standard criteria are: min_monthly_income, min_credit_score, min_age,
max_age, employment_types. ``max_credit_score`` is checked when published but
is not counted as unknown when absent (lenders do not cap scores).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

STANDARD_CRITERIA: tuple[str, ...] = (
    "min_monthly_income",
    "min_credit_score",
    "min_age",
    "max_age",
    "employment_types",
)

Outcome = Literal["pass", "fail", "unknown"]
Status = Literal["ELIGIBLE", "POTENTIAL", "REJECTED"]


@dataclass(frozen=True)
class CriterionResult:
    criterion: str
    outcome: Outcome
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"criterion": self.criterion, "outcome": self.outcome, "detail": self.detail}


@dataclass
class Evaluation:
    status: Status
    results: list[CriterionResult] = field(default_factory=list)

    @property
    def passed(self) -> list[CriterionResult]:
        return [r for r in self.results if r.outcome == "pass"]

    @property
    def failed(self) -> list[CriterionResult]:
        return [r for r in self.results if r.outcome == "fail"]

    @property
    def unknown(self) -> list[CriterionResult]:
        return [r for r in self.results if r.outcome == "unknown"]

    @property
    def coverage(self) -> Decimal:
        known = sum(1 for r in self.results if r.criterion in STANDARD_CRITERIA and r.outcome != "unknown")
        return (Decimal(known) / Decimal(len(STANDARD_CRITERIA))).quantize(Decimal("0.001"))


def _fmt(value: Any) -> str:
    if isinstance(value, Decimal):
        return f"{value.normalize():f}"
    return str(value)


def _min_check(name: str, label: str, user_value: Any, required: Any) -> CriterionResult:
    if required is None:
        return CriterionResult(name, "unknown", f"{label} requirement not published or not extracted")
    if user_value >= required:
        return CriterionResult(name, "pass", f"{label} {_fmt(user_value)} >= required minimum {_fmt(required)}")
    return CriterionResult(name, "fail", f"{label} {_fmt(user_value)} < required minimum {_fmt(required)}")


def _max_check(name: str, label: str, user_value: Any, limit: Any) -> CriterionResult:
    if limit is None:
        return CriterionResult(name, "unknown", f"{label} upper limit not published or not extracted")
    if user_value <= limit:
        return CriterionResult(name, "pass", f"{label} {_fmt(user_value)} <= maximum {_fmt(limit)}")
    return CriterionResult(name, "fail", f"{label} {_fmt(user_value)} > maximum {_fmt(limit)}")


def evaluate(user: dict[str, Any], product: dict[str, Any]) -> Evaluation:
    """Evaluate a user against a product's known criteria."""
    results = [
        _min_check("min_monthly_income", "monthly income", user["monthly_income"], product.get("min_monthly_income")),
        _min_check("min_credit_score", "credit score", user["credit_score"], product.get("min_credit_score")),
        _min_check("min_age", "age", user["age"], product.get("min_age")),
        _max_check("max_age", "age", user["age"], product.get("max_age")),
    ]
    if product.get("max_credit_score") is not None:
        results.append(
            _max_check("max_credit_score", "credit score", user["credit_score"], product["max_credit_score"])
        )

    allowed = product.get("employment_types")
    if not allowed:
        results.append(
            CriterionResult("employment_types", "unknown", "employment requirement not published or not normalized")
        )
    elif user["employment_status"] in allowed:
        results.append(
            CriterionResult(
                "employment_types",
                "pass",
                f"employment status '{user['employment_status']}' is accepted ({', '.join(allowed)})",
            )
        )
    else:
        results.append(
            CriterionResult(
                "employment_types",
                "fail",
                f"employment status '{user['employment_status']}' not in accepted ({', '.join(allowed)})",
            )
        )

    for text in product.get("other_criteria") or []:
        # Published conditions we hold no user data for (e.g. work experience).
        results.append(CriterionResult("other", "unknown", f"not evaluated (no user data): {text}"))

    if any(r.outcome == "fail" for r in results):
        status: Status = "REJECTED"
    elif any(r.outcome == "unknown" for r in results):
        status = "POTENTIAL"
    else:
        status = "ELIGIBLE"
    return Evaluation(status, results)
