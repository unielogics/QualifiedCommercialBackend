from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.dealer_os import prospect_router
from app.dealer_os import router as dealer_router
from app.dealer_os.models import DealerRepAppointment
from app.dealer_os.prospect_schemas import (
    ProspectAppointmentCreate,
    ProspectAppointmentResult,
)
from app.dealer_os.schemas import RepAppointmentCreate, RepAppointmentPatch, SessionCreate
from app.dealer_os.services import booking_appointments
from app.dealer_os.services import prospects as prospect_service
from app.enums import Role
from app.models.booking_notification import (
    BookingDeliveryEffect,
    BookingDeliveryOperation,
    BookingNotification,
)
from app.models.booking_settings import BookingSlugAlias
from app.routers import public as public_router
from app.services import booking_notify
from app.services.booking_reminders import _initial_delivery_complete
from app.services.google import calendar_sync


def test_prospect_booking_contract_uses_server_owned_identity() -> None:
    payload = ProspectAppointmentCreate(
        expected_version=3,
        idempotency_key="booking-request-123",
        starts_at=datetime(2026, 9, 25, 15, 0, tzinfo=UTC),
        meeting_mode="video",
    )

    assert payload.duration_min is None
    assert payload.transactional_sms_consent is False
    assert payload.trigger_outcome_key is None
    assert "invitee_email" not in ProspectAppointmentCreate.model_fields
    assert "contact_id" not in ProspectAppointmentCreate.model_fields
    with pytest.raises(ValidationError):
        ProspectAppointmentCreate(
            expected_version=3,
            idempotency_key="short",
            starts_at=datetime(2026, 9, 25, 15, 0, tzinfo=UTC),
        )


def test_prospect_booking_accepts_configured_outcome_keys_but_only_booking_actions() -> None:
    payload = ProspectAppointmentCreate(
        expected_version=3,
        idempotency_key="booking-request-123",
        starts_at=datetime(2026, 9, 25, 15, 0, tzinfo=UTC),
        trigger_outcome_key="  Custom_Booking_Outcome  ",
    )
    assert payload.trigger_outcome_key == "custom_booking_outcome"
    assert prospect_service.booking_outcome_config(
        {"workflow_action": "book_appointment"}
    )["workflow_action"] == "book_appointment"
    assert prospect_service.booking_outcome_config(
        {"target_stage_key": "booked", "requires_appointment": True}
    )["requires_appointment"] is True
    with pytest.raises(Exception) as exc:
        prospect_service.booking_outcome_config(
            {
                "target_stage_key": "not_interested",
                "set_do_not_contact": True,
                "clear_follow_up": True,
            }
        )
    assert getattr(exc.value, "detail", {}).get("code") == "outcome_not_booking_capable"
    endpoint = inspect.getsource(prospect_router.create_prospect_appointment)
    assert "service.booking_outcome_config" in endpoint
    assert endpoint.index("service.booking_outcome_config") < endpoint.index(
        "appointment = DealerRepAppointment("
    )


def test_outcome_read_exposes_delay_based_follow_up_requirement() -> None:
    row = SimpleNamespace(
        id=uuid4(),
        key="not_connected",
        label="Not connected",
        sort_order=1,
        is_active=True,
        is_system=True,
        action_config={"follow_up_delay_hours": 24},
    )

    assert prospect_router._outcome_read(row).requires_follow_up is True


def test_join_url_is_provider_owned_on_every_booking_write_contract() -> None:
    assert "join_url" not in RepAppointmentCreate.model_fields
    assert "join_url" not in RepAppointmentPatch.model_fields
    assert "join_url" not in SessionCreate.model_fields
    common = {
        "starts_at": datetime(2026, 9, 25, 15, 0, tzinfo=UTC),
        "invitee_name": "Alex Morgan",
        "invitee_email": "alex@example.com",
    }
    with pytest.raises(ValidationError):
        RepAppointmentCreate(**common, join_url="https://evil.example/meet")
    with pytest.raises(ValidationError):
        RepAppointmentPatch(join_url="https://evil.example/meet")
    with pytest.raises(ValidationError):
        SessionCreate(
            title="Dealer call",
            starts_at=common["starts_at"],
            join_url="https://evil.example/meet",
        )
    for endpoint in (
        dealer_router.create_standalone_rep_appointment,
        dealer_router.create_rep_appointment,
        dealer_router.patch_rep_appointment,
        dealer_router.create_session,
    ):
        assert "payload.join_url" not in inspect.getsource(endpoint)


def test_naive_appointment_patch_time_uses_firm_timezone_and_rejects_dst_gaps() -> None:
    assert dealer_router._appointment_patch_start(
        datetime(2026, 9, 25, 10, 30),
        timezone_name="America/New_York",
    ) == datetime(2026, 9, 25, 14, 30, tzinfo=UTC)
    aware = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
    assert dealer_router._appointment_patch_start(
        aware,
        timezone_name="America/New_York",
    ) == aware
    with pytest.raises(Exception) as missing:
        dealer_router._appointment_patch_start(
            datetime(2026, 3, 8, 2, 30),
            timezone_name="America/New_York",
        )
    assert getattr(missing.value, "status_code", None) == 422
    with pytest.raises(Exception) as ambiguous:
        dealer_router._appointment_patch_start(
            datetime(2026, 11, 1, 1, 30),
            timezone_name="America/New_York",
        )
    assert getattr(ambiguous.value, "status_code", None) == 422
    endpoint = inspect.getsource(dealer_router.patch_rep_appointment)
    assert "_appointment_patch_start" in endpoint
    assert "proposed_timezone" in endpoint


def test_prospect_booking_routes_and_response_contract_are_registered() -> None:
    routes = {
        (route.path, method): route
        for route in prospect_router.router.routes
        for method in route.methods
    }
    post = routes[("/dealer-os/prospects/{prospect_id}/appointments", "POST")]
    assert post.response_model is ProspectAppointmentResult
    assert ("/dealer-os/prospects/{prospect_id}/appointments", "GET") in routes

    dealer_routes = {
        (route.path, method)
        for route in dealer_router.router.routes
        for method in route.methods
    }
    assert ("/dealer-os/booking/settings", "GET") in dealer_routes
    assert ("/dealer-os/booking/settings", "PUT") in dealer_routes


@pytest.mark.asyncio
async def test_public_availability_delegates_to_shared_orphan_safe_implementation(
    monkeypatch,
) -> None:
    shared_result = SimpleNamespace(slots=[])
    shared = AsyncMock(return_value=shared_result)
    monkeypatch.setattr(dealer_router, "_booking_slots", shared)
    db = AsyncMock()
    host = SimpleNamespace(id=uuid4())
    booking = SimpleNamespace(duration_min=20)
    page_start = datetime(2026, 9, 25, tzinfo=UTC).date()

    result = await public_router._available_booking_slots(
        db,
        host,
        booking,
        start_date=page_start,
        days=4,
    )

    assert result is shared_result
    shared.assert_awaited_once_with(
        db,
        host,
        booking,
        duration_min=20,
        start_date=page_start,
        days=4,
    )


def test_orphan_appointment_without_calendar_event_blocks_shared_availability() -> None:
    zone = dealer_router.rep_workflows.tz("America/New_York")
    starts_at = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
    orphan = SimpleNamespace(
        calendar_event_id=None,
        starts_at=starts_at,
        duration_min=30,
        status="scheduled",
        crm_status="scheduled",
        archived_at=None,
    )
    booking = SimpleNamespace(buffer_before_min=5, buffer_after_min=10)

    busy = dealer_router._local_calendar_busy_intervals(
        booking=booking,
        calendar_rows=[],
        appointment_rows=[orphan],
        zone=zone,
        fallback_duration=20,
        time_min=starts_at - dealer_router.timedelta(hours=1),
        time_max=starts_at + dealer_router.timedelta(hours=2),
    )

    assert busy == [
        (
            starts_at.astimezone(zone) - dealer_router.timedelta(minutes=5),
            starts_at.astimezone(zone) + dealer_router.timedelta(minutes=40),
        )
    ]


@pytest.mark.asyncio
async def test_public_booking_contact_resolution_uses_the_calendar_host_as_actor(
    monkeypatch,
) -> None:
    host = SimpleNamespace(id=uuid4())
    contact = SimpleNamespace(id=uuid4())
    ensure_contact = AsyncMock(return_value=contact)
    monkeypatch.setattr(dealer_router, "_ensure_rep_contact", ensure_contact)
    db = SimpleNamespace(add=Mock(), flush=AsyncMock())
    event = SimpleNamespace(
        id=uuid4(),
        title="Booked call: Alex",
        starts_at=datetime(2026, 9, 25, 15, 0, tzinfo=UTC),
        duration_min=20,
        external_ref_kind="public_booking",
        external_ref_id=str(uuid4()),
    )
    booking = SimpleNamespace(duration_min=20, timezone="America/New_York")

    appointment = await booking_appointments.create_booking_appointment(
        db,
        event=event,
        host=host,
        booking=booking,
        origin="public",
        invitee_name="Alex Morgan",
        invitee_email="ALEX@example.com",
        invitee_phone="201-555-0100",
        company="Example Motors",
        creation_idempotency_key="booking:create:test-public",
    )

    assert appointment.contact_id == contact.id
    ensure_contact.assert_awaited_once()
    assert ensure_contact.await_args.kwargs["actor_user"] is host
    assert ensure_contact.await_args.kwargs["owner_user_id"] == host.id
    assert ensure_contact.await_args.kwargs["email"] == "alex@example.com"
    assert appointment.creation_idempotency_key == "booking:create:test-public"


def test_appointment_model_carries_prospect_and_idempotency_links() -> None:
    table = DealerRepAppointment.__table__
    assert next(iter(table.c.prospect_id.foreign_keys)).target_fullname == "dealer_prospects.id"
    assert next(iter(table.c.return_stage_id.foreign_keys)).target_fullname == (
        "dealer_prospect_stage_definitions.id"
    )
    assert table.c.creation_idempotency_key.unique is True
    assert "ix_dos_rep_appointments_prospect" in {index.name for index in table.indexes}


def test_booking_reliability_migration_is_chained_and_backfills_minutes() -> None:
    migration = Path("alembic/versions/0223_booking_reliability.py").read_text()
    assert 'down_revision = "0222_marketing_conversion"' in migration
    assert "minimum_notice_days) * 1440" in migration
    assert '"prospect_id"' in migration
    assert '"creation_idempotency_key"' in migration
    assert '"return_stage_id"' in migration
    assert "booking_slug_aliases" in migration
    assert "delivery_next_attempt_at" in migration


def test_booking_delivery_and_slug_alias_models_are_persistent() -> None:
    notice = BookingNotification.__table__
    assert "delivery_attempt_count" in notice.c
    assert "delivery_next_attempt_at" in notice.c
    assert "delivery_completed_at" in notice.c
    assert BookingSlugAlias.__table__.c.slug.unique is True

    row = type(
        "Notice",
        (),
        {"confirmation_email_status": "sent", "confirmation_sms_status": "disabled"},
    )()
    event = type("Event", (), {"google_event_id": "google-1"})()
    appointment = type(
        "Appointment",
        (),
        {"meeting_mode": "video", "join_url": "https://meet.google.com/example"},
    )()
    assert _initial_delivery_complete(row, event, appointment) is True
    appointment.join_url = None
    assert (
        _initial_delivery_complete(
            row,
            event,
            appointment,
            google_meet_enabled=False,
        )
        is True
    )


def test_delivery_state_separates_queued_meet_and_action_required() -> None:
    queued = prospect_router._prospect_booking_delivery(
        {
            "meeting_mode": "video",
            "google_sync_status": "pending",
            "confirmation_email_status": "pending",
        }
    )
    assert queued.state == "queued"

    retrying = prospect_router._prospect_booking_delivery(
        {
            "meeting_mode": "video",
            "google_sync_status": "pending",
            "confirmation_email_status": "pending",
            "delivery_error": "google_calendar_pending",
        }
    )
    assert retrying.state == "queued"

    ready = prospect_router._prospect_booking_delivery(
        {
            "meeting_mode": "video",
            "google_sync_status": "connected",
            "confirmation_email_status": "sent",
            "join_url": "https://meet.google.com/example",
        }
    )
    assert ready.state == "meet_ready"

    failed = prospect_router._prospect_booking_delivery(
        {
            "meeting_mode": "video",
            "google_sync_status": "unavailable",
            "delivery_error": "calendar unavailable",
        }
    )
    assert failed.state == "action_required"

    exhausted = prospect_router._prospect_booking_delivery(
        {
            "meeting_mode": "video",
            "google_sync_status": "pending",
            "delivery_error": "google_calendar_action_required",
        }
    )
    assert exhausted.state == "action_required"


def test_prospect_booking_defers_provider_work_until_after_response() -> None:
    endpoint_source = inspect.getsource(prospect_router.create_prospect_appointment)

    assert "await _attempt_prospect_booking_delivery" not in endpoint_source
    assert "booking_operations.enqueue" in endpoint_source
    assert 'operation_type="create"' in endpoint_source
    assert "booking_operations.wake_operation" in endpoint_source
    assert "background_tasks.add_task" in endpoint_source
    after_commit = endpoint_source.split("await db.commit()", 1)[1]
    assert "push_to_google" not in after_commit
    assert "send_invitee_invite" not in after_commit
    assert "send_confirmation_sms" not in after_commit


def test_prospect_booking_does_not_hold_owner_lock_during_google_preflight() -> None:
    endpoint_source = inspect.getsource(prospect_router.create_prospect_appointment)

    full_check = endpoint_source.index("if not await _appointment_slot_is_available(")
    owner_lock = endpoint_source.index("await lock_calendar_owner")
    local_recheck = endpoint_source.index("check_google=False")
    assert full_check < owner_lock < local_recheck


def test_google_event_private_properties_include_qc_and_prospect_identifiers() -> None:
    event = SimpleNamespace(
        id=uuid4(),
        owner_user_id=uuid4(),
        external_ref_kind="dealer_rep_appointment",
        external_ref_id=str(uuid4()),
        starts_at=datetime(2026, 9, 25, 15, 0, tzinfo=UTC),
        duration_min=30,
        title="Prospect call",
        description="Private prospect booking",
        status="pending",
    )
    prospect_id = uuid4()
    contact_id = uuid4()

    body = calendar_sync._event_body(
        event,
        private_properties={
            "qc_prospect_id": prospect_id,
            "qc_contact_id": contact_id,
        },
    )

    private = body["extendedProperties"]["private"]
    assert private["qc_event_id"] == str(event.id)
    assert private["qc_owner_user_id"] == str(event.owner_user_id)
    assert private["qc_external_ref_kind"] == "dealer_rep_appointment"
    assert private["qc_prospect_id"] == str(prospect_id)
    assert private["qc_contact_id"] == str(contact_id)


@pytest.mark.asyncio
async def test_google_staff_attendees_are_deduplicated(monkeypatch) -> None:
    push = AsyncMock(return_value="https://meet.google.com/example")
    monkeypatch.setattr(calendar_sync, "push_event", push)
    event = SimpleNamespace(id=uuid4())

    result = await booking_notify.push_to_google(
        AsyncMock(),
        event,
        invitee_email="dealer@example.com",
        invitee_name="Dealer",
        rep_email="agent@example.com",
        rep_name="Booking Agent",
        staff_attendees=[
            {"email": "AGENT@example.com", "displayName": "Duplicate"},
            {"email": "assigned@example.com", "displayName": "Assigned Agent"},
        ],
        private_properties={"qc_prospect_id": uuid4()},
    )

    assert result == "https://meet.google.com/example"
    attendees = push.await_args.kwargs["attendees"]
    assert [row["email"] for row in attendees] == [
        "dealer@example.com",
        "agent@example.com",
        "assigned@example.com",
    ]
    assert "qc_prospect_id" in push.await_args.kwargs["private_properties"]


@pytest.mark.asyncio
async def test_prospect_google_context_tags_and_invites_assigned_and_booking_agents() -> None:
    prospect_id = uuid4()
    contact_id = uuid4()
    booked_by_id = uuid4()
    assigned_id = uuid4()
    shared_owner_id = uuid4()
    appointment = SimpleNamespace(
        id=uuid4(),
        prospect_id=prospect_id,
        contact_id=contact_id,
        booked_by_user_id=booked_by_id,
        owner_user_id=shared_owner_id,
    )
    prospect = SimpleNamespace(owner_user_id=assigned_id)
    members = {
        booked_by_id: SimpleNamespace(
            id=booked_by_id, name="Booking Agent", email="booking@example.com"
        ),
        assigned_id: SimpleNamespace(
            id=assigned_id, name="Assigned Agent", email="assigned@example.com"
        ),
    }

    async def get(_model, record_id):
        if record_id == prospect_id:
            return prospect
        return members.get(record_id)

    db = SimpleNamespace(get=AsyncMock(side_effect=get))
    attendees, private = await booking_notify.prospect_google_context(db, appointment)

    assert [row["email"] for row in attendees] == [
        "booking@example.com",
        "assigned@example.com",
    ]
    assert private == {
        "qc_appointment_id": appointment.id,
        "qc_prospect_id": prospect_id,
        "qc_contact_id": contact_id,
        "qc_booked_by_user_id": booked_by_id,
        "qc_owner_user_id": shared_owner_id,
        "qc_assigned_user_id": assigned_id,
    }


def test_initial_delivery_workers_claim_one_notification_through_completion() -> None:
    from app.services import booking_operations, scheduler

    worker_source = inspect.getsource(booking_operations.process_operation)
    scheduler_source = inspect.getsource(scheduler.job_booking_reminders)

    assert ".with_for_update(skip_locked=True)" in worker_source
    assert 'effect.status = "processing"' in worker_source
    assert "await db.commit()" in worker_source
    assert "dispatch_due_operations" in scheduler_source


def test_lifecycle_delivery_operation_has_per_effect_idempotency_boundaries() -> None:
    operation = BookingDeliveryOperation.__table__
    effect = BookingDeliveryEffect.__table__

    assert operation.c.idempotency_key.unique is True
    assert next(iter(operation.c.appointment_id.foreign_keys)).target_fullname == (
        "dos_rep_appointments.id"
    )
    assert "ix_booking_delivery_operations_due" in {
        index.name for index in operation.indexes
    }
    assert any(
        constraint.name == "uq_booking_delivery_effect_operation_key"
        for constraint in effect.constraints
    )
    operation_check = next(
        constraint
        for constraint in operation.constraints
        if constraint.name == "ck_booking_delivery_operation_type"
    )
    assert "create" in str(operation_check.sqltext)


def test_lifecycle_delivery_migration_is_chained_after_identity_migration() -> None:
    migration = Path(
        "alembic/versions/0225_booking_delivery_operations.py"
    ).read_text()
    assert 'down_revision = "0224_prospect_identity_followups"' in migration
    assert "booking_delivery_operations" in migration
    assert "booking_delivery_effects" in migration
    assert "uq_booking_delivery_effect_operation_key" in migration


def test_cancel_and_reschedule_return_before_provider_delivery() -> None:
    cancel_source = inspect.getsource(dealer_router._cancel_rep_appointment)
    patch_source = inspect.getsource(dealer_router.patch_rep_appointment)

    for source in (cancel_source, patch_source):
        assert "booking_operations.enqueue" in source
        assert "booking_operations.wake_operation" in source
        after_local_commit = source.split("await db.commit()", 1)[1]
        assert "send_invitee_invite" not in after_local_commit
        assert "send_rep_invite" not in after_local_commit
        assert "send_sms_guarded" not in after_local_commit
        assert "push_to_google" not in after_local_commit


def test_provider_effect_is_claimed_before_network_execution() -> None:
    from app.services import booking_operations

    source = inspect.getsource(booking_operations.process_operation)
    claim = source.index('effect.status = "processing"')
    durable_claim = source.index("await db.commit()", claim)
    provider_call = source.index("await _run_effect", durable_claim)
    assert claim < durable_claim < provider_call
    assert "ambiguous_provider_outcome" in source


def test_stale_google_effect_retries_but_ambiguous_sends_do_not() -> None:
    from app.services import booking_operations

    assert booking_operations.stale_effect_recovery("google") == "retry"
    for effect in ("client_email", "host_email", "client_sms", "pin_delivery"):
        assert booking_operations.stale_effect_recovery(effect) == "action_required"

    source = inspect.getsource(booking_operations.process_operation)
    recovery = source.split('if effect.status == "processing":', 1)[1]
    assert "retrying_stale_idempotent_google_effect" in recovery
    assert "ambiguous_provider_outcome" in recovery


def test_unavailable_or_failed_effect_cannot_complete_operation() -> None:
    from app.services import booking_operations

    assert {"failed", "unavailable", "action_required", "processing"}.issubset(
        booking_operations._ACTION_REQUIRED_EFFECT_STATUSES
    )
    source = inspect.getsource(booking_operations.process_operation)
    assert "_ACTION_REQUIRED_EFFECT_STATUSES" in source
    google_source = inspect.getsource(booking_operations._run_effect).split(
        'if key == "google":', 1
    )[1]
    assert '"pending"' in google_source
    assert "_MAX_OPERATION_ATTEMPTS" in google_source
    assert 'effect.effect_key == "google"' in source
    assert '"pending" if retry_google else "action_required"' in source


def test_pending_effect_is_not_stranded_by_failed_sibling() -> None:
    from app.services import booking_operations

    assert (
        booking_operations.aggregate_operation_status(
            has_pending=True, has_failed=True
        )
        == "pending"
    )
    assert (
        booking_operations.aggregate_operation_status(
            has_pending=False, has_failed=True
        )
        == "action_required"
    )
    assert (
        booking_operations.aggregate_operation_status(
            has_pending=False, has_failed=False
        )
        == "completed"
    )
    source = inspect.getsource(booking_operations.process_operation)
    assert "if pending" in source[source.index("operation.next_attempt_at"):]


def test_booking_create_replay_requires_same_identity_and_actor() -> None:
    from app.services import booking_operations

    owner_id = uuid4()
    actor_id = uuid4()
    dealer_id = uuid4()
    starts_at = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
    appointment = SimpleNamespace(
        owner_user_id=owner_id,
        dealer_id=dealer_id,
        starts_at=starts_at,
        duration_min=30,
        invitee_email="Dealer@Example.com",
        invitee_phone="(201) 555-0101",
        booked_by_user_id=actor_id,
    )
    expected = {
        "owner_user_id": owner_id,
        "dealer_id": dealer_id,
        "starts_at": starts_at,
        "duration_min": 30,
        "invitee_email": "dealer@example.com",
        "invitee_phone": "+12015550101",
        "actor_user_id": actor_id,
        "require_actor_match": True,
    }

    assert booking_operations.creation_replay_matches(appointment, **expected)
    assert not booking_operations.creation_replay_matches(
        appointment, **{**expected, "invitee_email": "other@example.com"}
    )
    assert not booking_operations.creation_replay_matches(
        appointment, **{**expected, "actor_user_id": uuid4()}
    )
    assert not booking_operations.creation_replay_matches(
        appointment, **{**expected, "starts_at": starts_at.replace(hour=16)}
    )

    appointment.precall_application_data = {
        "creation_request_fingerprint": "full-request-a"
    }
    assert booking_operations.creation_replay_matches(
        appointment,
        **expected,
        request_fingerprint="full-request-a",
    )
    assert not booking_operations.creation_replay_matches(
        appointment,
        **expected,
        request_fingerprint="full-request-b",
    )


def test_public_replay_validates_identity_before_returning_secure_result() -> None:
    create_source = inspect.getsource(public_router.public_booking_create)
    replay_source = inspect.getsource(public_router._public_booking_replay_result)

    assert create_source.count("_assert_public_booking_replay_matches(") == 2
    assert create_source.index("_assert_public_booking_replay_matches(") < (
        create_source.index("_public_booking_replay_result(")
    )
    assert "return PublicBookingCreateResult(" in replay_source
    assert "include_sensitive" in replay_source
    assert "room_url=room_url" in replay_source
    assert create_source.count("include_sensitive=False") == 2


@pytest.mark.asyncio
async def test_legacy_public_replay_never_returns_room_or_pin(monkeypatch) -> None:
    appointment = SimpleNamespace(id=uuid4(), calendar_event_id=uuid4())
    notice = SimpleNamespace(
        precall_dealer_id=uuid4(),
        precall_intake_id=None,
        precall_pin_delivered_via="email",
    )
    result_proxy = Mock()
    result_proxy.scalar_one_or_none.return_value = notice
    db = SimpleNamespace(execute=AsyncMock(return_value=result_proxy), get=AsyncMock())
    monkeypatch.setattr(
        public_router.booking_operations,
        "find_by_idempotency_key",
        AsyncMock(return_value=None),
    )

    result = await public_router._public_booking_replay_result(
        db,
        appointment,
        include_sensitive=False,
    )

    assert result.ok is True
    assert result.room_url is None
    assert result.pin_delivered_via is None
    db.get.assert_not_awaited()


def test_public_idempotent_replay_precedes_throttle_for_caller_tokens() -> None:
    source = inspect.getsource(public_router.public_booking_create)
    first_replay = source.index("if existing is not None:")
    deferred_throttle = source.index("if defer_throttle:", first_replay)
    first_return = source.index("_public_booking_replay_result(", first_replay)
    assert first_replay < first_return < deferred_throttle
    assert "defer_throttle = bool(payload.creation_idempotency_key)" in source


def test_authenticated_replay_keys_are_actor_scoped_and_validated() -> None:
    standalone = inspect.getsource(dealer_router.create_standalone_rep_appointment)
    dealer = inspect.getsource(dealer_router.create_rep_appointment)

    assert "f\"standalone:{user.id}" in standalone
    assert "creation_replay_matches" in standalone
    assert "require_actor_match=True" in standalone
    assert "f\"dealer:{dealer.id}:{user.id}" in dealer
    assert "creation_replay_matches" in dealer
    assert "dealer_id=dealer.id" in dealer
    for source in (standalone, dealer):
        assert "creation_request_fingerprint" in source
        assert "request_fingerprint=" in source


def test_outcome_application_keeps_nested_intake_work_in_one_transaction() -> None:
    helper = inspect.getsource(dealer_router._start_rep_appointment_application)
    endpoint = inspect.getsource(dealer_router.start_rep_appointment_application)
    outcome = inspect.getsource(dealer_router.apply_rep_appointment_outcome)
    assert "commit=False" in helper
    assert "if commit:" in helper
    assert "commit=True" in endpoint
    assert "_start_rep_appointment_application" in outcome
    assert "commit=False" in outcome
    assert outcome.index("commit=False") < outcome.index("await db.commit()")


def test_application_room_delivery_cannot_run_before_the_local_commit() -> None:
    helper = inspect.getsource(dealer_router._start_rep_appointment_application)

    guard = helper.index("if payload.notify_client:")
    intake_create = helper.index("_create_admin_ai_lead_core")
    assert guard < intake_create
    assert "committed secure-room workflow" in helper


def test_outcome_application_has_a_durable_strict_idempotency_ledger() -> None:
    source = inspect.getsource(dealer_router.apply_rep_appointment_outcome)

    assert 'f"booking:outcome:{appointment.id}:"' in source
    assert "request_fingerprint = booking_operations.creation_request_fingerprint" in source
    assert "BookingDeliveryOperation.idempotency_key == operation_key" in source
    assert '"kind": "appointment_outcome"' in source
    assert 'outcome_operation.status = "completed"' in source
    assert '"result": {' in source
    assert source.index("db.add(outcome_operation)") < source.index("await db.commit()")
    assert source.index('outcome_operation.status = "completed"') < source.index(
        "await db.commit()"
    )


def test_booking_operations_are_serialized_per_appointment() -> None:
    from app.services import booking_operations

    source = inspect.getsource(booking_operations.process_operation)
    serialize = source.index("oldest_active_id")
    provider = source.index("await _run_effect")
    assert serialize < provider
    assert "operation.appointment_id" in source[serialize:provider]
    assert 'operation.status = "pending"' in source[serialize:provider]
    assert "claimed_state_key" in source
    assert "delivered_by_initial_create_operation" in source


def test_cancel_supersedes_a_not_yet_started_create_operation() -> None:
    from app.services import booking_operations

    source = inspect.getsource(booking_operations.enqueue)
    cancel_branch = source.split('if operation_type != "create":', 1)[1].split(
        "notice = None", 1
    )[0]
    assert '{"create", "update", "reschedule", "cancel"}' in cancel_branch
    assert 'BookingDeliveryOperation.status == "pending"' in cancel_branch


def test_google_action_required_is_read_from_durable_effect_ledger() -> None:
    source = inspect.getsource(dealer_router._appointment_read_rows)
    delivery = inspect.getsource(prospect_router._prospect_booking_delivery)

    assert "BookingDeliveryEffect.effect_key == \"google\"" in source
    assert 'data["google_sync_status"] = "action_required"' in source
    assert 'data["google_sync_error"]' in source
    assert 'google == "action_required"' in delivery


def test_host_email_provider_result_is_not_reported_as_sent_on_failure() -> None:
    from app.services import booking_operations

    notify_source = inspect.getsource(booking_notify.notify_host)
    effect_source = inspect.getsource(booking_operations._run_effect)
    host_effect = effect_source.split('if key == "host_email":', 1)[1].split(
        'if key == "client_email":', 1
    )[0]

    assert "return result" in notify_source
    assert 'return "unavailable"' in host_effect
    assert '("failed", result.message_id, result.detail)' in host_effect


def test_manual_delivery_retry_is_locked_durable_and_immediately_queued() -> None:
    from app.services import booking_operations

    endpoint = inspect.getsource(dealer_router.retry_rep_appointment_delivery)
    assert "for_update=True" in endpoint
    assert "booking_operations.enqueue_manual_retry" in endpoint
    assert 'notice.confirmation_email_status = "pending"' in endpoint
    assert 'notice.confirmation_sms_status = "pending"' in endpoint
    assert endpoint.index("await db.commit()") < endpoint.index(
        "booking_operations.wake_operation"
    )
    for direct_provider in (
        "push_to_google",
        "send_invitee_invite",
        "send_confirmation_sms",
    ):
        assert direct_provider not in endpoint
    helper = inspect.getsource(booking_operations.enqueue_manual_retry)
    assert 'effect.status in {"pending", "processing", "sent"}' in helper
    assert 'effect.status = "pending"' in helper


def test_manual_retry_key_is_stable_and_effect_specific() -> None:
    from app.services import booking_operations

    appointment = SimpleNamespace(
        id=uuid4(),
        title="Dealer review",
        starts_at=datetime(2026, 9, 25, 15, 0, tzinfo=UTC),
        duration_min=30,
        timezone="America/New_York",
        invitee_name="Alex Dealer",
        invitee_email="alex@example.com",
        invitee_phone="+12015550101",
        company="Example Motors",
        meeting_mode="video",
        location=None,
        notes=None,
        status="pending",
    )
    event = SimpleNamespace(
        id=uuid4(),
        title=appointment.title,
        starts_at=appointment.starts_at,
        duration_min=30,
        who="Alex Dealer <alex@example.com>",
        description="Review",
        status="pending",
    )
    key = booking_operations.manual_retry_idempotency_key(
        appointment,
        event,
        effect_key="client_email",
    )
    assert key == booking_operations.manual_retry_idempotency_key(
        appointment,
        event,
        effect_key="client_email",
    )
    assert key != booking_operations.manual_retry_idempotency_key(
        appointment,
        event,
        effect_key="client_sms",
    )


def test_outcome_provider_effects_are_locked_and_queued_before_commit() -> None:
    source = inspect.getsource(dealer_router.apply_rep_appointment_outcome)
    assert "for_update=True" in source
    assert 'effect_key="rebooking_email"' in source
    assert 'effect_key="google"' in source
    assert "booking_operations.enqueue_manual_retry" in source
    assert "send_as_user" not in source
    assert "push_to_google" not in source
    assert source.index("booking_operations.enqueue_manual_retry") < source.index(
        "await db.commit()"
    )
    assert source.index("await db.commit()") < source.index(
        "booking_operations.wake_operation"
    )
    crm_source = inspect.getsource(dealer_router.patch_rep_appointment_crm)
    assert "for_update=True" in crm_source


def test_timeline_reads_terminal_google_effect_from_durable_ledger() -> None:
    source = inspect.getsource(prospect_service.prospect_timeline)
    assert "BookingDeliveryEffect.effect_key == \"google\"" in source
    assert 'google_status = "action_required"' in source
    assert "google_effect.error" in source


def test_duplicate_check_excludes_the_contact_being_added_to_marketing() -> None:
    source = inspect.getsource(prospect_router.check_prospect_duplicate)
    assert "exclude_contact_id=contact_id" in source


def test_booking_availability_exposes_shared_meet_capability() -> None:
    assert "google_meet_enabled" in dealer_router.BookingAvailabilityRead.model_fields
    source = inspect.getsource(dealer_router._booking_slots)
    assert source.count("google_meet_enabled=bool(booking.google_meet_enabled)") == 2


def test_public_booking_server_owns_video_or_phone_method() -> None:
    assert "google_meet_enabled" in public_router.PublicBookingProfile.model_fields
    assert "meeting_mode" in public_router.PublicBookingProfile.model_fields
    profile = inspect.getsource(public_router.public_booking_profile)
    create = inspect.getsource(public_router.public_booking_create)
    service_create = inspect.getsource(
        booking_appointments.create_booking_appointment
    )
    assert 'meeting_mode="video" if booking.google_meet_enabled else "phone"' in profile
    assert 'meeting_mode="video" if booking.google_meet_enabled else "phone"' in create
    assert "meeting_mode=meeting_mode" in service_create
    # Old clients may echo the locked default but cannot change it.
    assert "payload.vertical != selected_variant" in create


def test_legacy_booked_move_links_both_sides_and_is_not_undoable() -> None:
    source = inspect.getsource(prospect_service.move_stage)
    booked = source.split('if destination.key == "booked":', 1)[1].split(
        'if destination.key == "not_interested":', 1
    )[0]
    assert "appointment.prospect_id = prospect.id" in booked
    assert "appointment.return_stage_id = current_stage.id" in booked
    assert 'destination.key not in {"booked", "converted"}' in source


def test_dnc_and_terminal_states_block_booking_calls_and_followups() -> None:
    booking_source = inspect.getsource(prospect_router.create_prospect_appointment)
    assert "outreach_service.is_suppressed" in booking_source
    assert "prospect.do_not_contact" in booking_source
    assert '"code": "prospect_contact_blocked"' in booking_source
    assert booking_source.index("prospect_contact_blocked") < booking_source.index(
        "appointment = DealerRepAppointment("
    )

    call_source = inspect.getsource(prospect_router.record_prospect_call_attempt)
    assert "if prospect.do_not_contact:" in call_source
    assert '"code": "prospect_contact_blocked"' in call_source

    patch_source = inspect.getsource(prospect_router.patch_prospect)
    assert 'stage.key in {"booked", "converted", "not_interested"}' in patch_source
    assert '"code": "follow_up_not_allowed"' in patch_source


def test_prospect_booking_replay_fingerprint_covers_canonical_request() -> None:
    common = {
        "prospect_id": uuid4(),
        "contact_id": uuid4(),
        "owner_user_id": uuid4(),
        "actor_user_id": uuid4(),
        "invitee_email": "dealer@example.com",
        "invitee_phone": "+12015550101",
        "starts_at": datetime(2026, 9, 25, 15, 0, tzinfo=UTC),
        "duration_min": 30,
        "meeting_mode": "video",
        "location": None,
        "notes": "Discuss working capital",
        "transactional_sms_consent": True,
        "trigger_outcome_key": "booked",
    }
    first = prospect_router._prospect_booking_replay_fingerprint(**common)
    assert first == prospect_router._prospect_booking_replay_fingerprint(**common)
    for key, value in (
        ("actor_user_id", uuid4()),
        ("starts_at", common["starts_at"].replace(hour=16)),
        ("duration_min", 45),
        ("meeting_mode", "phone"),
        ("location", "Dealer showroom"),
        ("notes", "Different agenda"),
        ("transactional_sms_consent", False),
        ("trigger_outcome_key", "wants_to_book"),
    ):
        assert first != prospect_router._prospect_booking_replay_fingerprint(
            **{**common, key: value}
        )

    route_source = inspect.getsource(prospect_router.create_prospect_appointment)
    assert "stored_fingerprint != replay_fingerprint" in route_source
    assert '"code": "idempotency_key_reused"' in route_source


def test_prospect_booking_notifies_assigned_owner_through_durable_effect() -> None:
    source = inspect.getsource(prospect_router.create_prospect_appointment)
    assert "assigned_notifications = await notify_users" in source
    assert "recipient_ids={prospect.owner_user_id}" in source
    assert "actor_user_id=user.id" in source
    assert "defer_email=True" in source
    assert "notification_ids=[row.id for row in assigned_notifications]" in source


def test_booked_prospect_cannot_silently_create_second_active_appointment() -> None:
    source = inspect.getsource(prospect_router.create_prospect_appointment)

    replay = source.index("if existing is not None")
    active_guard = source.index("active_appointment =")
    appointment_create = source.index("appointment = DealerRepAppointment(")
    assert replay < active_guard < appointment_create
    assert '"code": "active_appointment_exists"' in source
    assert "Reschedule that appointment" in source
    active_lookup = source[active_guard:appointment_create]
    assert ".with_for_update()" not in active_lookup


def test_underwriting_room_propagates_database_failures() -> None:
    source = inspect.getsource(dealer_router._prepare_underwriting_review_room)
    assert "from sqlalchemy.exc import SQLAlchemyError" in source
    assert "except SQLAlchemyError:" in source
    assert source.index("except SQLAlchemyError:") < source.index(
        "except Exception:"
    )


def test_cancelled_prospect_uses_first_active_follow_up_stage_then_emailed() -> None:
    source = inspect.getsource(dealer_router._cancel_rep_appointment)
    assert '"follow_up_%"' in source
    assert "DealerProspectStageDefinition.sort_order.asc()" in source
    assert 'row.key.startswith("follow_up_")' in source
    assert 'row.key == "emailed"' in source


def test_cancelled_dnc_prospect_never_resurrects_a_follow_up() -> None:
    source = inspect.getsource(dealer_router._cancel_rep_appointment)

    assert "prospect_outreach_service.is_suppressed" in source
    assert "contact_blocked = prospect.do_not_contact or suppression is not None" in source
    assert 'DealerProspectStageDefinition.key == "not_interested"' in source
    blocked_branch = source.split("if contact_blocked:", 1)[1]
    assert "prospect.next_follow_up_at = (" in blocked_branch
    assert "None\n                    if contact_blocked" in blocked_branch


def test_patch_retry_reuses_provider_operation_for_same_final_state() -> None:
    from app.services import booking_operations

    appointment_id = uuid4()
    event_id = uuid4()
    appointment = SimpleNamespace(
        id=appointment_id,
        title="Dealer review",
        starts_at=datetime(2026, 9, 25, 15, 0, tzinfo=UTC),
        duration_min=30,
        timezone="America/New_York",
        invitee_name="Alex Dealer",
        invitee_email="alex@example.com",
        invitee_phone="+12015550101",
        company="Example Motors",
        join_url="https://meet.google.com/example",
        meeting_mode="video",
        location=None,
        notes="Discuss working capital",
        status="pending",
    )
    event = SimpleNamespace(
        id=event_id,
        title=appointment.title,
        starts_at=appointment.starts_at,
        duration_min=appointment.duration_min,
        who="Alex Dealer <alex@example.com>",
        description="Confirmed dealer review",
        status="pending",
    )

    first = booking_operations.lifecycle_idempotency_key(appointment, event)
    retry = booking_operations.lifecycle_idempotency_key(appointment, event)
    assert first == retry
    appointment.join_url = "https://meet.google.com/provider-generated"
    assert booking_operations.lifecycle_idempotency_key(appointment, event) == first
    appointment.starts_at = datetime(2026, 9, 25, 16, 0, tzinfo=UTC)
    event.starts_at = appointment.starts_at
    assert booking_operations.lifecycle_idempotency_key(appointment, event) != first

    endpoint = inspect.getsource(dealer_router.patch_rep_appointment)
    assert "lifecycle_idempotency_key" in endpoint
    assert "find_by_idempotency_key" in endpoint
    assert "idempotency_key=delivery_key" in endpoint


def test_mutation_routes_lock_appointment_without_changing_read_default() -> None:
    loader = inspect.signature(dealer_router._load_owned_appointment)
    assert loader.parameters["for_update"].default is False
    assert "query.with_for_update()" in inspect.getsource(
        dealer_router._load_owned_appointment
    )
    assert "for_update=True" in inspect.getsource(
        dealer_router.patch_rep_appointment
    )
    assert "for_update=True" in inspect.getsource(
        dealer_router.cancel_rep_appointment
    )


def test_video_lifecycle_email_waits_for_google_meet_url() -> None:
    from app.services import booking_operations

    source = inspect.getsource(booking_operations._run_effect)
    client_email = source.split('if key == "client_email":', 1)[1]
    meet_guard = client_email.index('appointment.meeting_mode == "video"')
    send = client_email.index("booking_notify.send_invitee_invite")
    assert meet_guard < send
    assert "google_meet_url_unavailable" in client_email[:send]


def test_initial_booking_surfaces_commit_a_durable_create_operation() -> None:
    """No initial route may run Google/SES/SMS after its local commit."""

    for endpoint in (
        dealer_router.create_standalone_rep_appointment,
        dealer_router.create_rep_appointment,
        dealer_router.book_underwriting_review_preference,
    ):
        source = inspect.getsource(endpoint)
        assert "booking_operations.enqueue" in source
        assert 'operation_type="create"' in source
        assert "defer_email=True" in source
        assert "booking_operations.wake_operation" in source
        after_commit = source.split("await db.commit()", 1)[1]
        assert "push_to_google" not in after_commit
        assert "send_invitee_invite" not in after_commit
        assert "send_rep_invite" not in after_commit
        assert "send_confirmation_sms" not in after_commit

    public_create = inspect.getsource(public_router.public_booking_create)
    assert public_create.index("await _deliver_booking(") < public_create.index(
        "await db.commit()"
    )
    assert "delivery_state=delivery_state" in public_create
    public_delivery = inspect.getsource(public_router._deliver_booking)
    assert "booking_operations.enqueue" in public_delivery
    for provider_call in (
        "push_to_google",
        "notify_host",
        "send_invitee_invite",
        "send_confirmation_sms",
        "deliver_pin",
    ):
        assert provider_call not in public_delivery


def test_initial_creation_fingerprint_is_stable_and_slot_sensitive() -> None:
    from app.services import booking_operations

    owner_id = uuid4()
    kwargs = {
        "owner_user_id": owner_id,
        "starts_at": datetime(2026, 9, 25, 15, 0, tzinfo=UTC),
        "duration_min": 30,
        "invitee_email": " Dealer@Example.com ",
        "invitee_phone": "+12015550101",
        "origin": "public",
        "scope": "public:franco:dealer",
    }
    first = booking_operations.creation_idempotency_key(**kwargs)
    assert first == booking_operations.creation_idempotency_key(**kwargs)
    assert len(first) <= 80
    assert first != booking_operations.creation_idempotency_key(
        **{
            **kwargs,
            "starts_at": datetime(2026, 9, 25, 16, 0, tzinfo=UTC),
        }
    )

    token_key = booking_operations.creation_idempotency_key(
        **kwargs, caller_token="booking-request-0001"
    )
    # A timeout retry keeps its identity even if a client reconstructs the
    # payload differently; the caller token is authoritative within scope.
    assert token_key == booking_operations.creation_idempotency_key(
        **{
            **kwargs,
            "starts_at": datetime(2026, 9, 26, 16, 0, tzinfo=UTC),
            "caller_token": "booking-request-0001",
        }
    )
    # A deliberately new token allows a later rebooking, including after the
    # earlier appointment was cancelled, without colliding with its unique row.
    assert token_key != booking_operations.creation_idempotency_key(
        **kwargs, caller_token="booking-request-0002"
    )

    for endpoint in (
        dealer_router.create_standalone_rep_appointment,
        dealer_router.create_rep_appointment,
        dealer_router.book_underwriting_review_preference,
        public_router.public_booking_create,
    ):
        source = inspect.getsource(endpoint)
        assert "creation_idempotency_key" in source


def test_legacy_booking_create_contracts_accept_optional_caller_idempotency() -> None:
    assert RepAppointmentCreate.model_fields["creation_idempotency_key"].is_required() is False
    assert public_router.PublicBookingCreate.model_fields[
        "creation_idempotency_key"
    ].is_required() is False
    with pytest.raises(ValidationError):
        public_router.PublicBookingCreate(
            creation_idempotency_key="short",
            starts_at=datetime(2026, 9, 25, 15, 0, tzinfo=UTC),
            full_name="Alex Morgan",
            email="alex@example.com",
            phone="2015550100",
        )


def test_initial_effect_order_and_meet_gate_precede_confirmations() -> None:
    from app.services import booking_operations

    assert booking_operations._EFFECT_PRIORITY["google"] < (
        booking_operations._EFFECT_PRIORITY["client_email"]
    )
    assert booking_operations._EFFECT_PRIORITY["google"] < (
        booking_operations._EFFECT_PRIORITY["client_sms"]
    )
    source = inspect.getsource(booking_operations._run_effect)
    client_email = source.split('if key == "client_email":', 1)[1]
    assert client_email.index("google_meet_url_unavailable") < client_email.index(
        "booking_notify.send_invitee_invite"
    )
    client_sms = source.split('if key == "client_sms":', 1)[1]
    assert client_sms.index("google_meet_url_unavailable") < client_sms.index(
        "booking_reminders.send_confirmation_sms"
    )


def test_initial_operation_disables_legacy_confirmation_dispatcher() -> None:
    from app.services import booking_operations

    enqueue = inspect.getsource(booking_operations.enqueue)
    assert 'operation_type == "create"' in enqueue
    assert "notice.delivery_next_attempt_at = None" in enqueue
    migration = Path(
        "alembic/versions/0225_booking_delivery_operations.py"
    ).read_text()
    assert "'create','cancel','reschedule','update'" in migration


def test_reassignment_grants_new_owner_and_removes_stale_booker_access() -> None:
    """A prospect booking must not remain owned by the rep who first booked it.

    The SQL predicate is evaluated against the prospect's current owner and
    contact assignments on every request. The booked-by predicate is retained
    only for legacy/non-prospect appointments.
    """

    rep_id = uuid4()
    user = SimpleNamespace(id=rep_id, role=Role.FIELD_REP)

    rendered = str(
        dealer_router._appointment_access_filter(user).compile(
            compile_kwargs={"literal_binds": True}
        )
    )

    assert "dos_rep_appointments.prospect_id IS NULL" in rendered
    assert "dos_rep_appointments.booked_by_user_id" in rendered
    assert "dos_rep_appointments.prospect_id IS NOT NULL" in rendered
    assert "dealer_prospects.owner_user_id" in rendered
    assert "dos_rep_contact_assignments" in rendered
    assert "dos_rep_contact_assignments.user_id" in rendered
    assert rendered.count("dos_rep_appointments.booked_by_user_id") == 1
    assert rep_id.hex in rendered

    non_prospect_branch, prospect_branch = rendered.split(" OR ", 1)
    assert "prospect_id IS NULL" in non_prospect_branch
    assert "booked_by_user_id" in non_prospect_branch
    assert "prospect_id IS NOT NULL" in prospect_branch
    assert "dealer_prospects.owner_user_id" in prospect_branch
    assert "dos_rep_contact_assignments.user_id" in prospect_branch
    assert "booked_by_user_id" not in prospect_branch


def test_team_roles_retain_all_appointment_access() -> None:
    for role in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        assert dealer_router._appointment_access_filter(
            SimpleNamespace(id=uuid4(), role=role)
        ) is True


def test_appointment_listing_and_manage_routes_share_live_access_gate() -> None:
    loader = inspect.getsource(dealer_router._load_owned_appointment)
    listing = inspect.getsource(dealer_router.list_all_rep_appointments)

    assert "_appointment_access_filter(user)" in loader
    assert "row.booked_by_user_id != user.id" not in loader
    assert "_appointment_access_filter(user)" in listing

    # Workspace, edit and cancel all resolve the row through the same gate.
    assert "_load_owned_appointment" in inspect.getsource(
        dealer_router.get_rep_appointment_workspace
    )
    assert "_load_owned_appointment" in inspect.getsource(
        dealer_router.patch_rep_appointment
    )
    assert "_load_owned_appointment" in inspect.getsource(
        dealer_router.cancel_rep_appointment
    )
