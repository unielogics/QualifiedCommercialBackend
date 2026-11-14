"""A v2 earnings plan read through the entry points and keys every existing reader uses."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.capital_partners.services import operating_projection
from app.enums import Role
from app.schemas import earnings_improvement as v1_schema
from app.services import earnings_improvement as v1
from app.services import earnings_plan_envelope as envelope
from app.services import earnings_plan_service, financial_plan_operating, operating_plan_service
from app.services.operating_plan_guidance import client_evaluation
from app.tests.earnings_plan_fixture import REVIEWED_IN_DESIGN, file_body, file_cockpit, item_id

STAFF = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
V1_KEYS = set(v1.evaluate(v1_schema.empty_body(), file_cockpit()))


def approve(body, cockpit, design_ids=REVIEWED_IN_DESIGN, *, now=None):
    return earnings_plan_service.review(body, cockpit, [item_id(key) for key in design_ids], "approved", "Lease and price list checked.", STAFF,
                                        baseline_treatment="incremental_to_baseline", baseline_reconciliation_note="Not in the 2025 return.", now=now)


def test_the_v1_entry_point_reads_a_v2_plan_and_keeps_every_key_its_readers_use():
    cockpit = file_cockpit()
    result = v1.evaluate(file_body(), cockpit)
    assert result["schema_version"] == "earnings-plan-v2"
    assert V1_KEYS <= set(result)
    assert set(result["projection"]) == {"proposed", "approved"} and set(result["totals"]) == {"proposed", "approved"}
    for row in result["actions"]:
        assert {"id", "label", "kind", "status", "excluded", "review_state", "net_annual_impact", "first_year_impact", "included_in_approved_projection",
                "proposed_math_valid", "issues", "risk_flags", "source_file_ids", "explanation", "calculation"} <= set(row)
    assert result["context_stamp"] == v1.context_stamp(cockpit)


def test_before_and_after_are_the_design_numbers_on_the_file():
    result = v1.evaluate(file_body(), file_cockpit())
    assert result["comparison"]["baseline"] == {"earnings": -22377.0, "noi": -30777.0, "dscr": -0.03, "cash_after_debt": -799977.0, "operating_margin": -2.49, "debt_service": 777600.0}
    assert result["comparison"]["proposed"] == {"earnings": 182896.64, "noi": 174496.64, "dscr": 1.03, "cash_after_debt": 5296.64, "operating_margin": 11.63, "debt_service": 177600.0}
    assert result["comparison"]["loan_basis"] == "saved_terms" and result["comparison"]["target_gap"]["proposed"] == 39103.36
    proposed = result["projection"]["proposed"]
    assert (proposed["status"], proposed["earnings"], proposed["noi"], proposed["dscr_current"], proposed["dscr_with_refinance"]) == (
        "unreviewed_illustration", 182896.64, 174496.64, 0.24, 1.03)
    assert (proposed["first_year_earnings"], proposed["first_year_coverage_impact"], proposed["target_gap_refinance"]) == (145663.28, 168040.28, 39103.36)
    totals = result["totals"]["proposed"]
    assert (totals["annual_impact"], totals["first_year_impact"], totals["count"], totals["complete"]) == (205273.64, 168040.28, 11, True)
    assert result["baseline"]["earnings"] == -22377 and result["baseline"]["noi"] == -30777 and result["baseline"]["revenue"] == 1236000
    # The plan's own figures ride along for the plan screens and the plan document.
    assert result["plan"]["proposed"]["ebitda"] == 182896.64 and [row["label"] for row in result["plan"]["bridge"]["proposed"]] == list(envelope.engine.AREAS)
    assert result["missing"] == [] and result["risk_flags"] == []


def test_without_saved_terms_coverage_stays_on_todays_debt():
    result = v1.evaluate(file_body(), file_cockpit(terms=False))
    assert result["comparison"]["loan_basis"] == "current_debt"
    assert result["comparison"]["proposed"]["debt_service"] == 777600 and result["comparison"]["proposed"]["dscr"] == 0.24
    assert result["projection"]["proposed"]["dscr_with_refinance"] is None and result["projection"]["proposed"]["refinance_annual_debt_service"] is None
    assert result["released_cash_flow"]["available"] is False and result["plan"]["released"]["available_monthly"] == 0


def test_what_is_left_is_one_list_and_never_blanks_a_figure():
    result = v1.evaluate(file_body(), file_cockpit())
    assert [row["code"] for row in result["plan"]["to_finish"]] == ["confirm_source"] * 3 + ["answer_category"] * 2 + ["explain_change"] * 2 + ["attest"]
    assert [row["label"] for row in result["plan"]["to_finish"]][:5] == [
        "Confirm Utilities source amount", "Confirm Repairs and maintenance source amount", "Confirm Other source amount",
        "Answer Payroll taxes: any current cost?", "Answer Transportation: any current cost?"]
    # Two changes still lack their explanation, yet they are counted: the plan computes with eight things left to do.
    assert result["totals"]["proposed"]["complete"] is True and all(row["proposed_math_valid"] for row in result["actions"])
    progress = operating_plan_service.completion(file_body(), file_cockpit())
    assert (progress["complete"], progress["missing_count"], progress["active_expenses"], progress["active_adjustments"]) == (False, 8, 14, 11)
    assert progress["missing"][0] == "Confirm Utilities source amount" and progress["released_cash_flow"]["complete"] is True


def test_an_approval_counts_only_with_a_reviewer_and_the_figures_it_was_given_to():
    cockpit = file_cockpit()
    # An approval written into the body by hand has no reviewer: it does not count.
    forged = v1.evaluate(file_body(keep_design_statuses=True), cockpit)
    assert forged["projection"]["approved"]["earnings"] == -22377 and forged["totals"]["approved"]["count"] == 0
    assert [row["review_state"] for row in forged["actions"]].count("stale") == 3
    assert any("need another review" in flag for flag in forged["risk_flags"])

    reviewed = approve(file_body(), cockpit)
    result = v1.evaluate(reviewed, cockpit)
    assert result["projection"]["approved"]["status"] == "reviewed_illustration"
    # The design's reviewed scenario: the price list, the lease amendment and the cancelled tool.
    assert result["projection"]["approved"]["earnings"] == 52164 and result["projection"]["approved"]["noi"] == 43764
    assert result["comparison"]["approved"]["dscr"] == 0.29 and result["totals"]["approved"]["count"] == 3
    assert [row["review_state"] for row in result["actions"]].count("approved") == 3 and result["all_actions_reviewed"] is False

    # The source moves under the approval: it is asked for again instead of silently carrying over.
    other = deepcopy(cockpit)
    other["baseline"]["figures"]["revenue"] += 1000
    moved = v1.evaluate(reviewed, other)
    assert moved["totals"]["approved"]["count"] == 0 and len(moved["plan"]["stale_reviews"]) == 3


def test_coverage_takes_the_reviewed_scenario_whole_because_changes_interact():
    cockpit = file_cockpit()
    evaluation = v1.evaluate(approve(file_body(), cockpit), cockpit)
    approved = [row for row in evaluation["actions"] if row["included_in_approved_projection"]]
    whole = evaluation["additive_totals"]["approved"]
    assert whole == {"annual_impact": 74541.0, "first_year_impact": 63811.75, "count": 3}
    # Each change's own effect is what the full plan loses without it, so those three do not add up to the reviewed scenario.
    assert round(sum(row["net_annual_impact"] for row in approved), 2) != whole["annual_impact"]
    context = {"available": True, "state": "published", "version_number": 2, "version_id": "v", "evaluation": evaluation}
    adjustment = financial_plan_operating.coverage_adjustment(context, cockpit["plan"]["record"], cockpit["dscr"])
    assert (adjustment["status"], adjustment["applied"], adjustment["applied_count"]) == ("applied", True, 3)
    assert adjustment["applied_annual_impact"] == 63811.75 and adjustment["annual_impact"] == 74541.0


def test_shareable_package_and_client_view_hold_no_review_reason_or_reviewer():
    cockpit = file_cockpit(goal=215_000)
    body = approve(file_body(), cockpit)
    evaluation = v1.evaluate(body, cockpit)
    package = operating_plan_service.shareable_package(evaluation, cockpit["operating_evidence"])
    assert package["projection"]["proposed"]["earnings"] == 182896.64 and len(package["adjustments"]) == 11
    assert package["goal_progress"]["target"] == 215000 and package["goal_progress"]["proposed_gap"] == 40503.36
    assert package["cash_bridge"]["recurring_cash_released_monthly"] == 50000 and package["cash_bridge"]["required"] is False
    safe = client_evaluation(evaluation)
    assert safe["plan"]["proposed"]["ebitda"] == 182896.64 and safe["comparison"]["proposed"]["dscr"] == 1.03
    for text in (str(package), str(safe), str(earnings_plan_service.client_body(body))):
        assert "Lease and price list checked." not in text and str(STAFF.id) not in text and "Not in the 2025 return." not in text


def test_source_figures_that_moved_ask_for_confirmation_again():
    cockpit = file_cockpit()
    body = file_body()
    body["source"]["stamp"] = envelope.baseline_stamp(envelope.engine_baseline(cockpit))
    assert v1.evaluate(body, cockpit)["plan"]["source_changed"] is False
    body["source"]["stamp"] = "0" * 64
    result = v1.evaluate(body, cockpit)
    assert result["plan"]["source_changed"] is True and [row["code"] for row in result["plan"]["warnings"]] == ["source_changed"]
    assert [row["code"] for row in result["plan"]["to_finish"]].count("confirm_source") == 10
    # The figures still compute on today's source.
    assert result["comparison"]["proposed"]["earnings"] == 182896.64


def test_a_file_with_no_earnings_source_keeps_the_plan_readable():
    cockpit = file_cockpit(analyses=[])
    assert envelope.engine_baseline(cockpit)["available"] is False
    result = v1.evaluate(file_body(), cockpit)
    assert result["projection"]["proposed"]["status"] == "unavailable" and result["projection"]["proposed"]["earnings"] is None
    assert result["comparison"]["proposed"]["earnings"] is None and result["missing"]
    # What the changes are worth is still known; only the figure they start from is not.
    assert result["totals"]["proposed"]["annual_impact"] == 205273.64
    # One thing stands first on the list: the source. Nothing asks to reconcile to figures that are not there.
    codes = [row["code"] for row in result["plan"]["to_finish"]]
    assert codes[0] == "no_source" and "reconcile_revenue" not in codes and "confirm_source" not in codes
    assert operating_plan_service.completion(file_body(), cockpit)["missing_count"] == len(codes)


def test_assigning_more_released_cash_than_there_is_is_a_warning_not_a_block():
    cockpit = file_cockpit()
    body = file_body()
    body["allocations"][2]["monthly_amount"] = 60_000
    result = v1.evaluate(body, cockpit)
    released = result["released_cash_flow"]
    assert (released["complete"], released["status"], released["unallocated_monthly"]) == (False, "incomplete", -14200.0)
    assert result["plan"]["warnings"][0]["code"] == "over_allocated"
    assert all(row["code"] != "over_allocated" for row in result["plan"]["to_finish"])


def test_marketplace_projection_reads_a_saved_v2_plan():
    cockpit = file_cockpit()
    saved = {"id": "v2", "version_number": 2, "state": "published", "saved_at": "2026-11-12T10:00:00Z"}
    result = operating_projection.project(file_body(), cockpit, saved)
    assert result["status"] == "conditional" and result["qualification"] == "proposed"
    assert result["current"]["earnings_annual"] == -22377 and result["current"]["dscr_current_debt"] == -0.03
    assert result["refinance_only"]["dscr_with_refinance"] == -0.13
    proposed = result["proposed_operating"]
    assert (proposed["earnings_annual"], proposed["noi_annual"], proposed["dscr_with_refinance"], proposed["cash_after_debt_annual"]) == (182896.64, 174496.64, 1.03, 5296.64)
    assert result["reviewed_operating"]["earnings_annual"] is None
    everything = [item_id(key) for key in ("f:s1:1", "f:s2:0", "f:s3:1", "l:c1", "l:rent", "l:sal", "l:ins", "l:sw", "l:o1", "gr:g1", "na:a1")]
    body = file_body()
    for block in ("changes", "growth", "activities"):
        for row in body[block]:
            row["note"] = row["note"] or "Signed contracts on file."
    reviewed = earnings_plan_service.review(body, cockpit, everything, "approved", "All checked.", STAFF, baseline_treatment="incremental_to_baseline",
                                            baseline_reconciliation_note="Not in the return.", now=datetime(2026, 11, 12, tzinfo=UTC))
    assert operating_projection.project(reviewed, cockpit, saved)["qualification"] == "reviewed"


def test_a_v1_plan_is_untouched_by_the_dispatch():
    cockpit = file_cockpit()
    result = v1.evaluate(v1_schema.empty_body(), cockpit)
    assert result["schema_version"] == "earnings-improvement-v1" and "plan" not in result and "additive_totals" not in result


@pytest.mark.parametrize("goal", [None, 215_000])
def test_goal_blocks_appear_only_when_staff_set_a_goal(goal):
    result = v1.evaluate(file_body(), file_cockpit(goal=goal))
    assert ("goal_progress" in result) is bool(goal) and ("cash_bridge" in result) is bool(goal)
    assert result["plan"]["goal"]["amount"] == goal
