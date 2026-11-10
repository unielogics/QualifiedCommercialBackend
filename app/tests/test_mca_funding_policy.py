from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.lead_types import intake_variant_for, normalize_funding_intent
from app.schemas.funding_program import FundingProgramVersionCreate
from app.services import application_programs as programs
from app.services import mca_funding_policy as policy
from app.services import requirement_collection_completion as completion
from app.services.financial_cockpit import tax_years
from app.services.funding_program_baselines import suggested_baseline
from app.services.funding_programs import _validate_mca_commercial_limits
from app.services.main_street_programs import compute_main_street_program_fit, documents_for
from app.services.program_rules import evaluate_rules, validate_rules


@pytest.fixture(autouse=True)
def fixed_date(monkeypatch):
    monkeypatch.setattr(policy, "current_date", lambda: date(2026, 11, 9))


def debt(key, **changes):
    return {"id": key, "lender": "Same lender LLC", "balance": 5000, "monthly_payment": 300,
            "debt_type": "mca", "mca_modified": False, **changes}


def tax_source(key="tax-2025", **changes):
    return {"file_id": key, "classification": "tax_return", "accepted": True,
            "key_facts": {"entity_name": "Example LLC", "tax_form": "1120S", "tax_year": "2025",
                          "ordinary_business_income": 0, **changes}}


def assess(rows=None, sources=None, **changes):
    args = {"schedule_body": {"debts": rows if rows is not None else [debt("a"), debt("b")]},
            "tax_records": [year for source in sources or [tax_source()]
                            for year in tax_years([source], business_entity="Example LLC")],
            "bank_months": policy.required_statement_months(), "business_entity": "Example LLC"}
    return policy.assess_evidence(**(args | changes))


def test_two_existing_distinct_loans_and_zero_income_are_allowed_before_new_funding():
    result = assess()
    assert result["auto_underwriting_allowed"]
    assert result["active_mca_count"] == 2
    assert result["prior_year_ordinary_business_income"] == 0
    assert result["mca_count_basis"] == "existing_before_new_advance"
    assert assess([debt(str(index)) for index in range(3)])["recommendation_status"] == "not_eligible"


def test_removed_duplicates_do_not_count_and_weekly_term_loans_are_not_mcas():
    rows = [debt("a"), debt("b"), debt("old", status="dismissed"),
            debt("copy", duplicate_of_id="a"), debt("term", debt_type="term_loan", payment_frequency="weekly")]
    original = deepcopy(rows)
    result = assess(rows)
    assert result["auto_underwriting_allowed"] and result["active_mca_count"] == 2
    assert rows == original
    assert assess([debt("worksheet", mca_modified="no")])["auto_underwriting_allowed"]
    assert assess([debt("worksheet", mca_modified="yes")])["recommendation_status"] == "not_eligible"


@pytest.mark.parametrize("value,status", [(True, "not_eligible"), (None, "needs_information"),
                                          ("false", "needs_information"), (0, "needs_information")])
def test_unknown_modified_status_never_becomes_false(value, status):
    assert assess([debt("a", mca_modified=value)])["recommendation_status"] == status


def test_unknown_type_and_draft_only_schedule_require_review():
    assert assess([debt("a", debt_type="loan")])["active_mca_count"] is None
    assert assess(schedule_body=None)["recommendation_status"] == "needs_information"
    assert assess(schedule_body={"no_debt": True})["auto_underwriting_allowed"]


def test_negative_ordinary_income_cannot_be_cured_by_addbacks_personal_or_older_returns():
    sources = [tax_source(ordinary_business_income=-10, interest=500, depreciation=500),
               tax_source("old", tax_year="2024", ordinary_business_income=100000),
               tax_source("personal", tax_form="1040", ordinary_business_income=900000),
               tax_source("other", entity_name="Other LLC", ordinary_business_income=900000)]
    result = assess(sources=sources)
    assert result["recommendation_status"] == "not_eligible"
    assert result["prior_year_ordinary_business_income"] == -10
    assert result["source_refs"]["tax_files"] == ["tax-2025"]


@pytest.mark.parametrize("source", [tax_source(tax_year="2024"), tax_source(tax_form="1040"),
                                    tax_source(entity_name="Another Business"),
                                    tax_source(ordinary_business_income=None, net_income=60000, taxable_income=70000)])
def test_missing_correct_year_entity_or_income_measure_does_not_pass(source):
    assert assess(sources=[source])["recommendation_status"] == "needs_information"


def test_unaccepted_and_conflicting_returns_do_not_pass():
    assert assess(sources=[tax_source() | {"accepted": False}])["recommendation_status"] == "needs_information"
    assert assess(sources=[tax_source(), tax_source("amended", ordinary_business_income=-1)])["recommendation_status"] == "needs_information"
    records = tax_years([tax_source(), tax_source("amended", ordinary_business_income=-1)], business_entity="Example LLC")
    # Even if the extractor selects one conflicting source for its year,
    # accepted amendment selection must be resolved before claiming a pass.
    direct_conflict = [{**records[0], "ordinary_business_income": 0}, {**records[0], "ordinary_business_income": -1}]
    assert assess(tax_records=direct_conflict)["recommendation_status"] != "recommended"


def test_last_six_completed_months_not_six_files_or_six_stale_months():
    assert policy.required_statement_months() == ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    assert assess(bank_months=["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"])["recommendation_status"] == "needs_information"
    assert assess(bank_months=policy.required_statement_months()[:-1])["missing_statement_months"] == ["2026-10"]
    complete = policy.required_statement_months()
    partial = {"account-1234": complete, "account-5678": complete[:-1]}
    result = assess(bank_accounts=partial)
    assert not result["auto_underwriting_allowed"]
    assert result["missing_statement_months_by_account"]["account-5678"] == ["2026-10"]
    assert assess(bank_accounts={"account-1234": complete, "account-5678": complete})["auto_underwriting_allowed"]
    assert not assess(bank_accounts={})["auto_underwriting_allowed"]


def test_all_industries_get_exact_three_documents_and_separate_evidence_policy():
    for industry in ("restaurant_food_service", "trucking_logistics", "professional_practice", "other"):
        assert len(documents_for(policy.PROGRAM_KEY, industry)) == 3
    assert normalize_funding_intent(policy.PROGRAM_KEY) == policy.PROGRAM_KEY
    assert intake_variant_for("main_street", policy.PROGRAM_KEY) == "main_street_v1"
    assert programs._policy_key_for_profile(SimpleNamespace(vertical="main_street", funding_category=policy.PROGRAM_KEY)) == policy.EVIDENCE_POLICY_KEY
    assert programs._policy_key_for_profile(SimpleNamespace(vertical="mca", funding_category="mca_refinance")) == "mca_baseline"
    baseline = suggested_baseline(policy.PROGRAM_KEY)
    draft = FundingProgramVersionCreate(rules=baseline["rules"], requirements=baseline["requirements"], confirmed=True)
    validate_rules(draft.rules, enforce_supported_fields=True)
    assert len(draft.requirements) == 3
    assert {row.requirement_key for row in draft.requirements} == {"business_bank_statements_6_months", "business_debt_schedule", "business_tax_returns_1_years"}
    assert baseline["commercial_terms"]["pricing_basis"] == "factor_rate"
    assert baseline["commercial_terms"]["minimum_pricing"] == 1.22
    assert baseline["commercial_terms"]["maximum_pricing"] == 1.38
    assert baseline["scopes"][0]["excluded_naics_prefixes"] == []
    assert len(suggested_baseline("mca_refinance")["requirements"]) > 3
    screen = compute_main_street_program_fit(intent=policy.PROGRAM_KEY, key_metrics={"estimated_dscr": 10, "annual_ebitda": 1000000})
    assert set(screen) == {policy.PROGRAM_KEY} and not screen[policy.PROGRAM_KEY]["eligible"]


def test_published_rules_keep_zero_and_false_and_reject_unknown():
    rules = suggested_baseline(policy.PROGRAM_KEY)["rules"]
    facts = {"mca_funding_active_count": 2, "mca_funding_has_modified_mca": False,
             "mca_funding_prior_year_ordinary_income": 0, "mca_funding_last_six_months_complete": True}
    assert evaluate_rules(rules, facts).matched
    assert not evaluate_rules(rules, facts | {"mca_funding_has_modified_mca": None}).matched
    assert not evaluate_rules(rules, facts | {"mca_funding_prior_year_ordinary_income": -0.01}).matched


@pytest.mark.parametrize("basis,low,high", [("apr", 1.22, 1.38), ("factor_rate", 1.21, 1.38),
                                          ("factor_rate", 1.22, 1.39), ("factor_rate", None, 1.38)])
def test_standard_product_pricing_bounds_do_not_change_other_lenders_source_factors(basis, low, high):
    values = SimpleNamespace(pricing_basis=basis, minimum_pricing=low, maximum_pricing=high)
    with pytest.raises(HTTPException):
        _validate_mca_commercial_limits(policy.PROGRAM_KEY, values)
    _validate_mca_commercial_limits("another_lenders_source_loan", values)
    assert assess([debt("a", factor_rate=1.49)])["auto_underwriting_allowed"]


def test_prior_calendar_year_collection_cannot_fall_back_to_old_return():
    requirement = SimpleNamespace(requirement_key="business_tax_returns_1_years", canonical_key=None,
                                 collection_config={"required_years": 1, "reporting_period": "prior_calendar_year"})
    files = [SimpleNamespace(id="old"), SimpleNamespace(id="current")]
    analyses = {"old": SimpleNamespace(analysis={"key_facts": tax_source(tax_year="2024")["key_facts"]}),
                "current": SimpleNamespace(analysis={"key_facts": tax_source()["key_facts"]})}
    assert completion.tax_eligible_files(requirement, files, analyses, expected_entity="Example LLC") == [files[1]]
    assert completion.tax_eligible_files(requirement, files[:1], analyses, expected_entity="Example LLC") == []


def test_automatic_selection_cannot_replace_requested_mca_with_generic_products():
    candidates = [SimpleNamespace(program_key="ez_term", eligible=True),
                  SimpleNamespace(program_key=policy.PROGRAM_KEY, eligible=False)]
    profile = SimpleNamespace(funding_category=policy.PROGRAM_KEY)
    assert programs._automatic_candidates(profile, candidates) == []
    candidates[1].eligible = True
    assert programs._automatic_candidates(profile, candidates) == [candidates[1]]
