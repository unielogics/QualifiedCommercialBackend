"""The appointment row every booking gets, whatever surface it came from.

Public-page and AI-intake bookings used to create a bare CalendarEvent, which
the calendar could only show as a grey "internal" event: no CRM workspace, no
outcome, no file. "The calendar creates the right file based on origin" needs
a row the outcome flow can act on, so these bookings now get a
DealerRepAppointment like a rep booking does — with `origin` saying where it
came from, and for intakes the intake pre-linked as the file.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking_settings import BookingSettings
from app.models.event import CalendarEvent
from app.models.user import User

from ..models import DealerRepAppointment
from . import consent_delivery

GENERAL_PROGRAM_KEY = "general_funding_discussion"
GENERAL_PROGRAM_NAME = "General funding discussion / Not decided yet"


async def create_booking_appointment(
    db: AsyncSession,
    *,
    event: CalendarEvent,
    host: User,
    booking: BookingSettings,
    origin: str,
    invitee_name: str,
    invitee_email: str | None,
    invitee_phone: str | None,
    company: str | None = None,
    notes: str | None = None,
    program_key: str | None = None,
    program_name: str | None = None,
    requested_amount: str | None = None,
    full_address: str | None = None,
    kind: str = "intro_call",
    booked_by_user_id: UUID | None = None,
    converted_intake_id: UUID | None = None,
    contact_source: str = "public_booking",
) -> DealerRepAppointment:
    """Open the appointment row for a booking that has none, and point the
    calendar event at it. Flushes, never commits."""
    # The contact upsert lives with the rest of the field-desk CRM helpers.
    from ..router import _ensure_rep_contact

    phone = consent_delivery.normalize_phone(invitee_phone)
    email = (invitee_email or "").strip().lower() or None
    appt = DealerRepAppointment(
        dealer_id=None,
        origin=origin,
        owner_user_id=host.id,
        calendar_event_id=event.id,
        kind=kind,
        title=(event.title or f"Call with {invitee_name}")[:200],
        starts_at=event.starts_at,
        duration_min=event.duration_min or booking.duration_min,
        timezone=booking.timezone,
        invitee_name=invitee_name.strip()[:160],
        invitee_email=email,
        invitee_phone=phone,
        company=(company or "").strip()[:180] or None,
        program_key=(program_key or GENERAL_PROGRAM_KEY)[:64],
        program_name=(program_name or GENERAL_PROGRAM_NAME)[:180],
        requested_amount=(requested_amount or "").strip()[:40] or None,
        full_address=(full_address or "").strip()[:500] or None,
        notes=notes,
        status="pending",
        crm_status="scheduled",
        booked_by_user_id=booked_by_user_id,
        converted_intake_id=converted_intake_id,
    )
    db.add(appt)
    await db.flush()
    # The event now belongs to the appointment: the calendar lists it as one,
    # deep links resolve, and the workspace can apply an outcome to it.
    event.external_ref_kind = "dealer_rep_appointment"
    event.external_ref_id = str(appt.id)
    if email or phone:
        contact = await _ensure_rep_contact(
            db,
            owner_user_id=host.id,
            dealer_id=None,
            full_name=invitee_name,
            company=company,
            email=email,
            phone_e164=phone,
            source=contact_source,
        )
        appt.contact_id = contact.id
    await db.flush()
    return appt
