"""Canonical command, itemization and unchanged historical authority checks."""
from copy import deepcopy
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.schemas.earnings_improvement import completion, key_facts, validate_body
from app.services import earnings_improvement as engine
from app.services import operating_plan_commands as commands
from app.services import operating_plan_pdf, operating_plan_service, sheet_layout
from app.tests.test_earnings_improvement import NOW, SOURCE, action, approve, body, context, expense


def command(target="total_cost", value=10, **changes):
    return {"target": target, "mode": "percent", "direction": "decrease", "value": value, **changes}


def cost_plan(**changes):
    return body(action(command=command(), **changes))


def group_plan(rows=None, **changes):
    rows = rows or [expense(current_amount=1000), expense(id=str(uuid4()), label="Storage", current_amount=100, cadence="annual")]
    return body(action(kind="expense_group_change", expense_row_id=None, expense_category="rent", source_group_key="",
        command=command(allocation="proportional"), **changes), expenses=rows)


def price_plan(**changes):
    return body(action(kind="price_change", driver_key="annual-pricing", expense_row_id=None, price_basis="annual_revenue",
        command=command("annual_price_revenue", direction="increase", **changes)), historical_revenue_annual=100000)


def test_command_resolves_original_current_cost_without_mutating_or_compounding():
    raw = cost_plan()
    saved = deepcopy(raw)
    first = commands.resolve(raw)
    assert first["commands"][0]["gross_annual_effect"] == 1200
    assert first["body"]["adjustments"][0]["proposed_amount"] == 900
    assert commands.resolve(first["body"])["commands"] == first["commands"]
    assert commands.resolve(raw)["commands"] == first["commands"]
    assert raw == saved
    outcome = engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)
    assert outcome["baseline"]["noi"] == -15000
    assert outcome["projection"]["proposed"]["noi"] == -13800
    assert outcome["expense_changes"]["rows"][0]["proposed_period_cost"] == 900


@pytest.mark.parametrize("value", [None, 101])
def test_cleared_and_negative_target_commands_remain_unavailable(value):
    raw = cost_plan()
    raw["adjustments"][0]["command"]["value"] = value
    result = engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)
    assert result["commands"][0]["status"] == "incomplete"
    assert result["commands"][0]["proposed_value"] is None
    assert result["projection"]["proposed"]["noi"] is None


def test_group_money_allocates_exact_annual_target_deterministically_and_preserves_zero():
    rows = [expense(id=str(uuid4()), current_amount=1, cadence="annual") for _ in range(3)]
    rows.append(expense(id=str(uuid4()), current_amount=0, cadence="annual"))
    raw = group_plan(rows)
    raw["adjustments"][0]["command"] = command(mode="amount", value=.01, allocation="proportional")
    first = commands.resolve(raw)["commands"][0]
    reverse = deepcopy(raw)
    reverse["expenses"].reverse()
    second = commands.resolve(reverse)["commands"][0]
    assert first == second
    assert round(sum(row["proposed_annual_cost"] for row in first["allocations"]), 2) == 2.99
    assert sum(row["gross_annual_effect"] for row in first["allocations"]) == .01
    assert next(row for row in first["allocations"] if row["expense_row_id"] == rows[-1]["id"])["proposed_annual_cost"] == 0


@pytest.mark.parametrize("amount", [None, 0])
def test_unknown_or_all_zero_group_does_not_allocate_fabricated_weights(amount):
    raw = group_plan([expense(current_amount=amount)])
    result = commands.resolve(raw)["commands"][0]
    assert result["status"] == "incomplete" and result["allocations"] == []


def test_group_normalizes_frequency_and_exposes_budget_not_invented_unit_drivers():
    raw = group_plan([expense(current_amount=100, cadence="monthly"), expense(id=str(uuid4()), current_amount=400, cadence="quarterly")])
    result = engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)
    calc = result["commands"][0]
    assert calc["current_annual_cost"] == 2800 and calc["proposed_annual_cost"] == 2520
    assert result["totals"]["proposed"]["annual_impact"] == 280
    assert sum(row["gross_annual_effect"] for row in result["expense_changes"]["rows"]) == 280
    assert raw["expenses"][0]["current_units_per_period"] is None


def test_group_and_item_command_overlap_blocks_both_treatments():
    raw = group_plan()
    raw["adjustments"].append(action(id=str(uuid4()), driver_key="individual", command=command()))
    result = engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)
    assert result["projection"]["proposed"]["noi"] is None
    assert all(not row["proposed_math_valid"] for row in result["actions"])
    assert result["commands"][0]["allocations"] == []


def test_unit_quantity_command_retains_rate_and_fixed_charge_once():
    row = expense(cost_basis="per_unit", unit_label="consultation", current_amount=1000,
        current_units_per_period=100, current_unit_cost=8, current_fixed_amount=200)
    raw = body(action(command=command("unit_quantity", value=20)), expenses=[row])
    resolved = commands.resolve(raw)
    proposal = resolved["body"]["adjustments"][0]
    assert proposal["proposed_units_per_period"] == 80
    assert proposal["proposed_unit_cost"] == 8 and proposal["proposed_fixed_amount"] == 200
    assert resolved["commands"][0]["gross_annual_effect"] == 1920


def test_annual_pricing_has_no_units_requirement_and_composes_with_cost_savings():
    raw = price_plan()
    raw["adjustments"].append(action(id=str(uuid4()), command=command()))
    result = engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)
    assert result["commands"][0]["gross_annual_effect"] == 10000
    assert result["totals"]["proposed"]["annual_impact"] == 11200
    assert result["projection"]["proposed"]["noi"] == -3800
    assert "unit_economics" not in result
    assert not any("units" in path for path in completion(raw)["missing"])
    assert result["commands"][0]["current_annual_cost"] is None


@pytest.mark.parametrize("patch", [{"currency": "CAD"}, {"basis": "accrual"}, {"period_start": "2024-01-01", "period_end": "2024-12-31"}, {"historical_revenue_annual": 90000}])
def test_price_source_scope_cannot_be_reinterpreted(patch):
    raw = price_plan()
    raw.update(patch)
    result = engine.evaluate(raw, context(), now=NOW)
    assert not result["actions"][0]["proposed_math_valid"]


def test_annual_zero_revenue_is_not_an_existing_sales_price_basis():
    raw, cockpit = price_plan(mode="amount", value=10000), context()
    raw["historical_revenue_annual"] = cockpit["ebitda"]["potential"]["baseline"]["revenue"] = 0
    assert commands.resolve(raw, cockpit)["commands"][0]["status"] == "incomplete"


def test_unit_price_or_volume_cannot_overlap_annual_price_scope():
    raw = price_plan()
    raw["adjustments"].append(action(id=str(uuid4()), driver_key="product", kind="price_change", expense_row_id=None,
        units_per_period=100, current_unit_price=10, proposed_unit_price=11))
    result = engine.evaluate(raw, context(), now=NOW)
    assert all(not row["proposed_math_valid"] for row in result["actions"])


def test_command_review_binds_intent_all_group_rows_and_survives_reload():
    raw = group_plan()
    approved = approve(raw)
    result = engine.evaluate(approved, context(), now=NOW)
    assert result["actions"][0]["included_in_approved_projection"]
    assert operating_plan_service.protect_reviews(deepcopy(approved), approved) == approved
    resolved = commands.resolve(approved)["body"]
    assert engine.proposal_stamp(approved["adjustments"][0], body=approved) == engine.proposal_stamp(resolved["adjustments"][0], body=resolved)
    edited = deepcopy(approved)
    edited["expenses"][1]["current_amount"] += 1
    assert operating_plan_service.protect_reviews(edited, approved)["adjustments"][0]["status"] == "proposed"


def split_plan(unknown=False):
    parent = expense(excluded=True, source_group_key="source:rent")
    children = [expense(id=str(uuid4()), label="Treatment space", current_amount=600, source_group_key="source:rent", itemization_parent_id=parent["id"]),
        expense(id=str(uuid4()), label="Administration", current_amount=None if unknown else 400, source_group_key="source:rent", itemization_parent_id=parent["id"])]
    return body(expenses=[parent, *children], adjustments=[])


def test_split_preserves_reference_and_only_counts_children_unknown_remainder_stays_unknown():
    raw = split_plan()
    result = commands.resolve(raw)["itemization"][0]
    assert result["status"] == "balanced" and result["remainder_annual_amount"] == 0
    assert engine.evaluate(raw, context(), now=NOW)["disclosure"]["operating_monthly"] == 1000
    assert commands.resolve(split_plan(True))["itemization"][0]["remainder_annual_amount"] is None
    raw["expenses"][0]["excluded"] = False
    with pytest.raises(HTTPException):
        validate_body(raw)


def test_canonical_source_reference_wins_over_tampered_excluded_parent():
    raw, cockpit = split_plan(), context()
    raw["expenses"][0]["current_amount"] = 2000
    cockpit["operating_evidence"]["guidance"] = {"expense_groups": [{"key": "source:rent", "category": "rent", "baseline_annual_amount": 12000,
        "period_start": raw["period_start"], "period_end": raw["period_end"], "currency": "USD", "basis": "cash", "year": "2025", "source_file_ids": [SOURCE]}]}
    row = commands.resolve(raw, cockpit)["itemization"][0]
    assert row["reference_annual_amount"] == 12000 and row["recorded_parent_annual_amount"] == 24000
    assert row["children_annual_amount"] == 12000


def test_saved_handoff_keeps_historical_corrections_separate_from_future_commands():
    raw = cost_plan()
    raw["historical_corrections"] = [{"source_group_key": "source:rent", "reason": "Review the prior-period classification", "source_file_ids": [SOURCE], "evidence_note": "Compare signed lease"}]
    result = engine.evaluate(raw, context(), now=NOW)
    summary = engine.package_summary(result)
    handoff = summary["preparer_handoff"]
    assert handoff["historical"]["baseline_unchanged"]
    assert handoff["historical"]["corrections_for_preparer_review"][0]["status"] == "unresolved_preparer_review"
    assert handoff["future"]["commands"][0]["gross_annual_effect"] == 1200
    saved = operating_plan_pdf.build_html(body=raw, financial_totals={"improvement_plan": summary})
    assert "Tax-preparer handoff - saved version" in saved and "No return is filed or amended" in saved
    assert "Tax-preparer handoff - saved version" not in operating_plan_pdf.build_html(body=raw)


def test_empty_defaults_preserve_key_facts_and_review_stamp_and_worksheet_keeps_intent():
    raw = body()
    legacy = deepcopy(raw)
    legacy.pop("historical_corrections")
    for row in legacy["expenses"]:
        row.pop("itemization_parent_id")
    for row in legacy["adjustments"]:
        row.pop("command")
        row.pop("price_basis")
    assert key_facts(raw) == key_facts(legacy)
    assert engine.proposal_stamp(raw["adjustments"][0], raw["expenses"][0], raw) == engine.proposal_stamp(legacy["adjustments"][0], legacy["expenses"][0], legacy)
    assert engine.evaluate(raw, context(), now=NOW) == engine.evaluate(legacy, context(), now=NOW)
    planned = cost_plan()
    restored = validate_body(sheet_layout.unflatten("operating_plan", {}, planned))
    assert restored["adjustments"][0]["command"] == planned["adjustments"][0]["command"]


def source_context(raw, amount=12000):
    cockpit = context()
    cockpit["operating_evidence"]["guidance"] = {"expense_groups": [{"key": "source:rent", "category": "rent", "label": "Rental premises",
        "baseline_annual_amount": amount, "period_start": raw["period_start"], "period_end": raw["period_end"],
        "currency": "USD", "basis": "cash", "year": "2025", "source_file_ids": [SOURCE]}]}
    return cockpit


def test_unallocated_split_blocks_completion_unless_explicit_unresolved_correction():
    raw = split_plan()
    raw["expenses"][-1]["current_amount"] = 300
    assert any("itemization.unallocated" in path for path in completion(raw)["missing"])
    raw["historical_corrections"] = [{"source_group_key": "source:rent", "reason": "Preparer must reconcile prior occupancy expense", "source_file_ids": [SOURCE], "evidence_note": "Lease and ledger"}]
    assert not any("itemization." in path for path in completion(raw)["missing"])
    change = action(expense_row_id=raw["expenses"][1]["id"], command=command())
    raw["adjustments"] = [change]
    result = engine.evaluate(raw, source_context(raw), now=NOW)
    assert result["actions"][0]["gross_annual_impact"] == 720
    assert result["projection"]["proposed"]["noi"] is None
    with pytest.raises(HTTPException):
        approve(raw, source_context(raw))


def test_real_source_amount_controls_completion_not_a_balanced_forged_parent():
    raw = split_plan()
    raw["expenses"][0]["current_amount"] = 2000
    raw["expenses"][1]["current_amount"] = raw["expenses"][2]["current_amount"] = 1000
    assert not any("itemization." in path for path in completion(raw)["missing"])
    actual = operating_plan_service.completion(raw, source_context(raw))
    assert any("itemization.overallocated" in path for path in actual["missing"])


def test_classification_correction_does_not_block_a_balanced_matching_source():
    raw = split_plan()
    raw["historical_corrections"] = [{"source_group_key": "source:rent", "reason": "Preparer must review classification only", "source_file_ids": [SOURCE], "evidence_note": "Same total"}]
    raw["adjustments"] = [action(expense_row_id=raw["expenses"][1]["id"], command=command())]
    result = engine.evaluate(raw, source_context(raw), now=NOW)
    assert result["actions"][0]["proposed_math_valid"]
    assert result["projection"]["proposed"]["noi"] == -14280
    assert result["preparer_handoff"]["historical"]["corrections_for_preparer_review"][0]["status"] == "unresolved_preparer_review"


@pytest.mark.parametrize("field,value", [("currency", "CAD"), ("basis", "accrual"), ("period_start", "2024-01-01"), ("category", "salaries")])
def test_unsplit_equal_amount_group_cannot_bypass_source_scope(field, value):
    raw = cost_plan()
    raw["expenses"][0]["source_group_key"] = "source:rent"
    cockpit = source_context(raw)
    cockpit["operating_evidence"]["guidance"]["expense_groups"][0][field] = value
    result = engine.evaluate(raw, cockpit, now=NOW)
    assert result["expense_groups"][0]["status"] == "matches_source"
    assert not result["actions"][0]["proposed_math_valid"]
    assert result["projection"]["proposed"]["noi"] is None


def test_removed_command_restore_keeps_id_without_reviving_review():
    original = approve(cost_plan())
    removed = deepcopy(original)
    removed["adjustments"][0]["excluded"] = True
    saved_removed = operating_plan_service.protect_reviews(removed, original)
    restored = operating_plan_service.protect_reviews(original, saved_removed)
    assert restored["adjustments"][0]["id"] == original["adjustments"][0]["id"]
    assert restored["adjustments"][0]["status"] == "proposed"


def test_marketplace_omits_detailed_handoff_and_nested_source_participants():
    import json

    from app.capital_partners.services.listings import partner_operating_earnings

    raw = cost_plan()
    raw["historical_corrections"] = [{"source_group_key": "private-tax-key", "reason": "PRIVATE correction reason", "source_file_ids": [SOURCE], "evidence_note": "PRIVATE evidence"}]
    summary = engine.package_summary(engine.evaluate(raw, context(), now=NOW))
    public = partner_operating_earnings({"improvement_plan": summary})
    encoded = json.dumps(public)
    for private in ("preparer_handoff", "participant_expense_row_ids", "private-tax-key", "PRIVATE correction", SOURCE, raw["expenses"][0]["id"]):
        assert private not in encoded


@pytest.mark.asyncio
async def test_client_preview_preserves_command_but_strips_forged_review_and_rejects_other_file_evidence():
    raw = cost_plan(status="approved", review_reason="FORGED")
    cockpit = context()
    db = AsyncMock()
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=cockpit)):
        result = await operating_plan_service.client_preview(db, object(), raw, current=body())
        assert result["evaluation"]["commands"][0]["gross_annual_effect"] == 1200
        assert not result["evaluation"]["commands"][0]["included_in_approved_projection"]
        raw["historical_corrections"] = [{"source_group_key": "source:rent", "reason": "Check this source", "source_file_ids": [str(uuid4())], "evidence_note": ""}]
        with pytest.raises(HTTPException) as error:
            await operating_plan_service.client_preview(db, object(), raw, current=body())
        assert error.value.status_code == 422
    db.commit.assert_not_called()
    db.flush.assert_not_called()


@pytest.mark.asyncio
async def test_actual_public_route_returns_commands_unknowns_and_rejects_forged_allocations():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.routers import application_profiles as routes
    from app.tests.test_operating_client_preview import TOKEN, ReadOnlyMemory

    db = ReadOnlyMemory()
    db.draft.body = cost_plan()
    before = deepcopy(db.draft.body)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[routes.get_db] = lambda: db
    path = f"/api/v1/application-profiles/public/financial-forms/{TOKEN}/evaluate"
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=context())):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(path, json={"body": before})
            assert response.status_code == 200, response.text
            assert response.json()["evaluation"]["commands"][0]["gross_annual_effect"] == 1200
            cleared = deepcopy(before)
            cleared["adjustments"][0]["command"]["value"] = None
            response = await client.post(path, json={"body": cleared})
            assert response.status_code == 200 and response.json()["evaluation"]["commands"][0]["proposed_value"] is None
            forged = deepcopy(before)
            forged["adjustments"][0]["command"]["allocations"] = [{"proposed_annual_cost": 0}]
            assert (await client.post(path, json={"body": forged})).status_code == 422
    db.assert_unchanged(before)


@pytest.mark.asyncio
@pytest.mark.parametrize("submit", [False, True])
@pytest.mark.parametrize("correction", [False, True])
async def test_actual_version_publish_gate_keeps_unresolved_split_in_draft_or_captures_preparer_exception(submit, correction):
    from types import SimpleNamespace

    from app.services import financial_workspace as workspace
    from app.tests.test_financial_workspace import Memory
    from app.tests.test_financial_workspace import context as workspace_context

    db = Memory()
    profile, family, draft = workspace_context("operating_plan")
    raw = split_plan(True)
    if correction:
        raw["historical_corrections"] = [{"source_group_key": "source:rent", "reason": "Unresolved allocation for preparer", "source_file_ids": [SOURCE], "evidence_note": "Review ledger"}]
    draft.body = raw
    before = deepcopy(raw)
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), \
         patch.object(workspace, "load_workspace", AsyncMock(return_value={})), \
         patch.object(workspace, "source_files_for_profile", AsyncMock(return_value=[])), \
         patch.object(workspace, "filter_current_files", AsyncMock(return_value=[])), \
         patch.object(workspace, "_render", return_value=b"%PDF synthetic") as render, \
         patch.object(workspace, "_write_pdf") as storage, \
         patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), \
         patch("app.routers.application_profiles._requested_slot", AsyncMock(return_value=SimpleNamespace(id=uuid4(), status="requested"))), \
         patch("app.services.financial_publication.after_publication", AsyncMock()), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=source_context(raw))):
        if correction:
            await workspace.publish_version(db, profile, kind="operating_plan", expected_revision=4, idempotency_key="synthetic-split", submit=submit)
            assert len(db.versions) == 1 and storage.call_count == 1
            handoff = render.call_args.args[2]["improvement_plan"]["preparer_handoff"]
            assert handoff["historical"]["itemization"][0]["status"] == "unknown"
            assert handoff["historical"]["corrections_for_preparer_review"][0]["status"] == "unresolved_preparer_review"
        else:
            with pytest.raises(HTTPException) as error:
                await workspace.publish_version(db, profile, kind="operating_plan", expected_revision=4, idempotency_key="synthetic-split", submit=submit)
            assert error.value.status_code == 422 and error.value.detail["code"] == "operating_itemization_incomplete"
            assert error.value.detail["missing"][0].endswith("itemization.unknown")
            assert not db.versions and family.current_version_id is None
            storage.assert_not_called()
            render.assert_not_called()
            assert draft.body == before
