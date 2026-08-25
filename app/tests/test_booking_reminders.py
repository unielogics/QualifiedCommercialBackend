from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.booking_settings import BookingSettings
from app.models.event import CalendarEvent
from app.schemas.booking_settings import UserBookingSettingsUpdate
from app.services.booking_reminders import register_booking


class _FakeSession:
    def __init__(self) -> None:
        self.added = []

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_register_booking_snapshots_email_and_consent_gated_sms_schedule() -> None:
    starts_at = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
    event = CalendarEvent(
        id=uuid4(),
        title="Program intro",
        starts_at=starts_at,
        duration_min=20,
        owner_user_id=uuid4(),
    )
    booking = BookingSettings(
        user_id=event.owner_user_id,
        duration_min=20,
        reminder_email_enabled=True,
        reminder_email_minutes_before=1440,
        reminder_sms_enabled=True,
        reminder_sms_minutes_before=120,
        confirmation_email_enabled=True,
        confirmation_sms_enabled=True,
    )
    db = _FakeSession()

    row = await register_booking(
        db,
        event=event,
        booking=booking,
        invitee_name="Client Name",
        invitee_email="client@example.com",
        invitee_phone="(201) 555-0100",
        sms_consent=True,
        program_name="EZ Term",
        requested_amount="$250,000",
        full_address="100 Main St, Newark, NJ 07102",
    )

    assert db.added == [row]
    assert row.email_reminder_due_at == starts_at - timedelta(hours=24)
    assert row.sms_reminder_due_at == starts_at - timedelta(hours=2)
    assert row.confirmation_email_status == "pending"
    assert row.confirmation_sms_status == "pending"
    assert row.invitee_phone == "+12015550100"
    assert row.program_name == "EZ Term"


@pytest.mark.asyncio
async def test_register_booking_never_schedules_sms_without_consent() -> None:
    starts_at = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
    event = SimpleNamespace(id=uuid4(), starts_at=starts_at)
    booking = BookingSettings(
        user_id=uuid4(),
        reminder_sms_enabled=True,
        reminder_sms_minutes_before=120,
        confirmation_sms_enabled=True,
        reminder_email_enabled=False,
    )
    db = _FakeSession()

    row = await register_booking(
        db,
        event=event,
        booking=booking,
        invitee_name="Client Name",
        invitee_email=None,
        invitee_phone="2015550100",
        sms_consent=False,
    )

    assert row.sms_reminder_due_at is None
    assert row.sms_reminder_status == "blocked_no_consent"
    assert row.confirmation_sms_status == "blocked_no_consent"


def test_booking_settings_accepts_independent_buffers_and_twenty_minute_meetings() -> None:
    payload = UserBookingSettingsUpdate(
        duration_min=20,
        buffer_before_min=5,
        buffer_after_min=10,
        reminder_email_minutes_before=1440,
        reminder_sms_minutes_before=120,
    )

    assert payload.duration_min == 20
    assert payload.buffer_before_min == 5
    assert payload.buffer_after_min == 10
