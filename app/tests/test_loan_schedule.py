"""One loan schedule: interest-only months, simple interest under three
years, amortizing from three years, interest-only to maturity."""

from __future__ import annotations

from app.services import financial_plan as fp
from app.services.loan_schedule import build_schedule, default_structure, describe


def test_two_years_six_months_interest_only_then_principal_is_simple_interest_by_default() -> None:
    assert (
        default_structure(24) == "simple_interest"
        and default_structure(36) == "amortizing"
        and default_structure(None) == "simple_interest"
    )
    s = build_schedule(200000, 12, 24, interest_only_months=6)
    assert (
        s["structure"] == "simple_interest"
        and s["interest_only_months"] == 6
        and s["periods"] == 24
    )
    assert s["payment_io"] == 2000.0  # 200k × 12% / 12
    assert s["payment_after"] == round(200000 / 18 + 2000, 2)  # even principal plus flat interest
    assert s["coverage_payment_monthly"] == s["payment_after"]
    assert s["annual_debt_service"] == round(6 * 2000 + 6 * s["payment_after"], 2)
    assert s["total_interest"] == 24 * 2000.0 and s["balloon"] == 0.0
    assert [r["phase"] for r in s["rows"][:7]] == ["interest_only"] * 6 + ["simple_interest"]
    assert s["rows"][-1]["balance"] == 0.0 and s["rows"][5]["balance"] == 200000.0
    assert describe(s) == "over 24 months, 6 interest-only then simple interest"


def test_amortizing_from_three_years_and_interest_only_to_maturity() -> None:
    a = build_schedule(200000, 12, 36, interest_only_months=6)
    assert a["structure"] == "amortizing" and a["payment_io"] == 2000.0
    from app.services.financial_cockpit import amortized_payment

    assert a["payment_after"] == amortized_payment(200000, 12, 30)
    assert (
        a["rows"][6]["interest"] == 2000.0 and a["rows"][7]["interest"] < 2000.0
    )  # declining balance
    assert (
        a["rows"][-1]["balance"] == 0.0
        and abs(a["total_interest"] - (sum(r["payment"] for r in a["rows"]) - 200000)) < 1
    )
    io = build_schedule(200000, 12, 24, structure="interest_only")
    assert (
        io["structure"] == "interest_only"
        and io["payment_after"] is None
        and io["balloon"] == 200000.0
        and io["coverage_payment_monthly"] == 2000.0
    )
    assert describe(io) == "interest-only over 24 months, principal due at maturity"
    whole = build_schedule(200000, 12, 24, interest_only_months=24)
    assert (
        whole["structure"] == "interest_only"
    )  # interest-only for the whole term is the interest-only structure
    weekly = build_schedule(52000, 10, 12, structure="amortizing", periods_per_year=52)
    assert (
        weekly["periods"] == 52
        and abs(weekly["coverage_payment_monthly"] - weekly["payment_after"] * 52 / 12) < 0.01
    )
    assert build_schedule(0, 12, 24) is None and build_schedule(1000, 12, 0) is None


def test_the_plan_prices_its_loan_on_the_schedule_and_says_the_step_up() -> None:
    plan = fp.normalize_plan(
        {
            "new_loan_amount": 200000,
            "new_loan_rate_pct": 12,
            "new_loan_term_months": 24,
            "new_loan_interest_only_months": 6,
        }
    )
    assert (
        plan["new_loan_structure"] == "simple_interest"
        and plan["new_loan_interest_only_months"] == 6
    )
    quiet = {"payees": [], "months_read": []}
    service = {"source": "debt_schedule_rows", "monthly": 10000.0, "annual": 120000.0}
    dscr = {
        "headline": {"key": "tax_return", "numerator": 300000.0, "value": 2.5},
        "candidates": [
            {"key": "tax_return", "label": "Tax return 2025", "numerator": 300000.0, "value": 2.5}
        ],
    }
    evaluation = fp.evaluate_plan(
        plan,
        obligations_list=[],
        recurring=quiet,
        months=[],
        dscr=dscr,
        service=service,
        proposed=None,
        requested_amount=None,
    )
    assert evaluation["new_payment_monthly"] == round(
        200000 / 18 + 2000, 2
    )  # coverage stands on the post-interest-only payment
    assert (
        evaluation["loan_terms"]["payment_io"] == 2000.0
        and evaluation["loan_terms"]["structure"] == "simple_interest"
    )
    assert (
        evaluation["schedule"]["interest_only_months"] == 6
        and len(evaluation["schedule_rows"]) == 24
    )
    words = fp.describe_modifications(plan, evaluation, [], quiet)
    assert (
        words[0]["label"]
        == "New loan 200,000 at 12% over 24 months, 6 interest-only then simple interest"
    )
    assert words[0]["detail"] == "2,000 a month for 6 months, then 13,111 a month."
    capped = fp.normalize_plan(
        {"new_loan_amount": 1000, "new_loan_term_months": 12, "new_loan_interest_only_months": 40}
    )
    assert capped["new_loan_interest_only_months"] == 12
    long = fp.normalize_plan(
        {"new_loan_amount": 100000, "new_loan_rate_pct": 10, "new_loan_term_months": 60}
    )
    assert long["new_loan_structure"] == "amortizing"
    chosen = fp.normalize_plan(
        {
            "new_loan_amount": 100000,
            "new_loan_term_months": 60,
            "new_loan_structure": "simple_interest",
        }
    )
    assert chosen["new_loan_structure"] == "simple_interest"


def test_the_dealer_sheet_adopts_with_its_interest_only_months_and_prefills_them_back() -> None:
    import uuid
    from types import SimpleNamespace

    from app.services import plan_terms_link as link

    sheet = SimpleNamespace(
        id=uuid.uuid4(),
        version=2,
        funding_party_name="Quidity",
        approved_amount=200000.0,
        rate_pct=12.0,
        term_months=36,
        monthly_debt_service=7700.0,
        extra={
            "repayment_structure": "interest_only_then_amortizing",
            "interest_only_months": 6,
            "debt_service_treatment": "additive",
        },
    )
    recorded = link.recorded_from_production_sheet(sheet)
    assert (
        recorded["structure"] == "amortizing"
        and recorded["interest_only_months"] == 6
        and recorded["interest_only"] is False
    )
    plan = fp.normalize_plan(link.adopt({}, recorded))
    assert plan["new_loan_structure"] == "amortizing" and plan["new_loan_interest_only_months"] == 6
    evaluation = {
        "loan_terms": {
            "amount": 200000.0,
            "rate_pct": 12.0,
            "term_months": 36,
            "interest_only": False,
            "interest_only_months": 6,
        },
        "new_payment_monthly": 7700.0,
    }
    assert link.terms_link(plan, evaluation, recorded)["state"] == "mirrored"
    assert link.terms_link(
        plan,
        {**evaluation, "loan_terms": {**evaluation["loan_terms"], "interest_only_months": 0}},
        recorded,
    )["differences"] == ["structure"]


def test_client_terms_price_on_the_schedule_and_keep_their_structure() -> None:
    from app.services.application_terms import repayment_math, structure_block

    payment, count, periods, annual, total, cost = repayment_math(
        amount=200000, apr_pct=12, term_months=24, frequency="monthly", interest_only_months=6
    )
    assert (payment, count, periods) == (round(200000 / 18 + 2000, 2), 24, 12.0)
    assert (
        annual == round(12 * payment, 2) and cost == 24 * 2000.0 and total == 200000 + cost
    )  # coverage stands on the payment once principal starts
    level_payment, _c, _p, level_annual, _t, _cost = repayment_math(
        amount=100000, apr_pct=10, term_months=60, frequency="monthly"
    )
    from app.services.financial_cockpit import amortized_payment

    assert level_payment == amortized_payment(100000, 10, 60) and level_annual == round(
        level_payment * 12, 2
    )  # amortizing from three years, as before
    weekly, wcount, wperiods, wannual, wtotal, wcost = repayment_math(
        amount=52000, apr_pct=10, term_months=12, frequency="weekly", loan_structure="amortizing"
    )
    assert wcount == 52 and wperiods == 51.96 and wannual == round(weekly * 52, 2)
    assert (
        wtotal == round(52000 + wcost, 2) and abs(wtotal - wannual) < 1
    )  # the last payment carries the cents the level payment rounds away
    block = structure_block(
        amount=200000,
        apr_pct=12,
        term_months=24,
        frequency="monthly",
        custom_payments_per_year=None,
        loan_structure=None,
        interest_only_months=6,
    )
    assert block == {
        "loan_structure": "simple_interest",
        "interest_only_months": 6,
        "interest_only_payment": 2000.0,
        "structure_label": "Simple interest",
    }
