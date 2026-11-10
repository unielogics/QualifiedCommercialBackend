"""Principal at risk on every schedule, the coverage on every payment, and
the desk-only economics that never reach the package or the offering."""

from __future__ import annotations

import re

from app.capital_partners.services.listings import partner_terms
from app.services.deal_cost import effective_rate_pct, internal_economics
from app.services.financial_cockpit import plan_summary, proposed_terms_block
from app.services.loan_schedule import build_schedule, risk_profile


def test_the_schedule_says_how_long_the_principal_is_at_risk() -> None:
    s = build_schedule(200000, 12, 24, interest_only_months=6)
    assert s["full_exposure_months"] == 6
    assert s["rows"][5]["exposure_pct"] == 100.0 and s["rows"][5]["recovered_pct"] == 0.0
    assert s["rows"][6]["exposure_pct"] == 94.4 and s["rows"][-1]["exposure_pct"] == 0.0
    # 18 even principal payments in months 7..24: the average dollar is back at month 15.5
    assert s["weighted_average_life_months"] == 15.5
    assert s["recovery_milestones"] == {"25": 11.0, "50": 15.0, "75": 20.0, "100": 24.0}
    io = build_schedule(100000, 10, 12, structure="interest_only")
    assert io["recovery_milestones"] == {"25": 12.0, "50": 12.0, "75": 12.0, "100": 12.0}
    assert io["weighted_average_life_months"] == 12.0 and io["balloon_pct"] == 100.0


def test_every_payment_gets_its_own_coverage_and_the_lowest_is_the_one_that_counts() -> None:
    s = build_schedule(200000, 12, 24, interest_only_months=6)
    risk = risk_profile(s, income_monthly=20000, existing_service_monthly=2000)
    assert risk["coverage_during_interest_only"] == 5.0  # 20,000 / (2,000 + 2,000)
    assert risk["coverage_after_interest_only"] == 1.32  # 20,000 / (2,000 + 13,111.11)
    assert risk["lowest_coverage"] == 1.32 and risk["lowest_coverage_month"] == 7.0
    assert len(risk["coverage_by_period"]) == 24
    assert "fully exposed for the first 6 months" in risk["words"]
    assert (
        "half by month 15" in risk["words"] and "Weighted average life 15.5 months" in risk["words"]
    )
    assert "5.00x during the interest-only months, 1.32x once principal starts" in risk["words"]
    without_income = risk_profile(s)
    assert without_income["coverage_by_period"] == [] and without_income["lowest_coverage"] is None
    assert risk_profile(None) is None


def test_the_fees_come_off_the_proceeds_and_raise_the_true_cost_of_the_money() -> None:
    assert (
        effective_rate_pct(100000, [8791.59] * 12) == 10.0
    )  # a level 12-month loan at 10% nominal
    amortizing = build_schedule(100000, 10, 60)
    proposed = {
        "amount": 100000,
        "rate_pct": 10,
        "term_months": 60,
        "schedule": {k: v for k, v in amortizing.items() if k != "rows"},
        "schedule_rows": amortizing["rows"],
    }
    no_fees = internal_economics(
        {"accepted_amount": None, "origination_points": None, "consulting_fee": None}, proposed
    )
    assert no_fees["total_fees"] == 0.0 and no_fees["net_to_client"] == 100000.0
    assert no_fees["structure_rate_pct"] == 10.0 and no_fees["effective_rate_pct"] == 10.0
    assert "nets the full loan" in no_fees["words"]
    with_fees = internal_economics(
        {"accepted_amount": 100000, "origination_points": 3, "consulting_fee": 2500}, proposed
    )
    assert (
        with_fees["origination_fee"] == 3000.0
        and with_fees["consulting_fee"] == 2500.0
        and with_fees["total_fees"] == 5500.0
    )
    assert (
        with_fees["net_to_client"] == 94500.0
        and with_fees["fees_pct_of_loan"] == 5.5
        and with_fees["qc_earnings"] == 5500.0
    )
    assert with_fees["effective_rate_pct"] > 12.0  # 5.5 points off the proceeds on a five-year loan
    assert with_fees["audience"] == "desk" and "the client nets 94,500" in with_fees["words"]
    # Simple interest charges interest on the original principal: the structure itself costs more than the stated rate.
    simple = build_schedule(200000, 12, 24, interest_only_months=6)
    simple_terms = {
        "amount": 200000,
        "rate_pct": 12,
        "term_months": 24,
        "schedule": {k: v for k, v in simple.items() if k != "rows"},
        "schedule_rows": simple["rows"],
    }
    cost = internal_economics(
        {"accepted_amount": None, "origination_points": None, "consulting_fee": 5000}, simple_terms
    )
    assert (
        cost["structure_rate_pct"] > 16.0
        and cost["effective_rate_pct"] > cost["structure_rate_pct"]
    )
    assert "The structure itself costs" in cost["words"]
    assert (
        internal_economics(
            {"accepted_amount": None, "origination_points": None, "consulting_fee": None}, None
        )
        is None
    )


def _cockpit_with_terms() -> dict:
    return {
        "dscr": {
            "proposed": {
                "source": "term_sheet",
                "label": "Term sheet v1: Quidity",
                "amount": 200000,
                "rate_pct": 12,
                "term_months": 24,
                "monthly_payment": None,
                "structure": None,
                "interest_only_months": 6,
                "treatment": "additive",
                "retained_annual_debt_service": None,
                "payment_stated": False,
            },
            "headline": {"key": "tax_return", "numerator": 240000, "value": 1.5},
            "annual_debt_service": 24000,
        },
        "plan": {"record": {}, "evaluation": {"active": False}, "modifications": [], "impact": {}},
    }


def test_the_proposed_terms_carry_the_schedule_and_the_risk_but_never_a_fee() -> None:
    cockpit = _cockpit_with_terms()
    terms = proposed_terms_block(cockpit)
    assert terms["structure"] == "simple_interest" and terms["interest_only_months"] == 6
    assert terms["payment_io_monthly"] == 2000.0 and terms["payment_after_monthly"] == 13111.11
    assert (
        terms["monthly_payment"] == 13111.11
    )  # the contractual repayment installment stands in for a missing figure
    assert terms["describe"] == "over 24 months, 6 interest-only then simple interest"
    assert len(terms["schedule_rows"]) == 24 and terms["schedule_rows"][0]["exposure_pct"] == 100.0
    assert terms["risk"]["lowest_coverage"] == 1.32  # 20,000 a month over 2,000 existing + 13,111
    assert terms["risk"]["coverage_during_interest_only"] == 5.0
    member = partner_terms(terms)
    assert set(member) <= {
        "deal_budget_revision",
        "current_deal_budget_revision",
        "deal_budget_stale",
        "deal_budget",
        "active_proposal",
        "program_key",
        "program_name",
        "terms_state",
        "recorded_terms_ref",
        "collateral_revision",
        "current_collateral_revision",
        "collateral_stale",
        "collateral",
        "source",
        "label",
        "amount",
        "rate_pct",
        "term_months",
        "structure",
        "structure_label",
        "interest_only",
        "interest_only_months",
        "repayment_config",
        "payments_per_year",
        "monthly_payment",
        "coverage_payment_monthly",
        "regular_payment_monthly",
        "interest_only_payment_monthly",
        "repayment_payment_monthly",
        "maturity_balance",
        "payment_phases",
        "payment_io_monthly",
        "payment_after_monthly",
        "describe",
        "payment_note",
        "schedule",
        "schedule_rows",
        "risk",
    }
    assert member["regular_payment_monthly"] == member["coverage_payment_monthly"] == 13111.11
    assert member["interest_only_payment_monthly"] == 2000.0
    assert len(member["payment_phases"]) == 2
    assert "treatment" not in member and not any("fee" in k for k in member)
    cockpit["plan"]["economics"] = internal_economics(
        {"accepted_amount": 200000, "origination_points": 2, "consulting_fee": 7500}, terms
    )
    assert cockpit["plan"]["economics"]["total_fees"] == 11500.0
    assert "economics" not in plan_summary(cockpit)
    assert not any("fee" in k or "economics" in k for k in member)


def test_a_lender_stated_payment_is_named_when_it_differs_from_the_formula() -> None:
    cockpit = _cockpit_with_terms()
    cockpit["dscr"]["proposed"].update(
        {
            "monthly_payment": 36662,
            "structure": "amortizing",
            "interest_only_months": 0,
            "payment_stated": True,
            "amount": 400000,
            "term_months": 12,
        }
    )
    terms = proposed_terms_block(cockpit)
    assert terms["monthly_payment"] == 36662
    assert terms["payment_note"].startswith(
        "The recorded payment, 36,662 a month, is the lender's own figure"
    )
    assert terms["schedule"]["structure"] == "amortizing"


def test_the_package_draws_the_schedule_and_the_principal_at_risk_without_a_fee() -> None:
    from app.services.public_underwriting_packet_pdf import _plan_section

    terms = proposed_terms_block(_cockpit_with_terms())
    html = _plan_section({"plan": {"active": False}, "proposed_terms": terms}, compact=False)
    assert "Principal at risk" in html and "Weighted average life</strong> 15.5 months" in html
    assert "half by month 15, all by month 24" in html and "Fully exposed</strong> 6 months" in html
    assert (
        "Lowest coverage on a payment</strong> 1.32x (5.00x during the interest-only months, 1.32x once principal starts)"
        in html
    )
    assert "Schedule by year" in html and "Year 2" in html
    text = re.sub(r'src="data:[^"]*"', "", html).lower()  # the chart's PNG bytes are not words
    assert "2,000 a month for 6 months, then" in html and "fee" not in text
    brief = _plan_section({"plan": {"active": False}, "proposed_terms": terms}, compact=True)
    assert "Principal at risk" in brief and "Schedule by year" not in brief


def test_a_sheet_recorded_before_structures_existed_reads_the_structure_off_its_payment() -> None:
    from types import SimpleNamespace

    from app.services.financial_cockpit import proposed_payment, term_sheet_terms

    level = build_schedule(400000, 32, 13, structure="amortizing")["coverage_payment_monthly"]
    sheet = SimpleNamespace(
        version=1,
        funding_party_name="Quidity",
        approved_amount=400000,
        rate_pct=32,
        term_months=13,
        monthly_debt_service=round(level, 2),
        debt_service_is_level_payment=False,
        extra={},
    )
    terms = term_sheet_terms(sheet)
    assert (
        terms["structure"] == "amortizing" and terms["payment_stated"] is True
    )  # the lender's figure is the level payment
    cockpit = _cockpit_with_terms()
    cockpit["dscr"]["proposed"] = proposed_payment(
        terms
    )  # the DSCR block's own copy keeps the structure
    assert cockpit["dscr"]["proposed"]["structure"] == "amortizing"
    assert cockpit["dscr"]["proposed"]["payment_stated"] is True
    proposed = proposed_terms_block(cockpit)
    assert proposed["structure"] == "amortizing" and proposed["payment_note"] is None
    sheet.monthly_debt_service = (
        50000  # a figure no structure produces: the house default and a note
    )
    terms = term_sheet_terms(sheet)
    assert terms["structure"] is None
    cockpit["dscr"]["proposed"] = proposed_payment(terms)
    proposed = proposed_terms_block(cockpit)
    assert proposed["structure"] == "simple_interest"
    assert proposed["payment_note"].startswith("The recorded payment, 50,000 a month")
    legacy_level = SimpleNamespace(
        **{**sheet.__dict__, "debt_service_is_level_payment": True, "monthly_debt_service": 1}
    )
    assert term_sheet_terms(legacy_level)["structure"] == "amortizing"
    assert term_sheet_terms(legacy_level)["payment_stated"] is False
    # Grace Auto's real sheet: fully_amortizing in extra, weekly, 400k at 32% over 13 months, 36,662.07 a month.
    grace = SimpleNamespace(
        version=1,
        funding_party_name="Quidity",
        approved_amount=400000,
        rate_pct=32,
        term_months=13,
        monthly_debt_service=36662.07,
        debt_service_is_level_payment=False,
        extra={
            "repayment_structure": "fully_amortizing",
            "lender_payment_override": False,
            "payment_frequency": "weekly",
        },
    )
    cockpit["dscr"]["proposed"] = proposed_payment(term_sheet_terms(grace))
    proposed = proposed_terms_block(cockpit)
    assert proposed["structure"] == "amortizing" and proposed["payment_note"] is None
    assert proposed["monthly_payment"] == 36662.07
    assert abs(proposed["payment_after_monthly"] - 36662.07) / 36662.07 < 0.01


def test_counting_the_interest_received_the_capital_is_back_before_the_principal() -> None:
    s = build_schedule(200000, 12, 24, interest_only_months=6)
    assert (
        s["rows"][5]["cash_at_risk_pct"] == 94.0
    )  # six interest payments of 2,000 already in hand
    assert s["breakeven_month"] == 21.0  # 12,000 + 15 × 13,111 ≥ 200,000 at month 21
    assert s["rows"][20]["cash_at_risk_pct"] == 0.0 and s["rows"][19]["cash_at_risk_pct"] > 0
    words = risk_profile(s)["words"]
    assert "receipts equal the original advance by month 21" in words
    assert "does not eliminate credit exposure on unpaid principal" in words
    io = build_schedule(100000, 10, 12, structure="interest_only")
    assert io["breakeven_month"] == 12.0  # the balloon brings it back at maturity
    amortizing = build_schedule(100000, 10, 60)
    assert amortizing["breakeven_month"] == 48.0
