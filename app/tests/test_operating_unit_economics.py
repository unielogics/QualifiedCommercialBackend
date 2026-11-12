"""Business volume is modeled once, beside immutable historical evidence."""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.schemas.earnings_improvement import completion, empty_body, validate_body
from app.services import earnings_improvement as engine
from app.services import operating_plan_service
from app.services import operating_unit_economics as units
from app.tests.test_earnings_improvement import NOW, action, approve, body, context, expense


def unit_plan(*, change=100, mode="absolute", **extra):
    costs = [expense(id=str(uuid4()), category="cogs", label="Delivery", current_amount=30000,
        cadence="annual", volume_behavior="variable", source_group_key="delivery"),
        expense(id=str(uuid4()), label="Premises", current_amount=1000, volume_behavior="fixed"),
        expense(id=str(uuid4()), category="salaries", label="Staff", current_amount=2500,
            volume_behavior="mixed", current_fixed_amount=1500)]
    proposal = action(kind="unit_volume", label="Additional service visits", driver_key="business-volume",
        expense_row_id=None, cadence="annual", unit_change_mode=mode, unit_change_value=change)
    fields = {"unit_label": "completed service visit", "historical_units_annual": 1000,
        "historical_revenue_annual": 100000, "historical_units_period_start": "2025-01-01",
        "historical_units_period_end": "2025-12-31", **extra}
    return body(proposal, expenses=costs, **fields)


def test_business_volume_revenue_and_all_variable_costs_flow_once_to_noi_and_dscr():
    raw, cockpit = unit_plan(), context()
    original = deepcopy((raw, cockpit))
    result = engine.evaluate(raw, cockpit, now=NOW)
    value = result["unit_economics"]
    assert value["proposed_units_annual"] == 1100
    assert value["current_revenue_per_unit"] == 100
    assert value["proposed_revenue_annual"] == 110000
    assert value["variable_cost_change_annual"] == 4200
    assert value["net_annual_impact"] == result["totals"]["proposed"]["annual_impact"] == 5800
    assert value["rows"][1]["current_average_cost_per_unit"] == 12
    assert value["rows"][1]["proposed_annual_amount"] == 12000  # Rent is not scaled.
    assert result["projection"]["proposed"]["noi"] == -9200
    assert result["projection"]["proposed"]["earnings"] == -4200
    assert result["projection"]["proposed"]["dscr_current"] == -.35
    assert result["projection"]["approved"]["noi"] is None
    assert (raw, cockpit) == original


def test_percent_and_absolute_changes_are_equivalent_and_staff_review_remains_separate():
    raw = unit_plan(mode="percent", change=10)
    result = engine.evaluate(approve(raw), context(), now=NOW)
    assert result["unit_economics"]["net_annual_impact"] == 5800
    assert result["projection"]["approved"]["noi"] == -9200
    assert result["baseline"]["noi"] == -15000


def test_local_per_unit_costs_are_not_scaled_twice_or_rewritten():
    raw = unit_plan()
    local = expense(category="cogs", cost_basis="per_unit", unit_label="delivery hour",
        current_units_per_period=200, current_unit_cost=10, current_fixed_amount=50,
        current_amount=2050, volume_behavior="mixed")
    raw["expenses"] = [validate_body({**empty_body(), "expenses": [local]})["expenses"][0]]
    before = deepcopy(raw)
    value = units.evaluate(raw)
    assert value["rows"][0]["current_annual_amount"] == 24600
    assert value["rows"][0]["current_fixed_annual_amount"] == 600
    assert value["rows"][0]["current_variable_cost_per_unit"] == 24
    assert value["rows"][0]["proposed_annual_amount"] == 27000
    assert value["net_annual_impact"] == 7600
    assert raw == before


@pytest.mark.parametrize("change", [None, 0])
def test_unknown_and_zero_historical_units_never_divide_or_fabricate_a_ratio(change):
    raw = unit_plan()
    raw["historical_units_annual"] = change
    value = units.evaluate(raw)
    assert value["current_revenue_per_unit"] is None
    assert all(row["current_average_cost_per_unit"] is None for row in value["rows"])
    assert value["net_annual_impact"] is None
    assert value["missing"]


@pytest.mark.parametrize("field,value", [("period_start", "2024-01-01"),
    ("historical_units_period_start", None), ("period_end", "2025-06-30")])
def test_changed_source_period_preserves_counts_but_blocks_use_until_reconfirmed(field, value):
    raw = unit_plan()
    raw[field] = value
    result = units.evaluate(raw)
    assert result["historical_units_annual"] == 1000
    assert result["net_annual_impact"] is None
    assert result["proposed_units_annual"] is None
    assert all(row["proposed_annual_amount"] is None for row in result["rows"])
    assert "historical_unit_reporting_period" in result["missing"]


@pytest.mark.parametrize("change", [{"volume_behavior": "unknown"}, {"current_amount": None},
    {"volume_behavior": "mixed", "current_fixed_amount": None}])
def test_unknown_classification_fixed_amount_or_nonrecurring_total_blocks_volume_only(change):
    raw = unit_plan()
    raw["expenses"][0].update(change)
    result = engine.evaluate(raw, context(), now=NOW)
    assert result["unit_economics"]["net_annual_impact"] is None
    assert result["totals"]["proposed"]["annual_impact"] is None
    assert result["unit_economics"]["rows"][0]["missing"]
    assert completion(raw)["missing"]


def test_one_time_cost_remains_disclosed_outside_recurring_volume_effects():
    raw = unit_plan()
    one_time = expense(id=str(uuid4()), label="Past fit-out", current_amount=5000, cadence="one_time")
    raw["expenses"].append(validate_body({**empty_body(), "expenses": [one_time]})["expenses"][0])
    result = units.evaluate(raw)
    assert result["net_annual_impact"] == 5800
    row = result["rows"][-1]
    assert row["one_time_amount"] == 5000 and not row["recurring"]
    assert row["current_annual_amount"] is row["proposed_annual_amount"] is None
    assert "excluded from recurring" in row["scope_note"]
    rent = next(group for group in result["categories"] if group["key"] == "rent")
    assert rent["current_annual_amount"] == 12000 and rent["one_time_amount"] == 5000


def test_zero_revenue_and_zero_variable_cost_are_known_not_missing():
    raw = unit_plan(historical_revenue_annual=0)
    for row in raw["expenses"]:
        row.update(volume_behavior="variable", current_amount=0, current_fixed_amount=0)
    result = units.evaluate(raw)
    assert result["net_annual_impact"] == 0
    assert result["current_revenue_per_unit"] == 0
    assert result["status"] == "calculated"


@pytest.mark.parametrize("kind", ["price_change", "product_cost_change", "revenue_change", "marketing_investment", "unit_volume"])
def test_overlapping_sales_actions_identify_the_conflicting_action_and_repair(kind):
    raw = unit_plan()
    second = action(id=str(uuid4()), kind=kind, expense_row_id=None)
    raw["adjustments"].append(second)
    result = engine.evaluate(raw, context(), now=NOW)
    assert any(second["id"] in issue and "Keep" in issue for issue in result["actions"][0]["issues"])
    assert not result["actions"][0]["proposed_math_valid"]


def test_review_binds_every_cost_and_unit_input_but_preserves_unrelated_legacy_reviews():
    raw = approve(unit_plan())
    for field, value in (("historical_units_annual", 2000), ("historical_revenue_annual", 120000),
                         ("historical_units_period_end", "2024-12-31")):
        changed = deepcopy(raw)
        changed[field] = value
        assert operating_plan_service.protect_reviews(changed, raw)["adjustments"][0]["status"] == "proposed"
    changed = deepcopy(raw)
    changed["expenses"][1]["current_amount"] = 1100
    assert operating_plan_service.protect_reviews(changed, raw)["adjustments"][0]["status"] == "proposed"
    old = approve(body())
    changed = {**old, "unit_label": "visit", "historical_units_annual": 500}
    assert operating_plan_service.protect_reviews(changed, old)["adjustments"][0]["status"] == "approved"
    assert "unit_economics" not in engine.evaluate(body(), context(), now=NOW)


def test_source_acceptance_cannot_be_forged_through_unit_inputs():
    raw, cockpit = unit_plan(), context()
    cockpit["operating_evidence"]["files"][0]["eligible_for_review"] = False
    with pytest.raises(HTTPException) as exc:
        approve(raw, cockpit)
    assert exc.value.status_code == 422


def test_source_revenue_and_changed_source_groups_require_specific_reconciliation():
    raw, cockpit = unit_plan(), context()
    raw["historical_revenue_annual"] = 110000
    result = engine.evaluate(raw, cockpit, now=NOW)
    assert result["unit_economics"]["source_revenue_annual"] == 100000
    assert result["unit_economics"]["revenue_reconciled"] is False
    assert result["unit_economics"]["net_annual_impact"] is None
    raw["historical_revenue_annual"] = 100000
    raw["expenses"][0]["source_group_key"] = "source:old-period:cogs"
    result = engine.evaluate(raw, cockpit, now=NOW)
    assert raw["expenses"][0]["id"] in " ".join(result["actions"][0]["issues"])
    assert "Reconcile or relink" in " ".join(result["actions"][0]["issues"])
    assert result["totals"]["proposed"]["annual_impact"] is None


@pytest.mark.parametrize("category", ["rent", "salaries", "payroll_taxes", "insurance", "utilities", "marketing", "software",
    "professional_fees", "supplies", "repairs", "transportation", "cogs", "merchant_processing", "other",
    "loan_payments", "income_taxes", "owner_draws", "transfers", "depreciation_amortization"])
def test_all_expense_categories_have_descriptive_ratios_without_turning_debt_or_da_into_earnings(category):
    raw = unit_plan()
    raw["expenses"] = [validate_body({**empty_body(), "expenses": [expense(category=category,
        current_amount=1000, volume_behavior="variable")]})["expenses"][0]]
    value = units.evaluate(raw)
    row = value["rows"][0]
    assert row["current_average_cost_per_unit"] == 12
    assert value["categories"][0]["current_average_cost_per_unit"] == 12
    assert value["net_annual_impact"] == (10000 if category in units.OUTSIDE_VOLUME else 8800)


@pytest.mark.parametrize("mode,change", [("absolute", -1001), ("percent", -101), ("percent", 1e15)])
def test_invalid_proposed_volume_is_not_a_profit_quote(mode, change):
    result = units.evaluate(unit_plan(mode=mode, change=change))
    assert result["status"] == "incomplete" and result["net_annual_impact"] is None


def test_zero_proposed_units_retains_fixed_cost_and_never_divides():
    result = units.evaluate(unit_plan(mode="percent", change=-100))
    assert result["proposed_units_annual"] == result["proposed_revenue_annual"] == 0
    assert result["net_annual_impact"] == -58000
    assert result["rows"][1]["proposed_annual_amount"] == 12000
    assert all(row["proposed_average_cost_per_unit"] is None for row in result["rows"])


def test_scalar_worksheet_roundtrip_preserves_units_period_behavior_and_signed_change():
    from app.services import operating_plan_schema, sheet_layout

    raw = unit_plan(mode="percent", change=-10)
    cells = sheet_layout.flatten("operating_plan", raw)
    assert cells["historical_units_annual"] == "1000.0"
    assert cells["historical_units_period_start"] == "2025-01-01"
    assert cells[f"adjustments.{raw['adjustments'][0]['id']}.unit_change_mode"] == "percent"
    # The worksheet sends changed cells; unrelated empty text is not a patch.
    changed = {key: value for key, value in cells.items() if key in {
        "unit_label", "historical_units_annual", "historical_revenue_annual", "historical_units_period_start", "historical_units_period_end"}
        or key.endswith((".volume_behavior", ".unit_change_mode", ".unit_change_value"))}
    restored = validate_body(sheet_layout.unflatten("operating_plan", changed, raw))
    assert restored == raw
    assert "historical_units_period_start" in operating_plan_schema.CLIENT_HEADER_FIELDS
    assert "volume_behavior" in operating_plan_schema.CLIENT_EXPENSE_FIELDS


@pytest.mark.asyncio
async def test_real_public_preview_uses_canonical_volume_math_discards_forged_review_and_never_saves():
    from app.routers import application_profiles as routes
    from app.tests.test_operating_client_preview import TOKEN, ReadOnlyMemory

    db = ReadOnlyMemory()
    db.draft.body = unit_plan()
    original = deepcopy(db.draft.body)
    incoming = approve(deepcopy(original))
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=context())):
        result = await routes.public_operating_plan_evaluate(TOKEN, routes.PublicOperatingPlanEvaluate(body=incoming), db)
    assert result["evaluation"]["unit_economics"]["net_annual_impact"] == 5800
    assert result["evaluation"]["projection"]["proposed"]["noi"] == -9200
    assert result["evaluation"]["projection"]["approved"]["noi"] is None
    assert "PRIVATE REVIEW REASON" not in json.dumps(result)
    db.assert_unchanged(original)


@pytest.mark.asyncio
async def test_saved_version_freezes_unit_inputs_and_canonical_output_without_rewriting_source_earnings():
    from app.enums import Role
    from app.services import financial_workspace as workspace
    from app.tests.test_financial_workspace import Memory
    from app.tests.test_financial_workspace import context as workspace_context

    db = Memory()
    profile, family, draft = workspace_context("operating_plan")
    draft.body = unit_plan()
    original = deepcopy(draft.body)
    actor = SimpleNamespace(id=uuid4(), name="Synthetic desk", role=Role.LOAN_EXEC)
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), \
         patch.object(workspace, "load_workspace", AsyncMock(return_value={})), \
         patch.object(workspace, "_render", return_value=b"%PDF immutable synthetic") as render, \
         patch.object(workspace, "_write_pdf"), patch.object(workspace, "_project", AsyncMock()) as projection, \
         patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), \
         patch("app.routers.application_profiles._requested_slot", AsyncMock(return_value=None)), \
         patch("app.services.financial_publication.after_publication", AsyncMock()), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=context())):
        await workspace.publish_version(db, profile, kind="operating_plan", expected_revision=4,
            idempotency_key="synthetic-unit-version", actor=actor)
    saved = db.versions[0]
    assert saved.body["historical_units_annual"] == 1000
    assert saved.body["expenses"][0]["current_amount"] == original["expenses"][0]["current_amount"]
    assert saved.key_facts["improvement_plan"]["unit_economics"]["net_annual_impact"] == 5800
    assert saved.key_facts["improvement_plan"]["baseline"]["noi"] == -15000
    assert render.call_args.args[2]["improvement_plan"]["projection"]["proposed"]["noi"] == -9200
    assert projection.await_args.args[2] is family  # Existing projection remains the operating-plan no-op.


def test_reports_preserve_unit_outputs_and_strip_private_source_keys_and_reviews():
    from app.capital_partners.services.listings import partner_operating_earnings
    from app.services.operating_plan_pdf import build_html

    raw = approve(unit_plan())
    summary = engine.package_summary(engine.evaluate(raw, context(), now=NOW))
    summary["unit_economics"]["rows"][0].update(review_reason="PRIVATE", actor_id="PRIVATE", source_group_key="PRIVATE")
    output = partner_operating_earnings({"improvement_plan": summary})["improvement_plan"]["unit_economics"]
    assert output["net_annual_impact"] == 5800
    assert "PRIVATE" not in json.dumps(output)
    assert "source_group_key" not in json.dumps(output) and "expense_row_ids" not in json.dumps(output)
    document = build_html(body=raw, financial_totals={"improvement_plan": summary})
    assert "Annual business-unit economics" in document
    assert "Historical units:</b> 1000" in document
    assert "Added variable costs:</b> $4,200.00" in document
    assert "$5,800.00" in document and "PRIVATE REVIEW REASON" not in document


def test_legacy_empty_defaults_leave_existing_projection_and_review_stamp_identical():
    raw = body()
    legacy = deepcopy(raw)
    for field in ("unit_label", "historical_units_annual", "historical_revenue_annual", "historical_units_period_start", "historical_units_period_end"):
        legacy.pop(field, None)
    for row in legacy["expenses"]:
        row.pop("volume_behavior", None)
    for row in legacy["adjustments"]:
        row.pop("unit_change_mode", None)
        row.pop("unit_change_value", None)
    assert engine.evaluate(legacy, context(), now=NOW) == engine.evaluate(raw, context(), now=NOW)
    assert engine.proposal_stamp(legacy["adjustments"][0], legacy["expenses"][0], legacy) == engine.proposal_stamp(raw["adjustments"][0], raw["expenses"][0], raw)
