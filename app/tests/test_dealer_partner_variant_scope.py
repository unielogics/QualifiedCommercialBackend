from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.enums import Role
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers import communications, dealer_ai_intake, operator_files
from app.services import application_profiles, dealer_partner_access


@pytest.mark.asyncio
async def test_dealer_partner_detail_query_is_owner_and_auto_variant_scoped() -> None:
    statements = []

    async def execute(statement):
        statements.append(statement)
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    user = SimpleNamespace(id=uuid4(), role=Role.DEALER_PARTNER)
    with pytest.raises(HTTPException) as exc:
        await dealer_ai_intake._load_broker_dealer_lead(
            SimpleNamespace(execute=execute),
            user,
            uuid4(),
        )
    assert exc.value.status_code == 404
    sql = str(
        statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "public_underwriting_intakes.broker_id" in sql
    assert "public_underwriting_intakes.variant = 'dealer_gatekeeper_v1'" in sql


@pytest.mark.asyncio
async def test_real_estate_intake_cannot_be_assigned_to_auto_dealer_agent() -> None:
    intake = SimpleNamespace(
        id=uuid4(),
        variant=dealer_ai_intake.FUNDING_VARIANT,
        bucket_id=uuid4(),
    )
    user = SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN)
    with patch.object(
        dealer_ai_intake,
        "_load_admin_dealer_lead",
        AsyncMock(return_value=intake),
    ):
        with pytest.raises(HTTPException) as exc:
            await dealer_ai_intake.assign_lead_partner(
                intake.id,
                dealer_ai_intake.AssignPartnerRequest(broker_user_id=uuid4()),
                SimpleNamespace(headers={}, client=None),
                user,
                SimpleNamespace(),
            )
    assert exc.value.status_code == 409
    assert "auto-industry" in exc.value.detail


@pytest.mark.asyncio
async def test_admin_can_detach_a_legacy_partner_from_real_estate_intake() -> None:
    intake = SimpleNamespace(
        id=uuid4(),
        variant=dealer_ai_intake.FUNDING_VARIANT,
        bucket_id=uuid4(),
        broker_id=uuid4(),
    )
    user = SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN)
    db = SimpleNamespace(commit=AsyncMock())
    response = object()
    with (
        patch.object(
            dealer_ai_intake,
            "_load_admin_dealer_lead",
            AsyncMock(return_value=intake),
        ),
        patch.object(dealer_ai_intake.profiles_service, "find_profile", AsyncMock(return_value=None)),
        patch.object(dealer_ai_intake, "_log", AsyncMock()),
        patch.object(dealer_ai_intake, "_response", AsyncMock(return_value=response)),
    ):
        result = await dealer_ai_intake.assign_lead_partner(
            intake.id,
            dealer_ai_intake.AssignPartnerRequest(broker_user_id=None),
            SimpleNamespace(headers={}, client=None),
            user,
            db,
        )
    assert result is response
    assert intake.broker_id is None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_value", ["suspended", "active"])
async def test_dealer_partner_loader_rejects_suspended_and_deleted_accounts(status_value: str) -> None:
    partner = SimpleNamespace(
        id=uuid4(),
        role=Role.DEALER_PARTNER,
        deleted_at=None if status_value == "suspended" else object(),
        account_status=status_value,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=partner))
    with pytest.raises(HTTPException) as exc:
        await dealer_ai_intake._load_dealer_partner_user(db, partner.id)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_shared_standing_gate_requires_both_signed_agreements() -> None:
    user = SimpleNamespace(
        id=uuid4(),
        role=Role.DEALER_PARTNER,
        deleted_at=None,
        account_status="active",
        referral_partner_company_id=uuid4(),
    )
    individual = SimpleNamespace(first=lambda: (uuid4(),))
    missing_company = SimpleNamespace(first=lambda: None)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[individual, missing_company]))
    with pytest.raises(HTTPException) as exc:
        await dealer_partner_access.require_dealer_partner_standing(db, user)
    assert exc.value.status_code == 403
    assert "company" in exc.value.detail.casefold()
    for call in db.execute.await_args_list:
        sql = str(call.args[0].compile(dialect=postgresql.dialect()))
        assert "contract_agreements.signed_at IS NOT NULL" in sql


def test_operator_file_scope_is_owner_and_auto_variant_scoped() -> None:
    user = SimpleNamespace(id=uuid4(), role=Role.DEALER_PARTNER)
    statement = operator_files._scope_intake_stmt(
        user,
        select(PublicUnderwritingIntake),
    )
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "public_underwriting_intakes.broker_id" in sql
    assert "public_underwriting_intakes.variant = 'dealer_gatekeeper_v1'" in sql


@pytest.mark.asyncio
async def test_application_profile_intake_source_rejects_owned_real_estate_file() -> None:
    user = SimpleNamespace(id=uuid4(), role=Role.DEALER_PARTNER)
    intake = SimpleNamespace(
        id=uuid4(),
        broker_id=user.id,
        variant=dealer_ai_intake.FUNDING_VARIANT,
        client_id=None,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=intake))
    with patch.object(
        application_profiles,
        "require_dealer_partner_standing",
        AsyncMock(),
    ):
        with pytest.raises(HTTPException) as exc:
            await application_profiles._load_source(db, "intake", intake.id, user)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_communications_intake_scan_applies_standing_and_auto_scope() -> None:
    statements = []

    async def execute(statement):
        statements.append(statement)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    user = SimpleNamespace(id=uuid4(), role=Role.DEALER_PARTNER)
    with patch.object(
        communications,
        "require_dealer_partner_standing",
        AsyncMock(),
    ) as standing:
        assert await communications._visible_intakes(SimpleNamespace(execute=execute), user) == []
    standing.assert_awaited_once()
    sql = str(
        statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "public_underwriting_intakes.broker_id" in sql
    assert "public_underwriting_intakes.variant = 'dealer_gatekeeper_v1'" in sql
