from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.dealer_os.router import _booking_review_row, _sort_calendar_file_options
from app.dealer_os.router import router as dealer_router
from app.dealer_os.schemas import (
    RepAppointmentApplyOutcome,
    RepAppointmentFileLinkPatch,
    RepAppointmentFileOption,
)
from app.enums import Role
from app.routers.calendar import router as calendar_router
from app.schemas.booking_settings import UserBookingSettingsUpdate
from app.schemas.event import AppointmentOutcomeDefinitionCreate, AppointmentOutcomeDefinitionPatch
from app.services import calendar_v2


def test_calendar_v2_routes_are_registered() -> None:
    calendar_routes = {
        (route.path, method)
        for route in calendar_router.routes
        for method in (route.methods or set())
    }
    dealer_routes = {
        (route.path, method)
        for route in dealer_router.routes
        for method in (route.methods or set())
    }
    assert ("/calendar/workspace", "GET") in calendar_routes
    assert ("/calendar/outcomes", "GET") in calendar_routes
    assert ("/calendar/outcomes", "POST") in calendar_routes
    assert ("/calendar/outcomes/{outcome_id}", "PATCH") in calendar_routes
    assert ("/calendar/outcomes/{outcome_id}", "DELETE") in calendar_routes
    assert ("/dealer-os/appointments/{appointment_id}/apply-outcome", "POST") in dealer_routes
    assert ("/dealer-os/appointments/{appointment_id}/file-link", "PATCH") in dealer_routes
    assert ("/dealer-os/calendar/file-options", "GET") in dealer_routes


def test_calendar_v2_permissions_are_limited_to_privileged_operators() -> None:
    assert calendar_v2.can_use_calendar_v2(SimpleNamespace(role=Role.SUPER_ADMIN))
    assert calendar_v2.can_use_calendar_v2(SimpleNamespace(role=Role.LOAN_EXEC))
    assert not calendar_v2.can_use_calendar_v2(SimpleNamespace(role=Role.FIELD_REP))
    assert not calendar_v2.can_use_calendar_v2(SimpleNamespace(role=Role.CLIENT))
    assert calendar_v2.can_create_funding_file(SimpleNamespace(role=Role.SUPER_ADMIN))
    assert calendar_v2.can_create_funding_file(SimpleNamespace(role=Role.LOAN_EXEC))
    assert not calendar_v2.can_create_funding_file(SimpleNamespace(role=Role.FIELD_REP))
    assert calendar_v2.can_manage_outcome_catalog(SimpleNamespace(role=Role.SUPER_ADMIN))
    assert not calendar_v2.can_manage_outcome_catalog(SimpleNamespace(role=Role.LOAN_EXEC))
    assert not calendar_v2.can_manage_outcome_catalog(SimpleNamespace(role=Role.FIELD_REP))


def test_default_outcomes_cover_the_review_workflow() -> None:
    names = [item["name"] for item in calendar_v2.DEFAULT_OUTCOMES]
    assert names == ["Qualified", "Follow up", "Documents requested", "No show", "Not a fit"]
    assert all("log_activity" in item["effects"] for item in calendar_v2.DEFAULT_OUTCOMES)
    assert "file_action" in calendar_v2.DEFAULT_OUTCOMES[0]["effects"]
    assert "schedule_follow_up" in calendar_v2.DEFAULT_OUTCOMES[1]["effects"]


def test_outcome_definitions_normalize_names_and_effects() -> None:
    created = AppointmentOutcomeDefinitionCreate(
        name="  Ready   for review ",
        target_crm_status="follow_up",
        effects=["schedule_follow_up", "schedule_follow_up"],
    )
    assert created.name == "Ready for review"
    assert created.effects == ["log_activity", "schedule_follow_up"]

    patched = AppointmentOutcomeDefinitionPatch(effects=["close_enquiry"])
    assert patched.effects == ["log_activity", "close_enquiry"]


def test_calendar_outcome_payload_carries_one_retry_key_and_exact_file() -> None:
    file_id = uuid4()
    payload = RepAppointmentApplyOutcome(
        outcome_definition_id=uuid4(),
        note="Reviewed the request with the client.",
        idempotency_key="attempt-12345678",
        confirm=True,
        file_action="link_existing",
        existing_file_kind="intake",
        existing_file_id=file_id,
        apply_booking_data=True,
    )
    assert payload.idempotency_key == "attempt-12345678"
    assert payload.existing_file_id == file_id
    assert RepAppointmentFileLinkPatch(kind="intake", file_id=file_id, confirm=True).confirm


def test_file_options_merge_ai_intakes_and_funding_loans_by_recency() -> None:
    intake = RepAppointmentFileOption(
        kind="intake",
        id=uuid4(),
        label="Example AI Intake",
        subtitle="Owner · intake@example.com",
        status="submitted",
        href="/admin/ai-underwriter-leads?lead=example",
    )
    loan = RepAppointmentFileOption(
        kind="loan",
        id=uuid4(),
        label="Example Funding Loan",
        subtitle="QC-100 · loan@example.com",
        status="underwriting",
        href="/loans/example",
    )

    items = _sort_calendar_file_options(
        [
            (datetime(2026, 8, 30, tzinfo=UTC), intake),
            (datetime(2026, 8, 31, tzinfo=UTC), loan),
        ],
        limit=200,
    )

    assert [item.kind for item in items] == ["loan", "intake"]


def test_booking_data_review_distinguishes_matches_missing_and_conflicts() -> None:
    matching_phone = _booking_review_row(
        field="phone",
        label="Phone",
        current="(201) 555-0188",
        proposed="+1 201-555-0188",
        target_kind="intake",
    )
    missing_amount = _booking_review_row(
        field="requested_amount",
        label="Requested amount",
        current=None,
        proposed="$250,000",
        target_kind="loan",
    )
    conflicting_email = _booking_review_row(
        field="email",
        label="Email",
        current="old@example.com",
        proposed="new@example.com",
        target_kind="loan",
    )
    assert matching_phone.status == "matches"
    assert missing_amount.status == "missing_in_file"
    assert conflicting_email.status == "conflict"


def test_booking_settings_allow_recurring_breaks_and_dated_exceptions() -> None:
    payload = UserBookingSettingsUpdate(
        blocked_intervals=[
            {"weekday": 1, "start_time": "14:00", "end_time": "16:00", "label": "Monday break"},
            {"on_date": date(2026, 9, 2), "start_time": "10:00", "end_time": "12:00", "label": "Offsite"},
        ]
    )
    assert payload.blocked_intervals[0].weekday == 1
    assert payload.blocked_intervals[1].on_date == date(2026, 9, 2)


@pytest.mark.parametrize(
    "interval",
    [
        {"start_time": "14:00", "end_time": "16:00"},
        {"weekday": 1, "on_date": "2026-09-02", "start_time": "14:00", "end_time": "16:00"},
    ],
)
def test_booking_block_requires_exactly_one_day_scope(interval: dict) -> None:
    with pytest.raises(ValidationError, match="either a recurring weekday or one calendar date"):
        UserBookingSettingsUpdate(blocked_intervals=[interval])


def test_dated_breaks_reject_overlap_on_the_same_date() -> None:
    with pytest.raises(ValidationError, match="cannot overlap"):
        UserBookingSettingsUpdate(
            blocked_intervals=[
                {"on_date": "2026-09-02", "start_time": "10:00", "end_time": "12:00"},
                {"on_date": "2026-09-02", "start_time": "11:30", "end_time": "13:00"},
            ]
        )
