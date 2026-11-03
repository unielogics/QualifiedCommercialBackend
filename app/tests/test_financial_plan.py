"""The plan is one record everything reads: which obligations the new loan
pays off, which payees are not operating spend, the payment it adds. Debt
service after, every coverage basis after, the month replay and the levers
all derive from it, so an action in one place changes everywhere."""

from __future__ import annotations

from app.services import financial_plan as fp
from app.services.financial_cockpit import build_cockpit

ONDECK = {"key": "ondeck", "payee": "ONDECK ACH 4471", "kind": "merchant_cash_advance", "kind_label": "Merchant cash advance", "cadence": "weekly", "monthly_average": 6750.0, "monthly": {"2026-05": 6750.0, "2026-06": 6750.0}, "is_debt": True, "is_card": False, "accounts": ["Chase ••1204"]}
WAYFLYER = {"key": "wayflyer", "payee": "WAYFLYER", "kind": "merchant_cash_advance", "kind_label": "Merchant cash advance", "cadence": "weekly", "monthly_average": 5147.0, "monthly": {"2026-05": 5147.0, "2026-06": 5147.0}, "is_debt": True, "is_card": False, "accounts": ["Chase ••1204"]}
CARD = {"key": "barclaycard us", "payee": "Barclaycard US", "kind": "credit_card", "kind_label": "Credit card", "cadence": "irregular", "monthly_average": 1750.0, "monthly": {"2026-05": 3500.0}, "is_debt": False, "is_card": True, "accounts": ["Chase ••1204"]}
WISE = {"key": "wise inc", "payee": "Wise Inc", "kind": "transfer", "kind_label": "Transfer", "cadence": "irregular", "monthly_average": 10917.0, "monthly": {"2026-05": 11700.0, "2026-06": 10134.0}, "is_debt": False, "is_card": False, "accounts": ["Chase ••1204"]}
RECURRING = {"payees": [ONDECK, WAYFLYER, CARD, WISE], "months_read": ["2026-05", "2026-06"]}
MONTHS = [
    {"month": "2026-05", "label": "May 2026", "deposits": 58000.0, "withdrawals": 73000.0, "net": -15000.0, "ending_balance": 1265.0, "low_balance": 1044.0},
    {"month": "2026-06", "label": "Jun 2026", "deposits": 77000.0, "withdrawals": 63000.0, "net": 14000.0, "ending_balance": 15850.0, "low_balance": 515.0},
]
SERVICE = {"monthly": 11897.0, "annual": 142764.0, "source": "bank_observed"}
DSCR = {"headline": {"key": "bank_cash_flow", "numerator": 131063.0, "value": 0.92}, "candidates": [{"key": "bank_cash_flow", "label": "Bank cash flow", "numerator": 131063.0, "value": 0.92}, {"key": "tax_return", "label": "Tax return 2025", "numerator": 70000.0, "value": 0.49}]}


def _row(id_, lender, monthly, balance=None, vendor_key=None, is_mca=True, count=True):
    return {"id": id_, "lender": lender, "category": "mca" if is_mca else "loan", "is_mca": is_mca, "monthly_payment": monthly, "balance": balance, "payoff_amount": None, "payment_frequency": "weekly", "count_in_dscr": count, "vendor_key": vendor_key, "origin": "admin"}


def test_obligations_join_rows_to_payees_by_ledger_key_then_name_and_keep_the_rest() -> None:
    debt = {"rows": [_row("r1", "On Deck Capital", 6700.0, 40000.0, vendor_key="ondeck"), _row("r2", "Kapitus", 2000.0, 9000.0, is_mca=False)], "documented": [], "service": {"source": "debt_schedule_rows+bank_observed", "monthly": 13847.0}}
    out = fp.obligations(debt, RECURRING)
    by = {o["id"]: o for o in out}
    assert by["row:r1"]["linked_payee_key"] == "ondeck" and by["row:r1"]["observed_monthly"] == 6750.0 and by["row:r1"]["in_debt_service"]
    assert by["row:r2"]["linked_payee_key"] is None and by["row:r2"]["payoff"] == 9000.0
    assert by["payee:wayflyer"]["kind"] == "payee" and by["payee:wayflyer"]["in_debt_service"] and by["payee:wayflyer"]["monthly"] == 5147.0
    assert by["payee:barclaycard us"]["in_debt_service"] is False  # cards are shown, not in the denominator
    assert "payee:ondeck" not in by  # linked, so not listed twice

    by_name = fp.obligations({"rows": [_row("r3", "Wayflyer Ltd", 5000.0)], "documented": [], "service": {"source": "debt_schedule_rows", "monthly": 5000.0}}, RECURRING)
    assert {o["id"]: o["linked_payee_key"] for o in by_name}["row:r3"] == "wayflyer"
    statements_only = fp.obligations({"rows": [], "documented": [], "service": SERVICE}, RECURRING)
    assert [o["id"] for o in statements_only if o["in_debt_service"]] == ["payee:ondeck", "payee:wayflyer"]


def test_evaluating_a_plan_retires_payments_excludes_transfers_and_replays_the_months() -> None:
    obligations = fp.obligations({"rows": [], "documented": [], "service": SERVICE}, RECURRING)
    plan = fp.normalize_plan({"retire": ["payee:ondeck", "payee:wayflyer"], "exclude": ["payee:wise inc"], "new_payment_monthly": 1200})
    result = fp.evaluate_plan(plan, obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=SERVICE, proposed=None, requested_amount=70000)
    assert result["active"] and result["retired_monthly"] == 11897.0 and result["payoff_total"] is None and result["payoff_unknown"] == 2
    assert result["excluded_monthly"] == 10917.0
    assert (result["new_payment_monthly"], result["new_payment_source"]) == (1200.0, None)
    assert result["debt_service_before_new_payment_monthly"] == 0.0 and result["debt_service_after_monthly"] == 1200.0
    bank = result["candidates"][0]
    assert bank["numerator_after"] == 131063.0 + 10917.0 * 12
    assert bank["value_after"] == round(bank["numerator_after"] / (1200 * 12), 2) and bank["status_after"] == "very_strong"
    tax = result["candidates"][1]
    assert tax["numerator_after"] == 70000.0 and tax["value_after"] == round(70000 / 14400, 2)
    assert result["headline_after"] == bank["value_after"] and result["headline_key"] == "bank_cash_flow"
    may, jun = result["replay"]
    assert may["removed"] == 11897.0 and may["added"] == 1200.0 and may["cumulative"] == 10697.0
    assert may["ending_after"] == 1265.0 + 10697.0 and jun["ending_after"] == 15850.0 + 2 * 10697.0
    assert may["lowest_after"] == 1044.0 + 0 + 10697.0 / 2
    assert result["cash_kept"] == 2 * 10697.0 and result["net_after_avg"] == round(((58000 - (73000 - 11897 + 1200)) + (77000 - (63000 - 11897 + 1200))) / 2, 2)
    assert result["loan_amount_needed"] == 70000.0


def test_the_new_payment_falls_back_to_loan_terms_then_the_term_sheet() -> None:
    obligations = fp.obligations({"rows": [], "documented": [], "service": SERVICE}, RECURRING)
    terms = fp.evaluate_plan(fp.normalize_plan({"new_loan_amount": 100000, "new_loan_rate_pct": 12, "new_loan_term_months": 120}), obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=SERVICE, proposed=None, requested_amount=None)
    assert (terms["new_payment_monthly"], terms["new_payment_source"]) == (1434.71, "manual_terms")
    sheet = fp.evaluate_plan(fp.normalize_plan({}), obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=SERVICE, proposed={"monthly_payment": 999.0}, requested_amount=None)
    assert (sheet["new_payment_monthly"], sheet["new_payment_source"], sheet["active"]) == (999.0, "term_sheet", False)
    nothing = fp.evaluate_plan(fp.normalize_plan({}), obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=SERVICE, proposed=None, requested_amount=None)
    assert nothing["new_payment_monthly"] is None and nothing["debt_service_after_monthly"] == 11897.0 and nothing["headline_after"] == 0.92


def test_levers_rank_what_raises_coverage_and_carry_an_apply_payload() -> None:
    obligations = fp.obligations({"rows": [_row("r1", "OnDeck", 6750.0, 40000.0, vendor_key="ondeck"), _row("r2", "Wayflyer", 5147.0, 30000.0, vendor_key="wayflyer")], "documented": [], "service": {"source": "debt_schedule_rows", "monthly": 11897.0, "annual": 142764.0}}, RECURRING)
    plan = fp.normalize_plan({})
    evaluation = fp.evaluate_plan(plan, obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service={"monthly": 11897.0, "annual": 142764.0, "source": "debt_schedule_rows"}, proposed=None, requested_amount=70000)
    levers = fp.coverage_levers(plan, evaluation, obligations_list=obligations, recurring=RECURRING, dscr=DSCR, requested_amount=70000, terms=None)
    keys = [lever["key"] for lever in levers]
    assert keys[0] == "refinance_advances"
    refinance = levers[0]
    assert refinance["apply"]["retire"] == ["row:r1", "row:r2"] and refinance["apply"]["new_loan_amount"] == 70000.0 and refinance["apply"]["new_loan_term_months"] == 120
    from app.services.financial_cockpit import amortized_payment

    payment = amortized_payment(70000.0, 16.0, 120)
    assert refinance["dscr_after"] == round(131063.0 / (payment * 12), 2) and refinance["improves"]
    assert "payoff:row:r1" in keys and "exclude:wise inc" in keys and "income_document" in keys
    exclude = next(lever for lever in levers if lever["key"] == "exclude:wise inc")
    assert exclude["dscr_after"] == round((131063.0 + 10917.0 * 12) / 142764.0, 2) and exclude["apply"] == {"exclude": ["payee:wise inc"]}
    assert all(levers[i]["dscr_after"] is None or levers[i + 1]["dscr_after"] is None or levers[i]["dscr_after"] >= levers[i + 1]["dscr_after"] for i in range(len(levers) - 1))
    assert len(levers) <= fp.MAX_LEVERS


def test_the_cockpit_carries_obligations_plan_and_levers() -> None:
    from app.tests.test_financial_cockpit import LOAN_ROW, SIX_MONTHS, TAX_2025

    cockpit = build_cockpit([*SIX_MONTHS, TAX_2025], [LOAN_ROW], plan={"retire": ["row:row1"], "new_payment_monthly": 500})
    assert [o["id"] for o in cockpit["debt"]["obligations"]] == ["row:row1"]
    plan = cockpit["plan"]
    assert plan["record"]["retire"] == ["row:row1"] and plan["evaluation"]["active"]
    assert plan["evaluation"]["retired_monthly"] == 2000 and plan["evaluation"]["debt_service_after_monthly"] == 500.0
    assert plan["evaluation"]["headline_after"] == round(70000 / 6000, 2)
    assert plan["evaluation"]["payoff_total"] == 50000 and plan["evaluation"]["loan_amount_needed"] == 50000.0
    assert isinstance(plan["levers"], list)
    assert build_cockpit([], [])["plan"]["evaluation"]["active"] is False


def test_refinancing_advances_without_payoff_balances_says_the_coverage_is_optimistic() -> None:
    obligations = fp.obligations({"rows": [], "documented": [], "service": SERVICE}, RECURRING)
    plan = fp.normalize_plan({})
    evaluation = fp.evaluate_plan(plan, obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=SERVICE, proposed=None, requested_amount=70000)
    levers = fp.coverage_levers(plan, evaluation, obligations_list=obligations, recurring=RECURRING, dscr=DSCR, requested_amount=70000, terms=None)
    refinance = next(lever for lever in levers if lever["key"] == "refinance_advances")
    assert refinance["uncertain"] is True and "not recorded for 2 of 2 advances" in refinance["detail"] and "optimistic" in refinance["detail"]
    assert all("uncertain" in lever for lever in levers)


def test_the_cockpit_read_model_keeps_the_plan_and_the_obligations() -> None:
    """A pydantic read model drops keys it does not declare; the dashboard
    crashed on a cockpit without `plan` for exactly that reason."""
    import uuid

    from app.schemas.financial_cockpit import FinancialCockpitRead
    from app.tests.test_financial_cockpit import LOAN_ROW, SIX_MONTHS

    cockpit = build_cockpit(SIX_MONTHS, [LOAN_ROW], plan={"retire": ["row:row1"]})
    read = FinancialCockpitRead(profile_id=uuid.uuid4(), **cockpit).model_dump()
    assert read["plan"]["record"]["retire"] == ["row:row1"] and "evaluation" in read["plan"] and "levers" in read["plan"]
    assert read["debt"]["obligations"][0]["id"] == "row:row1"
    assert read["bank"]["recurring"]["payees"] == [] and "overdrafts" in read["bank"]
