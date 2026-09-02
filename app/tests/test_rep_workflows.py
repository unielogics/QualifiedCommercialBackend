from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.dealer_os.router import (
    _apply_sms_consent_to_rep_contacts,
    _global_search_appointment_access_filter,
    _global_search_contact_access_filter,
    _global_search_file_access_filter,
    _rep_inbox_access_filter,
    _search_context,
)
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
from app.enums import Role


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


def test_file_sms_consent_enables_an_existing_rep_conversation() -> None:
    consent_at = datetime(2026, 9, 1, 18, 30, tzinfo=UTC)
    previous_opt_out = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    contact = SimpleNamespace(
        sms_transactional_consented_at=None,
        sms_marketing_consented_at=None,
        sms_consent_meta=None,
        sms_opted_out_at=previous_opt_out,
    )
    meta = {"method": "rep_attested", "captured_at": consent_at.isoformat()}

    _apply_sms_consent_to_rep_contacts(
        [contact],
        transactional=True,
        marketing=False,
        consent_at=consent_at,
        meta=meta,
    )

    assert contact.sms_transactional_consented_at == consent_at
    assert contact.sms_marketing_consented_at is None
    assert contact.sms_consent_meta == meta
    assert contact.sms_opted_out_at is None


def test_field_desk_inbox_filter_is_owner_only_for_every_staff_role() -> None:
    user_id = uuid4()

    clause = _rep_inbox_access_filter(SimpleNamespace(id=user_id))

    assert clause.right.value == user_id
    assert "owner_user_id" in str(clause.left)


def test_global_search_keeps_rep_files_and_bookings_owner_scoped() -> None:
    user_id = uuid4()
    user = SimpleNamespace(id=user_id, role=Role.FIELD_REP)

    file_clause = _global_search_file_access_filter(user)
    appointment_clause = _global_search_appointment_access_filter(user)

    assert "owner_user_id" in str(file_clause)
    assert user_id.hex in str(file_clause.compile(compile_kwargs={"literal_binds": True}))
    assert "booked_by_user_id" in str(appointment_clause)
    assert appointment_clause.right.value == user_id


def test_global_search_keeps_assigned_contacts_available_to_reps() -> None:
    user_id = uuid4()
    clause = _global_search_contact_access_filter(
        SimpleNamespace(id=user_id, role=Role.FIELD_REP)
    )

    rendered = str(clause.compile(compile_kwargs={"literal_binds": True}))
    assert "dos_rep_contacts.owner_user_id" in rendered
    assert "dos_rep_contact_assignments" in rendered
    assert user_id.hex in rendered


def test_global_search_team_roles_retain_team_scope() -> None:
    user = SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN)

    assert "archived_at IS NULL" in str(_global_search_file_access_filter(user))
    assert _global_search_contact_access_filter(user) is True
    assert _global_search_appointment_access_filter(user) is True


def test_global_search_context_deduplicates_blank_values() -> None:
    assert _search_context("Client", "", None, "Client", "File") == "Client · File"
