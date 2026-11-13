"""Kind conversion retains explicit marketing math and rejects stale commands."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.enums import Role
from app.models.file_event import FileEvent
from app.models.financial_form_version import FinancialFormDraft
from app.routers import application_profiles as routes
from app.schemas.earnings_improvement import action_missing
from app.services import earnings_improvement as engine
from app.services import financial_workspace as workspace
from app.tests.test_earnings_improvement import NOW
from app.tests.test_financial_workspace import Memory
from app.tests.test_financial_workspace import context as workspace_context
from app.tests.test_minimal_operating_flow_policy import new_campaign
from app.tests.test_operating_client_preview import TOKEN, ReadOnlyMemory


def campaign(*, stale_command=False):
    raw, current = new_campaign()
    raw["adjustments"][0]["proposed_amount"] = 1000
    if stale_command:
        raw["adjustments"][0]["command"] = {"target": "total_cost", "mode": "percent",
            "direction": "decrease", "value": 10, "allocation": "none"}
    return raw, current


@pytest.mark.parametrize("field", ["product_name", "expense_row_id", "revenue_return_multiple", "contribution_margin_pct"])
def test_stale_cost_command_cannot_hide_required_marketing_inputs_or_crash(field):
    raw, current = campaign(stale_command=True)
    raw["adjustments"][0][field] = "" if field == "product_name" else None
    original = deepcopy((raw, current))
    assert field in action_missing(raw["adjustments"][0])
    result = engine.evaluate(raw, current, now=NOW, include_expense_changes=True)
    row = result["draft_feedback"]["actions"][0]
    prefix = f"adjustments.{raw['adjustments'][0]['id']}."
    assert prefix + field in row["math_missing"] and prefix + "command" in row["math_missing"]
    assert row["math_status"] == "incomplete" and row["gross_annual_delta"] is None
    assert result["projection"]["proposed"]["noi"] is None
    assert (raw, current) == original


def test_even_complete_marketing_inputs_do_not_execute_or_reinterpret_a_stale_cost_command():
    raw, current = campaign(stale_command=True)
    result = engine.evaluate(raw, current, now=NOW, include_expense_changes=True)
    row = result["actions"][0]
    assert not row["proposed_math_valid"] and row["net_annual_impact"] is None
    assert any("command target does not match" in issue for issue in row["issues"])
    assert result["commands"][0]["status"] == "incomplete"
    assert result["draft_feedback"]["actions"][0]["math_status"] == "incomplete"
    assert result["projection"]["proposed"]["noi"] is None


@pytest.mark.parametrize("kind,fields", [
    ("revenue_change", ("current_amount", "proposed_amount", "contribution_margin_pct")),
    ("product_cost_change", ("units_per_period", "current_unit_cost", "proposed_unit_cost")),
])
def test_other_unsupported_kinds_keep_their_own_required_math_fields(kind, fields):
    raw, current = campaign(stale_command=True)
    raw["adjustments"][0].update(kind=kind, **dict.fromkeys(fields))
    original = deepcopy(raw)
    result = engine.evaluate(raw, current, now=NOW, include_expense_changes=True)
    row = result["draft_feedback"]["actions"][0]
    prefix = f"adjustments.{raw['adjustments'][0]['id']}."
    assert all(prefix + field in row["math_missing"] for field in (*fields, "command"))
    assert row["math_status"] == "incomplete" and row["gross_annual_delta"] is None
    assert result["projection"]["proposed"]["noi"] is None
    assert raw == original


@pytest.mark.parametrize("proposed,multiple,annual", [(0, 2.5, 0), (1000, 2.5, 6000), (1000, 0, -12000)])
def test_explicit_new_marketing_keeps_the_same_formula_and_zero_semantics(proposed, multiple, annual):
    raw, current = campaign()
    raw["adjustments"][0].update(proposed_amount=proposed, revenue_return_multiple=multiple)
    original = deepcopy((raw, current))
    result = engine.evaluate(raw, current, now=NOW, include_expense_changes=True)
    row = result["draft_feedback"]["actions"][0]
    assert row["math_missing"] == [] and row["source_issues"] == []
    assert row["gross_annual_delta"] == annual and row["net_annual_delta"] == annual
    assert result["projection"]["proposed"]["noi"] == -15000 + annual
    assert result["projection"]["proposed"]["first_year_noi"] is None
    assert result["cash_bridge"]["operating_savings_annual"] == 0
    assert len(raw["expenses"]) == len(raw["adjustments"]) == 1
    assert (raw, current) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [False, True])
async def test_actual_public_preview_returns_actionable_incomplete_without_saving_or_exposing_review(complete):
    db = ReadOnlyMemory()
    raw, current = campaign(stale_command=True)
    if not complete:
        raw["adjustments"][0].update(revenue_return_multiple=None, contribution_margin_pct=None)
    db.draft.body = deepcopy(raw)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[routes.get_db] = lambda: db
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/v1/application-profiles/public/financial-forms/{TOKEN}/evaluate", json={"body": raw})
    assert response.status_code == 200, response.text
    result = response.json()["evaluation"]
    row = result["draft_feedback"]["actions"][0]
    assert f"adjustments.{raw['adjustments'][0]['id']}.command" in row["math_missing"]
    assert row["math_status"] == "incomplete" and row["gross_annual_delta"] is None
    assert not result["actions"][0]["included_in_approved_projection"]
    assert "review_reason" not in result["actions"][0]
    db.assert_unchanged(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("client", [False, True])
async def test_valid_new_marketing_survives_actual_saved_version_roundtrip_without_becoming_approved(client):
    raw, current = campaign()
    db = Memory()
    profile, family, draft = workspace_context("operating_plan")
    draft.body = deepcopy(raw)
    db.objects[(FinancialFormDraft, family.id)] = draft
    db.add(FileEvent(profile_id=profile.id, kind="operating_plan.goal_set",
        meta={"amount": "18000", "currency": "USD", "revision": 1}))
    actor = SimpleNamespace(id=None if client else uuid4(), role=Role.CLIENT if client else Role.LOAN_EXEC)
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), \
         patch.object(workspace, "load_workspace", AsyncMock(return_value={})), \
         patch.object(workspace, "_render", return_value=b"%PDF synthetic"), patch.object(workspace, "_write_pdf"), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)), \
         patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), \
         patch.object(routes, "_requested_slot", AsyncMock(return_value=SimpleNamespace(id=uuid4(), status="requested"))), \
         patch("app.services.financial_publication.after_publication", AsyncMock()), patch.object(workspace, "_project", AsyncMock()):
        await workspace.stage_draft(db, profile, family, draft, raw, actor=actor)
        await workspace.publish_version(db, profile, kind="operating_plan", expected_revision=draft.revision,
            idempotency_key="valid-new-marketing", actor=actor, submit=client)
    saved = db.versions[0]
    assert saved.body == raw and saved.status == ("client_submitted" if client else "published")
    result = engine.evaluate(saved.body, current, now=NOW, include_expense_changes=True)
    assert result["draft_feedback"]["actions"][0]["gross_annual_delta"] == 6000
    assert result["projection"]["proposed"]["noi"] == -9000
    assert result["projection"]["proposed"]["first_year_noi"] is None
    assert result["actions"][0]["review_state"] == "proposed" and not result["actions"][0]["included_in_approved_projection"]
    assert result["baseline"]["noi"] == -15000 and result["cash_bridge"]["operating_savings_annual"] == 0
