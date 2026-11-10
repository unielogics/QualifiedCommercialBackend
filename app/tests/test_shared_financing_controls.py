"""Shared proposal, security versioning and explicit approval boundaries."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.enums import Role
from app.models.application_terms import ApplicationTermSheet
from app.schemas.investor_collateral import CollateralPatch
from app.services import (
    application_terms,
    financial_cockpit,
    financing_projection,
    investor_collateral,
)
from app.tests.test_atomic_client_terms_plan import env as env
from app.tests.test_atomic_client_terms_plan import payload


def asset(**extra):
    return {"id": str(uuid4()), "asset_type": "equipment", "description": "Business equipment",
        "estimated_value": "100000.00", "existing_liens_total": "25000.00", "currency": "USD", **extra}


def test_asset_only_collateral_keeps_capacity_unknown_and_ids_distinct():
    row = asset()
    result = CollateralPatch(expected_revision=0, enabled=True, assets=[row])
    assert result.properties == [] and result.assets[0].available_lien_amount is None
    for invalid in ({"description": "   "}, {"available_lien_amount": "-1"}, {"asset_type": "property"}):
        with pytest.raises(ValidationError):
            CollateralPatch(expected_revision=0, enabled=True, assets=[asset(**invalid)])
    with pytest.raises(ValidationError, match="distinct"):
        CollateralPatch(expected_revision=0, enabled=True, assets=[row, row])


@pytest.mark.asyncio
async def test_legacy_property_patch_and_disabled_toggle_preserve_assets():
    old = asset()
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=None, investor_collateral={"enabled": True,
        "revision": 1, "assets": [old], "properties": []})
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: profile)), flush=AsyncMock())
    with (patch.object(investor_collateral, "property_options", AsyncMock(return_value=[])),
          patch.object(investor_collateral, "read", AsyncMock(side_effect=lambda _db, row, _user: row.investor_collateral)),
          patch("app.services.application_profiles.log_profile_action", AsyncMock())):
        result = await investor_collateral.update(db, profile, CollateralPatch(expected_revision=1, enabled=False),
            SimpleNamespace(role=Role.LOAN_EXEC, id=uuid4()))
    assert result["assets"] == [old] and result["enabled"] is False


@pytest.mark.asyncio
async def test_assets_cannot_link_documents_from_another_file():
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), investor_collateral={})
    db = SimpleNamespace(execute=AsyncMock(side_effect=[SimpleNamespace(scalar_one=lambda: profile),
        SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))]), flush=AsyncMock())
    request = CollateralPatch(expected_revision=0, enabled=True, assets=[asset(source_file_ids=[str(uuid4())])])
    with pytest.raises(HTTPException) as error:
        await investor_collateral.update(db, profile, request, SimpleNamespace(role=Role.LOAN_EXEC, id=uuid4()))
    assert error.value.status_code == 422 and profile.investor_collateral == {}


@pytest.mark.asyncio
async def test_terms_freeze_security_and_never_approve_coverage(env):
    env.profile.investor_collateral = {"enabled": True, "revision": 3, "properties": [], "assets": [asset()]}
    env.profile.underwriting_approved_dscr = 1.5
    original = deepcopy(env.profile.investor_collateral)
    env.session.commit()
    row = await application_terms.save_terms(env.db, env.profile, env.actor, payload(expected_collateral_revision=3))
    env.session.commit()
    assert row.collateral_revision == 3 and row.collateral_snapshot["assets"] == original["assets"]
    assert env.profile.underwriting_approved_dscr == 1.5
    env.profile.investor_collateral = {**original, "revision": 4, "assets": [asset(description="Additional assets")]}
    env.session.commit()
    result = await application_terms.read_terms(env.db, env.profile, row)
    assert result.collateral_stale and result.current_collateral_revision == 4
    assert result.collateral_snapshot["assets"] == original["assets"]
    assert result.current_collateral["assets"] != result.collateral_snapshot["assets"]
    with pytest.raises(HTTPException) as error:
        await application_terms.save_terms(env.db, env.profile, env.actor,
            payload(expected_version=1, expected_collateral_revision=3))
    assert error.value.status_code == 409
    assert len(env.session.scalars(select(ApplicationTermSheet)).all()) == 1 and row.is_current


@pytest.mark.parametrize("earnings", [-0.2, 0, None])
def test_reviewer_projection_preserves_signed_zero_unknown_values_without_lifecycle_writes(earnings):
    profile = SimpleNamespace(investor_collateral={"revision": 5}, underwriting_approved_dscr=1.3,
        underwriting_approved_amount=100000, underwriting_accepted_amount=None)
    before = deepcopy(vars(profile))
    cockpit = {"dscr": {"headline": {"value": earnings, "label": "2025 business earnings"}},
        "plan": {"record": {"new_loan_amount": 200000}, "evaluation": {"active": True},
            "impact": {"dscr": {"after": earnings}, "coverage_debt_service_monthly": {"before": 8000, "after": 4000}},
            "terms_link": {"state": "differs", "recorded": {"id": "terms-1", "version": 1, "collateral_revision": 4}}}}
    result = financing_projection.from_cockpit(profile, cockpit)
    assert result["proposed_amount"] == 200000
    assert result["current_dscr"] == earnings and result["with_plan_dscr"] == earnings
    assert result["current_annual_debt_service"] == 96000 and result["with_plan_annual_debt_service"] == 48000
    assert result["collateral_stale"] and vars(profile) == before


def test_changed_working_loan_keeps_recorded_security_separate():
    frozen = {"enabled": True, "revision": 2, "properties": [], "assets": [asset()]}
    record = {"kind": "client_terms", "id": "term-id", "version": 1, "program_key": "private_loan_bridge",
        "program_name": "Private Loan / Bridge", "amount": 100000, "collateral_snapshot": frozen, "collateral_revision": 2}
    cockpit = {"dscr": {"proposed": {**record, "rate_pct": 12, "term_months": 24}},
        "plan": {"terms_link": {"state": "differs", "recorded": record},
            "impact": {"loan": {"amount": 200000, "rate_pct": 15, "term_months": 36, "payment_monthly": 6933}},
            "evaluation": {}}}
    result = financial_cockpit.proposed_terms_block(cockpit)
    assert result["source"] == "plan" and result["amount"] == 200000
    assert result["program_key"] == "private_loan_bridge" and result["terms_state"] == "differs"
    assert result["collateral_snapshot"] == frozen and result["recorded_terms_ref"]["id"] == "term-id"


def test_cleared_scenario_does_not_adopt_a_recorded_loan_or_approval():
    profile = SimpleNamespace(investor_collateral={})
    cockpit = {"dscr": {"headline": {"value": -0.2}}, "proposed_terms": {"amount": 200000},
        "plan": {"record": {"new_loan_amount": None, "updated_at": "2026-11-09T12:00:00Z"},
            "evaluation": {"active": False}, "terms_link": {"state": "recorded_not_in_plan",
                "recorded": {"id": "issued", "version": 1, "amount": 200000}}}}
    result = financing_projection.from_cockpit(profile, cockpit)
    assert result["proposed_amount"] is None
    assert result["current_dscr"] == result["with_plan_dscr"] == -0.2
    assert result["terms_ref"]["id"] == "issued" and result["terms_state"] == "recorded_not_in_plan"


def test_cleared_proposal_is_only_an_immutable_recorded_reference():
    frozen = {"enabled": True, "revision": 2, "properties": [], "assets": [asset()]}
    recorded = {"kind": "client_terms", "id": "issued", "version": 1, "amount": 200000,
        "program_key": "private_loan_bridge", "program_name": "Private Loan / Bridge",
        "collateral_snapshot": frozen, "collateral_revision": 2}
    cockpit = {"dscr": {"proposed": {**recorded, "rate_pct": 15, "term_months": 36}},
        "plan": {"record": {"new_loan_amount": None}, "evaluation": {"active": False},
            "terms_link": {"state": "recorded_not_in_plan", "recorded": recorded}}}
    original = deepcopy(recorded)
    result = financial_cockpit.proposed_terms_block(cockpit)
    assert result["active_proposal"] is False and result["source"] == "recorded_reference"
    assert result["recorded_terms_ref"]["id"] == "issued" and result["collateral_snapshot"] == frozen
    assert recorded == original
