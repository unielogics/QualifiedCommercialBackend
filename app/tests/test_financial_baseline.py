"""One baseline: the filed return leads and every headline reads the same ladder.

The reference figures are the ones on the Financials design: revenue 1,236,000,
NOI -30,777, EBITDA -22,377, debt service 777,600, DSCR -0.03x.
"""

from app.services import financial_baseline
from app.services.financial_cockpit import build_cockpit
from app.services.financial_earnings_sources import change
from app.services.financial_metric_reporting import validated_annual_pair
from app.services.program_earnings_facts import annual_ebitda_fact
from app.tests.test_financial_metric_review import ACTOR, file

ENTITY = "Harbor Auto Detail LLC"
NAMED = {"salaries_and_wages": 348194, "rent": 196805, "insurance": 22600, "advertising": 18400,
         "professional_fees": 16500, "utilities": 14300, "repairs": 11200, "software": 9800}
DEBTS = [{"id": f"debt-{index}", "lender": lender, "monthly_payment": monthly, "balance": balance, "origin": "admin", "count_in_dscr": True}
         for index, (lender, monthly, balance) in enumerate((
             ("Northline Capital", 19400, 286000), ("Kestrel Funding", 16900, 241500), ("Bluewater Advance", 13200, 198000),
             ("Summit Merchant", 10500, 142300), ("First Coast Equipment", 4800, 212400)))]


def filed_return(year=2025, *, key=None, **facts):
    return file("tax_return", {"entity_name": ENTITY, "tax_form": "Form 1120-S", "tax_year": str(year),
        "gross_receipts": 1236000, "cost_of_goods_sold": 601886, "gross_profit": 634114, "total_income": 642514,
        "total_deductions": 664891, "ordinary_business_income": -22377, **NAMED, **facts}, key=key or f"return-{year}")


def statement(*, start="2026-01-01", end="2026-09-30", key="statement", **facts):
    return file("current_p_and_l", {"business_name": ENTITY, "currency": "USD", "basis": "accrual",
        "period_start": start, "period_end": end, "gross_revenue": 878400, "cost_of_goods_sold": 441300,
        "total_operating_expenses": 502725, "operating_income": -65625, "net_income": -70000,
        "interest": 2000, "income_taxes": 0, "depreciation_and_amortization": 6300, **facts}, key=key)


def cockpit(analyses, state=None, debts=None):
    return build_cockpit(analyses, DEBTS if debts is None else debts, business_entity=ENTITY, metric_review_state=state)


def selected(current):
    return current["ebitda"]["source_selection"]["selected_file_id"]


def decide(current, state=None, *, mode="automatic", choose=None, exclude=()):
    selection = current["ebitda"]["source_selection"]
    return change(state, selection, mode=mode, selected_file_id=choose, excluded_file_ids=list(exclude),
        reason="Reviewed the earnings source", expected_revision=(state or {}).get("revision", 0),
        source_stamp=selection["source_stamp"], idempotency_key=f"source-{mode}-{choose}-{len(exclude)}", actor=ACTOR)[0]


def test_reference_ladder_matches_the_design_and_every_headline():
    current = cockpit([filed_return()])
    baseline = current["baseline"]
    figures = baseline["figures"]
    assert baseline["source"]["kind"] == "tax_return" and baseline["source"]["label"] == "IRS 1120-S · 2025"
    assert (figures["revenue"], figures["cogs"], figures["operating_expenses"]) == (1236000, 601886, 664891)
    assert (figures["noi"], figures["other_income"], figures["ebitda"]) == (-30777, 8400, -22377)
    rows = {row["key"]: row["amount"] for row in baseline["ladder"]}
    assert rows["revenue"] + rows["cogs"] + rows["operating_expenses"] == rows["noi"] == -30777
    assert rows["noi"] + rows["other_income"] + (rows["depreciation_amortization"] or 0) == rows["ebitda"] == -22377
    assert baseline["metrics"]["current"] == {"ebitda": -22377, "noi": -30777, "dscr": -0.03,
        "free_cash_flow": -799977, "operating_margin": -2.49, "debt_service": 777600}
    assert [row["current_debt"] for row in baseline["targets"]] == [799977, 877737, 994377]
    # The one selection drives every headline.
    assert current["ebitda"]["headline"]["annualized_value"] == -22377
    assert current["dscr"]["headline"]["numerator"] == -22377 and current["dscr"]["headline"]["value"] == -0.03
    assert current["noi"]["headline"]["annualized_value"] == -30777
    assert current["net_profit"]["conclusion"]["value"] == -22377
    assert baseline["staff_adjustments"] == []


def test_a_return_without_printed_dates_is_its_full_tax_year_not_a_blank():
    current = cockpit([filed_return()])
    headline = current["ebitda"]["headline"]
    assert headline["period_assumed"] and headline["months_covered"] == 12 and headline["annualization_factor"] == 1
    assert headline["annualized_value"] == headline["value"] == -22377
    assert headline["provisional"] is False
    noi = current["noi"]
    assert noi["headline"]["period_assumed"] and noi["headline"]["annualized_value"] == -30777
    assert noi["status"] == "source_supported" and noi["review_needed"] is False
    assert "calendar_year_assumed" in noi["source_limitations"]
    assert current["baseline"]["source"]["period_assumed"]
    assert any("read as that full year" in note for note in current["baseline"]["notes"])


def test_expense_groups_add_up_to_the_return_with_the_remainder_named():
    groups = cockpit([filed_return()])["baseline"]["groups"]
    amounts = {row["key"]: row["amount"] for row in groups["rows"]}
    assert amounts["cogs"] == 601886 and amounts[financial_baseline.REMAINDER_KEY] == 27092
    assert groups["reconciles"] and groups["total"] == 664891
    assert sum(value for key, value in amounts.items() if key != "cogs") == 664891
    listed = financial_baseline.expense_groups({**filed_return()["key_facts"], "rent": 400000}, kind="tax_return")
    assert listed["status"] == "lines_exceed_total" and listed["reconciles"] is False
    assert all(row["key"] != financial_baseline.REMAINDER_KEY for row in listed["rows"])


def test_the_latest_statement_sits_beside_each_return_line_for_comparison_only():
    lines = {"salaries_and_wages": 263700, "rent": 147600, "insurance": 17325}
    current = cockpit([filed_return(), statement(**lines)])
    groups = current["baseline"]["groups"]
    beside = {row["key"]: row.get("statement_amount") for row in groups["rows"]}
    # Nine months, annualized: the design's P&L column.
    assert (beside["cogs"], beside["salaries_and_wages"], beside["rent"], beside["insurance"]) == (588400, 351600, 196800, 23100)
    # What the P&L does not name line by line is its own remainder (its depreciation line is named, so it is not in the rest).
    assert beside["utilities"] is None and beside["other_deductions"] == round((502725 - 263700 - 147600 - 17325 - 6300) * 12 / 9, 2)
    assert groups["statement"] == {"file_id": "statement", "label": groups["statement"]["label"], "months_covered": 9, "annualized": True}
    # The return is still the source and its own figures did not move.
    assert current["baseline"]["source"]["kind"] == "tax_return" and current["baseline"]["figures"]["ebitda"] == -22377
    assert {row["key"]: row["amount"] for row in groups["rows"]}["rent"] == 196805
    # With no P&L, or one too short to annualize, there is nothing to put beside the lines.
    assert "statement" not in cockpit([filed_return()])["baseline"]["groups"]
    assert "statement" not in cockpit([filed_return(), statement(end="2026-03-31")])["baseline"]["groups"]


def test_interest_is_financing_and_unread_add_backs_are_named():
    current = cockpit([filed_return(interest_expense=5000, depreciation=3000, ordinary_business_income=-22377)])
    figures = current["baseline"]["figures"]
    # 642,514 - 664,891 still equals the bottom line, so other income is 8,400.
    assert figures["noi"] == -22377 - 8400 + 5000 and figures["ebitda"] == -22377 + 5000 + 3000
    assert figures["operating_expenses"] == 664891 - 5000 and figures["unread"] == []
    groups = current["baseline"]["groups"]
    assert groups["outside_plan"] == [{"key": "interest", "label": "Interest (financing, not an operating cost)", "amount": 5000}]
    assert sum(row["amount"] for row in groups["rows"] if row["key"] != "cogs") + 5000 == 664891
    bare = cockpit([filed_return()])["baseline"]
    assert bare["figures"]["unread"] == ["interest", "depreciation_amortization"]
    assert sum("none is added back" in note for note in bare["notes"]) == 2


def test_other_income_needs_the_returns_own_identity_to_hold():
    misread = filed_return(total_income=1236000)  # Total income read off the gross receipts line.
    figures = cockpit([misread])["baseline"]["figures"]
    assert figures["other_income"] is None and figures["noi"] == figures["ebitda"] == -22377
    assert financial_baseline.other_income({"other_income": 1200}, revenue=None, cogs=None, deductions=None, income=5) == (1200, "other income as printed")


def test_the_filed_return_leads_a_newer_statement_and_staff_can_choose_it():
    current = cockpit([filed_return(), statement()])
    assert selected(current) == "return-2025" and current["ebitda"]["source_selection"]["baseline_policy"] == "filed_return_first"
    fallback = next(row for row in current["ebitda"]["source_selection"]["sources"] if row["file_id"] == "statement")
    assert fallback["fallback_only"] and fallback["baseline_eligible"] and not fallback["selected"]
    assert "Labelled fallback" in fallback["reason"]
    assert current["dscr"]["headline"]["key"] == "tax_return" and current["baseline"]["source"]["selected_by"] == "rule"
    assert [row["selected"] for row in current["baseline"]["sources"] if row["kind"] != "bank_proxy"] == [True, False]
    chosen = cockpit([filed_return(), statement()], decide(current, mode="manual", choose="statement"))
    assert selected(chosen) == "statement" and chosen["baseline"]["source"]["kind"] == "profit_and_loss"
    assert chosen["baseline"]["source"]["selected_by"] == "staff" and chosen["dscr"]["headline"]["key"] == "profit_and_loss"
    assert chosen["noi"]["headline"]["source_refs"][0]["file_id"] == "statement"


def test_a_statement_stands_in_without_a_return_and_its_ladder_adds_up():
    current = cockpit([statement(start="2025-01-01", end="2025-12-31")])
    baseline = current["baseline"]
    assert baseline["source"]["kind"] == "profit_and_loss" and baseline["source"]["fallback"]
    rows = {row["key"]: row["amount"] for row in baseline["ladder"]}
    assert rows["revenue"] + rows["cogs"] + rows["operating_expenses"] == rows["noi"] == -65625
    assert round(rows["noi"] + rows["other_income"] + rows["depreciation_amortization"], 2) == rows["ebitda"]
    assert rows["ebitda"] == current["ebitda"]["headline"]["annualized_value"] == current["dscr"]["headline"]["numerator"]


def test_the_fallback_never_reaches_back_past_the_excluded_return():
    older = statement(start="2024-01-01", end="2024-12-31")
    automatic = cockpit([filed_return(), older])
    excluded = cockpit([filed_return(), older], decide(automatic, exclude=["return-2025"]))
    assert selected(excluded) is None and excluded["ebitda"]["headline"] is None
    assert excluded["baseline"]["source"]["kind"] is None and excluded["baseline"]["reason"]
    assert excluded["noi"]["headline"] is None and excluded["dscr"]["headline"] is None
    newer = [filed_return(), statement()]
    standing = cockpit(newer, decide(cockpit(newer), exclude=["return-2025"]))
    assert selected(standing) == "statement" and standing["baseline"]["source"]["kind"] == "profit_and_loss"


def test_an_older_return_is_comparison_only():
    current = cockpit([filed_return(2024, ordinary_business_income=41550, total_income=682850, gross_receipts=1301050,
                                    cost_of_goods_sold=618200, gross_profit=682850, total_deductions=641300), filed_return()])
    rows = {row["file_id"]: row for row in current["ebitda"]["source_selection"]["sources"]}
    assert selected(current) == "return-2025" and rows["return-2024"]["comparative_only"]
    assert not rows["return-2024"]["baseline_eligible"]
    assert [row["label"] for row in current["baseline"]["sources"]] == ["Filed return 2025", "Filed return 2024"]


def test_an_accepted_return_is_validated_annual_earnings_for_program_rules():
    analyses = [filed_return(interest_expense=5000, depreciation=3000)]
    current = cockpit(analyses)
    pair = validated_annual_pair(current)
    assert pair["basis"] == "validated_filed_return_annual_ebitda" and pair["ebitda_annual"] == -14377
    assert pair["dscr_label"] == "Filed return EBITDA-based DSCR"
    value, source = annual_ebitda_fact(analyses, entity=ENTITY)
    assert value == -14377 and source["verification_status"] == "verified"
    assert source["eligibility_policy"] == "current_accepted_comparable_usd_filed_return_or_profit_and_loss"
    unaccepted = [{**analyses[0], "accepted": False}]
    assert validated_annual_pair(cockpit(unaccepted)) is None
    assert annual_ebitda_fact(unaccepted, entity=ENTITY)[0] is None
    assert cockpit(unaccepted)["noi"]["status"] == "provisional"


def test_plan_expense_groups_come_from_the_same_return_and_reconcile():
    from app.services.operating_plan_evidence import build_evidence

    analyses = [filed_return(), statement()]
    guidance = build_evidence(analyses, cockpit(analyses), business_name=ENTITY)["guidance"]
    assert guidance["reporting"]["source_file_id"] == "return-2025"
    groups = {row["key"].rsplit(":", 1)[-1]: row for row in guidance["expense_groups"]}
    # A year-only return still gives annual amounts for the plan to start from.
    assert groups["rent"]["baseline_annual_amount"] == 196805
    assert groups[financial_baseline.REMAINDER_KEY]["baseline_annual_amount"] == 27092 and groups[financial_baseline.REMAINDER_KEY]["derived"]
    assert sum(row["baseline_annual_amount"] for key, row in groups.items() if key != "cogs") == 664891
    assert guidance["expense_reconciliation"]["reconciles"] and guidance["expense_reconciliation"]["total"] == 664891


def test_a_fault_in_the_baseline_never_takes_the_cockpit_down(monkeypatch):
    def broken(*_args):
        raise RuntimeError("unexpected fact shape")

    monkeypatch.setattr(financial_baseline, "build", broken)
    current = cockpit([filed_return()])
    assert current["baseline"]["unavailable"] and current["baseline"]["ladder"] == []
    assert current["ebitda"]["headline"]["annualized_value"] == -22377 and current["dscr"]["headline"]["value"] == -0.03


def test_no_business_source_leaves_a_reason_instead_of_figures():
    baseline = cockpit([])["baseline"]
    assert baseline["source"]["kind"] is None and baseline["ladder"] == [] and baseline["groups"] is None
    assert baseline["metrics"]["current"]["ebitda"] is None and baseline["metrics"]["current"]["debt_service"] == 777600
    assert baseline["reason"]
