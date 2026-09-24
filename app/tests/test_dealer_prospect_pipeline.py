from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.dealer_os import crm_router, prospect_router
from app.dealer_os.crm_schemas import ContactAssignmentIn
from app.dealer_os.models import DealerRepContactAssignment
from app.dealer_os.prospect_schemas import (
    ProspectConversionRequest,
    ProspectCreate,
    ProspectDefinitionReorder,
    ProspectGeneralConversionRequest,
    ProspectMoveResult,
    ProspectMoveStage,
    ProspectOutcomeApply,
    ProspectPatch,
    ProspectPortfolioApplicationCreate,
)
from app.dealer_os.services import prospect_conversion, prospects
from app.enums import Role
from app.models.dealer_prospect import DealerProspect


def _user(role: Role, *, user_id=None):
    return SimpleNamespace(
        id=user_id or uuid4(),
        role=role,
        account_access_types=[],
        account_status="active",
        deleted_at=None,
        dealer_prospect_pipeline_enabled=True,
        name="Pipeline User",
        email="agent@example.com",
    )


def test_quick_add_accepts_frontend_name_alias_and_normalizes_phone() -> None:
    contact_id = uuid4()
    payload = ProspectCreate(
        contact_id=contact_id,
        name="  Rocio Martinez  ",
        dealer_name=" Grace Auto Sales ",
        email="ROCIO@EXAMPLE.COM",
        phone="(973) 555-0148",
        initial_note="  Spoke about dealership working capital.  ",
    )

    assert payload.contact_name == "Rocio Martinez"
    assert payload.contact_id == contact_id
    assert payload.dealer_name == "Grace Auto Sales"
    assert payload.phone == "+19735550148"
    assert payload.initial_note == "Spoke about dealership working capital."


def test_quick_add_normalizes_blank_initial_note_to_none() -> None:
    payload = ProspectCreate(
        contact_name="Rocio Martinez",
        dealer_name="Grace Auto Sales",
        email="rocio@example.com",
        phone="(973) 555-0148",
        initial_note="   ",
    )

    assert payload.initial_note is None


@pytest.mark.asyncio
async def test_quick_add_route_forwards_private_initial_note(monkeypatch) -> None:
    payload = ProspectCreate(
        contact_name="Rocio Martinez",
        dealer_name="Grace Auto Sales",
        email="rocio@example.com",
        phone="(973) 555-0148",
        initial_note="Spoke about dealership working capital.",
    )
    created = SimpleNamespace(id=uuid4())
    create_prospect = AsyncMock(return_value=created)
    stop_after_create = AsyncMock(side_effect=RuntimeError("stop after create"))
    monkeypatch.setattr(prospect_router.service, "create_prospect", create_prospect)
    monkeypatch.setattr(prospect_router, "_refresh_for_read", stop_after_create)

    with pytest.raises(RuntimeError, match="stop after create"):
        await prospect_router.quick_add_prospect(payload, _user(Role.LOAN_EXEC), SimpleNamespace())

    assert create_prospect.await_args.kwargs["initial_note"] == (
        "Spoke about dealership working capital."
    )


@pytest.mark.asyncio
async def test_create_prospect_records_private_initial_note_after_created_activity(
    monkeypatch,
) -> None:
    user = _user(Role.LOAN_EXEC)
    stage = SimpleNamespace(id=uuid4(), key="new")
    added = []

    class ScalarResult:
        @staticmethod
        def scalar_one_or_none():
            return stage

    class EmptyRowsResult:
        def scalars(self):
            return self

        @staticmethod
        def first():
            return None

    async def flush() -> None:
        for row in added:
            if hasattr(row, "id") and row.id is None:
                row.id = uuid4()

    db = SimpleNamespace(
        get=AsyncMock(return_value=user),
        execute=AsyncMock(
            side_effect=[ScalarResult(), EmptyRowsResult(), EmptyRowsResult()]
        ),
        add=Mock(side_effect=added.append),
        flush=AsyncMock(side_effect=flush),
    )
    monkeypatch.setattr(prospects, "find_duplicates", AsyncMock(return_value=[]))
    monkeypatch.setattr(prospects, "ensure_default_definitions", AsyncMock())

    prospect = await prospects.create_prospect(
        db,
        user,
        contact_name="Rocio Martinez",
        dealer_name="Grace Auto Sales",
        email="rocio@example.com",
        phone="+19735550148",
        source="quick_add",
        owner_user_id=None,
        initial_note="Spoke about dealership working capital.",
    )

    activities = [row for row in added if row.__class__.__name__ == "DealerProspectActivity"]
    assert [row.kind for row in activities] == ["prospect_created", "internal_note"]
    assert activities[1].body == "Spoke about dealership working capital."
    assert activities[1].metadata_json == {
        "private": True,
        "source": "prospect_creation",
    }
    assert prospect.last_activity_at is not None


@pytest.mark.asyncio
async def test_prospect_timeline_uses_stable_same_timestamp_tie_breaker() -> None:
    prospect = DealerProspect(
        id=uuid4(),
        owner_user_id=uuid4(),
        company_id=uuid4(),
        primary_contact_id=uuid4(),
        stage_definition_id=uuid4(),
        email_normalized="rocio@example.com",
        phone_normalized="+19735550148",
        dealer_name_normalized="grace auto sales",
        source="quick_add",
        version=1,
    )
    prospect.created_at = datetime.now(UTC)
    prospect.updated_at = prospect.created_at
    contact = SimpleNamespace(
        full_name="Rocio Martinez",
        email="rocio@example.com",
        phone_e164="+19735550148",
        sms_marketing_consented_at=None,
        sms_opted_out_at=None,
    )
    company = SimpleNamespace(name="Grace Auto Sales")
    stage = SimpleNamespace(id=prospect.stage_definition_id, key="new", label="New", sort_order=0)
    owner = SimpleNamespace(name="Pipeline User")

    async def get(model, _row_id):
        return {
            "DealerRepContact": contact,
            "DealerRepCompany": company,
            "DealerProspectStageDefinition": stage,
            "User": owner,
        }[model.__name__]

    class EmptyRowsResult:
        def scalars(self):
            return self

        @staticmethod
        def all():
            return []

    db = SimpleNamespace(get=get, execute=AsyncMock(return_value=EmptyRowsResult()))

    await prospects.prospect_read(db, prospect, include_activities=True)

    statement = str(db.execute.await_args_list[0].args[0])
    assert "dealer_prospect_activities.created_at DESC" in statement
    assert "dealer_prospect_activities.id DESC" in statement


def test_drag_move_accepts_explicit_null_action() -> None:
    payload = ProspectMoveStage(
        stage_key=" Follow Up 1 ",
        expected_version=4,
        action=None,
    )

    assert payload.stage_key == "follow up 1"
    assert payload.action is None


def test_conversion_requires_candidate_id_only_for_existing_actions() -> None:
    candidate_id = uuid4()
    assert ProspectConversionRequest(expected_version=1).action == "detect"
    assert (
        ProspectConversionRequest(
            action="link", expected_version=1, intake_id=candidate_id
        ).intake_id
        == candidate_id
    )
    with pytest.raises(ValueError):
        ProspectConversionRequest(action="reactivate", expected_version=1)
    with pytest.raises(ValueError):
        ProspectConversionRequest(action="create", expected_version=1, intake_id=candidate_id)


def test_general_conversion_requires_explicit_target_specific_inputs() -> None:
    application = ProspectPortfolioApplicationCreate(
        entity_type="llc",
        requested_amount=250_000,
        funding_purpose="working_capital",
        use_of_proceeds_note="Acquire additional dealer inventory.",
        secure_room_pin="482915",
    )
    payload = ProspectGeneralConversionRequest(
        target="portfolio_application",
        action="create",
        expected_version=3,
        portfolio_application=application,
    )
    assert payload.portfolio_application.requested_amount == 250_000

    with pytest.raises(ValueError):
        ProspectGeneralConversionRequest(
            target="portfolio_application", action="create", expected_version=3
        )
    with pytest.raises(ValueError):
        ProspectGeneralConversionRequest(
            target="dealer_ai_intake",
            action="create",
            expected_version=3,
            portfolio_application=application,
        )
    with pytest.raises(ValueError):
        ProspectGeneralConversionRequest(
            target="dealer_ai_intake", action="link", expected_version=3
        )


def test_reorder_rejects_duplicate_definition_ids() -> None:
    row_id = uuid4()
    with pytest.raises(ValueError):
        ProspectDefinitionReorder(ordered_ids=[row_id, row_id])


def test_dealer_identity_normalization_is_case_and_spacing_stable() -> None:
    assert prospects.normalize_dealer_name("  GRACE   Auto Sales  ") == "grace auto sales"
    assert prospects.normalize_dealer_name("Ｇｒａｃｅ Auto") == "grace auto"


def test_ai_intake_candidate_explains_all_matching_identity_signals() -> None:
    prospect = SimpleNamespace(
        email_normalized="rocio@example.com",
        phone_normalized="+19735550148",
        dealer_name_normalized="grace auto sales",
    )
    intake = SimpleNamespace(
        email="ROCIO@example.com",
        phone="(973) 555-0148",
        business_name="  Grace   Auto Sales ",
    )

    assert prospects.intake_candidate_match_reasons(prospect, intake) == [
        "email",
        "phone",
        "dealer_name",
    ]


def test_portfolio_candidate_explains_all_matching_identity_signals() -> None:
    prospect = SimpleNamespace(
        email_normalized="rocio@example.com",
        phone_normalized="+19735550148",
        dealer_name_normalized="grace auto sales",
    )
    application = SimpleNamespace(
        email="ROCIO@example.com",
        phone="(973) 555-0148",
        name="  Grace   Auto Sales ",
    )

    assert prospect_conversion.portfolio_candidate_match_reasons(
        prospect, application
    ) == ["email", "phone", "dealer_name"]


@pytest.mark.asyncio
async def test_complete_conversion_rejects_a_second_destination() -> None:
    prospect = SimpleNamespace(
        converted_application_id=uuid4(),
        converted_intake_id=None,
    )
    db = SimpleNamespace(execute=AsyncMock(), get=AsyncMock(), flush=AsyncMock())

    with pytest.raises(HTTPException) as error:
        await prospects.complete_target_conversion(
            db,
            _user(Role.LOAN_EXEC),
            prospect,
            target="dealer_ai_intake",
            destination_id=uuid4(),
            event_kind="dealer_ai_intake_linked",
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "prospect_already_converted"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_portfolio_creation_never_marks_prospect_converted(monkeypatch) -> None:
    prospect = SimpleNamespace(
        id=uuid4(),
        version=1,
        converted_application_id=None,
        converted_intake_id=None,
    )
    create_application = AsyncMock(side_effect=RuntimeError("room setup failed"))
    complete_conversion = AsyncMock()
    monkeypatch.setattr(
        prospect_router.service, "load_visible_prospect", AsyncMock(return_value=prospect)
    )
    monkeypatch.setattr(prospect_router, "_conversion_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        prospect_router.conversion_service,
        "create_portfolio_application",
        create_application,
    )
    monkeypatch.setattr(
        prospect_router.conversion_service,
        "acquire_conversion_identity_lock",
        AsyncMock(),
    )
    monkeypatch.setattr(
        prospect_router.service, "complete_target_conversion", complete_conversion
    )
    payload = ProspectGeneralConversionRequest(
        target="portfolio_application",
        action="create",
        expected_version=1,
        portfolio_application=ProspectPortfolioApplicationCreate(
            entity_type="llc",
            requested_amount=250_000,
            funding_purpose="working_capital",
            use_of_proceeds_note="Acquire additional dealer inventory.",
            secure_room_pin="482915",
        ),
    )
    request = Request(
        {"type": "http", "method": "POST", "path": f"/prospects/{prospect.id}/convert", "headers": []}
    )

    with pytest.raises(RuntimeError, match="room setup failed"):
        await prospect_router.convert_prospect(
            prospect.id, payload, request, _user(Role.LOAN_EXEC), SimpleNamespace()
        )

    complete_conversion.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_ai_conversion_honors_existing_portfolio_conversion(monkeypatch) -> None:
    application_id = uuid4()
    prospect = SimpleNamespace(
        id=uuid4(),
        version=4,
        converted_application_id=application_id,
        converted_intake_id=None,
    )
    now = datetime.now(UTC)
    read = prospect_router.ProspectRead(
        id=prospect.id,
        owner_user_id=None,
        company_id=uuid4(),
        primary_contact_id=uuid4(),
        contact_id=uuid4(),
        contact_name="Rocio Martinez",
        name="Rocio Martinez",
        dealer_name="Grace Auto Sales",
        email="rocio@example.com",
        phone="+19735550148",
        stage_id=uuid4(),
        stage_key="converted",
        stage_label="Converted",
        stage_sort_order=50,
        source="quick_add",
        next_follow_up_at=None,
        last_activity_at=now,
        call_attempt_count=0,
        do_not_contact=False,
        do_not_contact_reason=None,
        appointment_id=None,
        conversion_target="portfolio_application",
        converted_application_id=application_id,
        converted_intake_id=None,
        converted_at=now,
        version=4,
        created_at=now,
        updated_at=now,
    )
    find_candidates = AsyncMock()
    monkeypatch.setattr(
        prospect_router.service, "load_visible_prospect", AsyncMock(return_value=prospect)
    )
    monkeypatch.setattr(prospect_router.service, "prospect_read", AsyncMock(return_value=read))
    monkeypatch.setattr(prospect_router.service, "intake_candidates", find_candidates)
    db = SimpleNamespace(refresh=AsyncMock())

    result = await prospect_router.convert_prospect_to_ai_intake(
        prospect.id,
        ProspectConversionRequest(expected_version=1),
        Request({"type": "http", "method": "POST", "path": "/convert", "headers": []}),
        _user(Role.LOAN_EXEC),
        db,
    )

    assert result.status == "already_converted"
    assert result.conversion_target == "portfolio_application"
    assert result.application_id == application_id
    find_candidates.assert_not_awaited()


@pytest.mark.asyncio
async def test_general_conversion_retry_with_stale_version_returns_existing_destination(
    monkeypatch,
) -> None:
    application_id = uuid4()
    prospect = SimpleNamespace(
        id=uuid4(),
        version=7,
        converted_application_id=application_id,
        converted_intake_id=None,
    )
    now = datetime.now(UTC)
    read = prospect_router.ProspectRead(
        id=prospect.id,
        owner_user_id=None,
        company_id=uuid4(),
        primary_contact_id=uuid4(),
        contact_id=uuid4(),
        contact_name="Rocio Martinez",
        name="Rocio Martinez",
        dealer_name="Grace Auto Sales",
        email="rocio@example.com",
        phone="+19735550148",
        stage_id=uuid4(),
        stage_key="converted",
        stage_label="Converted",
        stage_sort_order=50,
        source="quick_add",
        next_follow_up_at=None,
        last_activity_at=now,
        call_attempt_count=0,
        do_not_contact=False,
        do_not_contact_reason=None,
        appointment_id=None,
        conversion_target="portfolio_application",
        converted_application_id=application_id,
        converted_intake_id=None,
        converted_at=now,
        version=7,
        created_at=now,
        updated_at=now,
    )
    find_candidates = AsyncMock()
    monkeypatch.setattr(
        prospect_router.service, "load_visible_prospect", AsyncMock(return_value=prospect)
    )
    monkeypatch.setattr(prospect_router.service, "prospect_read", AsyncMock(return_value=read))
    monkeypatch.setattr(prospect_router, "_conversion_candidates", find_candidates)
    db = SimpleNamespace(refresh=AsyncMock())

    result = await prospect_router.convert_prospect(
        prospect.id,
        ProspectGeneralConversionRequest(
            target="dealer_ai_intake",
            action="detect",
            expected_version=1,
        ),
        Request({"type": "http", "method": "POST", "path": "/convert", "headers": []}),
        _user(Role.LOAN_EXEC),
        db,
    )

    assert result.status == "already_converted"
    assert result.conversion_target == "portfolio_application"
    assert result.application_id == application_id
    find_candidates.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["portfolio_application", "dealer_ai_intake"])
async def test_active_conversion_candidate_cannot_use_reactivate(monkeypatch, target) -> None:
    candidate_id = uuid4()
    prospect = SimpleNamespace(
        id=uuid4(),
        version=1,
        converted_application_id=None,
        converted_intake_id=None,
    )
    candidate = SimpleNamespace(id=candidate_id)
    candidate_read = prospect_router.ProspectConversionCandidate(
        id=candidate_id,
        target=target,
        status="active",
        archived=False,
        display_name="Grace Auto Sales",
        email="rocio@example.com",
        phone="+19735550148",
        created_at=datetime.now(UTC),
        match_reasons=["dealer_name"],
        route=f"/{target}/{candidate_id}",
    )
    monkeypatch.setattr(
        prospect_router.service, "load_visible_prospect", AsyncMock(return_value=prospect)
    )
    monkeypatch.setattr(
        prospect_router, "_conversion_candidates", AsyncMock(return_value=[candidate])
    )
    monkeypatch.setattr(
        prospect_router,
        "_conversion_candidate_read",
        lambda *_args, **_kwargs: candidate_read,
    )
    monkeypatch.setattr(
        prospect_router.conversion_service,
        "acquire_conversion_identity_lock",
        AsyncMock(),
    )

    with pytest.raises(HTTPException) as error:
        await prospect_router.convert_prospect(
            prospect.id,
            ProspectGeneralConversionRequest(
                target=target,
                action="reactivate",
                candidate_id=candidate_id,
                expected_version=1,
            ),
            Request(
                {"type": "http", "method": "POST", "path": "/convert", "headers": []}
            ),
            _user(Role.LOAN_EXEC),
            SimpleNamespace(),
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "prospect_conversion_candidate_active"


@pytest.mark.asyncio
async def test_owner_reassignment_rotates_only_owner_derived_contact_grants(monkeypatch) -> None:
    actor = _user(Role.LOAN_EXEC)
    previous_owner_id = uuid4()
    next_owner = _user(Role.FIELD_REP)
    contact = SimpleNamespace(id=uuid4(), owner_user_id=previous_owner_id)
    company = SimpleNamespace(id=uuid4())
    prospect = SimpleNamespace(
        id=uuid4(),
        version=1,
        owner_user_id=previous_owner_id,
        primary_contact_id=contact.id,
        company_id=company.id,
        dealer_name_normalized="grace auto sales",
        email_normalized="rocio@example.com",
        phone_normalized="+19735550148",
    )
    added = []

    async def get(model, row_id):
        if model.__name__ == "DealerRepContact":
            return contact
        if model.__name__ == "DealerRepCompany":
            return company
        if model.__name__ == "User" and row_id == next_owner.id:
            return next_owner
        return None

    no_existing_assignment = SimpleNamespace(scalar_one_or_none=lambda: None)
    db = SimpleNamespace(
        get=get,
        execute=AsyncMock(side_effect=[SimpleNamespace(), no_existing_assignment]),
        add=Mock(side_effect=added.append),
    )
    monkeypatch.setattr(
        prospect_router.service, "load_visible_prospect", AsyncMock(return_value=prospect)
    )
    monkeypatch.setattr(
        prospect_router.service,
        "find_duplicates",
        AsyncMock(side_effect=RuntimeError("stop after assignment rotation")),
    )

    with pytest.raises(RuntimeError, match="stop after assignment rotation"):
        await prospect_router.patch_prospect(
            prospect.id,
            ProspectPatch(expected_version=1, owner_user_id=next_owner.id),
            actor,
            db,
        )

    delete_statement = db.execute.await_args_list[0].args[0]
    delete_params = delete_statement.compile().params
    assert delete_params["assignment_kind_1"] == "prospect_owner"
    assert previous_owner_id in delete_params.values()
    assert contact.id in delete_params.values()
    owner_grants = [row for row in added if isinstance(row, DealerRepContactAssignment)]
    assert len(owner_grants) == 1
    assert owner_grants[0].user_id == next_owner.id
    assert owner_grants[0].assignment_kind == "prospect_owner"


@pytest.mark.asyncio
async def test_explicit_contact_share_promotes_owner_grant_and_survives_transfer(monkeypatch) -> None:
    actor = _user(Role.LOAN_EXEC)
    shared_user_id = uuid4()
    contact = SimpleNamespace(id=uuid4(), owner_user_id=actor.id, dealer_id=None)
    existing = SimpleNamespace(
        contact_id=contact.id,
        user_id=shared_user_id,
        assigned_by_user_id=actor.id,
        assignment_kind="prospect_owner",
    )
    monkeypatch.setattr(crm_router, "_load_contact", AsyncMock(return_value=contact))
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: existing)
        ),
        add=Mock(),
        commit=AsyncMock(),
    )

    result = await crm_router.assign_contact(
        contact.id,
        ContactAssignmentIn(user_id=shared_user_id),
        actor,
        db,
    )

    assert result == {"assigned": True}
    assert existing.assignment_kind == "explicit"
    assert existing.assigned_by_user_id == actor.id
    db.add.assert_not_called()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_team_owned_portfolio_conversion_does_not_create_rep_reporting_row(
    monkeypatch,
) -> None:
    actor = _user(Role.LOAN_EXEC)
    prospect = SimpleNamespace(
        id=uuid4(),
        owner_user_id=actor.id,
        primary_contact_id=uuid4(),
        company_id=uuid4(),
        email_normalized="rocio@example.com",
        phone_normalized="+19735550148",
    )
    contact = SimpleNamespace(
        id=prospect.primary_contact_id,
        full_name="Rocio Martinez",
        email="rocio@example.com",
        phone_e164="+19735550148",
        dealer_id=None,
    )
    company = SimpleNamespace(id=prospect.company_id, name="Grace Auto Sales")
    added = []

    async def get(model, row_id):
        if model.__name__ == "DealerRepContact":
            return contact
        if model.__name__ == "DealerRepCompany":
            return company
        if model.__name__ == "User" and row_id == actor.id:
            return actor
        return None

    async def flush() -> None:
        for row in added:
            if row.__class__.__name__ == "DealerBusiness" and row.id is None:
                row.id = uuid4()

    db = SimpleNamespace(get=get, add=Mock(side_effect=added.append), flush=AsyncMock(side_effect=flush))
    monkeypatch.setattr(prospect_conversion, "next_case_ref", AsyncMock(return_value="QC-2026-00001"))
    monkeypatch.setattr(prospect_conversion, "propose_targets", AsyncMock())
    monkeypatch.setattr(prospect_conversion.buckets_link, "ensure_bucket", AsyncMock())
    monkeypatch.setattr(prospect_conversion.client_room, "initialize_room", AsyncMock())
    monkeypatch.setattr(prospect_conversion, "_link_contact", AsyncMock())
    monkeypatch.setattr(prospect_conversion, "log_action", AsyncMock())

    await prospect_conversion.create_portfolio_application(
        db,
        prospect,
        actor,
        ProspectPortfolioApplicationCreate(
            entity_type="llc",
            requested_amount=250_000,
            funding_purpose="working_capital",
            use_of_proceeds_note="Acquire additional dealer inventory.",
            secure_room_pin="482915",
        ),
    )

    assert not any(row.__class__.__name__ == "DealerRepLead" for row in added)


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("new", "emailed"),
        ("emailed", "follow_up_1"),
        ("follow_up_1", "follow_up_2"),
        ("follow_up_2", "follow_up_2"),
        ("booked", None),
    ],
)
def test_not_connected_advances_only_the_follow_up_sequence(
    current: str, expected: str | None
) -> None:
    assert (
        prospects.outcome_target_stage(current, {"stage_strategy": "advance_follow_up"}) == expected
    )


def test_not_connected_uses_configured_automatic_follow_up_delay() -> None:
    current = datetime(2026, 9, 16, 12, tzinfo=UTC)
    explicit = datetime(2026, 9, 18, 15, tzinfo=UTC)

    assert prospects.outcome_follow_up_at(
        {"follow_up_delay_hours": 24}, None, current_time=current
    ) == current + timedelta(hours=24)
    assert (
        prospects.outcome_follow_up_at(
            {"follow_up_delay_hours": 24}, explicit, current_time=current
        )
        == explicit
    )
    with pytest.raises(HTTPException) as error:
        prospects.validate_action_config({"follow_up_delay_hours": 0})
    assert error.value.status_code == 422


def test_outcome_action_config_is_allowlisted() -> None:
    assert prospects.validate_action_config(
        {"target_stage_key": "Follow Up 1", "requires_follow_up": True}
    ) == {"target_stage_key": "follow_up_1", "requires_follow_up": True}
    with pytest.raises(HTTPException) as error:
        prospects.validate_action_config({"send_arbitrary_webhook": True})
    assert error.value.status_code == 422
    assert (
        prospects.validate_action_config(
            {"target_stage_key": "", "email_action": "", "workflow_action": ""}
        )
        == {}
    )
    with pytest.raises(HTTPException):
        prospects.validate_action_config({"email_action": "send_whatever"})
    with pytest.raises(HTTPException):
        prospects.validate_action_config({"workflow_action": "run_whatever"})


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            {"target_stage_key": "emailed", "stage_strategy": "advance_follow_up"},
            "either a fixed target stage or an automatic stage strategy",
        ),
        (
            {"email_action": "missed_call", "set_do_not_contact": True},
            "blocks contact",
        ),
        (
            {"email_action": "missed_call", "suppress_email": True},
            "blocks contact",
        ),
        (
            {"clear_follow_up": True, "requires_follow_up": True},
            "clear follow-up while requiring or scheduling",
        ),
        (
            {"clear_follow_up": True, "follow_up_delay_hours": 24},
            "clear follow-up while requiring or scheduling",
        ),
        (
            {"requires_follow_up": True, "follow_up_delay_hours": 24},
            "either a required follow-up time or an automatic follow-up delay",
        ),
        (
            {"target_stage_key": "converted"},
            "AI Intake conversion workflow",
        ),
        (
            {"target_stage_key": "booked"},
            "requires a linked appointment",
        ),
        (
            {"target_stage_key": "not_interested", "clear_follow_up": True},
            "mark do-not-contact and clear follow-up",
        ),
        (
            {"target_stage_key": "not_interested", "set_do_not_contact": True},
            "mark do-not-contact and clear follow-up",
        ),
        (
            {
                "target_stage_key": "not_interested",
                "set_do_not_contact": True,
                "clear_follow_up": True,
                "workflow_action": "book_appointment",
            },
            "cannot create an email or start a workflow",
        ),
    ],
)
def test_outcome_action_config_rejects_contradictory_or_unsafe_effects(
    config: dict[str, object], message: str
) -> None:
    with pytest.raises(HTTPException) as error:
        prospects.validate_action_config(config)

    assert error.value.status_code == 422
    assert message in str(error.value.detail)


def test_default_outcome_action_configs_remain_valid() -> None:
    for outcome in prospects.DEFAULT_OUTCOMES:
        assert (
            prospects.validate_action_config(outcome["action_config"]) == outcome["action_config"]
        )


def test_client_will_call_back_is_non_stage_moving_with_two_day_safety_follow_up() -> None:
    definition = next(
        item for item in prospects.DEFAULT_OUTCOMES if item["key"] == "client_will_call_back"
    )
    config = definition["action_config"]
    assert definition["label"] == "Client will call back"
    assert "target_stage_key" not in config
    assert "stage_strategy" not in config
    assert config["increment_call_attempt"] is True
    assert config["follow_up_business_days"] == 2
    assert config["email_action"] == "client_will_call_back"
    assert prospects.outcome_target_stage("emailed", config) is None


def test_outcome_contract_normalizes_optional_ai_and_cc_controls() -> None:
    payload = ProspectOutcomeApply.model_validate(
        {
            "outcome_key": " CLIENT_WILL_CALL_BACK ",
            "expected_version": 3,
            "ai_draft_instructions": "  Thank them for today's call.  ",
            "cc_emails": "Broker@Example.com; agent@example.com,broker@example.com",
            "cc_scope": "this_and_future",
            "skip_email_draft": True,
        }
    )

    assert payload.outcome_key == "client_will_call_back"
    assert payload.ai_draft_instructions == "Thank them for today's call."
    assert payload.cc_emails == ["broker@example.com", "agent@example.com"]
    assert payload.cc_scope == "this_and_future"
    assert payload.skip_email_draft is True


@pytest.mark.parametrize(
    "config",
    [
        {
            "target_stage_key": "booked",
            "requires_appointment": True,
        },
        {
            "target_stage_key": "not_interested",
            "set_do_not_contact": True,
            "clear_follow_up": True,
        },
        {
            "set_do_not_contact": True,
            "clear_follow_up": True,
            "suppress_email": True,
        },
    ],
)
def test_outcome_action_config_accepts_safe_terminal_effects(
    config: dict[str, object],
) -> None:
    assert prospects.validate_action_config(config) == config


def test_optimistic_version_conflict_returns_machine_readable_detail() -> None:
    prospect = SimpleNamespace(version=7)
    with pytest.raises(HTTPException) as error:
        prospects.assert_expected_version(prospect, 6)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "prospect_version_conflict"
    assert error.value.detail["current_version"] == 7


def test_duplicate_response_does_not_leak_another_reps_record() -> None:
    owner_id = uuid4()
    rows = [SimpleNamespace(id=uuid4(), owner_user_id=owner_id)]

    detail = prospects.duplicate_detail(rows, _user(Role.FIELD_REP))

    assert detail["candidates"] == []
    assert detail["assignment_required"] is True


def test_duplicate_response_can_name_an_explicitly_selected_contact() -> None:
    contact_id = uuid4()
    row = SimpleNamespace(id=uuid4(), owner_user_id=uuid4(), primary_contact_id=contact_id)

    detail = prospects.duplicate_detail([row], _user(Role.FIELD_REP), known_contact_id=contact_id)

    assert detail["candidates"] == [
        {"prospect_id": str(row.id), "owner_user_id": str(row.owner_user_id)}
    ]
    assert detail["assignment_required"] is False


def test_pipeline_rollout_flag_is_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        prospects,
        "get_settings",
        lambda: SimpleNamespace(dealer_prospect_pipeline_enabled=False),
    )
    with pytest.raises(HTTPException) as error:
        prospects.require_pipeline_enabled()
    assert error.value.status_code == 404

    monkeypatch.setattr(
        prospects,
        "get_settings",
        lambda: SimpleNamespace(dealer_prospect_pipeline_enabled=True),
    )
    prospects.require_pipeline_enabled()


def test_pipeline_access_roles_are_explicit() -> None:
    prospects.require_prospect_actor(_user(Role.SUPER_ADMIN))
    prospects.require_prospect_actor(_user(Role.LOAN_EXEC))
    prospects.require_prospect_actor(_user(Role.FIELD_REP))
    with pytest.raises(HTTPException) as error:
        prospects.require_prospect_actor(_user(Role.CLIENT))
    assert error.value.status_code == 403


def test_pipeline_access_requires_per_user_assignment() -> None:
    user = _user(Role.FIELD_REP)
    user.dealer_prospect_pipeline_enabled = False

    with pytest.raises(HTTPException) as error:
        prospects.require_prospect_actor(user)

    assert error.value.status_code == 404


def test_historical_marketing_access_survives_sending_package_disable() -> None:
    rep = _user(Role.FIELD_REP)
    rep.dealer_prospect_pipeline_enabled = False

    prospects.require_prospect_history_reader(rep)
    prospects.require_prospect_history_reader(_user(Role.SUPER_ADMIN))
    with pytest.raises(HTTPException) as error:
        prospects.require_prospect_history_reader(_user(Role.CLIENT))
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_historical_prospect_loader_includes_archived_but_keeps_current_scope() -> None:
    rep = _user(Role.FIELD_REP)
    rep.dealer_prospect_pipeline_enabled = False
    archived = SimpleNamespace(id=uuid4(), archived_at=datetime.now(UTC))

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return archived

    db = SimpleNamespace(execute=AsyncMock(return_value=Result()))
    result = await prospects.load_visible_prospect_history(db, rep, archived.id)

    assert result is archived
    statement = str(db.execute.await_args.args[0])
    assert "dealer_prospects.archived_at IS NULL" not in statement
    assert "dealer_prospects.owner_user_id" in statement
    assert "dos_rep_contact_assignments" in statement


@pytest.mark.asyncio
async def test_previous_owner_cannot_read_marketing_history_after_reassignment() -> None:
    previous_owner = _user(Role.FIELD_REP)
    previous_owner.dealer_prospect_pipeline_enabled = False

    class MissingResult:
        @staticmethod
        def scalar_one_or_none():
            return None

    db = SimpleNamespace(execute=AsyncMock(return_value=MissingResult()))
    with pytest.raises(HTTPException) as error:
        await prospects.load_visible_prospect_history(db, previous_owner, uuid4())

    assert error.value.status_code == 404
    statement = str(db.execute.await_args.args[0])
    assert "dealer_prospects.owner_user_id" in statement
    assert "dos_rep_contact_assignments.user_id" in statement


def test_effective_pipeline_access_combines_master_user_and_eligibility(monkeypatch) -> None:
    user = _user(Role.FIELD_REP)
    monkeypatch.setattr(
        prospects,
        "get_settings",
        lambda: SimpleNamespace(dealer_prospect_pipeline_enabled=True),
    )
    assert prospects.pipeline_effective_enabled(user) is True

    user.dealer_prospect_pipeline_enabled = False
    assert prospects.pipeline_effective_enabled(user) is False
    user.dealer_prospect_pipeline_enabled = True
    user.account_status = "suspended"
    assert prospects.pipeline_effective_enabled(user) is False


def test_access_admin_endpoint_bypasses_master_switch(monkeypatch) -> None:
    def disabled() -> None:
        raise HTTPException(status_code=404, detail="disabled")

    monkeypatch.setattr(prospects, "require_pipeline_enabled", disabled)
    admin_request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/dealer-os/admin/prospect-access",
            "headers": [],
        }
    )
    prospect_router._require_pipeline_master(admin_request)

    agent_request = Request(
        {"type": "http", "method": "GET", "path": "/api/v1/dealer-os/prospects", "headers": []}
    )
    with pytest.raises(HTTPException):
        prospect_router._require_pipeline_master(agent_request)


@pytest.mark.asyncio
async def test_admin_can_enable_one_eligible_pipeline_user(monkeypatch) -> None:
    actor = _user(Role.SUPER_ADMIN)
    target = _user(Role.FIELD_REP)
    target.dealer_prospect_pipeline_enabled = False
    target.updated_at = None
    result = SimpleNamespace(scalar_one_or_none=lambda: target)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        add=Mock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    monkeypatch.setattr(
        prospects,
        "get_settings",
        lambda: SimpleNamespace(dealer_prospect_pipeline_enabled=True),
    )
    request = Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": f"/api/v1/dealer-os/admin/prospect-access/{target.id}",
            "headers": [],
        }
    )

    response = await prospect_router.update_prospect_user_access(
        target.id,
        prospect_router.ProspectUserAccessPatch(enabled=True, reason="Pilot cohort"),
        request,
        actor,
        db,
    )

    assert target.dealer_prospect_pipeline_enabled is True
    assert response.enabled is True
    assert response.effective_enabled is True
    event = db.add.call_args.args[0]
    assert event.action == "dealer_prospect_pipeline.enabled"
    assert event.reason == "Pilot cohort"


def test_unique_identity_indexes_are_scoped_to_dealer() -> None:
    indexes = {index.name: index for index in DealerProspect.__table__.indexes}
    assert [column.name for column in indexes["uq_dealer_prospect_email_active"].columns] == [
        "dealer_name_normalized",
        "email_normalized",
    ]
    assert [column.name for column in indexes["uq_dealer_prospect_phone_active"].columns] == [
        "dealer_name_normalized",
        "phone_normalized",
    ]


def test_conversion_schema_preserves_exactly_one_immutable_destination() -> None:
    table = DealerProspect.__table__
    assert "conversion_target" in table.c
    assert "converted_application_id" in table.c
    assert next(iter(table.c.converted_application_id.foreign_keys)).ondelete == "RESTRICT"
    assert next(iter(table.c.converted_intake_id.foreign_keys)).ondelete == "RESTRICT"
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if hasattr(constraint, "sqltext")
    }
    assert "portfolio_application" in constraints["ck_dealer_prospect_conversion_target"]
    assert "converted_application_id IS NOT NULL" in constraints[
        "ck_dealer_prospect_conversion_destination"
    ]

    migration = Path("alembic/versions/0222_marketing_conversion.py").read_text()
    assert 'down_revision = "0221_dealer_prospect_user_access"' in migration
    assert "SET conversion_target = 'dealer_ai_intake'" in migration
    assert migration.count('ondelete="RESTRICT"') == 2
    assert '"compose_mode"' in migration
    assert "compose_mode IN ('ai','manual')" in migration


def test_router_exposes_board_and_configuration_contracts() -> None:
    paths = {
        (route.path, method) for route in prospect_router.router.routes for method in route.methods
    }
    assert ("/dealer-os/prospects", "GET") in paths
    assert ("/dealer-os/prospects", "POST") in paths
    assert ("/dealer-os/prospect-owners", "GET") in paths
    assert ("/dealer-os/admin/prospect-access", "GET") in paths
    assert ("/dealer-os/admin/prospect-access/{user_id}", "PATCH") in paths
    assert ("/dealer-os/prospects/{prospect_id}/move-stage", "POST") in paths
    assert ("/dealer-os/prospects/{prospect_id}/outcomes", "POST") in paths
    assert (
        "/dealer-os/prospects/{prospect_id}/activities/{activity_id}/undo",
        "POST",
    ) in paths
    assert ("/dealer-os/prospects/{prospect_id}/convert-to-ai-intake", "POST") in paths
    assert ("/dealer-os/prospects/{prospect_id}/conversion-candidates", "GET") in paths
    assert ("/dealer-os/prospects/{prospect_id}/convert", "POST") in paths
    assert ("/dealer-os/prospect-stages/reorder", "POST") in paths
    assert ("/dealer-os/prospect-outcomes/reorder", "POST") in paths
    move_route = next(
        route
        for route in prospect_router.router.routes
        if route.path == "/dealer-os/prospects/{prospect_id}/move-stage"
    )
    assert move_route.response_model is ProspectMoveResult


def _prospect_for_move(*, converted_intake_id=None):
    return SimpleNamespace(
        id=uuid4(),
        stage_definition_id=uuid4(),
        primary_contact_id=uuid4(),
        appointment_id=None,
        converted_intake_id=converted_intake_id,
        next_follow_up_at=None,
        do_not_contact=False,
        do_not_contact_reason=None,
        last_activity_at=None,
        version=1,
    )


def _move_db(current_key: str, destination_key: str):
    current = SimpleNamespace(id=uuid4(), key=current_key)
    destination = SimpleNamespace(id=uuid4(), key=destination_key)
    result = SimpleNamespace(scalar_one_or_none=lambda: destination)

    async def get(model, _row_id):
        if model.__name__ == "DealerProspectStageDefinition":
            return current
        return None

    return SimpleNamespace(get=get, execute=AsyncMock(return_value=result))


@pytest.mark.asyncio
async def test_not_interested_move_requires_explicit_do_not_contact_confirmation() -> None:
    with pytest.raises(HTTPException) as error:
        await prospects.move_stage(
            _move_db("new", "not_interested"),
            _user(Role.FIELD_REP),
            _prospect_for_move(),
            stage_key="not_interested",
            expected_version=1,
            note=None,
            next_follow_up_at=None,
            action="none",
            appointment_id=None,
            confirm_do_not_contact=False,
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "do_not_contact_confirmation_required"


@pytest.mark.asyncio
async def test_not_interested_move_cannot_create_an_email_draft() -> None:
    with pytest.raises(HTTPException) as error:
        await prospects.move_stage(
            _move_db("new", "not_interested"),
            _user(Role.FIELD_REP),
            _prospect_for_move(),
            stage_key="not_interested",
            expected_version=1,
            note=None,
            next_follow_up_at=None,
            action="draft_email",
            appointment_id=None,
            confirm_do_not_contact=True,
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "do_not_contact_email_forbidden"


@pytest.mark.asyncio
async def test_booked_move_requires_an_appointment() -> None:
    with pytest.raises(HTTPException) as error:
        await prospects.move_stage(
            _move_db("new", "booked"),
            _user(Role.FIELD_REP),
            _prospect_for_move(),
            stage_key="booked",
            expected_version=1,
            note=None,
            next_follow_up_at=None,
            action="none",
            appointment_id=None,
            confirm_do_not_contact=False,
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "appointment_required"


@pytest.mark.asyncio
async def test_converted_move_requires_the_conversion_endpoint() -> None:
    with pytest.raises(HTTPException) as error:
        await prospects.move_stage(
            _move_db("new", "converted"),
            _user(Role.FIELD_REP),
            _prospect_for_move(),
            stage_key="converted",
            expected_version=1,
            note=None,
            next_follow_up_at=None,
            action="none",
            appointment_id=None,
            confirm_do_not_contact=False,
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "conversion_required"


# Conversion identity privacy and race-safety regressions.  These live at the
# end of the module to keep them separate from the conversion lifecycle tests
# above, which are also maintained by the broader Marketing regression suite.


def _unconverted_identity_prospect():
    return SimpleNamespace(
        id=uuid4(),
        version=1,
        owner_user_id=uuid4(),
        converted_application_id=None,
        converted_intake_id=None,
        dealer_name_normalized="grace auto sales",
        email_normalized="rocio@example.com",
        phone_normalized="+19735550148",
    )


@pytest.mark.parametrize("target", ["portfolio_application", "dealer_ai_intake"])
@pytest.mark.asyncio
async def test_detect_conversion_reports_hidden_matches_without_identity_details(
    monkeypatch, target: str
) -> None:
    prospect = _unconverted_identity_prospect()
    user = _user(Role.FIELD_REP)
    lock = AsyncMock()
    candidate_scan = AsyncMock(return_value=[])
    portfolio_hidden = AsyncMock(return_value=target == "portfolio_application")
    intake_hidden = AsyncMock(return_value=target == "dealer_ai_intake")
    create_portfolio = AsyncMock()
    create_intake = AsyncMock()
    monkeypatch.setattr(
        prospect_router.service, "load_visible_prospect", AsyncMock(return_value=prospect)
    )
    monkeypatch.setattr(
        prospect_router.conversion_service, "acquire_conversion_identity_lock", lock
    )
    monkeypatch.setattr(prospect_router, "_conversion_candidates", candidate_scan)
    monkeypatch.setattr(
        prospect_router.conversion_service,
        "portfolio_restricted_match_exists",
        portfolio_hidden,
    )
    monkeypatch.setattr(
        prospect_router.service,
        "intake_restricted_match_exists",
        intake_hidden,
    )
    monkeypatch.setattr(
        prospect_router.conversion_service,
        "create_portfolio_application",
        create_portfolio,
    )
    monkeypatch.setattr(
        prospect_router.service, "create_intake_from_prospect", create_intake
    )

    payload = ProspectGeneralConversionRequest(
        target=target,
        action="detect",
        expected_version=1,
    )
    request = Request(
        {"type": "http", "method": "POST", "path": "/prospects/x/convert", "headers": []}
    )
    with pytest.raises(HTTPException) as error:
        await prospect_router.convert_prospect(
            prospect.id, payload, request, user, SimpleNamespace()
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "prospect_conversion_restricted_match"
    assert set(error.value.detail) == {"code", "message"}
    serialized = str(error.value.detail).lower()
    assert "rocio" not in serialized
    assert "grace auto" not in serialized
    assert str(prospect.owner_user_id) not in serialized
    lock.assert_awaited_once()
    candidate_scan.assert_awaited_once()
    create_portfolio.assert_not_awaited()
    create_intake.assert_not_awaited()


@pytest.mark.parametrize("target", ["portfolio_application", "dealer_ai_intake"])
@pytest.mark.asyncio
async def test_visible_conversion_choice_takes_priority_over_hidden_match(
    monkeypatch, target: str
) -> None:
    prospect = _unconverted_identity_prospect()
    candidate_id = uuid4()
    candidate = SimpleNamespace(id=candidate_id)
    candidate_read = prospect_router.ProspectConversionCandidate(
        id=candidate_id,
        target=target,
        status="active",
        archived=False,
        display_name="Visible Grace Auto Sales file",
        email="rocio@example.com",
        phone="+19735550148",
        created_at=datetime.now(UTC),
        match_reasons=["email"],
        route=f"/{target}/{candidate_id}",
    )
    monkeypatch.setattr(
        prospect_router.service, "load_visible_prospect", AsyncMock(return_value=prospect)
    )
    monkeypatch.setattr(
        prospect_router.conversion_service,
        "acquire_conversion_identity_lock",
        AsyncMock(),
    )
    monkeypatch.setattr(
        prospect_router, "_conversion_candidates", AsyncMock(return_value=[candidate])
    )
    monkeypatch.setattr(
        prospect_router,
        "_conversion_candidate_read",
        lambda *_args, **_kwargs: candidate_read,
    )
    restricted = AsyncMock(return_value=True)
    monkeypatch.setattr(
        prospect_router, "_restricted_conversion_match_exists", restricted
    )

    with pytest.raises(HTTPException) as error:
        await prospect_router.convert_prospect(
            prospect.id,
            ProspectGeneralConversionRequest(
                target=target, action="detect", expected_version=1
            ),
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/prospects/x/convert",
                    "headers": [],
                }
            ),
            _user(Role.FIELD_REP),
            SimpleNamespace(),
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "prospect_conversion_choice_required"
    assert error.value.detail["candidates"][0]["id"] == str(candidate_id)
    restricted.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_detect_conversion_uses_hidden_match_privacy_guard(
    monkeypatch,
) -> None:
    prospect = _unconverted_identity_prospect()
    user = _user(Role.FIELD_REP)
    monkeypatch.setattr(
        prospect_router.service, "load_visible_prospect", AsyncMock(return_value=prospect)
    )
    lock = AsyncMock()
    monkeypatch.setattr(
        prospect_router.conversion_service, "acquire_conversion_identity_lock", lock
    )
    monkeypatch.setattr(
        prospect_router.service, "intake_candidates", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        prospect_router.service,
        "intake_restricted_match_exists",
        AsyncMock(return_value=True),
    )
    create_intake = AsyncMock()
    monkeypatch.setattr(
        prospect_router.service, "create_intake_from_prospect", create_intake
    )

    with pytest.raises(HTTPException) as error:
        await prospect_router.convert_prospect_to_ai_intake(
            prospect.id,
            ProspectConversionRequest(action="detect", expected_version=1),
            Request({"type": "http", "method": "POST", "path": "/convert", "headers": []}),
            user,
            SimpleNamespace(),
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "prospect_conversion_restricted_match"
    assert set(error.value.detail) == {"code", "message"}
    lock.assert_awaited_once()
    create_intake.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_visible_choice_takes_priority_over_hidden_match(
    monkeypatch,
) -> None:
    prospect = _unconverted_identity_prospect()
    candidate = SimpleNamespace(
        id=uuid4(),
        full_name="Rocio Martinez",
        business_name="Grace Auto Sales",
        status="collecting",
        outcome_status="submitted",
        created_at=datetime.now(UTC),
        email="rocio@example.com",
        phone="+19735550148",
    )
    monkeypatch.setattr(
        prospect_router.service, "load_visible_prospect", AsyncMock(return_value=prospect)
    )
    monkeypatch.setattr(
        prospect_router.conversion_service,
        "acquire_conversion_identity_lock",
        AsyncMock(),
    )
    monkeypatch.setattr(
        prospect_router.service,
        "intake_candidates",
        AsyncMock(return_value=[candidate]),
    )
    restricted = AsyncMock(return_value=True)
    monkeypatch.setattr(
        prospect_router.service, "intake_restricted_match_exists", restricted
    )

    with pytest.raises(HTTPException) as error:
        await prospect_router.convert_prospect_to_ai_intake(
            prospect.id,
            ProspectConversionRequest(action="detect", expected_version=1),
            Request({"type": "http", "method": "POST", "path": "/convert", "headers": []}),
            _user(Role.FIELD_REP),
            SimpleNamespace(),
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "prospect_conversion_choice_required"
    assert error.value.detail["candidates"][0]["id"] == str(candidate.id)
    restricted.assert_not_awaited()


@pytest.mark.parametrize("target", ["portfolio_application", "dealer_ai_intake"])
@pytest.mark.asyncio
async def test_only_explicit_create_may_pass_a_restricted_identity_match(
    monkeypatch, target: str
) -> None:
    prospect = _unconverted_identity_prospect()
    user = _user(Role.FIELD_REP)
    monkeypatch.setattr(
        prospect_router.service, "load_visible_prospect", AsyncMock(return_value=prospect)
    )
    monkeypatch.setattr(
        prospect_router.conversion_service,
        "acquire_conversion_identity_lock",
        AsyncMock(),
    )
    monkeypatch.setattr(
        prospect_router, "_conversion_candidates", AsyncMock(return_value=[])
    )
    restricted = AsyncMock(return_value=True)
    monkeypatch.setattr(
        prospect_router, "_restricted_conversion_match_exists", restricted
    )
    stop_after_explicit_choice = RuntimeError("explicit create reached")
    create_portfolio = AsyncMock(side_effect=stop_after_explicit_choice)
    create_intake = AsyncMock(side_effect=stop_after_explicit_choice)
    monkeypatch.setattr(
        prospect_router.conversion_service,
        "create_portfolio_application",
        create_portfolio,
    )
    monkeypatch.setattr(
        prospect_router.service, "create_intake_from_prospect", create_intake
    )
    portfolio_payload = (
        ProspectPortfolioApplicationCreate(
            entity_type="llc",
            requested_amount=250_000,
            funding_purpose="working_capital",
            use_of_proceeds_note="Acquire additional dealer inventory.",
            secure_room_pin="482915",
        )
        if target == "portfolio_application"
        else None
    )

    with pytest.raises(RuntimeError, match="explicit create reached"):
        await prospect_router.convert_prospect(
            prospect.id,
            ProspectGeneralConversionRequest(
                target=target,
                action="create",
                expected_version=1,
                portfolio_application=portfolio_payload,
            ),
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/prospects/x/convert",
                    "headers": [],
                }
            ),
            user,
            SimpleNamespace(),
        )

    restricted.assert_not_awaited()
    if target == "portfolio_application":
        create_portfolio.assert_awaited_once()
        create_intake.assert_not_awaited()
    else:
        create_intake.assert_awaited_once()
        create_portfolio.assert_not_awaited()


@pytest.mark.asyncio
async def test_locked_rescan_detects_candidate_created_after_prior_lookup(
    monkeypatch,
) -> None:
    prospect = _unconverted_identity_prospect()
    user = _user(Role.LOAN_EXEC)
    events: list[str] = []
    candidate = prospect_router.DealerBusiness(
        id=uuid4(),
        name="Grace Auto Sales",
        email="rocio@example.com",
        phone="+19735550148",
        status="active",
        archived_at=None,
        is_training=False,
        created_at=datetime.now(UTC),
    )

    async def acquire_lock(_db, _prospect) -> None:
        events.append("lock")

    async def rescan(_db, _prospect, _user, _target):
        events.append("rescan")
        return [candidate]

    monkeypatch.setattr(
        prospect_router.service, "load_visible_prospect", AsyncMock(return_value=prospect)
    )
    monkeypatch.setattr(
        prospect_router.conversion_service,
        "acquire_conversion_identity_lock",
        acquire_lock,
    )
    monkeypatch.setattr(prospect_router, "_conversion_candidates", rescan)
    create_portfolio = AsyncMock()
    monkeypatch.setattr(
        prospect_router.conversion_service,
        "create_portfolio_application",
        create_portfolio,
    )

    with pytest.raises(HTTPException) as error:
        await prospect_router.convert_prospect(
            prospect.id,
            ProspectGeneralConversionRequest(
                target="portfolio_application", action="detect", expected_version=1
            ),
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/prospects/x/convert",
                    "headers": [],
                }
            ),
            user,
            SimpleNamespace(),
        )

    assert events == ["lock", "rescan"]
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "prospect_conversion_choice_required"
    assert error.value.detail["candidates"][0]["id"] == str(candidate.id)
    create_portfolio.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversion_identity_lock_is_stable_and_transaction_scoped() -> None:
    prospect = _unconverted_identity_prospect()
    same_identity = _unconverted_identity_prospect()
    assert prospect_conversion.conversion_identity_lock_keys(prospect) == (
        prospect_conversion.conversion_identity_lock_keys(same_identity)
    )
    different_identity = _unconverted_identity_prospect()
    different_identity.dealer_name_normalized = "another dealer"
    different_identity.phone_normalized = "+19735550149"
    shared_email_locks = set(
        prospect_conversion.conversion_identity_lock_keys(prospect)
    ) & set(prospect_conversion.conversion_identity_lock_keys(different_identity))
    assert len(shared_email_locks) == 1
    assert prospect_conversion.conversion_identity_lock_keys(prospect) == tuple(
        sorted(prospect_conversion.conversion_identity_lock_keys(prospect))
    )

    db = SimpleNamespace(execute=AsyncMock())
    await prospect_conversion.acquire_conversion_identity_lock(db, prospect)

    statements = [call.args[0] for call in db.execute.await_args_list]
    assert len(statements) == 3
    assert all("pg_advisory_xact_lock" in str(statement) for statement in statements)
    acquired_keys = [next(iter(statement.compile().params.values())) for statement in statements]
    assert acquired_keys == list(prospect_conversion.conversion_identity_lock_keys(prospect))


@pytest.mark.parametrize(
    ("action", "archived", "expected_code"),
    [
        ("link", True, "prospect_conversion_reactivation_required"),
        ("reactivate", False, "prospect_conversion_candidate_active"),
    ],
)
@pytest.mark.asyncio
async def test_legacy_conversion_enforces_candidate_archive_action_parity(
    monkeypatch, action: str, archived: bool, expected_code: str
) -> None:
    prospect = _unconverted_identity_prospect()
    intake = SimpleNamespace(
        id=uuid4(),
        status="archived" if archived else "collecting",
        delete_requested_at=datetime.now(UTC) if archived else None,
    )
    monkeypatch.setattr(
        prospect_router.service, "load_visible_prospect", AsyncMock(return_value=prospect)
    )
    monkeypatch.setattr(
        prospect_router.conversion_service,
        "acquire_conversion_identity_lock",
        AsyncMock(),
    )
    monkeypatch.setattr(
        prospect_router.service,
        "intake_candidates",
        AsyncMock(return_value=[intake]),
    )
    complete = AsyncMock()
    monkeypatch.setattr(prospect_router.service, "complete_conversion", complete)

    with pytest.raises(HTTPException) as error:
        await prospect_router.convert_prospect_to_ai_intake(
            prospect.id,
            ProspectConversionRequest(
                action=action,
                intake_id=intake.id,
                expected_version=1,
            ),
            Request({"type": "http", "method": "POST", "path": "/convert", "headers": []}),
            _user(Role.FIELD_REP),
            SimpleNamespace(),
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == expected_code
    complete.assert_not_awaited()
