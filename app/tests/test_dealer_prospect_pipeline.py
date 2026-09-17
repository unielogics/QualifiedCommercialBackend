from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.dealer_os import prospect_router
from app.dealer_os.prospect_schemas import (
    ProspectConversionRequest,
    ProspectCreate,
    ProspectDefinitionReorder,
    ProspectMoveResult,
    ProspectMoveStage,
)
from app.dealer_os.services import prospects
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
