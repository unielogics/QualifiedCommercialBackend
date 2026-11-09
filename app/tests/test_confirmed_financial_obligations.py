"""The obligation list is membership, not a second classifier of bank debits."""

from copy import deepcopy

import pytest

from app.services import financial_plan as plans
from app.services.financial_cockpit import debt_service


def row(key="saved", **values):
    return {"id": key, "lender": "Example Funding", "category": "loan", "is_mca": False,
            "monthly_payment": 400, "balance": 10000, "payoff_amount": None,
            "count_in_dscr": True, "vendor_key": None, "origin": "admin", **values}


def observations():
    return {"payees": [
        {"key": "example", "payee": "Example Funding LLC", "kind": "loan", "is_debt": True,
         "is_card": False, "monthly_average": 450, "cadence": "monthly"},
        {"key": "card", "payee": "Business Card", "kind": "credit_card", "is_debt": False,
         "is_card": True, "monthly_average": 1200, "cadence": "monthly"},
        {"key": "unknown", "payee": "Unidentified fixed debit", "kind": "merchant_cash_advance",
         "is_debt": True, "is_card": False, "monthly_average": 900, "cadence": "weekly"},
    ], "observed_debt_service_monthly": 1350}


def test_bank_classification_and_card_payments_do_not_create_schedule_membership():
    recurring = observations()
    before = deepcopy(recurring)
    service = debt_service([], None, [], recurring)
    assert service["source"] == "bank_observed" and service["monthly"] == 1350
    assert plans.obligations({"rows": [], "service": service}, recurring) == []
    assert recurring == before


@pytest.mark.parametrize("source,field", [("debt_schedule_document", "document"), ("advance_terms", "documented")])
def test_unreviewed_documentary_lines_remain_evidence_not_confirmed_obligations(source, field):
    document_row = {"lender": "Example Funding", "monthly_payment": 400, "balance": 10000}
    debt = {"rows": [], "service": {"source": source, "monthly": 400},
            field: {"debts": [document_row]} if field == "document" else [document_row]}
    assert plans.obligations(debt, observations()) == []


def test_same_lender_loans_remain_separate_and_observations_do_not_override_saved_values():
    rows = [row("first", vendor_key="example"), row("second", monthly_payment=None, balance=None)]
    debt = {"rows": rows, "service": {"source": "accepted_debt_schedule", "schedule_authoritative": True}}
    before = deepcopy(debt)
    result = plans.obligations(debt, observations())
    assert [item["id"] for item in result] == ["row:first", "row:second"]
    assert result[0]["monthly"] == 400 and result[0]["observed_monthly"] is None
    assert result[1]["monthly"] is None and result[1]["balance"] is None
    assert result[1]["linked_payee_key"] is None
    assert all(item["linked_payee_key"] is None and item["replay_requires_review"] for item in result)
    assert all(item["candidate_payee_keys"] == ["example"] for item in result)
    assert debt == before


def test_known_ai_draft_rows_require_acceptance_while_explicit_legacy_rows_survive():
    rows = [row("ai", origin="ai_draft"), row("legacy", origin=None), row("staff")]
    service = {"source": "debt_schedule_rows"}
    assert [item["id"] for item in plans.obligations({"rows": rows, "service": service}, {})] == ["row:legacy", "row:staff"]
    accepted = {"source": "accepted_debt_schedule", "schedule_authoritative": True}
    assert [item["id"] for item in plans.obligations({"rows": rows, "service": accepted}, {})] == ["row:ai", "row:legacy", "row:staff"]


def test_an_unconfirmed_previous_payoff_does_not_subtract_candidate_cash_flow():
    recurring = observations()
    service = {"source": "bank_observed", "monthly": 1350, "annual": 16200}
    plan = plans.normalize_plan({"retire": ["payee:unknown"]})
    result = plans.evaluate_plan(plan, obligations_list=plans.obligations({"rows": [], "service": service}, recurring),
                                 recurring=recurring, months=[], dscr={"candidates": []}, service=service,
                                 proposed=None, requested_amount=None)
    assert result["retired_monthly"] == 0
    assert result["debt_service_after_monthly"] == 1350
    assert result["excluded_monthly"] == 0
