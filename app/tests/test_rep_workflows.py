from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.dealer_os.schemas import RepInboxThreadCreate
from app.dealer_os.services.rep_workflows import (
    SlotValidationError,
    is_stop_message,
    program_pdf_options,
    render_program_pdf,
    selected_program_pdfs,
    underwriting_window_end,
    validate_underwriting_slots,
)


def test_underwriting_slots_allow_weekdays_inside_48_business_hours() -> None:
    now = datetime(2026, 8, 21, 19, 0, tzinfo=UTC)  # Friday 3 PM ET
    slots = [
        datetime(2026, 8, 21, 20, 0, tzinfo=UTC),
        datetime(2026, 8, 24, 14, 0, tzinfo=UTC),
        datetime(2026, 8, 25, 18, 0, tzinfo=UTC),
    ]

    out = validate_underwriting_slots(slots, timezone_name="America/New_York", now=now)

    assert [row["starts_at"] for row in out] == [
        "2026-08-21T20:00:00+00:00",
        "2026-08-24T14:00:00+00:00",
        "2026-08-25T18:00:00+00:00",
    ]


def test_underwriting_slots_reject_weekends() -> None:
    now = datetime(2026, 8, 21, 19, 0, tzinfo=UTC)
    slots = [
        datetime(2026, 8, 22, 16, 0, tzinfo=UTC),
        datetime(2026, 8, 24, 14, 0, tzinfo=UTC),
        datetime(2026, 8, 25, 18, 0, tzinfo=UTC),
    ]

    with pytest.raises(SlotValidationError, match="Saturday and Sunday"):
        validate_underwriting_slots(slots, timezone_name="America/New_York", now=now)


def test_underwriting_slots_reject_duplicates() -> None:
    now = datetime(2026, 8, 21, 19, 0, tzinfo=UTC)
    slot = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)

    with pytest.raises(SlotValidationError, match="different"):
        validate_underwriting_slots([slot, slot, datetime(2026, 8, 25, 18, 0, tzinfo=UTC)], timezone_name="America/New_York", now=now)


def test_underwriting_window_skips_weekend_hours() -> None:
    now = datetime(2026, 8, 21, 19, 0, tzinfo=UTC)  # Friday 3 PM ET

    end = underwriting_window_end(
        timezone_name="America/New_York",
        now=now,
    )

    assert end == datetime(2026, 8, 25, 19, 0, tzinfo=UTC)


@pytest.mark.parametrize("body", ["STOP", " stop ", "Unsubscribe", "quit"])
def test_stop_messages_are_detected(body: str) -> None:
    assert is_stop_message(body) is True


def test_program_pdf_allowlist_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="Unknown program PDF"):
        selected_program_pdfs(["missing"])


def test_program_pdf_options_render_valid_pdf_bytes() -> None:
    options = program_pdf_options()
    assert options
    pdf = selected_program_pdfs([options[0]["key"]])[0]
    rendered = render_program_pdf(pdf)
    assert rendered.startswith(b"%PDF-")
    assert pdf.filename.endswith(".pdf")


def test_inbox_compose_requires_contact_for_selected_channels() -> None:
    with pytest.raises(ValidationError, match="mobile"):
        RepInboxThreadCreate(
            recipient_name="Jane Client",
            channels=["sms"],
            subject="Qualified Commercial",
            body="Hello",
        )

    payload = RepInboxThreadCreate(
        recipient_name="Jane Client",
        recipient_email="jane@example.com",
        recipient_phone="5551234567",
        channels=["email", "sms"],
        subject="Qualified Commercial",
        body="Hello",
    )
    assert payload.channels == ["email", "sms"]
