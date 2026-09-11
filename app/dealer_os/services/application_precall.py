"""Pre-call preparation backed by an AI Intake application profile.

Field Desk bookings retain their DealerBusiness workflow in ``precall.py``.
This module gives direct booking pages and explicitly opted-in calendar
appointments the same room/reminder behavior without manufacturing a dealer
record or merging a person based on contact details.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import CalendarEventStatus
from app.models.application_profile import ApplicationOwner, ApplicationProfile
from app.models.booking_notification import BookingNotification, BookingNotificationReminder
from app.models.booking_settings import BookingSettings
from app.models.bucket import BucketUploadLink
from app.models.client import Client
from app.models.event import CalendarEvent
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.services import application_profiles as profiles
from app.services import message_render
from app.services.email import ses_client
from app.services.notifications import notify_users
from app.services.sms import optout

from . import client_room, consent_delivery, precall

log = logging.getLogger(__name__)

VARIANTS = frozenset({"dealer", "real_estate", "main_street", "mca_refinance"})


@dataclass
class ApplicationDraftResult:
    intake: PublicUnderwritingIntake
    profile: ApplicationProfile
    room: object
    created: bool


def _room_url(token: str) -> str:
    from .client_room import room_url

    return room_url(token)


async def _room_link(db: AsyncSession, intake: PublicUnderwritingIntake) -> BucketUploadLink | None:
    if intake.bucket_upload_link_id:
        link = await db.get(BucketUploadLink, intake.bucket_upload_link_id)
        if link is not None and link.status == "active":
            return link
    return await client_room.active_link(db, intake.bucket_id)


async def room_for_intake(
    db: AsyncSession, intake: PublicUnderwritingIntake, *, passcode: str | None = None
):
    from .client_room import ClientRoom

    link = await _room_link(db, intake)
    if link is None:
        raise RuntimeError("The AI Intake has no active application room")
    return ClientRoom(link=link, url=_room_url(link.token), passcode=passcode)


def _isolated_client(
    *, host: User, notice: BookingNotification, company: str | None, variant: str, data: dict
) -> Client:
    source = "booking_preparation"
    return Client(
        name=(notice.invitee_name or "Applicant").strip(),
        email=(notice.invitee_email or "").strip().lower() or None,
        phone=notice.invitee_phone,
        referral_source=source,
        originating_agent_id=host.id,
        current_agent_id=host.id,
        source_channel=source,
        lead_source="other",
        lead_temperature="warm",
        financing_support_needed="yes",
        relationship_context="new_lead",
        client_experience_mode="self_directed",
        client_experience_mode_reason=source,
        client_experience_mode_locked_by="firm",
        lead_intake={
            "source": source,
            "business_name": company,
            "precall_variant": variant,
            **data,
        },
    )


async def create_draft_for_booking(
    db: AsyncSession,
    *,
    notice: BookingNotification,
    event: CalendarEvent,
    booking: BookingSettings,
    host: User,
    request,
    appointment=None,
    variant: str | None = None,
    company: str | None = None,
    notes: str | None = None,
    application_data: dict | None = None,
) -> ApplicationDraftResult:
    """Create or return the appointment's explicit AI Intake target.

    No email/name/phone matching occurs here. Idempotency is the persisted
    notice/appointment target, so retries can never create a second file.
    """
    existing_id = (
        notice.precall_intake_id
        or getattr(appointment, "precall_intake_id", None)
        or (
            getattr(appointment, "converted_intake_id", None)
            if getattr(appointment, "origin", None) == "intake"
            else None
        )
    )
    if existing_id:
        intake = await db.get(PublicUnderwritingIntake, existing_id)
        if intake is None:
            raise RuntimeError("The linked AI Intake preparation file no longer exists")
        profile = (
            await db.execute(select(ApplicationProfile).where(ApplicationProfile.intake_id == intake.id))
        ).scalar_one_or_none()
        if profile is None:
            profile = await profiles.resolve_profile(db, "intake", intake.id, host)
        notice.precall_dealer_id = None
        notice.precall_intake_id = intake.id
        if appointment is not None:
            appointment.precall_intake_id = intake.id
        await db.flush()
        return ApplicationDraftResult(
            intake=intake,
            profile=profile,
            room=await room_for_intake(db, intake),
            created=False,
        )

    selected_variant = (variant or booking.precall_default_variant or "main_street").strip().lower()
    allowed = set(booking.precall_allowed_variants or VARIANTS)
    if selected_variant not in VARIANTS or selected_variant not in allowed:
        raise ValueError("The selected pre-call vertical is not enabled for this booking page")

    data = dict(application_data or {})
    client = _isolated_client(
        host=host,
        notice=notice,
        company=company,
        variant=selected_variant,
        data=data,
    )
    db.add(client)
    await db.flush()

    from app.routers.dealer_ai_intake import AdminLeadCreate, _create_admin_ai_lead_core

    pin = f"{secrets.randbelow(900000) + 100000}"
    requested_amount = data.get("requested_amount")
    try:
        amount_value = float(requested_amount) if requested_amount not in (None, "") else None
    except (TypeError, ValueError):
        amount_value = None
    result = await _create_admin_ai_lead_core(
        AdminLeadCreate(
            variant=selected_variant,
            full_name=notice.invitee_name,
            email=notice.invitee_email,
            phone=notice.invitee_phone,
            business_name=company,
            investor_name=company if selected_variant == "real_estate" else None,
            requested_amount=amount_value,
            loan_purpose=notice.program_name,
            intent="working_capital" if selected_variant == "main_street" else None,
            industry="other" if selected_variant == "main_street" else None,
            notify_client=False,
            force_new=True,
            secure_room_pin=pin,
        ),
        request=request,
        user=host,
        db=db,
        commit=False,
        client_override=client,
    )
    intake = await db.get(PublicUnderwritingIntake, result.intake.id)
    if intake is None:
        raise RuntimeError("AI Intake creation did not return a persisted intake")
    state = dict(intake.intake_state or {})
    state["booking_preparation"] = {
        "event_id": str(event.id),
        "notification_id": str(notice.id),
        "notes": notes,
        **data,
    }
    intake.intake_state = state
    profile = await profiles.resolve_profile(db, "intake", intake.id, host)
    profile.is_draft = True
    owners = await profiles.owner_rows(db, profile)
    for owner in owners:
        if isinstance(owner, ApplicationOwner) and owner.backfill_needs_review:
            owner.ownership_pct = None
            owner.backfill_needs_review = False
    profile.backfill_needs_review = False

    notice.precall_dealer_id = None
    notice.precall_intake_id = intake.id
    notice.precall_application_data = data
    if appointment is not None:
        appointment.precall_intake_id = intake.id
        appointment.precall_application_data = data
    await profiles.log_profile_action(
        db,
        profile,
        host,
        "precall.draft_created",
        "AI Intake draft created from a booked appointment",
        target_type="appointment",
        metadata={"event_id": str(event.id), "variant": selected_variant},
    )
    await db.flush()
    return ApplicationDraftResult(
        intake=intake,
        profile=profile,
        room=await room_for_intake(db, intake, passcode=pin),
        created=True,
    )


async def readiness(db: AsyncSession, profile: ApplicationProfile) -> precall.Readiness:
    rows = await profiles.owner_rows(db, profile)
    states: list[precall.OwnerState] = []
    total = round(sum(float(owner.ownership_pct or 0) for owner in rows), 2)
    ownership_complete = bool(rows) and abs(total - 100.0) < 0.005
    contact_complete = True
    required_count = 0
    done_count = 0
    all_credit_done = True
    for owner in rows:
        required = float(owner.ownership_pct or 0) >= precall.OWNER_CREDIT_THRESHOLD
        has_email = bool((owner.email or "").strip()) and "@" in (owner.email or "")
        has_phone = consent_delivery.normalize_phone(owner.phone) is not None
        status = "not_required"
        if required:
            required_count += 1
            if not (has_email and has_phone):
                contact_complete = False
            if owner.credit_pulled_at is not None:
                status = "done"
                done_count += 1
            elif owner.invite_token_hash:
                status = "sent"
                all_credit_done = False
            else:
                status = "todo"
                all_credit_done = False
        states.append(
            precall.OwnerState(
                id=owner.id,
                first_name=owner.first_name,
                last_name=owner.last_name,
                ownership_pct=float(owner.ownership_pct) if owner.ownership_pct is not None else None,
                is_primary=bool(owner.is_primary),
                required=required,
                has_email=has_email,
                has_phone=has_phone,
                credit_status=status,
                editable=owner.credit_pulled_at is None and not owner.invite_token_hash,
            )
        )
    state = await profiles.verification_state(db, profile)
    bank_complete = bool(state.business_banking_complete)
    if state.bank_connection_count:
        bank_detail = f"{state.bank_connection_count} institution{'s' if state.bank_connection_count != 1 else ''} connected"
    elif bank_complete:
        bank_detail = f"{state.bank_statement_months} statement month{'s' if state.bank_statement_months != 1 else ''} approved"
    else:
        bank_detail = ""
    return precall.Readiness(
        ownership_complete=ownership_complete,
        ownership_total=total,
        contact_complete=contact_complete,
        owners=states,
        bank_complete=bank_complete,
        bank_detail=bank_detail,
        credit_complete=ownership_complete and contact_complete and required_count > 0 and all_credit_done,
        credit_required=required_count,
        credit_done=done_count,
    )


async def schedule(
    db: AsyncSession,
    *,
    notice: BookingNotification,
    booking: BookingSettings,
    event: CalendarEvent,
    timezone_name: str | None = None,
) -> list[BookingNotificationReminder]:
    if not booking.precall_enabled:
        return []
    rows: list[BookingNotificationReminder] = []
    now = datetime.now(UTC)
    timezone = precall._tz(timezone_name or booking.timezone)
    for key, channel, due in precall._plan_steps(booking, notice, event.starts_at, now=now, tz=timezone):
        row = BookingNotificationReminder(
            booking_notification_id=notice.id,
            kind="precall",
            step_key=key,
            channel=channel,
            minutes_before=precall.STEP_MARKERS[key],
            due_at=due,
        )
        db.add(row)
        rows.append(row)
    await db.flush()
    return rows


async def mark_complete(
    db: AsyncSession,
    *,
    notice: BookingNotification,
    profile: ApplicationProfile,
    event: CalendarEvent | None,
) -> None:
    if notice.precall_completed_at is not None:
        return
    notice.precall_completed_at = datetime.now(UTC)
    for row in await precall._pending_rows(db, notice):
        row.status = "skipped"
        row.error = "precall_complete"
    recipients = {value for value in (notice.booked_by_user_id, event.owner_user_id if event else None) if value}
    if recipients:
        try:
            await notify_users(
                db,
                recipient_ids=recipients,
                event_type="precall_ready",
                category="calendar",
                priority="high",
                title=f"{notice.invitee_name} is ready for the call",
                body="Owners, bank evidence, and owner credit authorizations are complete.",
                target_type="application_profile",
                target_id=str(profile.id),
                deep_link=f"/admin/ai-underwriter-leads?lead={profile.intake_id}&view=underwriting",
                meta={"profile_id": str(profile.id), "booking_notification_id": str(notice.id)},
                email=True,
                push=True,
            )
        except Exception:  # noqa: BLE001
            log.exception("AI Intake pre-call ready notification failed profile=%s", profile.id)
    await profiles.log_profile_action(
        db, profile, None, "precall.completed", "Pre-call preparation completed"
    )


async def on_progress(db: AsyncSession, profile: ApplicationProfile, *, commit: bool = True) -> bool:
    if not profile.intake_id:
        return False
    notices = list(
        (
            await db.execute(
                select(BookingNotification).where(
                    BookingNotification.precall_intake_id == profile.intake_id,
                    BookingNotification.precall_completed_at.is_(None),
                )
            )
        ).scalars().all()
    )
    if not notices:
        return False
    ready = await readiness(db, profile)
    if not ready.complete:
        return False
    for notice in notices:
        event = await db.get(CalendarEvent, notice.event_id)
        await mark_complete(db, notice=notice, profile=profile, event=event)
    if commit:
        await db.commit()
    else:
        await db.flush()
    return True


def status_for(notice: BookingNotification, ready: precall.Readiness | None, enabled: bool) -> str:
    if not enabled or not notice.precall_intake_id:
        return "disabled"
    if notice.precall_completed_at is not None or (ready is not None and ready.complete):
        return "complete"
    if notice.precall_stopped_at is not None:
        return "stopped"
    return "in_progress"


async def dispatch_row(
    db: AsyncSession,
    *,
    reminder: BookingNotificationReminder,
    notice: BookingNotification,
    event: CalendarEvent,
    booking: BookingSettings,
    host: User,
    now: datetime,
) -> bool:
    intake = await db.get(PublicUnderwritingIntake, notice.precall_intake_id) if notice.precall_intake_id else None
    profile = (
        await db.execute(select(ApplicationProfile).where(ApplicationProfile.intake_id == notice.precall_intake_id))
    ).scalar_one_or_none() if notice.precall_intake_id else None
    if intake is None or profile is None:
        reminder.status = "cancelled"
        reminder.error = "draft_missing"
        return False
    if notice.precall_stopped_at is not None or not booking.precall_enabled:
        reminder.status = "cancelled"
        reminder.error = notice.precall_stop_reason or "host_disabled"
        return False
    if event.status == CalendarEventStatus.CANCELLED or event.starts_at <= now + timedelta(hours=precall.FINAL_CUTOFF_HOURS):
        reminder.status = "cancelled"
        reminder.error = "call_started" if event.status != CalendarEventStatus.CANCELLED else "cancelled"
        return False
    ready = await readiness(db, profile)
    if ready.complete:
        await mark_complete(db, notice=notice, profile=profile, event=event)
        reminder.status = "skipped"
        reminder.error = "precall_complete"
        return False
    room = await room_for_intake(db, intake)
    values = precall.template_values(
        notice=notice,
        event=event,
        booking=booking,
        host=host,
        dealer=SimpleNamespace(name=intake.business_name or intake.full_name),
        room_link=room.url,
        ready=ready,
        stop_link=precall.stop_url(notice) if reminder.channel == "email" else "",
        timezone_name=booking.timezone,
    )
    config = precall.step_config(booking, reminder.step_key or "nudge_1")
    if reminder.channel == "sms":
        phone = notice.invitee_phone or ""
        if not notice.sms_consent or not phone:
            reminder.status = "skipped"
            reminder.error = "no_consent"
            return False
        if await optout.is_opted_out(db, phone):
            await precall.stop_sequence(db, notice, reason="sms_stop", channels=("sms",))
            reminder.status = "cancelled"
            reminder.error = "sms_stop"
            return False
        local_hour = now.astimezone(precall._tz(booking.timezone)).hour
        if local_hour < precall.QUIET_START_HOUR or local_hour >= precall.QUIET_END_HOUR:
            return False
        if await precall._recent_automated_sms(db, phone, hours=precall.SMS_SPACING_HOURS):
            reminder.due_at = now + timedelta(hours=1)
            return False
        body = message_render.with_stop_notice(message_render.render(config.get("sms"), values))
        try:
            result = await consent_delivery.send_sms_guarded(
                db, phone, body, context=f"precall_{reminder.step_key}"
            )
        except Exception:
            log.exception("AI Intake pre-call SMS failed notification=%s", notice.id)
            reminder.status = "failed"
            reminder.sent_at = now
            reminder.error = "sms_provider_exception"
            return False
        reminder.rendered_body = body
    else:
        if not notice.invitee_email:
            reminder.status = "skipped"
            reminder.error = "no_email"
            return False
        subject = message_render.render(config.get("email_subject"), values) or "Before your call"
        body = message_render.render_lines(config.get("email_body"), values)
        footer = precall._stop_footer(notice, booking, values)
        if footer:
            body = f"{body}\n\n{footer}"
        result = await asyncio.to_thread(
            ses_client.send_email,
            to_email=notice.invitee_email,
            subject=subject,
            body_text=body,
        )
        reminder.rendered_body = f"{subject}\n\n{body}"
    reminder.status = "sent" if result.ok else "failed"
    reminder.sent_at = now
    reminder.provider_message_id = getattr(result, "message_id", None)
    reminder.error = None if result.ok else (result.detail or "")[:1000]
    if not result.ok:
        notice.last_error = (result.detail or "")[:1000]
    await profiles.log_profile_action(
        db,
        profile,
        None,
        "precall.step_sent",
        f"Pre-call {reminder.step_key} {reminder.channel}: {'accepted' if result.ok else 'failed'}",
        metadata={"channel": reminder.channel, "provider_message_id": reminder.provider_message_id},
    )
    return bool(result.ok)


async def send_kit(
    db: AsyncSession,
    *,
    notice: BookingNotification,
    event: CalendarEvent,
    booking: BookingSettings,
    host: User,
    intake: PublicUnderwritingIntake,
    profile: ApplicationProfile,
    channels: tuple[str, ...] = ("email", "sms"),
    pin: str | None = None,
) -> dict[str, bool]:
    ready = await readiness(db, profile)
    room = await room_for_intake(db, intake, passcode=pin)
    values = precall.template_values(
        notice=notice,
        event=event,
        booking=booking,
        host=host,
        dealer=SimpleNamespace(name=intake.business_name or intake.full_name),
        room_link=room.url,
        ready=ready,
        pin=pin,
        stop_link=precall.stop_url(notice),
    )
    out = {"email": False, "sms": False}
    if "email" in channels and notice.invitee_email:
        body = precall.precall_block(booking, values)
        body = f"{body}\n\n{precall._stop_footer(notice, booking, values)}"
        try:
            result = await asyncio.to_thread(
                ses_client.send_email,
                to_email=notice.invitee_email,
                subject=message_render.render("Your secure room for your call with {rep}", values),
                body_text=body,
            )
            out["email"] = bool(result.ok)
            if not result.ok:
                notice.last_error = (result.detail or "")[:1000]
        except Exception:  # noqa: BLE001
            log.exception("AI Intake pre-call kit email failed notification=%s", notice.id)
            notice.last_error = "email_provider_exception"
    if "sms" in channels and notice.sms_consent and notice.invitee_phone:
        if not await optout.is_opted_out(db, notice.invitee_phone):
            template = (
                precall.message_text(booking, "pin_sms")
                if pin
                else precall.step_config(booking, "nudge_1").get("sms")
            )
            body = message_render.with_stop_notice(message_render.render(template, values))
            try:
                result = await consent_delivery.send_sms_guarded(
                    db, notice.invitee_phone, body, context="precall_kit"
                )
                out["sms"] = bool(result.ok)
                if not result.ok:
                    notice.last_error = (result.detail or "")[:1000]
            except Exception:  # noqa: BLE001
                log.exception("AI Intake pre-call kit SMS failed notification=%s", notice.id)
                notice.last_error = "sms_provider_exception"
    await profiles.log_profile_action(
        db,
        profile,
        None,
        "precall.kit_sent",
        "Pre-call room kit sent",
        metadata={**out, "rotated_pin": bool(pin), "notification_id": str(notice.id)},
    )
    return out


async def rotate_passcode(db: AsyncSession, intake: PublicUnderwritingIntake):
    from app.routers.buckets import _hash_passcode

    from .client_room import ClientRoom

    link = await _room_link(db, intake)
    if link is None:
        raise RuntimeError("The AI Intake has no active application room")
    pin = f"{secrets.randbelow(900000) + 100000}"
    link.passcode_hash = _hash_passcode(pin)
    link.encrypted_passcode = None
    link.passcode_encryption_provider = None
    link.passcode_set_by_client_at = None
    link.expires_at = None
    await db.flush()
    return ClientRoom(link=link, url=_room_url(link.token), passcode=pin)
