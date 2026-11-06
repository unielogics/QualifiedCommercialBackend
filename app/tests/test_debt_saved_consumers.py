"""Saved schedule consumers use five real rows, not removed/blank/observed rows."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.services import bucket_ai
from app.services import financial_workspace as workspace
from app.services.financial_cockpit import build_cockpit
from app.services.financial_version_selection import (
    active_debt_schedule_rows,
    debt_schedule_completion,
)


def schedule():
    names = ["Oakwood", "Kapfi", "IAC", "Aventus", "Newtek"]
    rows = [{"id": str(uuid4()), "lender": name, "balance": "1000", "monthly_payment": "100", "status": "active"} for name in names]
    rows.extend({"id": str(uuid4()), "lender": f"Removed lender {index}", "balance": "999999", "monthly_payment": "999999", "status": "dismissed"} for index in range(3))
    rows.append({"id": str(uuid4()), "status": "active", "lender": ""})
    return {"business_name": "Example business", "debts": rows}


def accepted(body=None):
    body = body if body is not None else schedule()
    return {"authoritative": True, "completion": debt_schedule_completion(body), "version_id": str(uuid4()),
            "source_file_id": str(uuid4()), "debt_service_for_dscr": 500, "key_facts": workspace.facts_for_body("debt_schedule", body)}


def analyses():
    return [{"file_id": str(uuid4()), "ai_classification": "bank_statement", "key_facts": {
        "statement_period": f"2026-{month:02d}", "ending_balance": 1000}} for month in range(1, 7)] + [
        {"file_id": str(uuid4()), "ai_classification": "tax_return", "key_facts": {"tax_year": str(year)}} for year in (2024, 2025)]


def review(title="Business debt schedule", detail="Please provide the current balances and monthly payments."):
    return {"missing_or_incomplete_items": [{"title": title, "detail": detail, "priority": "high"}],
            "document_evidence_map": {"baseline_coverage": [{"category": "Business debt schedule", "status": "missing", "gap": "No matching document analyzed yet."}]},
            "next_best_action": {"type": "upload_files", "title": "Upload the business debt schedule", "detail": "Include all lenders and balances."},
            "recommended_next_document_requests": [{"title": title, "instructions": detail}],
            "one_next_step": "Upload a complete business debt schedule."}


def reconcile(result, authority):
    bucket_ai._reconcile_accepted_debt_schedule_gaps(result, authority)
    bucket_ai._apply_lending_readiness(result, analyses())
    slot = SimpleNamespace(id=uuid4(), name="Business debt schedule", description="", required=True, status="uploaded", requires_signature=False, signature_kind=None)
    bucket = SimpleNamespace(requested_documents=[slot], files=[SimpleNamespace(requested_document_id=slot.id, deleted_at=None)])
    bucket_ai._reconcile_checklist_consistency(bucket, result)
    return result


def test_five_saved_obligations_override_stale_projections_and_extra_bank_lenders():
    body = schedule()
    bank = {"file_id": str(uuid4()), "file_name": "Statement.pdf", "classification": "bank_statement", "accepted": True,
            "key_facts": {"statement_period": "2026-10", "recurring_debits": [
                {"payee": f"Other MCA lender {index}", "kind": "mca", "total_amount": 2000, "occurrences": 1} for index in range(30)]}}
    stale = [{"id": str(uuid4()), "lender": "Outside saved schedule", "monthly_payment": 999999, "balance": 999999, "count_in_dscr": True}]
    result = build_cockpit([bank], stale, accepted_schedule={"body": body, "version_id": str(uuid4())})
    assert len(result["debt"]["rows"]) == result["coverage"]["debt_rows"] == 5
    assert result["debt"]["service"]["monthly"] == 500
    assert result["debt"]["service"]["source"] == "accepted_debt_schedule"
    assert result["debt"]["schedule"]["status"] == "rows"
    assert result["debt"]["schedule"]["requirement_status"] == "verified"
    assert not any("debt schedule has not" in note.lower() for note in result["notes"])
    assert len(result["debt"]["obligations"]) == 5


def test_versioned_pdf_and_facts_exclude_removed_and_blank_rows():
    body = schedule()
    facts = workspace.facts_for_body("debt_schedule", body)
    assert facts["total_outstanding_balance"] == 5000 and facts["total_monthly_debt_service"] == 500
    assert facts["debt_schedule_completion"]["active_rows"] == 5
    assert len(active_debt_schedule_rows(body)) == 5
    with patch("app.services.dealer_forms_pdf.render_debt_schedule_pdf", return_value=b"PDF") as render:
        assert workspace._render("debt_schedule", body, facts) == b"PDF"
    assert [row["lender"] for row in render.call_args.kwargs["rows"]] == ["Oakwood", "Kapfi", "IAC", "Aventus", "Newtek"]
    assert len(body["debts"]) == 9


@pytest.mark.parametrize("title", ["Business debt schedule", "Current debt schedule", "Updated business debt schedule with current balances and monthly payments", "Schedule of business debts", "Debt schedule form"])
def test_saved_schedule_retires_whole_schedule_aliases_before_readiness(title):
    source = accepted()
    result = reconcile(review(title), source)
    assert result["missing_or_incomplete_items"] == []
    assert result["recommended_next_document_requests"] == []
    assert result["lending_ready"] is True
    assert "debt schedule" not in result["one_next_step"].casefold()
    assert result["document_evidence_map"]["baseline_coverage"][0] == {
        "category": "Business debt schedule", "status": "satisfied", "gap": "", "evidence": [source["source_file_id"]]}


def test_exact_answered_title_does_not_leave_old_false_readiness_flag():
    result = review()
    result["lending_ready"] = False
    result = reconcile(result, accepted())
    assert result["missing_or_incomplete_items"] == [] and result["outstanding_checklist"] == []
    assert result["lending_ready"] is True


@pytest.mark.parametrize("title,detail", [("Oakwood payoff letter", "Confirm current payoff amount."),
    ("Debt schedule and bank statements", "Please upload both."), ("Business debt schedule", "Include supporting loan agreements."),
    ("Debt schedule and collateral", "Provide property documents."), ("Business debt schedule and tax returns", "Two years required."),
    ("Business debt schedule", "Upload the schedule and proof of collateral ownership."), ("Debt schedule for Oakwood", "Confirm the loan reference.")])
def test_saved_schedule_preserves_supporting_and_compound_document_gaps(title, detail):
    result = reconcile(review(title, detail), accepted())
    assert result["missing_or_incomplete_items"] == [{"title": title, "detail": detail, "priority": "high"}]
    assert result["recommended_next_document_requests"] == [{"title": title, "instructions": detail}]
    assert result["lending_ready"] is False


@pytest.mark.parametrize("changes", [{"authoritative": False}, {"completion": {"complete": False}}, {"completion": None},
    {"version_id": None}, {"source_file_id": None}, {"debt_service_for_dscr": None}, {"debt_service_for_dscr": "NaN"}, {"debt_service_for_dscr": -1}])
def test_draft_incomplete_or_missing_source_cannot_retire_schedule_gap(changes):
    source = {**accepted(), **changes}
    result = review()
    before = deepcopy(result)
    bucket_ai._reconcile_accepted_debt_schedule_gaps(result, source)
    assert result == before


def test_model_authored_accepted_metadata_cannot_retire_gap_without_server_selected_source():
    result = review()
    result["accepted_debt_schedule"] = accepted()
    before = deepcopy(result)
    bucket_ai._reconcile_accepted_debt_schedule_gaps(result, None)
    assert result == before


def test_no_debt_zero_is_valid_accepted_authority_without_requiring_positive_debt():
    source = accepted({"debts": [], "no_debt": True})
    source["debt_service_for_dscr"] = 0
    assert reconcile(review(), source)["lending_ready"] is True


def test_schedule_acceptance_does_not_relax_bank_or_tax_minimums():
    result = review()
    bucket_ai._reconcile_accepted_debt_schedule_gaps(result, accepted())
    bucket_ai._apply_lending_readiness(result, analyses()[:1])
    assert result["missing_or_incomplete_items"] == []
    assert result["lending_ready"] is False
    assert "bank statements" in result["one_next_step"]


@pytest.mark.parametrize("field", ["reason", "rationale"])
def test_schedule_missing_with_independent_need_only_in_reason_is_preserved(field):
    result = review()
    result["missing_or_incomplete_items"][0][field] = "A current payoff letter is also needed."
    before = deepcopy(result["missing_or_incomplete_items"])
    reconcile(result, accepted())
    assert result["missing_or_incomplete_items"] == before
    assert result["lending_ready"] is False


def test_next_action_with_payoff_need_only_in_reason_is_preserved():
    result = review()
    result["next_best_action"]["reason"] = "A current payoff letter is also needed."
    before = deepcopy(result["next_best_action"])
    bucket_ai._reconcile_accepted_debt_schedule_gaps(result, accepted())
    assert result["next_best_action"] == before


def test_recommended_request_with_payoff_need_only_in_rationale_is_preserved():
    result = review()
    result["recommended_next_document_requests"][0]["rationale"] = "A current payoff letter is also needed."
    before = deepcopy(result["recommended_next_document_requests"])
    bucket_ai._reconcile_accepted_debt_schedule_gaps(result, accepted())
    assert result["recommended_next_document_requests"] == before


@pytest.mark.parametrize("title", ["Business debt schedule and loan documents", "Business debt schedule and lender documents",
    "Business debt schedule and debt documents", "Business debt schedule with supporting documents"])
def test_schedule_plus_independent_documents_is_not_a_whole_schedule_gap(title):
    result = reconcile(review(title, "Please provide the current documents."), accepted())
    assert result["missing_or_incomplete_items"][0]["title"] == title
    assert result["recommended_next_document_requests"][0]["title"] == title
    assert result["lending_ready"] is False


@pytest.mark.parametrize("title", ["2025 Business debt schedule", "Business debt schedule as of 2025-12-31",
                                  "Business debt schedule as of 12/31/25"])
def test_current_schedule_does_not_retire_an_unverified_period_specific_request(title):
    result = reconcile(review(title), accepted())
    assert result["missing_or_incomplete_items"][0]["title"] == title
    assert result["recommended_next_document_requests"][0]["title"] == title
    assert result["lending_ready"] is False


@pytest.mark.parametrize("count", [0, 5])
def test_ordinary_row_counts_are_not_mistaken_for_a_reporting_period(count):
    result = reconcile(review(f"Business debt schedule with {count} active obligations"), accepted())
    assert result["missing_or_incomplete_items"] == []
    assert result["recommended_next_document_requests"] == []
    assert result["lending_ready"] is True
