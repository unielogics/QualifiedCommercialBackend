"""Expense drafts preserve source period, identity, scope and unknown values."""

from copy import deepcopy

from app.services.operating_plan_evidence import build_evidence
from app.services.operating_plan_guidance import (
    build_guidance,
    client_evaluation,
    client_evidence,
    reconcile_groups,
)
from app.tests.test_financial_metric_review import build
from app.tests.test_noi_governance_consumers import annual_pnl, business_tax


def source_with_costs(**values):
    source = business_tax()
    source["key_facts"].update(advertising=12000, rent=36000, salaries_and_wages=240000,
                               officer_compensation=60000, total_deductions=500000, **values)
    return source


def test_named_tax_expenses_are_drafts_with_one_total_and_multiple_salary_groups():
    source = source_with_costs()
    cockpit = build([source])
    guidance, candidates = build_guidance([source], cockpit, business_name="Example LLC", naics_code="621340")
    groups = guidance["expense_groups"]
    assert next(row for row in groups if row["category"] == "marketing")["baseline_annual_amount"] == 12000
    assert len([row for row in groups if row["category"] == "salaries"]) == 2
    assert not any("deductions" in row["key"] for row in groups)
    assert all(row["source_candidate_key"] == row["key"] for row in candidates)
    assert all(row["cadence"] == "annual" for row in candidates)
    assert "therapist" in " ".join(guidance["business"]["questions"])
    assert all(row["source_file_ids"] == [source["file_id"]] for row in groups)


def test_current_source_does_not_combine_prior_year_or_forecast_expense_totals():
    source = source_with_costs()
    old = deepcopy(source)
    old.update(file_id="older-source", analysis_id="older-analysis")
    old["key_facts"].update(tax_year="2024", period_start="2024-01-01", period_end="2024-12-31", advertising=999999)
    future = deepcopy(source)
    future.update(file_id="future-source", analysis_id="future-analysis")
    future["key_facts"].update(tax_year="2027", period_start="2027-01-01", period_end="2027-12-31", advertising=888888)
    guidance, _ = build_guidance([old, source, future], build([old, source, future]))
    assert {row["year"] for row in guidance["expense_groups"]} == {2025}
    assert next(row for row in guidance["expense_groups"] if row["category"] == "marketing")["baseline_annual_amount"] == 12000


def test_missing_cost_is_not_zero_and_known_zero_remains_a_valid_reference():
    source = business_tax()
    source["key_facts"].update(advertising=0)
    guidance, candidates = build_guidance([source], build([source]))
    assert next(row for row in candidates if row["category"] == "marketing")["current_amount"] == 0
    assert not any(row["category"] == "rent" for row in candidates)
    assert any(row["category"] == "rent" for row in guidance["essentials"])


def test_source_total_wins_over_duplicate_itemization_and_list_only_detail_is_supported():
    source = source_with_costs()
    source["key_facts"]["expense_lines"] = [{"category": "advertising", "amount": 7000}, {"category": "advertising", "amount": 5000}]
    guidance, _ = build_guidance([source], build([source]))
    assert next(row for row in guidance["expense_groups"] if row["category"] == "marketing")["baseline_annual_amount"] == 12000
    del source["key_facts"]["advertising"]
    guidance, _ = build_guidance([source], build([source]))
    assert next(row for row in guidance["expense_groups"] if row["category"] == "marketing")["baseline_annual_amount"] == 12000


def test_partial_year_pnl_is_explicitly_annualized_and_unrelated_tax_is_not_adopted():
    source = annual_pnl(period_end="2025-06-30", rent=6000)
    guidance, candidates = build_guidance([source], build([source]))
    rent = next(row for row in candidates if row["category"] == "rent")
    assert rent["current_amount"] == 12000
    assert "run-rate" in rent["warning"]
    unrelated = source_with_costs()
    unrelated["key_facts"]["business_name"] = "Unrelated Company LLC"
    unrelated["key_facts"]["entity_name"] = "Unrelated Company LLC"
    guidance, _ = build_guidance([unrelated], build([unrelated]))
    assert guidance["expense_groups"] == []


def test_split_lines_reconcile_without_becoming_an_automatic_noi_change():
    source = source_with_costs()
    guidance, _ = build_guidance([source], build([source]))
    group = next(row for row in guidance["expense_groups"] if row["category"] == "marketing")
    rows = [{"category": "marketing", "label": "Ads", "current_amount": 700, "cadence": "monthly", "source_group_key": group["key"]},
            {"category": "marketing", "label": "Agency", "current_amount": 300, "cadence": "monthly", "source_group_key": group["key"]}]
    result = next(row for row in reconcile_groups({"expenses": rows}, {"guidance": guidance}) if row["key"] == group["key"])
    assert result["disclosed_annual_amount"] == 12000 and result["status"] == "matches_source"
    rows[1]["current_amount"] = 400
    result = next(row for row in reconcile_groups({"expenses": rows}, {"guidance": guidance}) if row["key"] == group["key"])
    assert result["difference_annual"] == 1200 and result["status"] == "differs_from_source"
    assert "not an automatic" in " ".join(result["notes"])
    rows[1]["current_amount"] = None
    assert next(row for row in reconcile_groups({"expenses": rows}, {"guidance": guidance}) if row["key"] == group["key"])["disclosed_annual_amount"] is None


def test_multiperiod_pnl_uses_only_the_selected_column_and_ambiguous_columns_stay_unknown():
    source = annual_pnl()
    cockpit = build([source])
    source["key_facts"]["rent"] = 999999
    current = {**source["key_facts"], "rent": 36000}
    prior = {**current, "period_start": "2024-01-01", "period_end": "2024-12-31", "rent": 24000}
    source["key_facts"]["income_statements"] = [prior, current]
    guidance, _ = build_guidance([source], cockpit)
    assert next(row for row in guidance["expense_groups"] if row["category"] == "rent")["baseline_annual_amount"] == 36000
    source["key_facts"]["income_statements"].append(deepcopy(current))
    assert not build_guidance([source], cockpit)[0]["expense_groups"]


def test_client_evidence_exposes_relevant_sources_not_the_entire_file_room():
    source = source_with_costs()
    evidence = build_evidence([source], build([source]))
    evidence["files"].append({"file_id": "private-credit", "file_name": "Private credit report", "eligible_for_guidance": True,
                              "analysis_id": "PRIVATE_ANALYSIS", "content_hash": "PRIVATE_HASH"})
    public = client_evidence(evidence)
    assert "private-credit" not in str(public)
    assert "analysis_id" not in str(public) and "content_hash" not in str(public)
    assert source["file_id"] in str(public)


def test_client_evaluation_removes_authority_and_private_review_fields():
    from app.schemas.earnings_improvement import empty_body
    from app.services.earnings_improvement import evaluate

    source = source_with_costs()
    cockpit = build([source])
    cockpit["operating_evidence"] = build_evidence([source], cockpit)
    safe = client_evaluation(evaluate(empty_body(), cockpit))
    assert "context_stamp" not in safe
    assert "authority_stamp" not in str(safe)
    assert "actions" in safe and "expense_groups" in safe


def test_underwriting_packet_preserves_marketing_inputs_and_first_year_noi():
    from app.services.public_underwriting_packet_pdf import _operating_plan_lines
    from app.tests.test_operating_guidance_reporting import reporting_fixture

    _, financials = reporting_fixture()
    text = "\n".join(_operating_plan_lines(financials))
    assert "first-year NOI $-9,000" in text
    assert "revenue return multiple: 3" in text
    assert "product name: Example service" in text
    assert "incremental spend: 1000" in text
    assert "Expense clarification: Source advertising" in text
    assert "recurring improvement $6,000 per year" in text


def test_investor_marketing_effect_is_explained_as_a_revenue_assumption_after_spend():
    from types import SimpleNamespace
    from unicodedata import normalize

    import fitz

    from app.services.investor_presentation_pdf import render_investor_presentation_pdf
    from app.tests.test_investor_presentation import synthetic_financials
    from app.tests.test_operating_guidance_reporting import reporting_fixture

    _, financials = reporting_fixture()
    data = synthetic_financials()
    data["operating_earnings"]["improvement_plan"] = financials["improvement_plan"]
    raw = render_investor_presentation_pdf(
        intake=SimpleNamespace(business_name="Synthetic services LLC", variant="mca_refinance_v1",
                               requested_loan_amount=180000, intake_state={}), financials=data,
    )
    document = fitz.open(stream=raw, filetype="pdf")
    text = normalize("NFKC", " ".join(" ".join(page.get_text().split()) for page in document))
    assert "assumed 3.0x revenue" in text and "50.0% contribution margin" in text
    assert "added spend deducted" in text
