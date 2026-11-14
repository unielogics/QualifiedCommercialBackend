"""The Python plan engine against the Earnings Plan design's own numbers."""
from __future__ import annotations

import copy

import pytest

from app.services import earnings_plan_math as engine
from app.tests.earnings_plan_fixture import (
    QUESTIONS,
    REFERENCE,
    item_id,
    measured_body,
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


def test_operating_costs_the_loan_adds_come_off_coverage_with_the_terms_and_off_nothing_else(result):
    # The same figures as the dashboard's earningsPlanMath.test.ts: (182,896.64 - 12,000) / 177,600.
    costly = engine.evaluate(reference_body(), {**reference_baseline(), "loan_operating_annual": 12_000}, goal=REFERENCE["plan"]["noi_goal"], questions=QUESTIONS)
    with_terms = costly["metrics"]["proposed"]["with_terms"]
    assert near(with_terms["ebitda"], 182_896.64) and near(with_terms["noi"], 174_496.64)
    assert near(with_terms["dscr"], 170_896.64 / 177_600) and near(with_terms["free_cash_flow"], -6_703.36)
    assert near(with_terms["operating_margin"], result["metrics"]["proposed"]["with_terms"]["operating_margin"])
    assert near(costly["metrics"]["today"]["with_terms"]["dscr"], (-22_377 - 12_000) / 177_600)
    assert near(costly["metrics"]["reviewed"]["with_terms"]["free_cash_flow"], 52_164 - 12_000 - 177_600)
    assert near(costly["target"]["proposed"], 51_103.36) and costly["debt_service"]["loan_operating"] == 12_000
    # Against today's debt the loan does not exist, so neither do its costs.
    assert costly["metrics"]["proposed"]["current_debt"] == result["metrics"]["proposed"]["current_debt"] and costly["proposed"] == result["proposed"]
    # Without saved terms there is no loan to bring them.
    none = engine.evaluate(reference_body(), {**reference_baseline(), "debt_service_with_terms": None, "loan_operating_annual": 12_000}, questions=QUESTIONS)
    assert none["debt_service"]["loan_operating"] == 0 and none["metrics"]["proposed"]["with_terms"] is None
    assert near(none["target"]["proposed"], 1.25 * 777_600 - 182_896.64) and result["debt_service"]["loan_operating"] == 0


def test_a_cost_that_follows_revenue_can_be_changed_by_dollars_a_year():
    # The same figures as the dashboard's earningsPlanMath.test.ts.
    body = reference_body()
    body.update(growth=[], activities=[])
    words = {"direction": "down", "mode": "annual", "start_month": 1, "note": "Agreed with the owner."}
    body["changes"] = [
        {"id": item_id("pricing"), "target": {"kind": "pricing"}, "direction": "up", "mode": "percent", "value": 2, "start_month": 1, "note": "List prices rise 2%."},
        # Card processing is 2.0% of all revenue ($24,720 on the source): $4,720 a year less is $20,000 at the source's revenue.
        {**words, "id": item_id("l:o1"), "target": {"kind": "line", "line_id": "o1"}, "value": 4_720},
        # Detail supplies are $105 a unit on 3,600 units ($378,000): $18,000 a year less is $100 a unit.
        {**words, "id": item_id("l:c1"), "target": {"kind": "line", "line_id": "c1"}, "value": 18_000},
        # A fixed cost has no driver: dollars a year is simply the amount.
        {**words, "id": item_id("l:sal"), "target": {"kind": "line", "line_id": "sal"}, "value": 30_194},
    ]
    lines = engine.evaluate(body, reference_baseline())["proposed"]["lines"]
    # The new rate follows the driver: revenue is 2% higher, so the percent-of-revenue cost is too; volume did not move.
    assert near(lines["o1"], 20_000 * 1.02) and near(lines["c1"], 360_000) and near(lines["sal"], 318_000)
    # Without the pricing change the cost lands exactly on the dollars asked for.
    body["changes"] = body["changes"][1:]
    assert near(engine.evaluate(body, reference_baseline())["proposed"]["lines"]["o1"], 20_000)
    # Raising works the same way, and a cut larger than the cost stops at zero.
    body["changes"][0].update(direction="up", value=5_280)
    assert near(engine.evaluate(body, reference_baseline())["proposed"]["lines"]["o1"], 30_000)
    body["changes"][0].update(direction="down", value=99_000)
    assert engine.evaluate(body, reference_baseline())["proposed"]["lines"]["o1"] == 0


# ---------------------------------------------------------------------------
# Revenue is the known figure: a stream states it, and the price of one unit comes from dividing.
# The same figures as the dashboard's earningsPlanMath.test.ts.
# ---------------------------------------------------------------------------

def metric(factor_id: str, direction: str, value: float, mode: str = "percent") -> dict:
    return {"id": item_id(f"f:s1:{factor_id}"), "target": {"kind": "factor", "stream_id": "s1", "factor_id": factor_id}, "direction": direction,
            "mode": mode, "value": value, "start_month": 1, "note": "Agreed with the owner."}


def test_a_stream_that_is_all_revenue_divides_it_by_its_volume():
    result = engine.evaluate(measured_body(), reference_baseline())
    model = result["model"]
    # $1,236,000 a year is $103,000 a month: 400 units a month is $257.50 a unit, 1,200 hours a month $85.83 an hour.
    assert model["total"] == 1_236_000 and model["reconciles"] is True and model["difference"] == 0
    assert model["revenue"] == {"s1": 1_236_000} and model["volume"] == {"s1": 4_800} and model["measures"] == {"s1:f2": 14_400}
    assert model["factors"] == {"s1": [400, 257.5, 1_200]}
    # Supplies are a cost per unit, wages a cost per labor hour, card processing a share of revenue.
    assert result["rates"]["c1"] == pytest.approx(78.75) and result["rates"]["sal"] == pytest.approx(348_194 / 14_400) and result["rates"]["o1"] == pytest.approx(0.02)
    assert result["behaviors"]["sal"] == "unit"
    # With nothing proposed the plan is the source, to the dollar.
    assert near(result["proposed"]["noi"], -30_777) and near(result["proposed"]["revenue"], 1_236_000)
    volume, hours = result["per_unit"]
    assert (volume["stream_id"], volume["factor_id"], hours["factor_id"]) == ("s1", None, "f2")
    # Every cost on the source ($601,886 + $664,891) over the same units: one unit costs more than it brings in.
    assert volume["today"] == pytest.approx({"quantity": 4_800, "revenue": 257.5, "cost": 263.911875, "noi": -6.411875})
    assert hours["today"] == pytest.approx({"quantity": 14_400, "revenue": 85.8333333333, "cost": 87.970625, "noi": -2.1372916667})
    assert volume["proposed"] == pytest.approx(volume["today"]) and hours["reviewed"] == pytest.approx(hours["today"])


def test_volume_price_and_a_measure_alongside_each_move_what_follows_them():
    body = measured_body()
    body["changes"] = [metric("f0", "up", 10), metric("f2", "down", 5), metric("f1", "up", 5, "amount")]
    result = engine.evaluate(body, reference_baseline())
    plan = result["proposed"]
    # 440 units at $262.50; hours follow the units and are then cut 5% (1,200 x 1.1 x 0.95 a month).
    assert plan["factors"]["s1"] == pytest.approx([440, 262.5, 1_140]) and plan["stream_volume"]["s1"] == pytest.approx(5_280)
    assert plan["measures"]["s1:f2"] == pytest.approx(15_048)
    assert near(plan["revenue"], 1_386_000)
    assert near(plan["lines"]["c1"], 415_800) and near(plan["lines"]["sal"], 363_862.73) and near(plan["lines"]["o1"], 27_720)
    assert near(plan["noi"], 62_754.27)
    volume, hours = result["per_unit"]
    assert volume["proposed"] == pytest.approx({"quantity": 5_280, "revenue": 262.5, "cost": 250.6147215909, "noi": 11.8852784091})
    assert hours["proposed"] == pytest.approx({"quantity": 15_048, "revenue": 92.1052631579, "cost": 87.9349900319, "noi": 4.170273126})
    # Nothing here is reviewed, so the reviewed figures stay where the source is.
    assert volume["reviewed"] == pytest.approx(volume["today"])
    # Cutting hours alone moves the wages that follow them and nothing else.
    body["changes"] = [metric("f2", "down", 5)]
    alone = engine.evaluate(body, reference_baseline())["proposed"]
    assert near(alone["revenue"], 1_236_000) and near(alone["lines"]["sal"], 348_194 * 0.95) and near(alone["lines"]["c1"], 378_000)
    assert near(alone["noi"], -30_777 + 348_194 * 0.05)


def test_streams_always_add_up_to_the_source_while_one_holds_the_rest():
    body = measured_body()
    body["streams"] = [
        {"id": "s1", "name": "Other revenue", "type": "custom", "remainder": True, "factors": [{"id": "f1", "label": "Revenue per month", "value": None, "unit": "$", "volume": False}]},
        {"id": "s2", "name": "Detail packages", "type": "units", "monthly_revenue": 75_000, "factors": [
            {"id": "f0", "label": "Units sold", "value": 300, "unit": "units / mo", "volume": True}, {"id": "f1", "label": "Price per unit", "value": None, "unit": "$", "volume": False}]},
    ]
    body["lines"] = [{**line, "behavior": "fixed", "link": "all", "measure": None} for line in body["lines"]]
    base = reference_baseline()
    model = engine.evaluate(body, base)["model"]
    # $75,000 a month over 300 units is $250 a unit; the other stream is what is left of $103,000 a month.
    assert model["revenue"] == {"s1": 336_000, "s2": 900_000} and model["total"] == 1_236_000 and model["reconciles"] is True
    assert model["factors"] == {"s1": [28_000], "s2": [300, 250]} and model["volume"] == {"s1": 0, "s2": 3_600}
    # A stream with nothing to divide by has no per-unit figures; the measured one carries its share of the costs.
    rows = engine.evaluate(body, base)["per_unit"]
    assert [(row["stream_id"], row["factor_id"]) for row in rows] == [("s2", None)]
    assert rows[0]["today"] == pytest.approx({"quantity": 3_600, "revenue": 250, "cost": 256.2251213592, "noi": -6.2251213592})
    # The source changes: the stated stream keeps what it states and the rest follows.
    moved = engine.evaluate(body, {**base, "revenue": 1_300_000})["model"]
    assert moved["revenue"] == {"s1": 400_000, "s2": 900_000} and moved["reconciles"] is True
    # A stream with no volume typed yet still brings its revenue, and a change to that volume has nothing to move.
    unmeasured = copy.deepcopy(body)
    unmeasured["streams"][1]["factors"][0]["value"] = None
    unmeasured["changes"] = [{**metric("f0", "up", 10), "target": {"kind": "factor", "stream_id": "s2", "factor_id": "f0"}}]
    result = engine.evaluate(unmeasured, base)
    assert result["model"]["revenue"]["s2"] == 900_000 and result["model"]["volume"]["s2"] == 0 and result["model"]["factors"]["s2"] == [0, 75_000]
    assert near(result["proposed"]["revenue"], 1_236_000) and result["per_unit"] == []
    # A price change on the stream that holds the rest moves that stream alone.
    priced = copy.deepcopy(body)
    priced["changes"] = [{**metric("f1", "up", 10), "target": {"kind": "factor", "stream_id": "s1", "factor_id": "f1"}}]
    assert engine.evaluate(priced, base)["proposed"]["stream_revenue"] == pytest.approx({"s1": 369_600, "s2": 900_000})
    # Streams that state more than the source leave nothing for the rest, and the model says by how much it is over.
    body["streams"][1]["monthly_revenue"] = 110_000
    over = engine.evaluate(body, base)
    assert over["model"]["revenue"] == {"s1": 0, "s2": 1_320_000} and over["model"]["difference"] == 84_000 and over["model"]["reconciles"] is False
    assert [item["code"] for item in over["to_finish"] if item["step"] == 1] == ["reconcile_revenue"]


def test_a_plan_described_as_units_times_price_still_gets_per_unit_figures(result):
    # The design's three streams: $250 a unit, $1,500 a contract, $100 an hour, each carrying its share of every cost.
    rows = {row["stream_id"]: row for row in result["per_unit"]}
    assert [row["factor_id"] for row in result["per_unit"]] == [None, None, None]
    assert rows["s1"]["today"] == pytest.approx({"quantity": 3_600, "revenue": 250, "cost": 256.2251213592, "noi": -6.2251213592})
    assert rows["s2"]["today"]["revenue"] == pytest.approx(1_500) and rows["s3"]["today"]["revenue"] == pytest.approx(100)
    assert rows["s1"]["proposed"] == pytest.approx({"quantity": 4_080, "revenue": 262, "cost": 231.5407474683, "noi": 30.4592525317})
    # Shared by revenue, the streams' costs add back up to every cost on the source.
    assert sum(row["today"]["cost"] * row["today"]["quantity"] for row in result["per_unit"]) == pytest.approx(601_886 + 664_891)
    # One sale a month, as plans were once seeded, counts nothing: there is no per-unit figure to print.
    seeded = reference_body()
    seeded.update(changes=[], growth=[], activities=[])
    seeded["lines"] = [{**line, "behavior": "fixed", "link": "all"} for line in seeded["lines"]]
    seeded["streams"] = [{"id": "s1", "name": "All revenue", "type": "custom", "factors": [
        {"id": "f0", "label": "Sales", "value": 1, "unit": "count", "volume": True}, {"id": "f1", "label": "Revenue per month", "value": 103_000, "unit": "$", "volume": False}]}]
    old = engine.evaluate(seeded, reference_baseline())
    assert old["model"]["total"] == 1_236_000 and old["per_unit"] == []
