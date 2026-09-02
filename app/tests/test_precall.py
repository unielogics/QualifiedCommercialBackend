from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.dealer_os.services import client_room, precall
from app.models.booking_notification import BookingNotification
from app.models.booking_settings import BookingSettings
from app.schemas.booking_settings import UserBookingSettingsUpdate
from app.services import message_render
from app.services.booking_reminders import render_reminder_email, render_reminder_sms


def _booking(**overrides) -> BookingSettings:
    values = dict(
        user_id=uuid4(), duration_min=20, timezone="America/New_York",
        reminder_sms_enabled=True, precall_enabled=True, precall_messages={},
        confirmation_messages={}, reminder_email_messages={},
    )
    values.update(overrides)
    return BookingSettings(**values)


def _notice(**overrides) -> BookingNotification:
    values = dict(
        event_id=uuid4(), invitee_name="Ada Lovelace", invitee_email="ada@example.com",
        invitee_phone="+18625550100", sms_consent=True,
    )
    values.update(overrides)
    return BookingNotification(**values)


# --- step planning -------------------------------------------------------------


def test_plan_steps_books_nudge_after_booking_and_before_call_inside_quiet_hours() -> None:
    tz = ZoneInfo("America/New_York")
    now = datetime(2026, 9, 1, 22, 30, tzinfo=tz).astimezone(UTC)  # booked late evening
    starts_at = now + timedelta(days=4)
    rows = precall._plan_steps(_booking(), _notice(), starts_at, now=now, tz=tz)
    by_step = {}
    for key, channel, due in rows:
        by_step.setdefault(key, {})[channel] = due
    # nudge_1 = booking + 24h → 22:30 next day, shifted forward into 09:00 the day after
    n1 = by_step["nudge_1"]["email"].astimezone(tz)
    assert (n1.hour, n1.minute) == (9, 0)
    assert n1.date() == (now.astimezone(tz) + timedelta(days=2)).date()
    assert set(by_step["nudge_1"]) == {"email", "sms"}
    # nudge_2 = call − 24h, not shifted (email may go any time; SMS waits for the window at send time)
    assert by_step["nudge_2"]["email"] == starts_at - timedelta(hours=24)


def test_plan_steps_skips_first_nudge_for_a_call_tomorrow_and_uses_the_short_fallback() -> None:
    tz = ZoneInfo("America/New_York")
    now = datetime(2026, 9, 1, 10, 0, tzinfo=tz).astimezone(UTC)
    starts_at = now + timedelta(hours=20)
    rows = precall._plan_steps(_booking(), _notice(), starts_at, now=now, tz=tz)
    steps = {key for key, _, _ in rows}
    assert "nudge_1" not in steps
    assert {due for key, _, due in rows if key == "nudge_2"} == {starts_at - timedelta(hours=4)}


def test_plan_steps_has_no_sms_rows_without_consent() -> None:
    tz = ZoneInfo("America/New_York")
    now = datetime(2026, 9, 1, 10, 0, tzinfo=tz).astimezone(UTC)
    rows = precall._plan_steps(_booking(), _notice(sms_consent=False), now + timedelta(days=5), now=now, tz=tz)
    assert rows and all(channel == "email" for _, channel, _ in rows)


def test_plan_steps_honours_host_overrides() -> None:
    tz = ZoneInfo("America/New_York")
    now = datetime(2026, 9, 1, 10, 0, tzinfo=tz).astimezone(UTC)
    booking = _booking(precall_messages={"nudge_1": {"after_hours": 4, "channel": "email"}, "nudge_2": {"before_hours": 48}})
    rows = precall._plan_steps(booking, _notice(), now + timedelta(days=6), now=now, tz=tz)
    n1 = [(ch, due) for key, ch, due in rows if key == "nudge_1"]
    assert n1 == [("email", now + timedelta(hours=4))]
    assert {due for key, _, due in rows if key == "nudge_2"} == {now + timedelta(days=6) - timedelta(hours=48)}


# --- readiness rules ----------------------------------------------------------------


def _owner(**kw):
    base = dict(credit_pulled_at=None, credit_workflow_status=None, credit_provider_error_category=None, invite_token_hash=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_owner_credit_status_vocabulary() -> None:
    assert precall._owner_credit_status(_owner(), required=False) == "not_required"
    assert precall._owner_credit_status(_owner(), required=True) == "todo"
    assert precall._owner_credit_status(_owner(invite_token_hash="x"), required=True) == "sent"
    assert precall._owner_credit_status(_owner(credit_pulled_at=datetime.now(UTC)), required=True) == "done"
    assert precall._owner_credit_status(_owner(credit_workflow_status="declined"), required=True) == "declined"
    assert precall._owner_credit_status(_owner(credit_provider_error_category="no_hit"), required=True) == "failed"


def test_readiness_missing_phrase_and_done_count() -> None:
    ready = precall.Readiness(ownership_complete=True, ownership_total=100.0, contact_complete=True, bank_complete=False, credit_complete=False)
    assert ready.done_count == 1
    assert ready.missing_phrase == "connect your business bank and authorize your soft credit check"
    ready.bank_complete = True
    ready.credit_complete = True
    assert ready.complete and ready.missing_phrase == ""
    partial = precall.Readiness(ownership_complete=True, ownership_total=100.0, contact_complete=False)
    assert not partial.ownership_step_complete  # a 20%+ owner without email/phone is not done


# --- PIN rules ---------------------------------------------------------------------


def test_client_chosen_pin_rules() -> None:
    assert client_room.passcode_problem("12345") is not None
    assert client_room.passcode_problem("111111") is not None
    assert client_room.passcode_problem("123456") is not None
    assert client_room.passcode_problem("482913", "482913") is not None
    assert client_room.passcode_problem("482913", "111000") is None


# --- rendering ----------------------------------------------------------------------


def test_render_leaves_unknown_placeholders_and_appends_stop_notice_once() -> None:
    out = message_render.render("Hi {first}, {unknown} {room_link}", {"{first}": "Ada", "{room_link}": "https://r/x"})
    assert out == "Hi Ada, {unknown} https://r/x"
    assert message_render.with_stop_notice("Hello. reply stop to opt out.") == "Hello. reply stop to opt out."
    assert message_render.with_stop_notice("Hello").endswith(message_render.STOP_NOTICE)
    assert message_render.disallowed_placeholders("PIN {pin}") == ["{pin}"]
    assert message_render.disallowed_placeholders("PIN {pin}", allow_pin=True) == []


def test_reminder_sms_default_appends_precall_line_only_when_something_is_open() -> None:
    kwargs = dict(event_title="Intro call", when="Tue 10:00 AM", invitee_name="Ada", rep_name="Sam", join_url=None)
    open_line = render_reminder_sms(None, extra={"{precall}": "Still needed: connect your business bank → https://r/x"}, **kwargs)
    assert "Still needed" in open_line and open_line.endswith(message_render.STOP_NOTICE)
    done = render_reminder_sms(None, extra={"{precall}": ""}, **kwargs)
    assert "Still needed" not in done
    custom = render_reminder_sms("See you {time} {precall}", extra={"{precall}": ""}, **kwargs)
    assert custom == f"See you Tue 10:00 AM {message_render.STOP_NOTICE}"


def test_reminder_email_uses_host_template_or_default() -> None:
    values = {"{rep}": "Sam", "{precall}": "", "{first}": "Ada"}
    subject, body = render_reminder_email(
        {"subject": "Tomorrow with {rep}", "body": "Hi {first},\nsee you soon."},
        host_name="Sam", event_title="Intro", when="Tue", duration_min=20, details=[], join_url=None, values=values,
    )
    assert (subject, body) == ("Tomorrow with Sam", "Hi Ada,\nsee you soon.")
    subject, body = render_reminder_email(None, host_name="Sam", event_title="Intro", when="Tue", duration_min=20, details=[], join_url=None, values=values)
    assert subject == "Reminder: Intro" and "Sam" in body


def test_template_values_render_the_precall_line_from_readiness() -> None:
    booking = _booking()
    host = SimpleNamespace(name="Sam Rep", email="sam@qc.com")
    event = SimpleNamespace(starts_at=datetime(2026, 9, 8, 14, 0, tzinfo=UTC))
    ready = precall.Readiness(ownership_complete=False, ownership_total=0.0, contact_complete=True)
    values = precall.template_values(
        notice=_notice(), event=event, booking=booking, host=host, dealer=SimpleNamespace(name="Ada Motors"),
        room_link="https://r/x", ready=ready, pin="482913",
    )
    assert values["{first}"] == "Ada" and values["{business}"] == "Ada Motors" and values["{pin}"] == "482913"
    assert values["{done}"] == "0 of 3" and "https://r/x" in values["{precall}"]
    assert "September 8" in values["{time}"]


# --- settings validation -----------------------------------------------------------


def test_settings_reject_pin_outside_the_pin_messages_and_clean_templates() -> None:
    with pytest.raises(ValueError):
        UserBookingSettingsUpdate(precall_messages={"nudge_1": {"sms": "Your PIN is {pin}"}})
    with pytest.raises(ValueError):
        UserBookingSettingsUpdate(reminder_email_messages={"1440": {"subject": "PIN {pin}", "body": "x"}})
    ok = UserBookingSettingsUpdate(
        confirmation_messages={"sms": "Confirmed. PIN {pin} {room_link}", "email_subject": "  "},
        precall_messages={"nudge_1": {"after_hours": 12, "channel": "EMAIL", "sms": " ", "email_body": "Hi {first}"}, "nudge_2": {}},
        reminder_email_messages={"1440": {"subject": "Tomorrow", "body": "See you {time}"}, "999": {"subject": "x", "body": "y"}},
    )
    assert ok.confirmation_messages == {"sms": "Confirmed. PIN {pin} {room_link}"}
    assert ok.precall_messages == {"nudge_1": {"after_hours": 12.0, "channel": "email", "email_body": "Hi {first}"}}
    assert ok.reminder_email_messages == {"1440": {"subject": "Tomorrow", "body": "See you {time}"}}
    with pytest.raises(ValueError):
        UserBookingSettingsUpdate(precall_messages={"nudge_1": {"channel": "carrier pigeon"}})


# --- origin → file rule --------------------------------------------------------------


def test_origin_is_explicit_when_the_surface_says_and_role_based_otherwise() -> None:
    assert precall.origin_for("field_desk", "super_admin") == "field_desk"
    assert precall.origin_for("calendar", "field_rep") == "calendar"
    assert precall.origin_for(None, "field_rep") == "field_desk"
    assert precall.origin_for(None, "Role.FIELD_REP".lower()) == "field_desk"
    assert precall.origin_for(None, "super_admin") == "calendar"
    assert precall.origin_for("bogus", "loan_exec") == "calendar"


def test_only_field_desk_bookings_open_the_draft() -> None:
    assert precall.opens_draft("field_desk")
    assert not precall.opens_draft("calendar")
    assert not precall.opens_draft("public")
    assert not precall.opens_draft("intake")
    assert not precall.opens_draft(None)


def test_public_booking_origin_follows_the_host_or_the_link() -> None:
    from app.routers.public import public_booking_origin

    assert public_booking_origin("field_rep", None) == "field_desk"
    assert public_booking_origin("super_admin", "field_desk_product") == "field_desk"
    assert public_booking_origin("super_admin", None) == "public"
    assert public_booking_origin("super_admin", "newsletter") == "public"
