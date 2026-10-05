from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.dealer_os import router as dealer_router
from app.dealer_os.prospect_schemas import ProspectCreate
from app.dealer_os.schemas import DealerCreate, RepAppointmentCreate
from app.dealer_os.services import booking_appointments, prospects
from app.enums import Role
from app.lead_types import (
    intake_variant_for,
    legacy_funding_purpose,
    normalize_funding_intent,
    normalize_lead_type,
)
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers import client_access
from app.services.application_profiles import _vertical_for_intake


def test_canonical_lead_registry_preserves_legacy_aliases() -> None:
    assert normalize_lead_type(None) == "dealer"
    assert normalize_lead_type("auto-dealer") == "dealer"
    assert normalize_lead_type("business") == "main_street"
    assert normalize_lead_type("mca_refinance") == "main_street"
    assert normalize_lead_type("commercial_foreclosure_bailout_v1") == "real_estate"
    assert normalize_funding_intent("floorplan") == "general_capital"
    assert normalize_funding_intent("refinance") == "debt_refinance"
    assert legacy_funding_purpose("mca_refinance") == "refinance"
    assert intake_variant_for("main_street", "mca_refinance") == "mca_refi_v1"
    assert intake_variant_for("real_estate", "real_estate") == "real_estate_dscr_v1"


def test_prospect_create_accepts_neutral_and_legacy_business_names() -> None:
    common = {
        "name": "Alex Owner",
        "email": "alex@example.com",
        "phone": "+12125550123",
    }
    neutral = ProspectCreate(
        **common,
        business_name="Alex Plumbing",
        lead_type="main_street",
        funding_intent="refinance",
    )
    legacy = ProspectCreate(**common, dealer_name="Alex Motors", lead_type="dealer")

    assert neutral.business_name == "Alex Plumbing"
    assert neutral.dealer_name == "Alex Plumbing"
    assert neutral.funding_intent == "debt_refinance"
    assert legacy.business_name == "Alex Motors"
    assert legacy.lead_type == "dealer"


def test_new_prospect_and_application_require_explicit_lead_type() -> None:
    with pytest.raises(ValidationError, match="lead_type"):
        ProspectCreate(
            contact_name="Alex Owner",
            business_name="Alex Plumbing",
            email="alex@example.com",
            phone="+12125550123",
        )
    with pytest.raises(ValidationError, match="lead_type"):
        DealerCreate(
            business_name="Alex Plumbing",
            entity_type="llc",
            funding_goal=125_000,
            funding_intent="working_capital",
            use_of_proceeds_note="Working capital.",
            secure_room_pin="482915",
        )


def test_application_create_derives_canonical_and_legacy_funding_fields() -> None:
    payload = DealerCreate(
        business_name="Northside Market",
        lead_type="main_street",
        entity_type="llc",
        funding_goal=125_000,
        funding_intent="business_acquisition",
        use_of_proceeds_note="Acquire the operating business.",
        secure_room_pin="482915",
    )

    assert payload.name == "Northside Market"
    assert payload.funding_intent == "business_acquisition"
    assert payload.funding_purpose == "other"


def test_booking_create_normalizes_application_classification() -> None:
    payload = RepAppointmentCreate(
        lead_type="business",
        funding_intent="mca_refi",
        starts_at=datetime(2026, 10, 5, 15, tzinfo=UTC),
        invitee_name="Alex Owner",
        invitee_email="alex@example.com",
    )

    assert payload.lead_type == "main_street"
    assert payload.funding_intent == "mca_refinance"


def test_standalone_field_desk_booking_requires_explicit_lead_type() -> None:
    payload = RepAppointmentCreate(
        starts_at=datetime(2026, 10, 5, 15, tzinfo=UTC),
        invitee_name="Alex Owner",
        invitee_email="alex@example.com",
    )

    with pytest.raises(HTTPException) as error:
        dealer_router._standalone_booking_classification(
            payload, origin="field_desk"
        )

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "lead_type_required"
    assert dealer_router._standalone_booking_classification(
        payload, origin="calendar"
    ) == ("main_street", None, False)


def test_appointment_application_route_is_owned_by_persisted_classification() -> None:
    appointment = SimpleNamespace(
        lead_type="main_street",
        funding_intent="mca_refinance",
    )

    assert (
        dealer_router._validate_appointment_conversion_classification(
            appointment,
            variant=None,
            lead_type=None,
            funding_intent=None,
        )
        == "mca_refinance"
    )
    with pytest.raises(HTTPException) as error:
        dealer_router._validate_appointment_conversion_classification(
            appointment,
            variant="real_estate",
            lead_type="real_estate",
            funding_intent="real_estate",
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "appointment_classification_mismatch"


@pytest.mark.asyncio
async def test_appointment_file_options_exclude_other_business_types() -> None:
    matching = SimpleNamespace(
        id=uuid4(),
        created_at=datetime(2026, 10, 4, tzinfo=UTC),
        variant="main_street_v1",
        intake_state={"lead_type": "main_street", "funding_intent": "working_capital"},
        business_name="Alex Plumbing",
        full_name="Alex Owner",
        email="alex@example.com",
        status="submitted",
    )
    other_type = SimpleNamespace(
        id=uuid4(),
        created_at=datetime(2026, 10, 3, tzinfo=UTC),
        variant="dealer_gatekeeper_v1",
        intake_state={"lead_type": "dealer"},
        business_name="Alex Motors",
        full_name="Alex Owner",
        email="alex@example.com",
        status="submitted",
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [matching, other_type])
            )
        )
    )
    appointment = SimpleNamespace(
        lead_type="main_street",
        funding_intent="working_capital",
    )

    result = await dealer_router._list_calendar_file_options(
        db,
        SimpleNamespace(role=Role.FIELD_REP, id=uuid4()),
        q="",
        limit=20,
        appointment=appointment,
    )

    assert [item.id for item in result.items] == [matching.id]


@pytest.mark.asyncio
async def test_non_real_estate_appointment_cannot_create_funding_loan() -> None:
    appointment = SimpleNamespace(
        lead_type="dealer",
        funding_intent="general_capital",
    )
    with pytest.raises(HTTPException) as error:
        await dealer_router._create_calendar_funding_file(
            SimpleNamespace(),
            appointment,
            SimpleNamespace(role=Role.SUPER_ADMIN),
        )

    assert error.value.status_code == 409
    assert (
        error.value.detail["code"]
        == "appointment_funding_file_classification_mismatch"
    )


@pytest.mark.parametrize(
    ("lead_type", "funding_intent", "variant", "vertical"),
    [
        ("dealer", "general_capital", "dealer_gatekeeper_v1", "dealer"),
        ("main_street", "working_capital", "main_street_v1", "main_street"),
        ("main_street", "mca_refinance", "mca_refi_v1", "main_street"),
        ("real_estate", "real_estate", "real_estate_dscr_v1", "real_estate"),
    ],
)
def test_application_profile_keeps_field_desk_vertical(
    lead_type: str,
    funding_intent: str,
    variant: str,
    vertical: str,
) -> None:
    intake = SimpleNamespace(
        variant=variant,
        intake_state={"lead_type": lead_type, "funding_intent": funding_intent},
    )
    assert _vertical_for_intake(intake) == vertical


@pytest.mark.asyncio
async def test_reclassification_is_locked_after_conversion() -> None:
    prospect = SimpleNamespace(
        id=uuid4(),
        lead_type="dealer",
        funding_intent=None,
        conversion_target="dealer_ai_intake",
        converted_application_id=None,
        converted_intake_id=uuid4(),
    )
    db = SimpleNamespace(execute=AsyncMock())
    actor = SimpleNamespace(id=uuid4(), role=Role.FIELD_REP)

    with pytest.raises(HTTPException) as error:
        await prospects.assert_reclassification_allowed(
            db,
            prospect,
            actor=actor,
            lead_type="main_street",
            funding_intent="working_capital",
            reason="The original type was selected incorrectly.",
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "prospect_reclassification_locked"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_reclassification_requires_reason_and_voids_unsent_drafts() -> None:
    prospect = SimpleNamespace(
        id=uuid4(),
        lead_type="dealer",
        funding_intent=None,
        conversion_target=None,
        converted_application_id=None,
        converted_intake_id=None,
    )
    actor = SimpleNamespace(id=uuid4(), role=Role.FIELD_REP)
    db = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await prospects.assert_reclassification_allowed(
            db,
            prospect,
            actor=actor,
            lead_type="main_street",
            funding_intent="working_capital",
            reason=None,
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "reclassification_reason_required"

    draft = SimpleNamespace(
        id=uuid4(),
        status="pending_review",
        auto_send_at=object(),
        review_stopped_at=None,
        cancelled_at=None,
        cancelled_by_user_id=None,
        cancellation_source=None,
        secure_bundle_token_hash="secret",
        secure_bundle_expires_at=object(),
        version=2,
    )
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [draft]))
    db.execute = AsyncMock(return_value=result)

    lead_type, intent, voided = await prospects.assert_reclassification_allowed(
        db,
        prospect,
        actor=actor,
        lead_type="main_street",
        funding_intent="working_capital",
        reason="This is an operating business.",
    )

    assert (lead_type, intent) == ("main_street", "working_capital")
    assert voided == [{"draft_id": str(draft.id), "previous_status": "pending_review"}]
    assert draft.status == "cancelled"
    assert draft.auto_send_at is None
    assert draft.cancelled_by_user_id == actor.id
    assert draft.cancellation_source == "prospect_reclassified"
    assert draft.secure_bundle_token_hash is None
    assert draft.version == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lead_type", "funding_intent", "variant", "builder_name"),
    [
        ("dealer", "general_capital", "dealer_gatekeeper_v1", "_create_bucket_for_intake"),
        ("main_street", "working_capital", "main_street_v1", "_create_bucket_for_main_street"),
        ("main_street", "mca_refinance", "mca_refi_v1", "_create_bucket_for_mca_refi"),
        ("real_estate", "real_estate", "real_estate_dscr_v1", "_create_bucket_for_funding_review"),
    ],
)
async def test_intake_conversion_routes_by_classification(
    monkeypatch: pytest.MonkeyPatch,
    lead_type: str,
    funding_intent: str,
    variant: str,
    builder_name: str,
) -> None:
    from app.routers import dealer_ai_intake
    from app.services import application_profiles

    prospect = SimpleNamespace(
        id=uuid4(),
        primary_contact_id=uuid4(),
        company_id=uuid4(),
        owner_user_id=uuid4(),
        email_normalized="alex@example.com",
        phone_normalized="+12125550123",
        lead_type=lead_type,
        funding_intent=funding_intent,
    )
    contact = SimpleNamespace(full_name="Alex Owner")
    company = SimpleNamespace(name="Alex Business", industry="restaurant")
    client = SimpleNamespace(
        id=uuid4(),
        originating_agent_id=None,
        current_agent_id=None,
    )
    bucket = SimpleNamespace(id=uuid4())
    link = SimpleNamespace(id=uuid4())
    added: list[object] = []

    async def get(model, row_id):
        if row_id == prospect.primary_contact_id:
            return contact
        if row_id == prospect.company_id:
            return company
        return None

    db = SimpleNamespace(get=get, add=Mock(side_effect=added.append), flush=AsyncMock())
    for helper in (
        "_find_or_create_client",
        "_find_or_create_funding_client",
        "_find_or_create_mca_client",
    ):
        monkeypatch.setattr(dealer_ai_intake, helper, AsyncMock(return_value=client))
    builders: dict[str, AsyncMock] = {}
    for helper in (
        "_create_bucket_for_intake",
        "_create_bucket_for_main_street",
        "_create_bucket_for_mca_refi",
        "_create_bucket_for_funding_review",
    ):
        builders[helper] = AsyncMock(return_value=(bucket, link))
        monkeypatch.setattr(dealer_ai_intake, helper, builders[helper])
    provision = AsyncMock()
    monkeypatch.setattr(application_profiles, "provision_profile_for_intake", provision)

    intake = await prospects.create_intake_from_prospect(
        db,
        SimpleNamespace(),
        prospect,
        SimpleNamespace(id=uuid4(), name="Agent", email="agent@example.com"),
    )

    assert isinstance(intake, PublicUnderwritingIntake)
    assert intake.variant == variant
    assert intake.loan_purpose == funding_intent
    assert intake.intake_state["lead_type"] == lead_type
    assert intake.intake_state["funding_intent"] == funding_intent
    builders[builder_name].assert_awaited_once()
    provision.assert_awaited_once_with(db, intake)


@pytest.mark.asyncio
async def test_client_access_legacy_mca_profile_overrides_generic_funding_category() -> None:
    profile = SimpleNamespace(
        id=uuid4(),
        intake_id=uuid4(),
        dealer_id=None,
        client_id=None,
        primary_bucket_id=uuid4(),
        vertical="mca",
        funding_category="working_capital",
        industry=None,
        entity_type=None,
        naics_code=None,
        naics_label=None,
    )
    intake = SimpleNamespace(
        id=profile.intake_id,
        variant="mca_refi_v1",
        intake_state={},
        business_name="Alex Business",
        email="alex@example.com",
        phone="+12125550123",
    )
    target = SimpleNamespace(id=uuid4(), name="Alex Owner", email="alex@example.com")
    actor = SimpleNamespace(id=uuid4())
    added: list[object] = []

    def scalar_result(value):
        return SimpleNamespace(scalar_one_or_none=lambda: value)

    def rows_result(rows):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    execute = AsyncMock(
        side_effect=[
            scalar_result(profile.id),
            rows_result([profile]),
            rows_result([intake]),
            rows_result([]),
        ]
    )

    async def flush() -> None:
        for row in added:
            if getattr(row, "id", None) is None:
                row.id = uuid4()

    db = SimpleNamespace(
        execute=execute,
        get=AsyncMock(return_value=None),
        add=Mock(side_effect=added.append),
        flush=AsyncMock(side_effect=flush),
    )

    assigned = await client_access._assign_audit_scopes(
        db,
        target=target,
        client=None,
        intake=intake,
        profile_ids=[profile.id],
        actor=actor,
    )

    dealer = added[0]
    assert assigned == [profile.id]
    assert dealer.lead_type == "main_street"
    assert dealer.funding_intent == "mca_refinance"
    assert dealer.funding_purpose == "refinance"


@pytest.mark.asyncio
async def test_booking_legacy_mca_variant_overrides_generic_loan_purpose() -> None:
    intake = SimpleNamespace(
        id=uuid4(),
        variant="mca_refi_v1",
        intake_state={},
        loan_purpose="working_capital",
    )
    event = SimpleNamespace(
        id=uuid4(),
        title="MCA refinance review",
        starts_at=datetime(2026, 10, 5, 15, tzinfo=UTC),
        duration_min=30,
        external_ref_kind=None,
        external_ref_id=None,
    )
    booking = SimpleNamespace(timezone="America/New_York", duration_min=30)
    db = SimpleNamespace(
        get=AsyncMock(return_value=intake),
        add=Mock(),
        flush=AsyncMock(),
    )

    appointment = await booking_appointments.create_booking_appointment(
        db,
        event=event,
        host=SimpleNamespace(id=uuid4()),
        booking=booking,
        origin="public",
        invitee_name="Alex Owner",
        invitee_email=None,
        invitee_phone=None,
        precall_intake_id=intake.id,
    )

    assert appointment.lead_type == "main_street"
    assert appointment.funding_intent == "mca_refinance"
