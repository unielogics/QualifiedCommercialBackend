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
CONFIRMED_SERVICE = {**SERVICE, "source": "debt_schedule_rows"}
DSCR = {"headline": {"key": "bank_cash_flow", "numerator": 131063.0, "value": 0.92}, "candidates": [{"key": "bank_cash_flow", "label": "Bank cash flow", "numerator": 131063.0, "value": 0.92}, {"key": "tax_return", "label": "Tax return 2025", "numerator": 70000.0, "value": 0.49}]}


def _row(id_, lender, monthly, balance=None, vendor_key=None, is_mca=True, count=True):
    return {"id": id_, "lender": lender, "category": "mca" if is_mca else "loan", "is_mca": is_mca, "monthly_payment": monthly, "balance": balance, "payoff_amount": None, "payment_frequency": "weekly", "count_in_dscr": count, "vendor_key": vendor_key, "origin": "admin"}


def _confirmed_obligations():
    return fp.obligations({"rows": [
        _row("ondeck", "ONDECK ACH 4471", 6750.0, vendor_key="ondeck"),
        _row("wayflyer", "WAYFLYER", 5147.0, vendor_key="wayflyer"),
    ], "service": CONFIRMED_SERVICE}, RECURRING)


def test_obligations_link_explicit_rows_without_promoting_unmatched_payees() -> None:
    debt = {"rows": [_row("r1", "On Deck Capital", 6700.0, 40000.0, vendor_key="ondeck"),
                     _row("r2", "Kapitus", 2000.0, 9000.0, is_mca=False)],
            "documented": [], "service": {"source": "debt_schedule_rows+bank_observed", "monthly": 13847.0}}
    out = fp.obligations(debt, RECURRING)
    by = {o["id"]: o for o in out}
    assert set(by) == {"row:r1", "row:r2"}
    assert by["row:r1"]["linked_payee_key"] == "ondeck" and by["row:r1"]["observed_monthly"] == 6750.0
    assert by["row:r1"]["in_debt_service"]
    assert by["row:r2"]["linked_payee_key"] is None and by["row:r2"]["payoff"] == 9000.0
    by_name = fp.obligations({"rows": [_row("r3", "Wayflyer Ltd", 5000.0)], "service": CONFIRMED_SERVICE}, RECURRING)
    assert by_name[0]["linked_payee_key"] == "wayflyer"
    assert fp.obligations({"rows": [], "documented": [], "service": SERVICE}, RECURRING) == []


def test_evaluating_a_plan_retires_payments_excludes_transfers_and_replays_the_months() -> None:
    obligations = _confirmed_obligations()
    plan = fp.normalize_plan({"retire": ["row:ondeck", "row:wayflyer"], "exclude": ["payee:wise inc"], "new_payment_monthly": 1200})
    result = fp.evaluate_plan(plan, obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=CONFIRMED_SERVICE, proposed=None, requested_amount=70000)
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
    obligations = _confirmed_obligations()
    terms = fp.evaluate_plan(fp.normalize_plan({"new_loan_amount": 100000, "new_loan_rate_pct": 12, "new_loan_term_months": 120}), obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=CONFIRMED_SERVICE, proposed=None, requested_amount=None)
    assert (terms["new_payment_monthly"], terms["new_payment_source"]) == (1434.71, "manual_terms")
    # A term sheet on file is not a decision: nothing is added until the desk adopts it.
    on_file = fp.evaluate_plan(fp.normalize_plan({}), obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=CONFIRMED_SERVICE, proposed={"monthly_payment": 999.0}, requested_amount=None)
    assert on_file["new_payment_monthly"] is None and on_file["active"] is False and on_file["debt_service_after_monthly"] == 11897.0
    sheet = fp.evaluate_plan(fp.normalize_plan({"new_payment_source": "term_sheet"}), obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=CONFIRMED_SERVICE, proposed={"monthly_payment": 999.0, "amount": 100000, "rate_pct": 12, "term_months": 120, "label": "Term sheet v1: Bank"}, requested_amount=None)
    assert (sheet["new_payment_monthly"], sheet["new_payment_source"], sheet["active"]) == (999.0, "term_sheet", True)
    assert sheet["loan_terms"]["source"] == "term_sheet" and sheet["loan_terms"]["amount"] == 100000.0
    lines = fp.describe_modifications(fp.normalize_plan({"new_payment_source": "term_sheet"}), sheet, obligations, RECURRING)
    assert lines[0]["label"] == "New payment 999 a month from the recorded loan terms" and "Term sheet v1: Bank: 100,000 at 12% over 120 months" in lines[0]["detail"]
    nothing = fp.evaluate_plan(fp.normalize_plan({}), obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=CONFIRMED_SERVICE, proposed=None, requested_amount=None)
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
    obligations = _confirmed_obligations()
    plan = fp.normalize_plan({})
    evaluation = fp.evaluate_plan(plan, obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=CONFIRMED_SERVICE, proposed=None, requested_amount=70000)
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


TAX_YEARS = [{"year": "2025", "officer_compensation": 96000.0, "ebitda": 70000.0}]


def test_add_backs_overrides_and_an_interest_only_loan_change_every_figure_together() -> None:
    obligations = _confirmed_obligations()
    plan = fp.normalize_plan(
        {
            "retire": ["row:ondeck"],
            "add_backs": [{"kind": "owner_salary", "amount_monthly": 8000, "note": "Owner draws a salary; discretionary"}, {"kind": "bogus", "amount_annual": 12000}, {"kind": "other", "amount_monthly": 0}],
            "new_loan_amount": 150000,
            "new_loan_rate_pct": 12,
            "new_loan_interest_only": True,
        }
    )
    assert [a["kind"] for a in plan["add_backs"]] == ["owner_salary", "other"]  # the unknown kind folds to other; zero drops
    assert plan["add_backs"][1]["amount_monthly"] == 1000.0 and plan["new_loan_interest_only"] is True
    result = fp.evaluate_plan(plan, obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=CONFIRMED_SERVICE, proposed=None, requested_amount=70000)
    assert result["add_backs_monthly"] == 9000.0 and result["add_backs_annual"] == 108000.0
    assert result["new_payment_monthly"] == 1500.0 and result["new_payment_source"] == "manual_terms"  # 150k × 12% / 12
    assert {k: result["loan_terms"][k] for k in ("amount", "rate_pct", "term_months", "interest_only", "structure")} == {"amount": 150000.0, "rate_pct": 12.0, "term_months": None, "interest_only": True, "structure": "interest_only"}
    assert result["debt_service_after_monthly"] == round(11897.0 - 6750.0 + 1500.0, 2)
    bank, tax = result["candidates"]
    assert bank["numerator_after"] == 131063.0 + 108000.0 and tax["numerator_after"] == 70000.0 + 108000.0
    assert result["income_before_annual"] == 131063.0 and result["income_after_annual"] == bank["numerator_after"]
    assert result["headline_after"] == round(bank["numerator_after"] / (result["debt_service_after_monthly"] * 12), 2)

    overridden = fp.normalize_plan({"overrides": {"income_annual": 200000, "income_note": "Per the CPA letter", "debt_service_monthly": 3000}})
    out = fp.evaluate_plan(overridden, obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=CONFIRMED_SERVICE, proposed=None, requested_amount=None)
    assert out["active"] and out["debt_service_before_monthly"] == 3000.0 and out["debt_service_after_monthly"] == 3000.0
    assert all(c["numerator_after"] == 200000.0 and c["overridden"] for c in out["candidates"])
    assert out["headline_after"] == round(200000 / 36000, 2)
    alone = fp.evaluate_plan(fp.normalize_plan({"overrides": {"income_annual": 90000}, "new_payment_monthly": 2000}), obligations_list=[], recurring={"payees": [], "months_read": []}, months=[], dscr={"candidates": [], "headline": None}, service={"monthly": None}, proposed=None, requested_amount=None)
    assert alone["candidates"][0]["key"] == "desk_override" and alone["headline_after"] == round(90000 / 24000, 2)


def test_modifications_and_impact_read_as_original_change_and_result() -> None:
    obligations = _confirmed_obligations()
    plan = fp.normalize_plan({"retire": ["row:ondeck"], "exclude": ["payee:wise inc"], "add_backs": [{"kind": "owner_salary", "amount_monthly": 8000}], "overrides": {"debt_service_monthly": 12000, "debt_service_note": "Includes the truck lease"}, "new_loan_amount": 150000, "new_loan_rate_pct": 12, "new_loan_term_months": 60})
    evaluation = fp.evaluate_plan(plan, obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=CONFIRMED_SERVICE, proposed=None, requested_amount=70000)
    accounts = [{"label": "Chase ••1204", "counted": True}, {"label": "Chase ••7777", "counted": False}]
    lines = fp.describe_modifications(plan, evaluation, obligations, RECURRING, accounts)
    assert [m["kind"] for m in lines] == ["retire", "exclude", "add_back", "override_debt_service", "loan", "accounts"]
    assert lines[0]["label"] == "Pay off ONDECK ACH 4471 with the new loan" and lines[0]["monthly_effect"] == -6750.0
    assert "150,000 at 12% over 60 months" in lines[4]["label"] and lines[4]["monthly_effect"] == -evaluation["new_payment_monthly"]
    assert lines[5]["detail"] == "Not counted: Chase ••7777."
    impact = fp.plan_impact(evaluation, DSCR, accounts)
    assert impact["active"] and impact["dscr"]["before"] == 0.92 and impact["dscr"]["after"] == evaluation["headline_after"]
    assert impact["debt_service_monthly"]["before"] == 12000.0 and impact["debt_service_monthly"]["change"] == round(evaluation["debt_service_after_monthly"] - 12000.0, 2)
    assert impact["loan"]["interest_only"] is False and impact["loan"]["payment_monthly"] == evaluation["new_payment_monthly"]
    assert impact["accounts"] == {"counted": ["Chase ••1204"], "not_counted": ["Chase ••7777"]}


def test_levers_offer_the_officer_add_back_and_interest_only_when_they_raise_coverage() -> None:
    obligations = _confirmed_obligations()
    plan = fp.normalize_plan({"retire": ["row:ondeck", "row:wayflyer"], "new_loan_amount": 150000, "new_loan_rate_pct": 12, "new_loan_term_months": 60})
    evaluation = fp.evaluate_plan(plan, obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=CONFIRMED_SERVICE, proposed=None, requested_amount=70000)
    levers = {lever["key"]: lever for lever in fp.coverage_levers(plan, evaluation, obligations_list=obligations, recurring=RECURRING, dscr=DSCR, requested_amount=70000, terms=None, tax_years=TAX_YEARS)}
    comp = levers["addback:officer_compensation"]
    assert comp["apply"]["add_backs"][0]["amount_monthly"] == 8000.0 and comp["apply"]["add_backs"][0]["source"] == "tax_return"
    assert comp["dscr_after"] == round((131063.0 + 96000.0) / (evaluation["debt_service_after_monthly"] * 12), 2)
    io = levers["interest_only"]
    assert io["apply"] == {"new_loan_interest_only": True, "new_loan_structure": "interest_only", "new_loan_interest_only_months": None, "new_payment_monthly": None}
    assert io["dscr_after"] == round(131063.0 / ((evaluation["debt_service_before_new_payment_monthly"] + 1500.0) * 12), 2)
    assert io["dscr_after"] > evaluation["headline_after"]
    # Once applied, neither lever is offered again.
    applied = fp.normalize_plan({**plan, **comp["apply"], **io["apply"]})
    again = fp.evaluate_plan(applied, obligations_list=obligations, recurring=RECURRING, months=MONTHS, dscr=DSCR, service=CONFIRMED_SERVICE, proposed=None, requested_amount=70000)
    keys = {lever["key"] for lever in fp.coverage_levers(applied, again, obligations_list=obligations, recurring=RECURRING, dscr=DSCR, requested_amount=70000, terms=None, tax_years=TAX_YEARS)}
    assert "addback:officer_compensation" not in keys and "interest_only" not in keys


def test_the_recorded_loan_terms_are_offered_as_levers_not_assumed() -> None:
    quiet = {"payees": [], "months_read": []}
    service = {"source": "debt_schedule_rows", "monthly": 20932.0, "annual": 251184.0}
    obligations = fp.obligations({"rows": [_row("r1", "David Bender", 18417.0, 1200000.0, is_mca=False), _row("r2", "SBA", 2515.0, 477480.0, is_mca=False)], "documented": [], "service": service}, quiet)
    dscr = {"headline": {"key": "tax_return", "numerator": 258000.0, "value": 1.03}, "candidates": [{"key": "tax_return", "label": "Tax return 2025", "numerator": 258000.0, "value": 1.03}]}
    plan = fp.normalize_plan({})
    proposed = {"monthly_payment": 36662.07, "amount": 400000, "rate_pct": 32, "term_months": 13, "label": "Term sheet v1: Dealer capital advance"}
    evaluation = fp.evaluate_plan(plan, obligations_list=obligations, recurring=quiet, months=[], dscr=dscr, service=service, proposed=proposed, requested_amount=400000)
    assert evaluation["active"] is False and evaluation["debt_service_after_monthly"] == 20932.0  # nothing assumed
    additive = {**proposed, "source": "term_sheet", "treatment": "additive", "retained_annual_debt_service": None}
    levers = {lever["key"]: lever for lever in fp.coverage_levers(plan, evaluation, obligations_list=obligations, recurring=quiet, dscr=dscr, requested_amount=400000, terms=additive)}
    add = levers["term_sheet:add"]
    assert add["apply"]["new_payment_source"] == "term_sheet" and add["dscr_after"] == round(258000.0 / ((20932.0 + 36662.07) * 12), 2)
    assert "additional debt" in add["detail"] and "term_sheet:refinance" not in levers
    refinance = {**additive, "treatment": "refinance", "retained_annual_debt_service": 30180.0}
    levers = {lever["key"]: lever for lever in fp.coverage_levers(plan, evaluation, obligations_list=obligations, recurring=quiet, dscr=dscr, requested_amount=400000, terms=refinance)}
    refi = levers["term_sheet:refinance"]
    assert refi["apply"]["retire"] == ["row:r1", "row:r2"] and refi["apply"]["new_payment_source"] == "term_sheet"
    assert refi["dscr_after"] == round(258000.0 / ((30180.0 / 12 + 36662.07) * 12), 2) and "retained per the sheet" in refi["detail"]
    adopted = fp.evaluate_plan(fp.normalize_plan(refi["apply"]), obligations_list=obligations, recurring=quiet, months=[], dscr=dscr, service=service, proposed=proposed, requested_amount=400000)
    assert adopted["active"] and adopted["retired_monthly"] == 20932.0 and adopted["new_payment_monthly"] == 36662.07
    again = {lever["key"] for lever in fp.coverage_levers(fp.normalize_plan(refi["apply"]), adopted, obligations_list=obligations, recurring=quiet, dscr=dscr, requested_amount=400000, terms=refinance)}
    assert "term_sheet:add" not in again and "term_sheet:refinance" not in again
