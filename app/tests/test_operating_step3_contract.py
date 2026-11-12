"""Preview-only comparisons and auditable presentation placement boundaries."""
import json
from copy import deepcopy
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.capital_partners.services import ppm
from app.schemas.earnings_improvement import key_facts, validate_body
from app.services import earnings_improvement as engine
from app.services import operating_expense_changes, operating_plan_service, sheet_layout
from app.tests.test_earnings_improvement import NOW, action, approve, body, context, expense
from app.tests.test_marketplace_ppm_lifecycle import ppm_harness
from app.tests.test_ppm_saved_source_stability import linked
from app.tests.test_ppm_saved_source_stability import real_sources as real_sources


def planned_cost(**changes):
    raw = body()
    raw["adjustments"][0].update(label="", driver_key="", explanation="", source_note="", effective_date=None, implementation_cost=None)
    raw["adjustments"][0].update(changes)
    return validate_body(raw)


def test_arithmetic_does_not_wait_for_explanation_or_invent_setup_and_timing():
    raw = planned_cost()
    original = deepcopy(raw)
    result = engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)
    row = result["expense_changes"]["rows"][0]
    assert row["status"] == "calculated" and row["gross_annual_effect"] == 2400
    assert row["net_annual_effect"] is row["first_year_effect"] is None
    assert not row["included_in_proposed_projection"] and not row["included_in_approved_projection"]
    assert "implementation_cost" in row["completion_missing"]
    assert result["expense_changes"]["gross_projection"]["noi"] == -12600
    assert result["projection"]["proposed"]["noi"] is None
    assert raw == original


def test_distinct_proposed_frequency_is_explicit_and_has_the_same_annual_math():
    raw = planned_cost(cadence="annual", current_amount=12000, proposed_amount=9600)
    row = operating_expense_changes.evaluate(raw)["rows"][0]
    assert row["cadence"] == "monthly" and row["proposed_cadence"] == "annual"
    assert row["current_period_cost"] == 1000 and row["proposed_period_cost"] == 9600
    assert row["gross_annual_effect"] == 2400


@pytest.mark.parametrize("patch", [{"expense_category": "not-a-category"}, {"source_group_key": "x" * 241}])
def test_placement_is_validated_without_expanding_financial_rules(patch):
    raw = planned_cost()
    raw["adjustments"][0].update(patch)
    with pytest.raises(HTTPException) as error:
        validate_body(raw)
    assert error.value.status_code == 422


def test_linked_expense_owner_wins_even_when_its_group_is_empty():
    raw = planned_cost(expense_category="marketing", source_group_key="stale-other-group")
    linked_action = raw["adjustments"][0]
    assert linked_action["expense_category"] == "rent"
    assert linked_action["source_group_key"] == ""
    result = engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)
    assert result["expense_changes"]["rows"][0]["expense_category"] == "rent"
    assert result["actions"][0]["expense_category"] == "rent"


def test_standalone_placement_survives_schema_worksheet_key_facts_and_saved_report():
    raw = planned_cost()
    activity = action(id=str(uuid4()), kind="revenue_change", expense_row_id=None,
        expense_category="marketing", source_group_key="source:private-source-reference", current_amount=1000,
        proposed_amount=1500, contribution_margin_pct=50)
    raw["adjustments"].append(activity)
    raw = validate_body(raw)
    cells = sheet_layout.flatten("operating_plan", raw)
    key = f"adjustments.{activity['id']}"
    assert cells[key + ".expense_category"] == "marketing"
    assert cells[key + ".source_group_key"] == "source:private-source-reference"
    restored = validate_body(sheet_layout.unflatten("operating_plan", {key + ".expense_category": "software"}, raw))
    assert restored["adjustments"][1]["expense_category"] == "software"
    assert restored["adjustments"][1]["source_group_key"] == activity["source_group_key"]
    facts = key_facts(restored)
    assert facts["operating_plan"]["adjustments"][1]["expense_category"] == "software"
    summary = engine.package_summary(engine.evaluate(restored, context(), now=NOW))
    assert summary["adjustments"][1]["source_group_key"] == activity["source_group_key"]
    assert "expense_changes" not in summary


def test_empty_placement_does_not_change_legacy_review_or_source_facts():
    raw = approve(body())
    legacy = deepcopy(raw)
    for row in legacy["adjustments"]:
        row.pop("expense_category", None)
        row.pop("source_group_key", None)
    assert engine.evaluate(raw, context(), now=NOW) == engine.evaluate(legacy, context(), now=NOW)
    assert key_facts(raw) == key_facts(legacy)
    assert "expense_category" not in key_facts(raw)["operating_plan"]["adjustments"][0]
    assert "source_group_key" not in key_facts(raw)["operating_plan"]["adjustments"][0]
    assert engine.proposal_stamp(raw["adjustments"][0], raw["expenses"][0], raw) == engine.proposal_stamp(legacy["adjustments"][0], legacy["expenses"][0], legacy)


def test_equal_total_cannot_hide_changed_current_unit_drivers():
    cost = expense(cost_basis="per_unit", current_amount=1000, unit_label="hour", current_units_per_period=20,
        current_unit_cost=50, current_fixed_amount=0)
    change = action(cost_basis="per_unit", current_amount=None, proposed_amount=None, unit_label="hour",
        current_units_per_period=10, current_unit_cost=100, current_fixed_amount=0,
        proposed_units_per_period=10, proposed_unit_cost=80, proposed_fixed_amount=0)
    result = operating_expense_changes.evaluate(body(change, expenses=[cost]))
    row = result["rows"][0]
    assert row["status"] == "incomplete" and row["gross_annual_effect"] is None
    assert any("current_unit_reconciliation" in path for path in row["missing_math"])


def test_unknown_unchanged_cost_preserves_known_subtotal_without_complete_projection():
    raw = planned_cost()
    raw["expenses"].append(expense(id=str(uuid4()), label="Unconfirmed insurance", category="insurance", current_amount=None))
    result = engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)["expense_changes"]
    assert result["summary"]["known_gross_annual_subtotal"] == 2400
    assert result["summary"]["gross_annual_effect"] is None and not result["summary"]["complete"]
    assert result["gross_projection"]["noi"] is None


def test_first_year_uses_only_explicit_horizon_and_charges_setup_once_when_active():
    raw = planned_cost(implementation_cost=600, implementation_cost_cadence="one_time", effective_date="2026-01-01")
    result = operating_expense_changes.evaluate(raw)["rows"][0]
    assert result["gross_annual_effect"] == result["net_annual_effect"] == 2400
    assert result["first_year_effect"] == 1800
    raw["projection_start"] = None
    assert operating_expense_changes.evaluate(raw)["rows"][0]["first_year_effect"] is None
    raw["projection_start"] = "2026-01-01"
    raw["adjustments"][0]["effective_date"] = "2028-01-01"
    assert operating_expense_changes.evaluate(raw)["rows"][0]["first_year_effect"] == 0


def test_additional_unscoped_items_have_their_own_canonical_group_in_the_same_category():
    raw = planned_cost()
    raw["expenses"][0]["source_group_key"] = "irs:rent"
    raw["expenses"].append(expense(id=str(uuid4()), label="Additional occupancy", current_amount=50))
    groups = operating_expense_changes.evaluate(raw)["source_groups"]
    assert len(groups) == 2
    manual = next(row for row in groups if row["source_group_key"] == "")
    assert manual["expense_category"] == "rent" and manual["current_annual_cost"] == 600
    assert manual["proposed_annual_cost"] == 600 and "Additional items" in manual["label"]


def test_source_variance_blocks_only_the_source_bridge_not_known_cost_arithmetic():
    raw, cockpit = planned_cost(current_amount=10000, proposed_amount=1000), context()
    raw["expenses"][0].update(current_amount=10000, source_group_key="irs:rents")
    cockpit["operating_evidence"]["guidance"] = {"expense_groups": [{"key": "irs:rents", "category": "rent",
        "label": "Rent reference", "baseline_annual_amount": 12000, "currency": "USD", "year": 2025}]}
    result = engine.evaluate(raw, cockpit, now=NOW, include_expense_changes=True)
    comparison = result["expense_changes"]
    assert comparison["summary"]["gross_annual_effect"] == 108000
    assert not comparison["source_comparable"] and comparison["source_reconciliation_issues"]
    assert comparison["gross_projection"]["noi"] is comparison["gross_projection"]["earnings"] is None
    assert result["baseline"]["noi"] == -15000


def test_missing_source_context_never_claims_an_imported_group_is_reconciled():
    raw = planned_cost()
    raw["expenses"][0]["source_group_key"] = "irs:rents"
    result = operating_expense_changes.evaluate(raw, baseline=context()["ebitda"]["headline"], noi_comparable=True)
    assert result["source_comparable"] is False
    assert result["source_reconciliation_issues"]


def test_valid_product_pair_stays_specialized_instead_of_becoming_a_fake_duplicate_cost_conflict():
    row = expense(category="cogs", current_amount=400)
    price = action(id=str(uuid4()), kind="price_change", driver_key="service-product", units_per_period=100,
        current_unit_price=10, proposed_unit_price=12)
    product_cost = action(id=str(uuid4()), kind="product_cost_change", driver_key="service-product", units_per_period=100,
        current_unit_cost=4, proposed_unit_cost=5)
    raw = body(price, product_cost, expenses=[row])
    result = engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)
    assert result["totals"]["proposed"]["annual_impact"] == 1200
    assert result["expense_changes"]["rows"][0]["status"] == "specialized"
    assert result["expense_changes"]["rows"][0]["gross_annual_effect"] is None
    assert not any("Choose the intended treatment" in issue for issue in result["expense_changes"]["rows"][0]["issues"])


@pytest.mark.asyncio
async def test_real_client_preview_returns_arithmetic_but_strips_forged_review_and_never_saves():
    from app.routers import application_profiles as routes
    from app.tests.test_operating_client_preview import TOKEN, ReadOnlyMemory

    db = ReadOnlyMemory()
    db.draft.body = planned_cost()
    before = deepcopy(db.draft.body)
    incoming = deepcopy(before)
    incoming["adjustments"][0].update(status="approved", reviewed_by_user_id=str(uuid4()), reviewed_at=NOW.isoformat(),
        review_reason="FORGED PRIVATE REASON", review_source_stamp="a" * 64)
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=context())):
        result = await routes.public_operating_plan_evaluate(TOKEN, routes.PublicOperatingPlanEvaluate(body=incoming), db)
    assert result["evaluation"]["expense_changes"]["rows"][0]["gross_annual_effect"] == 2400
    assert result["evaluation"]["actions"][0]["proposed_math_valid"] is False
    assert not result["evaluation"]["expense_changes"]["rows"][0]["included_in_approved_projection"]
    assert "FORGED" not in json.dumps(result)
    assert "reviewed_by_user_id" not in json.dumps(result)
    assert "context_stamp" not in result["evaluation"]
    db.assert_unchanged(before)


@pytest.mark.asyncio
async def test_http_public_preview_exposes_computed_validity_but_rejects_it_in_writes():
    from app.routers import application_profiles as routes
    from app.tests.test_operating_client_preview import TOKEN, ReadOnlyMemory

    db = ReadOnlyMemory()
    db.draft.body = body()
    before = deepcopy(db.draft.body)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[routes.get_db] = lambda: db
    path = f"/api/v1/application-profiles/public/financial-forms/{TOKEN}/evaluate"
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=context())):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(path, json={"body": before})
            assert response.status_code == 200, response.text
            assert response.json()["evaluation"]["actions"][0]["proposed_math_valid"] is True
            forged = deepcopy(before)
            forged["adjustments"][0]["proposed_math_valid"] = True
            denied = await client.post(path, json={"body": forged})
            assert denied.status_code == 422
    assert "proposed_math_valid" not in engine.package_summary(engine.evaluate(before, context(), now=NOW))["adjustments"][0]
    db.assert_unchanged(before)


def test_placement_is_auditable_in_pdf_without_exposing_group_identifiers_to_partners():
    from app.capital_partners.services.listings import partner_operating_earnings
    from app.services.operating_plan_pdf import build_html

    raw = planned_cost()
    raw["adjustments"][0].update(expense_row_id=None, expense_category="rent", source_group_key="private-group-reference")
    evaluation = engine.evaluate(raw, context(), now=NOW, include_expense_changes=True)
    summary = engine.package_summary(evaluation)
    summary["expense_groups"] = [{"key": "private-group-reference", "category": "rent", "label": "Rent reference", "notes": []}]
    document = build_html(body=raw, financial_totals={"improvement_plan": summary})
    assert "Activity placement" in document and "Rent reference" in document
    assert "private-group-reference" not in document
    partner = partner_operating_earnings({"improvement_plan": summary})
    assert partner["improvement_plan"]["adjustments"][0]["expense_category"] == "rent"
    assert partner["improvement_plan"]["adjustments"][0]["source_group_label"] == "Rent reference"
    assert "private-group-reference" not in json.dumps(partner)
    assert "expense_changes" not in json.dumps(partner)


@pytest.mark.asyncio
async def test_opt_in_preview_keeps_real_saved_ppm_source_stable(real_sources, monkeypatch):
    from app.capital_partners import router_admin

    async with ppm_harness() as h:
        listing, profile, _render, _stored = await linked(h, monkeypatch)
        await router_admin._apply_file_financing(h.db, listing)
        await h.db.commit()
        before = await ppm.saved_context(h.db, listing)
        saved = deepcopy((profile.financial_plan, profile.deal_budget, listing.source_snapshot))
        result = await operating_plan_service.preview(h.db, profile, planned_cost())
        assert "expense_changes" in result["evaluation"]
        after = await ppm.saved_context(h.db, listing)
        assert before["fingerprint"] == after["fingerprint"]
        assert (profile.financial_plan, profile.deal_budget, listing.source_snapshot) == saved
        assert not h.db.new and not h.db.dirty and not h.db.deleted
