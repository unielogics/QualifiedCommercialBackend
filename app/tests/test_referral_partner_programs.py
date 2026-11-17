from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers import dealer_ai_intake as api
from app.services import dealer_partner_access, referral_programs
from app.tests._business_profile_fixture import APPLICATION_START_DETAILS, BUSINESS_PROFILE


@pytest.mark.parametrize("role", [Role.DEALER_PARTNER, Role.PROFESSIONAL_REFERRAL_PARTNER, Role.BROKER])
@pytest.mark.parametrize("variant", ["dealer", "real_estate", "main_street", "mca_refinance"])
async def test_each_partner_creates_the_selected_type_under_their_own_identity(role, variant):
    user = SimpleNamespace(id=uuid4(), name="Referral Partner", email="partner@example.com", role=role)
    client = SimpleNamespace(id=uuid4(), email="client@example.com", lead_intake={})
    bucket = SimpleNamespace(id=uuid4(), ai_context={})
    link = SimpleNamespace(id=uuid4(), token="room-token")
    rows = []

    def add(row):
        if getattr(row, "id", None) is None:
            row.id = uuid4()
        rows.append(row)

    db = SimpleNamespace(add=add, flush=AsyncMock(), commit=AsyncMock())

    async def reload(*_):
        return next(row for row in rows if isinstance(row, PublicUnderwritingIntake))

    payload = api.BrokerLeadCreate(
        full_name="Client Owner", email=client.email, phone="9735550148",
        variant=variant, program_key="selected_program", secure_room_pin="123456",
        invitation_channels=[], **BUSINESS_PROFILE, **APPLICATION_START_DETAILS,
    )
    program = referral_programs.ReferralProgram(program_key="selected_program", name="Selected Program", variants=[variant])
    with ExitStack() as stack:
        stack.enter_context(patch.object(api, "_require_dealer_partner", AsyncMock()))
        stack.enter_context(patch.object(referral_programs, "validate_request", AsyncMock(return_value=program)))
        stack.enter_context(patch.object(api, "_latest_active_intake_by_email", AsyncMock(return_value=None)))
        for name in ("_find_or_create_client", "_find_or_create_funding_client", "_find_or_create_mca_client"):
            stack.enter_context(patch.object(api, name, AsyncMock(return_value=client)))
        for name in ("_create_bucket_for_intake", "_create_bucket_for_funding_review", "_create_bucket_for_main_street", "_create_bucket_for_mca_refi"):
            stack.enter_context(patch.object(api, name, AsyncMock(return_value=(bucket, link))))
        stack.enter_context(patch.object(api, "_load_admin_dealer_lead", AsyncMock(side_effect=reload)))
        stack.enter_context(patch.object(api, "_log", AsyncMock()))
        stack.enter_context(patch.object(api.profiles_service, "provision_profile_for_intake", AsyncMock()))
        response = stack.enter_context(patch.object(api, "_response", AsyncMock()))
        await api.create_broker_ai_lead(payload, SimpleNamespace(), user, db)
    intake = next(row for row in rows if isinstance(row, PublicUnderwritingIntake))
    assert intake.variant == api._ADMIN_VARIANT_CONSTANTS[variant]
    assert intake.broker_id == user.id
    assert intake.source_user_id == user.id
    assert intake.intake_state["requested_funding_program"]["program_key"] == "selected_program"
    assert bucket.ai_context["requested_program_key"] == "selected_program"
    assert response.await_args.kwargs["include_management"] is False


async def test_duplicate_client_does_not_expose_another_partners_file_id():
    user = SimpleNamespace(id=uuid4(), role=Role.DEALER_PARTNER)
    existing = SimpleNamespace(id=uuid4(), broker_id=uuid4())
    payload = api.AdminLeadCreate(full_name="Client", email="client@example.com", phone="9735550148", secure_room_pin="123456", **BUSINESS_PROFILE)
    with patch.object(api, "_latest_active_intake_by_email", AsyncMock(return_value=existing)):
        with pytest.raises(HTTPException) as exc:
            await api._create_admin_ai_lead_core(payload, SimpleNamespace(), user, SimpleNamespace())
    assert exc.value.status_code == 409
    assert "intake_id" not in exc.value.detail


def test_partner_cannot_assign_another_owner_or_force_duplicate_files():
    values = dict(full_name="Client", email="client@example.com", phone="9735550148", secure_room_pin="123456", **BUSINESS_PROFILE)
    for patch_values in ({"broker_user_id": uuid4()}, {"client_id": uuid4()}, {"force_new": True}):
        with pytest.raises(ValidationError):
            api.BrokerLeadCreate(**values, **patch_values)


@pytest.mark.parametrize("variant", list(api._ADMIN_VARIANT_CONSTANTS.values()))
def test_ownership_is_program_independent_and_rejects_another_owner(variant):
    user = SimpleNamespace(id=uuid4())
    intake = SimpleNamespace(broker_id=user.id, variant=variant)
    assert dealer_partner_access.dealer_partner_intake_is_owned(user, intake)
    intake.broker_id = uuid4()
    assert not dealer_partner_access.dealer_partner_intake_is_owned(user, intake)


async def test_catalog_uses_active_scopes_and_rejects_retired_or_wrong_type():
    program_id = uuid4()
    row = SimpleNamespace(id=program_id, program_key="business", name="Business", short_description="Capital")
    def scope():
        return SimpleNamespace(is_active=True, intake_variants=[], vertical="main_street", intent_keys=[])
    with (
        patch.object(referral_programs.funding_programs, "catalog_rows", AsyncMock(return_value=[row])),
        patch.object(referral_programs.funding_programs, "scopes_by_program", AsyncMock(return_value={program_id: [scope()]})),
    ):
        programs = await referral_programs.active_catalog(SimpleNamespace())
        assert programs[0].variants == ["main_street"]
        await referral_programs.validate_request(SimpleNamespace(), "business", "main_street")
        for key, variant in (("retired", "main_street"), ("business", "real_estate")):
            with pytest.raises(HTTPException) as exc:
                await referral_programs.validate_request(SimpleNamespace(), key, variant)
            assert exc.value.status_code == 422


@pytest.mark.parametrize("role", [Role.DEALER_PARTNER, Role.PROFESSIONAL_REFERRAL_PARTNER, Role.FIELD_REP, Role.CAPITAL_PARTNER, Role.LENDER, Role.VENDOR, "unknown"])
def test_thin_and_unknown_roles_do_not_inherit_firm_wide_client_or_loan_access(role):
    from sqlalchemy import select

    from app.models.client import Client
    from app.models.loan import Loan
    from app.scoping import scope_client_query, scope_loan_query

    user = SimpleNamespace(role=role)
    assert "false" in str(scope_client_query(user, select(Client))).lower()
    assert "false" in str(scope_loan_query(user, select(Loan))).lower()
