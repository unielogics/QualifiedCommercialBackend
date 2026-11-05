"""The recorded terms and the Financials plan mirror each other."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services import financial_plan as fp
from app.services import plan_terms_link as link


def _client_row(**over):
    base = dict(id=uuid.uuid4(), version=2, program_name="Term loan", amount=150000.0, apr_pct=12.0, term_months=60, payments_per_year=12.0, periodic_payment=3336.67, debt_service_treatment="additive", retained_annual_debt_service=None)
    base.update(over)
    return SimpleNamespace(**base)


def _sheet(**over):
    base = dict(id=uuid.uuid4(), version=1, funding_party_name="Quidity", approved_amount=400000.0, rate_pct=32.0, term_months=13, monthly_debt_service=36662.07, extra={"repayment_structure": "fully_amortizing", "debt_service_treatment": "additive"})
    base.update(over)
    return SimpleNamespace(**base)


def test_recording_terms_adopts_them_into_the_plan_with_the_sheet_s_own_treatment() -> None:
    recorded = link.recorded_from_client_terms(_client_row())
    assert recorded["kind"] == "client_terms" and recorded["payment_monthly"] == 3336.67 and recorded["label"] == "Client terms v2: Term loan"
    plan = fp.normalize_plan(link.adopt({"retire": ["payee:ondeck"], "add_backs": [{"kind": "owner_salary", "amount_monthly": 1000}]}, recorded, by=uuid.uuid4()))
    assert plan["new_payment_monthly"] == 3336.67 and plan["new_payment_source"] == "recorded_terms"
    assert (plan["new_loan_amount"], plan["new_loan_rate_pct"], plan["new_loan_term_months"], plan["new_loan_interest_only"]) == (150000.0, 12.0, 60, False)
    assert plan["terms_ref"]["id"] == recorded["id"] and plan["retire"] == ["payee:ondeck"] and plan["add_backs"][0]["kind"] == "owner_salary"
    assert plan["overrides"]["debt_service_monthly"] is None

    weekly = link.recorded_from_client_terms(_client_row(payments_per_year=51.96, periodic_payment=800.0))
    assert weekly["payment_monthly"] == round(800.0 * 51.96 / 12, 2)

    refinance = link.recorded_from_production_sheet(_sheet(extra={"repayment_structure": "interest_only", "debt_service_treatment": "refinance", "retained_annual_debt_service": 30180.0}))
    assert refinance["interest_only"] is True and refinance["treatment"] == "refinance"
    refi_plan = fp.normalize_plan(link.adopt({}, refinance))
    assert refi_plan["overrides"]["debt_service_monthly"] == 2515.0 and refi_plan["overrides"]["debt_service_note"].startswith("Refinance per Term sheet v1: Quidity")
    assert refi_plan["new_loan_interest_only"] is True
    # Re-recorded as additive: the refinance override goes, a desk-typed override would stay.
    back = fp.normalize_plan(link.adopt(refi_plan, link.recorded_from_production_sheet(_sheet())))
    assert back["overrides"]["debt_service_monthly"] is None


def test_withdrawing_the_adopted_terms_releases_the_plan_and_keeps_the_rest() -> None:
    sheet = _sheet()
    plan = fp.normalize_plan(link.adopt({"retire": ["row:r1"], "exclude": ["payee:wise inc"]}, link.recorded_from_production_sheet(sheet)))
    other = link.release(plan, str(uuid.uuid4()))
    assert other["new_payment_monthly"] == 36662.07  # a different sheet's withdrawal leaves it alone
    released = fp.normalize_plan(link.release(plan, str(sheet.id)))
    assert released["new_payment_monthly"] is None and released["new_loan_amount"] is None and released["terms_ref"] is None
    assert released["retire"] == ["row:r1"] and released["exclude"] == ["payee:wise inc"]


def test_the_cockpit_says_whether_the_plan_mirrors_the_recorded_terms() -> None:
    recorded = link.recorded_from_production_sheet(_sheet())
    none = link.terms_link(fp.normalize_plan({}), {"loan_terms": None, "new_payment_monthly": None}, None)
    assert none["state"] == "none"
    not_in_plan = link.terms_link(fp.normalize_plan({}), {"loan_terms": None, "new_payment_monthly": None}, recorded)
    assert not_in_plan["state"] == "recorded_not_in_plan" and "not in the plan" in not_in_plan["summary"]
    adopted = fp.normalize_plan(link.adopt({}, recorded))
    evaluation = {"loan_terms": {"amount": 400000.0, "rate_pct": 32.0, "term_months": 13, "interest_only": False}, "new_payment_monthly": 36662.07}
    mirrored = link.terms_link(adopted, evaluation, recorded)
    assert mirrored["state"] == "mirrored" and mirrored["adopted_from"]["version"] == 1
    changed = link.terms_link({**adopted, "new_loan_rate_pct": 28.0}, {**evaluation, "loan_terms": {**evaluation["loan_terms"], "rate_pct": 28.0}, "new_payment_monthly": 35800.0}, recorded)
    assert changed["state"] == "differs" and changed["differences"] == ["rate", "payment"]
    plan_only = link.terms_link(fp.normalize_plan({"new_loan_amount": 100000, "new_loan_rate_pct": 10, "new_loan_term_months": 60}), {"loan_terms": {"amount": 100000.0, "rate_pct": 10.0, "term_months": 60, "interest_only": False}, "new_payment_monthly": 2124.7}, None)
    assert plan_only["state"] == "plan_only"


def test_the_cockpit_read_model_carries_the_terms_link_and_names_the_recorded_terms() -> None:
    from app.services.financial_cockpit import build_cockpit
    from app.tests.test_financial_cockpit import LOAN_ROW, SIX_MONTHS, TAX_2025

    recorded = link.recorded_from_production_sheet(_sheet())
    plan = link.adopt({}, recorded)
    cockpit = build_cockpit([*SIX_MONTHS, TAX_2025], [LOAN_ROW], plan=plan, proposed_terms={"source": "term_sheet", "label": recorded["label"], "amount": 400000.0, "rate_pct": 32.0, "term_months": 13, "monthly_payment": 36662.07}, recorded_terms=recorded)
    assert cockpit["plan"]["terms_link"]["state"] == "mirrored"
    assert cockpit["plan"]["evaluation"]["new_payment_monthly"] == 36662.07 and cockpit["plan"]["evaluation"]["new_payment_source"] == "recorded_terms"
    words = [m["label"] for m in cockpit["plan"]["modifications"]]
    assert words == ["New payment 36,662 a month from Term sheet v1: Quidity"]
    assert not any(lever["key"].startswith("term_sheet:") for lever in cockpit["plan"]["levers"])
