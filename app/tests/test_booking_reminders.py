from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.models.booking_notification import BookingNotification, BookingNotificationReminder
from app.models.booking_settings import BookingSettings
from app.models.event import CalendarEvent
from app.schemas.booking_settings import UserBookingSettingsUpdate
from app.services.booking_availability import (
    booking_window_bounds,
    daily_booking_windows,
    slot_fits_daily_schedule,
    slot_overlaps_blocked_interval,
    slot_within_custom_booking_window,
)
from app.services import booking_reminders
from app.services.booking_reminders import register_booking, send_confirmation_sms


class _FakeSession:
    def __init__(self) -> None:
        self.added = []

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
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
        reminder_email_minutes=[2880, 1440, 60],
        reminder_sms_enabled=True,
        reminder_sms_minutes_before=120,
        reminder_sms_minutes=[120, 30],
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

    assert db.added[0] is row
    reminders = [item for item in db.added if isinstance(item, BookingNotificationReminder)]
    assert [(item.channel, item.minutes_before) for item in reminders] == [
        ("email", 2880),
        ("email", 1440),
        ("email", 60),
        ("sms", 120),
        ("sms", 30),
    ]
    assert row.email_reminder_due_at == starts_at - timedelta(hours=48)
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
    assert not any(isinstance(item, BookingNotificationReminder) for item in db.added)


@pytest.mark.asyncio
async def test_register_booking_schedules_rep_email_and_in_app_reminders_only() -> None:
    starts_at = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
    event = SimpleNamespace(id=uuid4(), starts_at=starts_at)
    rep_id = uuid4()
    booking = BookingSettings(
        user_id=uuid4(),
        reminder_email_enabled=True,
        reminder_email_minutes=[1440, 60],
        reminder_sms_enabled=True,
        reminder_sms_minutes=[120],
    )
    db = _FakeSession()

    await register_booking(
        db,
        event=event,
        booking=booking,
        invitee_name="Client Name",
        invitee_email="client@example.com",
        invitee_phone="2015550100",
        sms_consent=True,
        booked_by_user_id=rep_id,
    )

    reminders = [item for item in db.added if isinstance(item, BookingNotificationReminder)]
    assert [(item.channel, item.minutes_before) for item in reminders] == [
        ("email", 1440),
        ("email", 60),
        ("rep", 1440),
        ("rep", 60),
        ("sms", 120),
    ]
    assert all(item.channel != "rep_sms" for item in reminders)


def test_booking_settings_accepts_independent_buffers_and_twenty_minute_meetings() -> None:
    payload = UserBookingSettingsUpdate(
        duration_min=20,
        buffer_before_min=5,
        buffer_after_min=10,
        reminder_email_minutes_before=1440,
        reminder_email_minutes=[2880, 1440, 60],
        reminder_sms_minutes_before=120,
        reminder_sms_minutes=[120, 30],
    )

    assert payload.duration_min == 20
    assert payload.buffer_before_min == 5
    assert payload.buffer_after_min == 10
    assert payload.reminder_email_minutes == [2880, 1440, 60]
    assert payload.reminder_sms_minutes == [120, 30]


def test_booking_settings_rejects_duplicate_reminder_rows() -> None:
    with pytest.raises(ValueError, match="cannot contain duplicate"):
        UserBookingSettingsUpdate(reminder_email_minutes=[1440, 1440])


def test_booking_settings_validates_and_sorts_recurring_blocked_times() -> None:
    payload = UserBookingSettingsUpdate(
        blocked_intervals=[
            {"weekday": 3, "start_time": "14:00", "end_time": "16:00", "label": "Review"},
            {"weekday": 1, "start_time": "14:00", "end_time": "16:00", "label": " Break "},
        ],
    )

    assert [interval.weekday for interval in payload.blocked_intervals] == [1, 3]
    assert payload.blocked_intervals[0].label == "Break"


@pytest.mark.parametrize(
    ("blocked_intervals", "message"),
    [
        (
            [
                {"weekday": 1, "start_time": "13:00", "end_time": "15:00"},
                {"weekday": 1, "start_time": "14:00", "end_time": "16:00"},
            ],
            "cannot overlap",
        ),
        (
            [{"weekday": 1, "start_time": "08:30", "end_time": "10:00"}],
            "inside the daily booking",
        ),
    ],
)
def test_booking_settings_rejects_invalid_blocked_times(blocked_intervals, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        UserBookingSettingsUpdate(blocked_intervals=blocked_intervals)


def test_booking_settings_normalizes_split_weekly_schedule() -> None:
    payload = UserBookingSettingsUpdate(
        weekly_schedule=[
            {
                "weekday": 2,
                "intervals": [
                    {"start_time": "13:00", "end_time": "17:30"},
                    {"start_time": "08:30", "end_time": "12:00"},
                ],
            },
            {"weekday": 1, "intervals": []},
        ],
    )

    assert payload.available_days == [2]
    assert payload.start_time == "08:30"
    assert payload.end_time == "17:30"
    assert [item.start_time for item in payload.weekly_schedule[1].intervals] == [
        "08:30",
        "13:00",
    ]


def test_booking_settings_rejects_overlapping_schedule_ranges() -> None:
    with pytest.raises(ValueError, match="cannot overlap"):
        UserBookingSettingsUpdate(
            weekly_schedule=[
                {
                    "weekday": 1,
                    "intervals": [
                        {"start_time": "09:00", "end_time": "12:00"},
                        {"start_time": "11:30", "end_time": "15:00"},
                    ],
                }
            ]
        )


def test_booking_settings_rejects_reversed_advance_window() -> None:
    with pytest.raises(ValueError, match="Latest booking day"):
        UserBookingSettingsUpdate(
            advance_booking_window_enabled=True,
            minimum_notice_days=6,
            maximum_advance_days=5,
        )


def test_booking_window_uses_custom_days_only_when_enabled() -> None:
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 31, 10, 15, tzinfo=zone)
    custom = SimpleNamespace(
        advance_booking_window_enabled=True,
        minimum_notice_days=2,
        maximum_advance_days=5,
    )
    earliest, latest = booking_window_bounds(custom, now)

    assert earliest == datetime(2026, 9, 2, 10, 15, tzinfo=zone)
    assert latest == datetime(2026, 9, 5, 23, 59, 59, 999999, tzinfo=zone)
    assert not slot_within_custom_booking_window(
        custom,
        datetime(2026, 9, 2, 10, 0, tzinfo=zone),
        now_local=now,
    )
    assert slot_within_custom_booking_window(
        custom,
        datetime(2026, 9, 2, 10, 15, tzinfo=zone),
        now_local=now,
    )

    default = SimpleNamespace(advance_booking_window_enabled=False)
    default_earliest, default_latest = booking_window_bounds(default, now)
    assert default_earliest == datetime(2026, 8, 31, 12, 15, tzinfo=zone)
    assert default_latest.date().isoformat() == "2026-09-15"


def test_daily_booking_windows_supports_different_and_split_day_hours() -> None:
    booking = SimpleNamespace(
        weekly_schedule=[
            {
                "weekday": 1,
                "intervals": [
                    {"start_time": "09:00", "end_time": "12:00"},
                    {"start_time": "14:00", "end_time": "18:00"},
                ],
            },
            {
                "weekday": 2,
                "intervals": [{"start_time": "11:00", "end_time": "15:00"}],
            },
            {"weekday": 3, "intervals": []},
        ]
    )
    zone = ZoneInfo("America/New_York")

    assert daily_booking_windows(booking, datetime(2026, 8, 31).date()) == [
        (540, 720),
        (840, 1080),
    ]
    assert daily_booking_windows(booking, datetime(2026, 9, 1).date()) == [(660, 900)]
    assert daily_booking_windows(booking, datetime(2026, 9, 2).date()) == []
    assert slot_fits_daily_schedule(
        booking,
        datetime(2026, 8, 31, 14, 0, tzinfo=zone),
        datetime(2026, 8, 31, 14, 30, tzinfo=zone),
    )
    assert not slot_fits_daily_schedule(
        booking,
        datetime(2026, 8, 31, 12, 0, tzinfo=zone),
        datetime(2026, 8, 31, 14, 30, tzinfo=zone),
    )


def test_booking_block_preserves_the_full_break_including_buffers() -> None:
    booking = SimpleNamespace(
        buffer_before_min=5,
        buffer_after_min=5,
        blocked_intervals=[
            {"weekday": 1, "start_time": "14:00", "end_time": "16:00", "label": "Break"},
        ],
    )
    zone = ZoneInfo("America/New_York")

    assert slot_overlaps_blocked_interval(
        booking,
        datetime(2026, 8, 31, 13, 55, tzinfo=zone),
        datetime(2026, 8, 31, 14, 15, tzinfo=zone),
    )
    assert slot_overlaps_blocked_interval(
        booking,
        datetime(2026, 8, 31, 16, 0, tzinfo=zone),
        datetime(2026, 8, 31, 16, 20, tzinfo=zone),
    )
    assert not slot_overlaps_blocked_interval(
        booking,
        datetime(2026, 8, 31, 16, 5, tzinfo=zone),
        datetime(2026, 8, 31, 16, 25, tzinfo=zone),
    )


def _notice(**overrides) -> BookingNotification:
    row = BookingNotification(
        id=uuid4(),
        event_id=uuid4(),
        invitee_name="paresh",
        invitee_phone="+15551234567",
        sms_consent=True,
        confirmation_email_status="sent",
        confirmation_sms_status="pending",
        email_reminder_status="pending",
        sms_reminder_status="pending",
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def test_record_delivery_error_dates_the_failure() -> None:
    row = _notice()
    before = datetime.now(UTC)

    row.record_delivery_error("Configured but SMS_PRODUCTION is disabled")

    assert row.last_error == "Configured but SMS_PRODUCTION is disabled"
    assert row.last_error_at is not None and row.last_error_at >= before


def test_record_delivery_error_truncates_and_treats_empty_as_no_error() -> None:
    row = _notice()

    row.record_delivery_error("x" * 1200)
    assert len(row.last_error or "") == 1000

    row.record_delivery_error("   ")
    assert row.last_error is None
    assert row.last_error_at is None


def test_clear_delivery_error_holds_while_another_channel_is_failing() -> None:
    """One text field serves four channels, so a success cannot speak for all."""
    row = _notice(confirmation_email_status="failed")
    row.record_delivery_error("Email provider unavailable")

    row.confirmation_sms_status = "sent"
    row.clear_delivery_error()

    assert row.last_error == "Email provider unavailable"

    row.confirmation_email_status = "sent"
    row.clear_delivery_error()

    assert row.last_error is None
    assert row.last_error_at is None


@pytest.mark.asyncio
async def test_confirmation_sms_success_clears_a_stale_failure(monkeypatch) -> None:
    """The regression: a resolved failure survived every later success.

    A provider swap left bookings reading "Configured but SMS_PRODUCTION is
    disabled" while texts for those same bookings were being delivered, because
    nothing cleared the field and the panel dated it from `updated_at`.
    """
    row = _notice(last_error="Configured but SMS_PRODUCTION is disabled")
    row.last_error_at = datetime(2026, 8, 31, 19, 38, tzinfo=UTC)
    event = CalendarEvent(
        id=row.event_id,
        title="Program intro",
        starts_at=datetime(2026, 9, 5, 15, 0, tzinfo=UTC),
        duration_min=20,
        owner_user_id=uuid4(),
    )

    async def _delivered(db, phone, body, *, context="", client_id=None):
        return SimpleNamespace(ok=True, detail="Sent through the handset relay.")

    monkeypatch.setattr(booking_reminders.consent_delivery, "send_sms_guarded", _delivered)
    db = _FakeSession()

    await send_confirmation_sms(db, row, event, timezone_name="America/New_York")

    assert row.confirmation_sms_status == "sent"
    assert row.last_error is None
    assert row.last_error_at is None


@pytest.mark.asyncio
async def test_confirmation_sms_failure_records_the_reason_and_the_time(monkeypatch) -> None:
    row = _notice()
    event = CalendarEvent(
        id=row.event_id,
        title="Program intro",
        starts_at=datetime(2026, 9, 5, 15, 0, tzinfo=UTC),
        duration_min=20,
        owner_user_id=uuid4(),
    )

    async def _refused(db, phone, body, *, context="", client_id=None):
        return SimpleNamespace(ok=False, detail="The SMS relay is unreachable.")

    monkeypatch.setattr(booking_reminders.consent_delivery, "send_sms_guarded", _refused)
    db = _FakeSession()

    await send_confirmation_sms(db, row, event, timezone_name="America/New_York")

    assert row.confirmation_sms_status == "failed"
    assert row.last_error == "The SMS relay is unreachable."
    assert row.last_error_at is not None
