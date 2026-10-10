"""The LLM is always faked here; no network calls are made."""

import pytest

from app.config import get_settings
from app.matching.llm_normalizer import EmploymentNormalization, accept, normalize_ambiguous_products
from tests.helpers import add_product

TEXT = "Employees of private limited companies, public sector undertakings and government bodies"


def test_accept_requires_confidence_and_verbatim_evidence():
    good = EmploymentNormalization(
        employment_types=["salaried"], confident=True, evidence="Employees of private limited companies"
    )
    assert accept(good, TEXT) == ["salaried"]
    assert accept(good.model_copy(update={"confident": False}), TEXT) is None
    assert accept(good.model_copy(update={"evidence": "salaried professionals only"}), TEXT) is None
    assert accept(good.model_copy(update={"employment_types": []}), TEXT) is None
    assert accept(None, TEXT) is None


@pytest.mark.db
def test_disabled_llm_leaves_criteria_unknown(db, settings):
    pid = add_product(db, product_name="amb", employment_criteria_text=TEXT)
    report = normalize_ambiguous_products(settings, db, llm=lambda t: pytest.fail("LLM must not be called"))
    assert report == {
        "llm_enabled": False,
        "ambiguous_products": 1,
        "llm_invocations": 0,
        "normalized_product_ids": [],
        "left_unknown_product_ids": [pid],
    }


@pytest.mark.db
def test_enabled_llm_normalizes_once_per_product(db, settings, monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    get_settings.cache_clear()
    s = get_settings()
    good = add_product(db, product_name="good", employment_criteria_text=TEXT)
    bad = add_product(db, product_name="bad", employment_criteria_text="Customers with a stable profile")
    add_product(db, product_name="clear", employment_types=["salaried"], employment_criteria_text="Salaried")

    calls = []

    def fake_llm(text):
        calls.append(text)
        if text == TEXT:
            return EmploymentNormalization(
                employment_types=["salaried"], confident=True, evidence="public sector undertakings"
            )
        return EmploymentNormalization(employment_types=["salaried"], confident=True, evidence="invented quote")

    report = normalize_ambiguous_products(s, db, llm=fake_llm)
    assert report["llm_invocations"] == 2 and len(calls) == 2  # the already-structured product is skipped
    assert report["normalized_product_ids"] == [good] and report["left_unknown_product_ids"] == [bad]
    row = db.execute(
        "SELECT employment_types, employment_types_source FROM loan_products WHERE id = %s", (good,)
    ).fetchone()
    assert row == {"employment_types": ["salaried"], "employment_types_source": "llm_normalized"}
    # Normalization is stored, so the next run only retries the unresolved product.
    assert normalize_ambiguous_products(s, db, llm=fake_llm)["llm_invocations"] == 1


@pytest.mark.db
def test_llm_exception_leaves_unknown(db, settings, monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    get_settings.cache_clear()
    pid = add_product(db, product_name="amb", employment_criteria_text=TEXT)

    def broken(_):
        raise TimeoutError("llm down")

    report = normalize_ambiguous_products(get_settings(), db, llm=broken)
    assert report["left_unknown_product_ids"] == [pid]
