"""The book-a-call widget on a file takes its call times from the shared
availability answer, and a fault in that lookup disables the widget rather
than failing the whole file."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import DBAPIError

from app.routers import dealer_ai_intake as core

NOW = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)


def _slot(hours_from_now: int) -> SimpleNamespace:
    starts = NOW + timedelta(hours=hours_from_now)
    return SimpleNamespace(starts_at=starts, label=f"slot+{hours_from_now}h", date_label=starts.strftime("%a %b %d"))


@pytest.fixture
def booking_host(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    owner = SimpleNamespace(id="owner", name="Denny Matos", email="denny@example.com")
    booking = SimpleNamespace(duration_min=20)
    monkeypatch.setattr(core, "_booking_settings_for_primary_admin", AsyncMock(return_value=(owner, booking)))
    monkeypatch.setattr(core, "_now", lambda: NOW)
    return owner


async def test_call_times_come_from_the_availability_answers_slots(monkeypatch: pytest.MonkeyPatch, booking_host: SimpleNamespace) -> None:
    availability = SimpleNamespace(slots=[_slot(1), _slot(30), _slot(2), _slot(3), _slot(4)])
    monkeypatch.setattr(core, "_available_booking_slots", AsyncMock(return_value=availability))

    owner, booking, slots = await core._dealer_call_slots(AsyncMock())

    assert owner is booking_host and booking.duration_min == 20
    # the next day's times come first, three at most
    assert [slot["label"] for slot in slots] == ["slot+1h", "slot+2h", "slot+3h"]
    assert slots[0]["starts_at"] == _slot(1).starts_at.isoformat()
    assert slots[0]["date_label"] == _slot(1).date_label


async def test_a_bare_list_of_slots_is_still_understood(monkeypatch: pytest.MonkeyPatch, booking_host: SimpleNamespace) -> None:
    monkeypatch.setattr(core, "_available_booking_slots", AsyncMock(return_value=[_slot(40), _slot(50)]))
    _, _, slots = await core._dealer_call_slots(AsyncMock())
    assert [slot["label"] for slot in slots] == ["slot+40h", "slot+50h"]


async def test_the_widget_is_disabled_not_the_file_when_call_times_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "_dealer_call_slots", AsyncMock(side_effect=RuntimeError("calendar exploded")))

    widget = await core._decorate_widget(AsyncMock(), SimpleNamespace(id="intake-1"), {"type": "book_call", "prompt": "Book a call"})

    assert widget is not None
    assert widget["type"] == "book_call" and widget["prompt"] == "Book a call"
    assert widget["slots"] == []
    assert widget["host_name"] == "Qualified Commercial" and widget["duration_min"] == 30
    assert "follow up" in widget["disabled_reason"]


async def test_a_database_fault_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = DBAPIError("SELECT 1", {}, Exception("connection lost"))
    monkeypatch.setattr(core, "_dealer_call_slots", AsyncMock(side_effect=broken))
    with pytest.raises(DBAPIError):
        await core._decorate_widget(AsyncMock(), SimpleNamespace(id="intake-1"), {"type": "book_call"})


async def test_other_widgets_pass_through_untouched() -> None:
    widget = {"type": "upload", "prompt": "Send the statements"}
    assert await core._decorate_widget(AsyncMock(), SimpleNamespace(id="i"), widget) is widget
    assert await core._decorate_widget(AsyncMock(), SimpleNamespace(id="i"), None) is None
