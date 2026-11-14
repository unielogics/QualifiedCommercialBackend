"""The Python plan engine against the Earnings Plan design's own numbers."""
from __future__ import annotations

import copy

import pytest

from app.services import earnings_plan_math as engine
from app.tests.earnings_plan_fixture import (
    QUESTIONS,
    REFERENCE,
    item_id,
    reference_baseline,
    reference_body,
)

EXPECTED = REFERENCE["expected"]
KINDS = {"chg": "change", "gr": "growth", "na": "activity"}


def near(value, expected):
    return value == pytest.approx(expected, abs=1e-4)


@pytest.fixture(scope="module")
def result():
    return engine.evaluate(reference_body(), reference_baseline(), goal=REFERENCE["plan"]["noi_goal"], questions=QUESTIONS)


def test_revenue_model_reproduces_the_source_and_reads_each_line_rate(result):
    model = EXPECTED["model"]
    assert result["model"]["volume"] == pytest.approx(model["volume"])
    assert result["model"]["revenue"] == pytest.approx(model["revenue"])
    assert near(result["model"]["total"], model["total"])
    assert result["model"]["reconciles"] is True
    assert result["rates"] == pytest.approx(EXPECTED["rates"])
    assert result["behaviors"]["c1"] == "unit" and result["behaviors"]["c2"] == "pct" and result["behaviors"]["sal"] == "fixed"


def test_items_keep_their_area_start_and_review_state(result):
    got = {row["id"]: row for row in result["items"]}
    assert len(got) == len(EXPECTED["items"])
    for expected in EXPECTED["items"]:
        row = got[item_id(expected["id"])]
        assert (row["kind"], row["area"], row["start_month"], row["included"], row["reviewed"]) == (
            KINDS[expected["kind"]], expected["area"], expected["start"], expected["included"], expected["reviewed"])


@pytest.mark.parametrize("view", ["proposed", "reviewed"])
def test_run_rate_scenarios_match_the_design(result, view):
    expected, got = EXPECTED[view], result[view]
    for key in ("revenue", "cogs", "opex", "noi", "ebitda", "activity_spend"):
        assert near(got[key], expected[key]), key
    assert got["lines"] == pytest.approx(expected["lines"], abs=1e-4)
    assert got["stream_revenue"] == pytest.approx(expected["stream_revenue"], abs=1e-4)
    assert got["stream_volume"] == pytest.approx(expected["stream_volume"], abs=1e-4)
    for stream, values in expected["factors"].items():
        assert got["factors"][stream] == pytest.approx(values)


@pytest.mark.parametrize("view", ["proposed", "reviewed"])
def test_first_year_follows_each_change_start_month(result, view):
    for got, expected in zip(result["months"][view], EXPECTED["months"][view], strict=True):
        assert near(got["ebitda"], expected["ebitda"]) and near(got["noi"], expected["noi"])
    assert near(result["first_year"][view]["ebitda"], EXPECTED["first_year"][view]["ebitda"])
    assert near(result["first_year"][view]["noi"], EXPECTED["first_year"][view]["noi"])


def test_each_change_shows_what_the_plan_loses_without_it(result):
    for design_id, expected in EXPECTED["effects"].items():
        got = result["effects"][item_id(design_id)]
        assert near(got["ebitda"], expected["ebitda"]), design_id
        assert near(got["revenue"], expected["revenue"]), design_id
        assert near(got["first_year"], expected["first_year"]), design_id


def test_section_totals_are_leave_one_out_and_the_bridge_adds_up(result):
    assert [row["ebitda"] for row in result["sections"]] == pytest.approx(EXPECTED["sections"], abs=1e-4)
    for view, scenario in (("proposed", "proposed_with_terms"), ("reviewed", "reviewed_with_terms")):
        expected = REFERENCE["scenarios"][scenario]["bridge"]
        assert [(row["label"], pytest.approx(row["value"], abs=1e-4)) for row in expected] == [(row["label"], row["value"]) for row in result["bridge"][view]]
        # The bridge is cumulative, so it lands exactly on the plan figure.
        assert near(REFERENCE["baseline"]["ebitda"] + sum(row["value"] for row in result["bridge"][view]), result[view]["ebitda"])
    # Changes interact, so the sections' own totals do not add up to the plan.
    assert sum(row["ebitda"] for row in result["sections"]) != pytest.approx(result["proposed"]["ebitda"] - REFERENCE["baseline"]["ebitda"], abs=1)


@pytest.mark.parametrize(("scenario", "view", "debt"), [
    ("proposed_with_terms", "proposed", "with_terms"),
    ("reviewed_with_terms", "reviewed", "with_terms"),
    ("proposed_current_debt", "proposed", "current_debt"),
])
def test_six_metrics_before_and_after(result, scenario, view, debt):
    expected = {row["label"]: row for row in REFERENCE["scenarios"][scenario]["metrics"]}
    today, after = result["metrics"]["today"]["current_debt"], result["metrics"][view][debt]
    for label, key in (("EBITDA", "ebitda"), ("NOI", "noi"), ("DSCR", "dscr"), ("Free cash flow", "free_cash_flow"),
                       ("Operating margin", "operating_margin"), ("Debt service", "debt_service")):
        assert near(today[key], expected[label]["before"]), label
        assert near(after[key], expected[label]["after"]), label


def test_goal_released_cash_and_the_coverage_target(result):
    assert near(result["goal"]["amount"], EXPECTED["goal"]["amount"])
    assert near(result["goal"]["proposed_gap"], EXPECTED["goal"]["gap"])
    released = REFERENCE["scenarios"]["proposed_with_terms"]["released"]
    assert near(result["released"]["available_monthly"], released["available_monthly"])
    assert near(result["released"]["allocated_monthly"], released["allocated_monthly"])
    assert result["released"]["over_allocated"] is False
    # "Still to find for 1.25x" on the file page.
    assert round(result["target"]["proposed"]) == 39_103


def test_to_finish_is_one_list_in_step_order(result):
    assert [(row["label"], row["step"]) for row in result["to_finish"]] == [(row["label"], row["step"]) for row in EXPECTED["to_finish"]]
    assert [row["code"] for row in result["to_finish"]] == ["confirm_source"] * 3 + ["answer_category"] * 2 + ["explain_change"] * 2 + ["attest"]


def test_no_changes_reproduces_the_baseline_exactly():
    body = reference_body()
    body.update(changes=[], growth=[], activities=[])
    result = engine.evaluate(body, reference_baseline())
    for view in ("proposed", "reviewed"):
        assert near(result[view]["ebitda"], REFERENCE["baseline"]["ebitda"])
        assert near(result[view]["noi"], REFERENCE["baseline"]["noi"])
        assert near(result[view]["revenue"], REFERENCE["baseline"]["revenue"])
    assert all(row["value"] == pytest.approx(0, abs=1e-6) for row in result["bridge"]["proposed"])


def test_a_source_that_is_not_fully_itemized_still_starts_from_its_own_figures():
    body = reference_body()
    # Drop a cost line: the lines no longer add up to the return.
    body["lines"] = [line for line in body["lines"] if line["id"] != "rep"]
    body["changes"] = []
    body["growth"] = []
    body["activities"] = []
    result = engine.evaluate(body, reference_baseline())
    assert near(result["proposed"]["noi"], REFERENCE["baseline"]["noi"])
    assert near(result["proposed"]["ebitda"], REFERENCE["baseline"]["ebitda"])


def test_a_switched_off_or_rejected_change_leaves_the_plan_and_keeps_its_own_effect():
    body = reference_body()
    rent = next(row for row in body["changes"] if row["target"] == {"kind": "line", "line_id": "rent"})
    baseline = reference_baseline()
    full = engine.evaluate(copy.deepcopy(body), baseline)
    rent["included"] = False
    off = engine.evaluate(copy.deepcopy(body), baseline)
    assert near(full["proposed"]["ebitda"] - off["proposed"]["ebitda"], 28_805)
    assert near(off["effects"][rent["id"]]["ebitda"], 28_805)
    assert not next(row for row in off["items"] if row["id"] == rent["id"])["included"]
    rent.update(included=True, status="rejected")
    rejected = engine.evaluate(copy.deepcopy(body), baseline)
    assert near(rejected["proposed"]["ebitda"], off["proposed"]["ebitda"])
    assert near(rejected["reviewed"]["ebitda"], off["reviewed"]["ebitda"])


def test_pricing_and_whole_group_changes():
    body = reference_body()
    body.update(growth=[], activities=[])
    body["changes"] = [
        {"id": item_id("pricing"), "target": {"kind": "pricing"}, "direction": "up", "mode": "percent", "value": 2, "start_month": 1, "note": "List prices rise 2%."},
        {"id": item_id("g:oth"), "target": {"kind": "group", "group": "oth"}, "direction": "down", "mode": "amount", "value": 2_092, "start_month": 1, "note": "Trim other costs."},
    ]
    result = engine.evaluate(body, reference_baseline())
    proposed = result["proposed"]
    # 2% on every stream; the percent-of-revenue lines (fleet parts, card processing) follow revenue up.
    assert near(proposed["revenue"], 1_236_000 * 1.02)
    assert near(proposed["lines"]["c2"], 118_800 * 1.02)
    # The group moves in proportion: card processing first follows revenue, then both lines share the cut.
    other_before = 24_720 * 1.02 + 2_372
    assert near(proposed["lines"]["o1"] + proposed["lines"]["o2"], other_before - 2_092)
    assert near(proposed["lines"]["o1"] / proposed["lines"]["o2"], 24_720 * 1.02 / 2_372)
    assert [row["area"] for row in result["items"]] == [0, 4]
    assert result["bridge"]["proposed"][0]["label"] == "Pricing" and result["bridge"]["proposed"][0]["value"] > 0
