"""Booking contact synchronization must not bypass request or issued-term controls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.dealer_os import router as calendar_files
from app.enums import Role


def appointment(amount="200000"):
    return SimpleNamespace(
        dealer_id=None, converted_intake_id=uuid4(), linked_loan_id=None,
        invitee_name="Updated Owner", invitee_email="owner@example.com", invitee_phone=None,
        company="Test Company", full_address=None, requested_amount=amount, program_name=None,
    )


def intake():
    return SimpleNamespace(
        id=uuid4(), bucket_id=uuid4(), full_name="Original Owner", email="owner@example.com",
        phone=None, business_name="Test Company", loan_purpose="working_capital", requested_loan_amount=70_000,
        intake_state={"current_funding_request": {"requested_amount": "200000.00", "revision": 1}},
    )


@pytest.mark.asyncio
async def test_booking_cannot_change_submitted_request_before_first_correction():
    original = intake()
    original.intake_state = {}
    with patch.object(calendar_files, "_load_calendar_intake_file", AsyncMock(return_value=original)):
        with pytest.raises(HTTPException) as error:
            await calendar_files._apply_calendar_booking_data(
                SimpleNamespace(), appointment(), SimpleNamespace(role=Role.FIELD_REP),
            )
    assert error.value.status_code == 403
    assert original.requested_loan_amount == 70_000
    assert original.full_name == "Original Owner"


@pytest.mark.asyncio
async def test_same_current_booking_amount_updates_contact_without_rewriting_original_request():
    original = intake()
    with (
        patch.object(calendar_files, "_load_calendar_intake_file", AsyncMock(return_value=original)),
        patch.object(calendar_files, "_appointment_classification", return_value=("main_street", "working_capital")),
        patch.object(calendar_files, "_persist_appointment_intake_classification", AsyncMock()),
    ):
        result = await calendar_files._apply_calendar_booking_data(
            SimpleNamespace(get=AsyncMock(return_value=None)), appointment(), SimpleNamespace(role=Role.LOAN_EXEC),
        )
    assert result.status == "completed"
    assert original.full_name == "Updated Owner"
    assert original.requested_loan_amount == 70_000
    assert original.intake_state["current_funding_request"]["requested_amount"] == "200000.00"


@pytest.mark.asyncio
async def test_booking_does_not_replace_existing_loan_amount():
    booking = appointment()
    booking.converted_intake_id = None
    booking.linked_loan_id = uuid4()
    loan = SimpleNamespace(id=booking.linked_loan_id, client_id=uuid4(), amount=150_000, address="Original address")
    with patch.object(calendar_files, "_load_calendar_loan_file", AsyncMock(return_value=loan)):
        result = await calendar_files._apply_calendar_booking_data(
            SimpleNamespace(get=AsyncMock(return_value=None)), booking, SimpleNamespace(role=Role.LOAN_EXEC),
        )
    assert result.status == "completed"
    assert loan.amount == 150_000


@pytest.mark.asyncio
async def test_booking_comparison_uses_current_intake_request():
    rows = await calendar_files._appointment_booking_data_review(
        SimpleNamespace(get=AsyncMock(return_value=intake())), appointment(), None,
    )
    request = next(row for row in rows if row.field == "requested_amount")
    assert request.status == "matches"
    assert float(request.current_value) == 200_000
