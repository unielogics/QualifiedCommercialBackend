"""Every reader of a saved plan gets the same six figures.

The plan's screens and document, the File page, the investor package, the
marketplace projection, the partner view and the AI context each read the plan
through a different block of one evaluation. These tests hold those blocks to
the plan's own six figures: EBITDA, NOI, DSCR, free cash flow, operating margin
and debt service, today and with the plan.
"""
from __future__ import annotations

import pytest

from app.capital_partners.services import operating_projection
from app.capital_partners.services.listings import partner_operating_earnings
from app.services import earnings_improvement as v1
from app.services import earnings_plan_pdf as document
from app.services import earnings_plan_service as plan_service
from app.services import financial_baseline, operating_plan_schema, operating_plan_service
from app.tests.earnings_plan_fixture import file_body, file_cockpit
from app.tests.test_earnings_plan_pdf import words
from app.tests.test_earnings_plan_service import approve, finished
from app.tests.test_financial_baseline import filed_return

VERSION = {"id": "v", "version_number": 3, "state": "published", "saved_at": "2026-11-14T14:02:00+00:00"}


def file_with(*, terms: bool = True, loan_costs=0.0):
    """The design's file, optionally with a recurring loan cost the budget classes as an operating expense."""
    cockpit = file_cockpit(goal=215_000, terms=terms)
    if loan_costs != 0.0:
        cockpit["plan"]["evaluation"]["annual_new_operating_expenses"] = loan_costs
        cockpit["baseline"] = financial_baseline.build(cockpit, [filed_return()])
    body = finished(approve(file_body(), cockpit))
    return body, cockpit


def readers(body, cockpit):
    """The plan's figures as each reader is given them: {reader: {moment: (ebitda, noi, dscr, free cash flow, margin %, debt service)}}."""
    evaluation = v1.evaluate(body, cockpit, include_expense_changes=True)
    plan = evaluation["plan"]
    basis = "with_terms" if plan["debt_service"]["with_terms"] is not None else "current_debt"

    def own(figures):
        return (round(figures["ebitda"], 2), round(figures["noi"], 2), round(figures["dscr"], 2), round(figures["free_cash_flow"], 2),
                round(figures["operating_margin"] * 100, 2), figures["debt_service"])

    def block(figures):
        return (figures["earnings"], figures["noi"], figures["dscr"], figures["cash_after_debt"], figures["operating_margin"], figures["debt_service"])

    result = {"plan": {"today": own(plan["metrics"]["today"]["current_debt"]), "proposed": own(plan["metrics"]["proposed"][basis]),
                       "reviewed": own(plan["metrics"]["reviewed"][basis])},
              # The File page's strip and the plan summary.
              "comparison": {"today": block(evaluation["comparison"]["baseline"]), "proposed": block(evaluation["comparison"]["proposed"]),
                             "reviewed": block(evaluation["comparison"]["approved"])}}

    def package(value):
        """The investor package, the partner view and the AI context read a projection row and work the rest out from it."""
        rows = {}
        for moment, key in (("proposed", "proposed"), ("reviewed", "approved")):
            row = value["projection"][key]
            terms = basis == "with_terms"
            debt = row["refinance_annual_debt_service"] if terms else row["annual_debt_service"]
            rows[moment] = (row["earnings"], row["noi"], row["dscr_with_refinance"] if terms else row["dscr_current"], debt)
        return rows

    shared = operating_plan_service.shareable_package(evaluation, cockpit.get("operating_evidence"))
    result["package"] = package(shared)
    result["partner"] = package(partner_operating_earnings({"improvement_plan": shared})["improvement_plan"])
    projected = operating_projection.project(body, cockpit, VERSION)

    def point(row, terms):
        return (row["earnings_annual"], row["noi_annual"], row["dscr_with_refinance"] if terms else row["dscr_current_debt"], row["cash_after_debt_annual"])

    result["marketplace"] = {"today": point(projected["current"], False), "proposed": point(projected["proposed_operating"], basis == "with_terms")}
    return result, evaluation


@pytest.mark.parametrize("terms", [True, False])
def test_the_six_figures_are_the_same_for_every_reader(terms):
    body, cockpit = file_with(terms=terms)
    seen, evaluation = readers(body, cockpit)
    plan = seen["plan"]
    assert plan["today"] == (-22377.0, -30777.0, -0.03, -799977.0, -2.49, 777600.0)
    assert plan["proposed"] == ((182896.64, 174496.64, 1.03, 5296.64, 11.63, 177600.0) if terms else (182896.64, 174496.64, 0.24, -594703.36, 11.63, 777600.0))
    # The File page reads all six, to the cent.
    assert seen["comparison"] == plan
    # The package, the partner view and the AI context carry earnings, NOI, coverage and debt service...
    for reader in ("package", "partner"):
        for moment in ("proposed", "reviewed"):
            ebitda, noi, dscr, _cash, _margin, debt = plan[moment]
            assert seen[reader][moment] == (ebitda, noi, dscr, debt), (reader, moment)
    # ...and free cash flow and margin follow from those without a second definition.
    row = operating_plan_service.shareable_package(evaluation, cockpit.get("operating_evidence"))["projection"]["proposed"]
    debt = row["refinance_annual_debt_service"] if terms else row["annual_debt_service"]
    assert round(row["earnings"] - debt, 2) == plan["proposed"][3] and round(row["noi"] / row["revenue"] * 100, 2) == plan["proposed"][4]
    # The marketplace shows earnings, NOI, coverage and cash after debt.
    for moment in ("today", "proposed"):
        ebitda, noi, dscr, cash, _margin, _debt = plan[moment]
        assert seen["marketplace"][moment] == (ebitda, noi, dscr, cash), moment
    # And the document a saved version prints is drawn from the same stored figures.
    facts = plan_service.version_facts(operating_plan_schema.key_facts(body), body, cockpit, evaluation, client_submission=False, goal=cockpit["_operating_goal"])
    text = words(document.build_html(body=body, financial_totals=facts))
    after = "1.03x" if terms else "0.24x"
    assert "EBITDA +$205,274 −$22,377 → $182,897" in text and f"−0.03x → {after}" in text


def test_operating_costs_the_loan_adds_come_off_coverage_once_for_every_reader():
    body, cockpit = file_with(loan_costs=12_000)
    # The file's own baseline: earnings as filed, coverage with the terms after the loan's costs.
    block = cockpit["baseline"]
    assert block["debt_service"]["new_operating_annual"] == 12_000 and block["debt_service"]["new_operating_confirmed"] is True
    assert block["metrics"]["current"]["dscr"] == -0.03 and block["metrics"]["current"]["free_cash_flow"] == -799977
    assert (block["metrics"]["with_terms"]["ebitda"], block["metrics"]["with_terms"]["dscr"], block["metrics"]["with_terms"]["free_cash_flow"]) == (-22377, -0.19, -211977)
    assert next(row for row in block["targets"] if row["ratio"] == 1.25)["with_terms"] == round(1.25 * 177600 + 22377 + 12000, 2)

    seen, evaluation = readers(body, cockpit)
    plan = seen["plan"]
    # EBITDA, NOI and margin are the plan's; DSCR and free cash flow carry the loan's costs: (182,896.64 - 12,000) / 177,600.
    assert plan["proposed"] == (182896.64, 174496.64, 0.96, -6703.36, 11.63, 177600.0)
    assert plan["reviewed"] == (52164.0, 43764.0, 0.23, -137436.0, 3.42, 177600.0)
    assert plan["today"] == (-22377.0, -30777.0, -0.03, -799977.0, -2.49, 777600.0)
    assert seen["comparison"] == plan
    for reader in ("package", "partner"):
        for moment in ("proposed", "reviewed"):
            ebitda, noi, dscr, _cash, _margin, debt = plan[moment]
            assert seen[reader][moment] == (ebitda, noi, dscr, debt), (reader, moment)
    assert seen["marketplace"]["proposed"] == (182896.64, 174496.64, 0.96, -6703.36)
    # What is still to find for 1.25x counts them too, in the engine, the package and the comparison alike.
    assert round(evaluation["plan"]["target"]["proposed"], 2) == 51103.36 == evaluation["projection"]["proposed"]["target_gap_refinance"] == evaluation["comparison"]["target_gap"]["proposed"]
    assert evaluation["projection"]["proposed"]["annual_new_operating_expenses"] == 12_000
    # Against today's debt nothing changes: the costs belong to the loan.
    assert evaluation["projection"]["proposed"]["dscr_current"] == 0.24 and round(evaluation["plan"]["metrics"]["proposed"]["current_debt"]["free_cash_flow"], 2) == -594703.36
    # The marketplace's "loan terms alone" point agrees with the file's baseline.
    alone = operating_projection.project(body, cockpit, VERSION)["refinance_only"]
    assert (alone["dscr_with_refinance"], alone["cash_after_debt_annual"]) == (-0.19, -211977.0)

    facts = plan_service.version_facts(operating_plan_schema.key_facts(body), body, cockpit, evaluation, client_submission=False, goal=cockpit["_operating_goal"])
    text = words(document.build_html(body=body, financial_totals=facts))
    for line in ("DSCR +0.99x −0.03x → 0.96x", "Free cash flow +$793,274 −$799,977 → −$6,703", "EBITDA +$205,274 −$22,377 → $182,897",
                 "Needs $51,103 more EBITDA a year for 1.25x", "Operating costs the loan adds $12,000 / yr", "DSCR from terms alone −0.03x → −0.19x",
                 "The saved loan terms add $12,000 a year of operating costs; they come off DSCR and free cash flow with the terms, and leave EBITDA and NOI as the plan produces them."):
        assert line in text, line


def test_a_loan_cost_the_budget_has_not_confirmed_counts_as_zero_and_is_flagged():
    body, cockpit = file_with(loan_costs=None)
    assert cockpit["baseline"]["debt_service"]["new_operating_annual"] == 0 and cockpit["baseline"]["debt_service"]["new_operating_confirmed"] is False
    seen, evaluation = readers(body, cockpit)
    # Never a blank: the figures are the ones without the cost, and every reader is told it is missing.
    assert seen["plan"]["proposed"] == (182896.64, 174496.64, 1.03, 5296.64, 11.63, 177600.0) and seen["comparison"] == seen["plan"]
    assert seen["package"]["proposed"] == (182896.64, 174496.64, 1.03, 177600.0)
    assert evaluation["projection"]["proposed"]["annual_new_operating_expenses"] is None
    assert any("recurring operating cost whose amount is not confirmed" in flag for flag in evaluation["risk_flags"])
    facts = plan_service.version_facts(operating_plan_schema.key_facts(body), body, cockpit, evaluation, client_submission=False, goal=cockpit["_operating_goal"])
    assert "it is counted as zero until the budget is confirmed" in words(document.build_html(body=body, financial_totals=facts))
    # A file with no such cost says nothing about it.
    plain_body, plain_cockpit = file_with()
    plain = v1.evaluate(plain_body, plain_cockpit)
    assert plain_cockpit["baseline"]["debt_service"]["new_operating_confirmed"] is True and not any("not confirmed" in flag for flag in plain["risk_flags"])
