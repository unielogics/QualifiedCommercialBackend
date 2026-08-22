from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.dealer_os.services.rep_workflows import (
    SlotValidationError,
    is_stop_message,
    validate_underwriting_slots,
)


def test_underwriting_slots_allow_weekdays_inside_48_business_hours() -> None:
    now = datetime(2026, 8, 21, 19, 0, tzinfo=timezone.utc)  # Friday 3 PM ET
    slots = [
        datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc),
    ]

    out = validate_underwriting_slots(slots, timezone_name="America/New_York", now=now)

    assert [row["starts_at"] for row in out] == [
        "2026-08-21T20:00:00+00:00",
        "2026-08-24T14:00:00+00:00",
        "2026-08-25T18:00:00+00:00",
    ]


def test_underwriting_slots_reject_weekends() -> None:
    now = datetime(2026, 8, 21, 19, 0, tzinfo=timezone.utc)
    slots = [
        datetime(2026, 8, 22, 16, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc),
    ]

    with pytest.raises(SlotValidationError, match="Saturday and Sunday"):
        validate_underwriting_slots(slots, timezone_name="America/New_York", now=now)


def test_underwriting_slots_reject_duplicates() -> None:
    now = datetime(2026, 8, 21, 19, 0, tzinfo=timezone.utc)
    slot = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)

    with pytest.raises(SlotValidationError, match="different"):
        validate_underwriting_slots([slot, slot, datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)], timezone_name="America/New_York", now=now)


@pytest.mark.parametrize("body", ["STOP", " stop ", "Unsubscribe", "quit"])
def test_stop_messages_are_detected(body: str) -> None:
    assert is_stop_message(body) is True
