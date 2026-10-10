"""Optional, narrowly scoped LLM step.

Purpose: when a product's published employment requirement is free text that
deterministic keyword rules could not map (e.g. "Employees of private limited
companies and PSUs"), ask an LLM to map that text onto our fixed employment
categories. This runs once per *product* (and the result is stored), never per
user-product pair, and only when ``LLM_ENABLED=true``.

Guards against invention:
* output is schema-constrained to the fixed category list;
* the model must quote evidence that appears verbatim in the source text;
* low-confidence or unverifiable answers leave the criterion unknown;
* the LLM never sees user data and never decides eligibility.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import psycopg
from pydantic import BaseModel

from app.config import Settings

log = logging.getLogger(__name__)

EmploymentCategory = Literal["salaried", "self_employed", "unemployed", "student", "retired"]

SYSTEM_PROMPT = (
    "You map a lender's published employment eligibility text onto a fixed set of categories: "
    "salaried, self_employed, unemployed, student, retired. Only include a category when the text "
    "explicitly supports it. Employees of companies, PSUs or government bodies are salaried. "
    "Business owners and self-employed professionals are self_employed. If the text does not let you "
    "decide, return an empty list and confident=false. 'evidence' must be an exact quote from the text."
)


class EmploymentNormalization(BaseModel):
    employment_types: list[EmploymentCategory]
    confident: bool
    evidence: str


def _call_llm(settings: Settings, text: str) -> EmploymentNormalization | None:
    import anthropic

    client = anthropic.Anthropic()  # credentials from ANTHROPIC_API_KEY / profile
    response = client.messages.parse(
        model=settings.llm_model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Employment eligibility text:\n<text>\n{text}\n</text>"}],
        output_format=EmploymentNormalization,
    )
    if response.stop_reason == "refusal":
        log.warning("llm refused normalization request")
        return None
    return response.parsed_output


def accept(result: EmploymentNormalization | None, source_text: str) -> list[str] | None:
    """Return the categories only if the answer is confident and evidenced."""
    if result is None or not result.confident or not result.employment_types:
        return None
    evidence = " ".join(result.evidence.split()).lower()
    if not evidence or evidence not in " ".join(source_text.split()).lower():
        return None
    return sorted(set(result.employment_types))


def normalize_ambiguous_products(settings: Settings, conn: psycopg.Connection, llm=None) -> dict[str, Any]:
    """Normalize free-text employment criteria for active products. ``llm`` is injectable for tests."""
    candidates = conn.execute(
        "SELECT id, employment_criteria_text FROM loan_products"
        " WHERE is_active AND employment_types IS NULL AND employment_criteria_text IS NOT NULL"
        " ORDER BY id LIMIT %s",
        (settings.llm_max_calls_per_run,),
    ).fetchall()
    report: dict[str, Any] = {
        "llm_enabled": settings.llm_enabled,
        "ambiguous_products": len(candidates),
        "llm_invocations": 0,
        "normalized_product_ids": [],
        "left_unknown_product_ids": [],
    }
    if not settings.llm_enabled or not candidates:
        report["left_unknown_product_ids"] = [c["id"] for c in candidates]
        return report

    call = llm or (lambda text: _call_llm(settings, text))
    for product in candidates:
        text = product["employment_criteria_text"]
        report["llm_invocations"] += 1
        try:
            categories = accept(call(text), text)
        except Exception:
            log.exception("llm normalization failed", extra={"product_id": product["id"]})
            categories = None
        if categories is None:
            report["left_unknown_product_ids"].append(product["id"])
            continue
        conn.execute(
            "UPDATE loan_products SET employment_types = %s, employment_types_source = 'llm_normalized',"
            " extraction_notes = extraction_notes || %s::jsonb, updated_at = now() WHERE id = %s",
            (categories, '["employment_types normalized from free text by LLM (evidence-checked)"]', product["id"]),
        )
        report["normalized_product_ids"].append(product["id"])
    return report
