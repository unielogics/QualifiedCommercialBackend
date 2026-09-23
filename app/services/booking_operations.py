"""Durable provider effects for booking creation and lifecycle changes.

Routes commit the local appointment/calendar state and these rows together,
then return.  A best-effort wake-up and the scheduler both drain the same
ledger.  Every provider effect is claimed and committed as ``processing``
before any network call, so a crash can leave an action-required effect but
can never blindly repeat an SES/SMS send with an ambiguous outcome.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.models import DealerRepAppointment, DealerRepAppointmentActivity
from app.dealer_os.services import consent_delivery
from app.models.booking_notification import (
    BookingDeliveryEffect,
    BookingDeliveryOperation,
    BookingNotification,
)
from app.models.booking_settings import BookingSettings
from app.models.event import CalendarEvent
from app.models.user import User
from app.services import booking_metrics, booking_notify, booking_reminders
from app.services.notifications import deliver_deferred_notification_email
from app.services.team_calendar import effective_booking_settings

log = logging.getLogger(__name__)

_TERMINAL_EFFECT_STATUSES = {
    "sent",
    "failed",
    "unavailable",
    "skipped",
    "action_required",
}
_ACTION_REQUIRED_EFFECT_STATUSES = {
    "failed",
    "unavailable",
    "action_required",
    "processing",
}
_STALE_CLAIM_AFTER = timedelta(minutes=10)
_MAX_OPERATION_ATTEMPTS = 8

_EFFECT_PRIORITY = {
    # Google must mint/persist Meet before any client-facing confirmation.
    "google": 10,
    "host_email": 20,
    "client_email": 30,
    "old_client_email": 35,
    "rep_calendar": 40,
    "rebooking_email": 45,
    "client_sms": 50,
    "pin_delivery": 60,
    "document_request": 70,
}


def record_idempotency_replay(
    *, operation_type: str, appointment_id: UUID, surface: str
) -> None:
    booking_metrics.emit(
        "booking_operation_idempotency_replay",
        operation_type=operation_type,
        appointment_id=str(appointment_id),
        surface=surface,
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _retry_delay(attempt_count: int) -> timedelta:
    # Fast first recovery, then bounded exponential backoff.
    seconds = min(15 * (2 ** max(0, attempt_count - 1)), 15 * 60)
    return timedelta(seconds=seconds)


def aggregate_operation_status(*, has_pending: bool, has_failed: bool) -> str:
    """Pending work always wins so retryable siblings are never stranded."""

    if has_pending:
        return "pending"
    if has_failed:
        return "action_required"
    return "completed"


def stale_effect_recovery(effect_key: str) -> str:
    """Classify an in-flight effect found after a worker crash."""

    return "retry" if effect_key == "google" else "action_required"


def lifecycle_idempotency_key(
    appointment: DealerRepAppointment,
    event: CalendarEvent | None,
) -> str:
    """Stable key for the provider-visible final state of a PATCH.

    The API predates explicit PATCH idempotency keys. A browser retry after a
    response timeout therefore arrives with a different *transition* (the
    reschedule already happened, so it looks like an update) but the exact
    same final state. Hashing that state makes both requests reuse one durable
    operation and prevents duplicate revised invitations.
    """

    state = {
        "appointment_id": str(appointment.id),
        "title": appointment.title,
        "starts_at": _iso(appointment.starts_at),
        "duration_min": appointment.duration_min,
        "timezone": appointment.timezone,
        "invitee_name": appointment.invitee_name,
        "invitee_email": appointment.invitee_email,
        "invitee_phone": appointment.invitee_phone,
        "company": appointment.company,
        # join_url is provider-generated for Google Meet and may appear between
        # an original request timing out and its retry. It is deliberately not
        # part of the client-operation identity.
        "meeting_mode": appointment.meeting_mode,
        "location": appointment.location,
        "notes": appointment.notes,
        "status": appointment.status,
        "event": (
            {
                "id": str(event.id),
                "title": event.title,
                "starts_at": _iso(event.starts_at),
                "duration_min": event.duration_min,
                "who": event.who,
                "description": event.description,
                "status": str(event.status),
            }
            if event is not None
            else None
        ),
    }
    digest = hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"booking:lifecycle:{appointment.id}:{digest}"


def creation_idempotency_key(
    *,
    owner_user_id: UUID,
    starts_at: datetime,
    duration_min: int,
    invitee_email: str | None,
    invitee_phone: str | None,
    origin: str,
    scope: str,
    caller_token: str | None = None,
) -> str:
    """Fingerprint a booking submission with a legacy-safe fallback.

    New clients send a stable caller token. Older clients still receive a
    deterministic key from the canonical booking identity, preventing a
    response-timeout retry from creating a second local/provider operation.
    """

    clean_token = (caller_token or "").strip()
    state = (
        {
            "owner_user_id": str(owner_user_id),
            "origin": origin,
            "scope": scope,
            "caller_token": clean_token,
        }
        if clean_token
        else {
            "owner_user_id": str(owner_user_id),
            "starts_at": _iso(starts_at),
            "duration_min": int(duration_min),
            "invitee_email": (invitee_email or "").strip().lower(),
            "invitee_phone": (invitee_phone or "").strip(),
            "origin": origin,
            "scope": scope,
        }
    )
    digest = hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    # creation_idempotency_key is varchar(80).
    return f"booking:create:{digest}"


def creation_request_fingerprint(data: dict[str, Any]) -> str:
    """Hash the complete canonical caller request behind an idempotency key."""

    digest = hashlib.sha256(
        json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    return digest


def creation_replay_matches(
    appointment: DealerRepAppointment,
    *,
    owner_user_id: UUID,
    dealer_id: UUID | None,
    starts_at: datetime,
    duration_min: int,
    invitee_email: str | None,
    invitee_phone: str | None,
    actor_user_id: UUID | None = None,
    require_actor_match: bool = False,
    request_fingerprint: str | None = None,
) -> bool:
    """Verify that a caller token is replaying the same booking identity.

    Caller-provided idempotency tokens intentionally do not include mutable
    request fields in their hash.  Never return the booking (and especially a
    secure room URL) until those fields have been compared with the original
    row.  This also protects shared-calendar surfaces where different agents
    could otherwise reuse the same token.
    """

    def utc_minute(value: datetime) -> datetime:
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).replace(second=0, microsecond=0)

    expected_email = (invitee_email or "").strip().lower() or None
    actual_email = (appointment.invitee_email or "").strip().lower() or None
    expected_phone = consent_delivery.normalize_phone(invitee_phone)
    actual_phone = consent_delivery.normalize_phone(appointment.invitee_phone)
    if require_actor_match and appointment.booked_by_user_id != actor_user_id:
        return False
    if request_fingerprint is not None and (
        (appointment.precall_application_data or {}).get(
            "creation_request_fingerprint"
        )
        != request_fingerprint
    ):
        return False
    return all(
        (
            appointment.owner_user_id == owner_user_id,
            appointment.dealer_id == dealer_id,
            utc_minute(appointment.starts_at) == utc_minute(starts_at),
            int(appointment.duration_min) == int(duration_min),
            actual_email == expected_email,
            actual_phone == expected_phone,
        )
    )


def manual_retry_idempotency_key(
    appointment: DealerRepAppointment,
    event: CalendarEvent,
    *,
    effect_key: str,
    delivery_payload: dict[str, Any] | None = None,
) -> str:
    state = {
        "appointment_state": lifecycle_idempotency_key(appointment, event),
        "effect_key": effect_key,
        "delivery_payload": delivery_payload or {},
    }
    digest = hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"booking:retry:{digest}"


async def enqueue_manual_retry(
    db: AsyncSession,
    *,
    appointment: DealerRepAppointment,
    event: CalendarEvent,
    actor_user_id: UUID,
    effect_key: str,
    delivery_payload: dict[str, Any] | None = None,
) -> tuple[BookingDeliveryOperation, BookingDeliveryEffect]:
    """Queue one selected provider retry under the appointment row lock.

    The route locks the appointment first. An already-running matching effect
    is returned unchanged, making double-clicks and response-timeout retries
    safe. A terminal manual attempt is explicitly re-armed; successful sends
    are never repeated.
    """

    if effect_key not in {
        "google",
        "client_email",
        "client_sms",
        "rebooking_email",
    }:
        raise ValueError(f"unsupported manual booking retry: {effect_key}")
    stable_payload = dict(delivery_payload or {})
    active = (
        await db.execute(
            select(BookingDeliveryOperation, BookingDeliveryEffect)
            .join(
                BookingDeliveryEffect,
                BookingDeliveryEffect.operation_id
                == BookingDeliveryOperation.id,
            )
            .where(
                BookingDeliveryOperation.appointment_id == appointment.id,
                BookingDeliveryOperation.status.in_(["pending", "processing"]),
                BookingDeliveryEffect.effect_key == effect_key,
                BookingDeliveryEffect.status.in_(["pending", "processing"]),
            )
            .order_by(BookingDeliveryOperation.created_at.desc())
            .limit(1)
        )
    ).first()
    if active is not None and effect_key == "google" and stable_payload:
        active_payload = active[0].payload or {}
        if any(active_payload.get(key) != value for key, value in stable_payload.items()):
            active = None
    if active is not None:
        operation, effect = active
        record_idempotency_replay(
            operation_type="manual_retry",
            appointment_id=appointment.id,
            surface=effect_key,
        )
        return operation, effect

    key = manual_retry_idempotency_key(
        appointment,
        event,
        effect_key=effect_key,
        delivery_payload=stable_payload,
    )
    operation = (
        await db.execute(
            select(BookingDeliveryOperation)
            .where(BookingDeliveryOperation.idempotency_key == key)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if operation is not None:
        effect = (
            await db.execute(
                select(BookingDeliveryEffect)
                .where(
                    BookingDeliveryEffect.operation_id == operation.id,
                    BookingDeliveryEffect.effect_key == effect_key,
                )
                .with_for_update()
            )
        ).scalar_one()
        record_idempotency_replay(
            operation_type="manual_retry",
            appointment_id=appointment.id,
            surface=effect_key,
        )
        if effect.status in {"pending", "processing", "sent"}:
            return operation, effect
        effect.status = "pending"
        effect.attempt_count = 0
        effect.last_attempt_at = None
        effect.completed_at = None
        effect.provider_message_id = None
        effect.error = None
        operation.status = "pending"
        operation.actor_user_id = actor_user_id
        operation.attempt_count = 0
        operation.next_attempt_at = datetime.now(UTC)
        operation.claimed_at = None
        operation.completed_at = None
        operation.last_error = None
        operation.payload = {
            **stable_payload,
            "manual_retry": True,
            "sequence": int(datetime.now(UTC).timestamp()),
        }
        await db.flush()
        return operation, effect

    operation = BookingDeliveryOperation(
        appointment_id=appointment.id,
        event_id=event.id,
        actor_user_id=actor_user_id,
        operation_type=(
            "cancel"
            if appointment.status == "cancelled" or appointment.archived_at is not None
            else "update"
        ),
        idempotency_key=key,
        status="pending",
        payload={
            **stable_payload,
            "manual_retry": True,
            "sequence": int(datetime.now(UTC).timestamp()),
        },
        next_attempt_at=datetime.now(UTC),
    )
    db.add(operation)
    await db.flush()
    effect = BookingDeliveryEffect(
        operation_id=operation.id,
        effect_key=effect_key,
        status="pending",
    )
    db.add(effect)
    await db.flush()
    return operation, effect


async def enqueue(
    db: AsyncSession,
    *,
    appointment: DealerRepAppointment,
    event: CalendarEvent | None,
    actor_user_id: UUID | None,
    operation_type: str,
    old_email: str | None = None,
    old_starts_at: datetime | None = None,
    notification_ids: list[UUID] | None = None,
    idempotency_key: str | None = None,
    delivery_payload: dict[str, Any] | None = None,
) -> BookingDeliveryOperation:
    """Persist one lifecycle operation and its exact provider effects."""

    if operation_type not in {"create", "cancel", "reschedule", "update"}:
        raise ValueError(f"unsupported booking operation: {operation_type}")
    key = idempotency_key or (
        f"booking:{operation_type}:{appointment.id}:{uuid.uuid4().hex}"
    )
    existing = (
        await db.execute(
            select(BookingDeliveryOperation).where(
                BookingDeliveryOperation.idempotency_key == key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        record_idempotency_replay(
            operation_type=operation_type,
            appointment_id=appointment.id,
            surface="delivery_operation",
        )
        return existing

    # A newer edit makes an older not-yet-started edit obsolete. Cancellation
    # supersedes every queued edit; a worker that already started rechecks the
    # appointment status before each remaining effect.
    if operation_type != "create":
        superseded_types = (
            {"create", "update", "reschedule", "cancel"}
            if operation_type == "cancel"
            else {"update", "reschedule"}
        )
        await db.execute(
            update(BookingDeliveryOperation)
            .where(
                BookingDeliveryOperation.appointment_id == appointment.id,
                BookingDeliveryOperation.operation_type.in_(superseded_types),
                BookingDeliveryOperation.status == "pending",
            )
            .values(
                status="superseded",
                next_attempt_at=None,
                completed_at=datetime.now(UTC),
                last_error="superseded_by_newer_operation",
            )
        )

    notice = None
    if appointment.calendar_event_id:
        notice = (
            await db.execute(
                select(BookingNotification).where(
                    BookingNotification.event_id == appointment.calendar_event_id
                )
            )
        ).scalar_one_or_none()
    host = (
        await db.get(User, appointment.owner_user_id)
        if appointment.owner_user_id
        else None
    )
    rep = (
        await db.get(User, appointment.booked_by_user_id)
        if appointment.booked_by_user_id
        else None
    )
    booking = None
    if host is not None:
        booking = (
            await db.execute(
                select(BookingSettings).where(BookingSettings.user_id == host.id)
            )
        ).scalar_one_or_none()
        if booking is not None:
            booking = await effective_booking_settings(db, booking)
    clean_old_email = (old_email or "").strip().lower() or None
    clean_new_email = (appointment.invitee_email or "").strip().lower() or None
    notification_ids = list(dict.fromkeys(notification_ids or []))
    payload: dict[str, Any] = {
        "old_email": clean_old_email,
        "old_starts_at": _iso(old_starts_at),
        "sequence": 0 if operation_type == "create" else int(datetime.now(UTC).timestamp()),
        "notification_ids": [str(value) for value in notification_ids],
        **(delivery_payload or {}),
    }
    operation = BookingDeliveryOperation(
        appointment_id=appointment.id,
        event_id=event.id if event else None,
        actor_user_id=actor_user_id,
        operation_type=operation_type,
        idempotency_key=key,
        status="pending",
        payload=payload,
        next_attempt_at=datetime.now(UTC),
    )
    db.add(operation)
    await db.flush()

    effect_keys: list[str] = []
    if event is not None:
        effect_keys.append("google")
        if (
            operation_type == "create"
            and host is not None
            and host.email
            and (delivery_payload or {}).get("notify_host", True)
        ):
            effect_keys.append("host_email")
        if clean_new_email and (
            operation_type != "create"
            or booking is None
            or booking.confirmation_email_enabled
        ):
            effect_keys.append("client_email")
        if rep is not None and (host is None or rep.id != host.id) and rep.email:
            effect_keys.append("rep_calendar")
        if (
            operation_type != "cancel"
            and clean_old_email
            and clean_old_email != clean_new_email
        ):
            effect_keys.append("old_client_email")
    if notice is not None and notice.invitee_phone and notice.sms_consent and (
        operation_type == "cancel"
        or (
            operation_type == "create"
            and notice.confirmation_sms_status == "pending"
        )
    ):
        effect_keys.append("client_sms")
    if operation_type == "create" and notice is not None:
        # The old BookingNotification dispatcher must not race this operation.
        notice.delivery_next_attempt_at = None
        if notice.precall_dealer_id or notice.precall_intake_id:
            effect_keys.append("pin_delivery")
        if (delivery_payload or {}).get("document_request_dealer_id"):
            effect_keys.append("document_request")
    effect_keys.extend(
        f"rep_notification_{index}" for index, _ in enumerate(notification_ids)
    )
    for effect_key in effect_keys:
        db.add(
            BookingDeliveryEffect(
                operation_id=operation.id,
                effect_key=effect_key,
                status="pending",
            )
        )
    await db.flush()
    return operation


async def find_by_idempotency_key(
    db: AsyncSession, key: str
) -> BookingDeliveryOperation | None:
    return (
        await db.execute(
            select(BookingDeliveryOperation).where(
                BookingDeliveryOperation.idempotency_key == key
            )
        )
    ).scalar_one_or_none()


async def queued_results(
    db: AsyncSession, operation: BookingDeliveryOperation
) -> dict[str, str]:
    effects = list(
        (
            await db.execute(
                select(BookingDeliveryEffect).where(
                    BookingDeliveryEffect.operation_id == operation.id
                )
            )
        )
        .scalars()
        .all()
    )
    results: dict[str, str] = {}
    for effect in effects:
        key = effect.effect_key
        value = (
            "queued"
            if effect.status in {"pending", "processing"}
            else effect.status
        )
        if key == "client_email":
            results["client_email"] = value
        elif key == "client_sms":
            results["client_sms"] = value
        elif key == "rep_calendar":
            results["rep_calendar"] = value
        elif key == "google":
            results["google"] = value
        elif key == "host_email":
            results["host_email"] = value
        elif key == "pin_delivery":
            results["pin_delivery"] = value
        elif key == "document_request":
            results["document_request_delivery"] = value
        elif key.startswith("rep_notification_"):
            results["rep"] = value
    return results


async def _operation_context(
    db: AsyncSession, operation: BookingDeliveryOperation
) -> tuple[
    DealerRepAppointment | None,
    CalendarEvent | None,
    BookingNotification | None,
    User | None,
    User | None,
    BookingSettings | None,
]:
    appointment = await db.get(DealerRepAppointment, operation.appointment_id)
    event = await db.get(CalendarEvent, operation.event_id) if operation.event_id else None
    notice = None
    if event is not None:
        notice = (
            await db.execute(
                select(BookingNotification).where(
                    BookingNotification.event_id == event.id
                )
            )
        ).scalar_one_or_none()
    host = (
        await db.get(User, appointment.owner_user_id)
        if appointment is not None and appointment.owner_user_id
        else None
    )
    rep = (
        await db.get(User, appointment.booked_by_user_id)
        if appointment is not None and appointment.booked_by_user_id
        else None
    )
    booking = None
    if host is not None:
        booking = (
            await db.execute(
                select(BookingSettings).where(BookingSettings.user_id == host.id)
            )
        ).scalar_one_or_none()
        if booking is not None:
            booking = await effective_booking_settings(db, booking)
    return appointment, event, notice, host, rep, booking


async def _initial_delivery_kit(
    db: AsyncSession,
    *,
    notice: BookingNotification | None,
    event: CalendarEvent,
    booking: BookingSettings,
    host: User,
) -> dict[str, Any] | None:
    """Rebuild the exact pre-call confirmation data from durable references.

    The room PIN is already encrypted on its upload-link row.  Reconstructing
    it here avoids placing plaintext credentials in the operation JSON while
    still allowing a worker in a later process to finish the confirmation.
    """

    if notice is None or not (notice.precall_dealer_id or notice.precall_intake_id):
        return None
    from app.dealer_os.services import application_precall, client_room, precall

    room = None
    ready = None
    target = None
    if notice.precall_dealer_id:
        from app.dealer_os.models import DealerBusiness

        dealer = await db.get(DealerBusiness, notice.precall_dealer_id)
        if dealer is not None:
            room = await client_room.get_room(db, dealer)
            ready = await precall.readiness(db, dealer)
            target = dealer
    elif notice.precall_intake_id:
        from app.models.application_profile import ApplicationProfile
        from app.models.public_underwriting_intake import PublicUnderwritingIntake

        intake = await db.get(PublicUnderwritingIntake, notice.precall_intake_id)
        profile = (
            await db.execute(
                select(ApplicationProfile).where(
                    ApplicationProfile.intake_id == notice.precall_intake_id
                )
            )
        ).scalar_one_or_none()
        if intake is not None and profile is not None:
            link = await client_room.active_link(db, intake.bucket_id)
            if link is not None:
                room = await application_precall.room_for_intake(
                    db,
                    intake,
                    passcode=await asyncio.to_thread(client_room.read_passcode, link),
                )
            ready = await application_precall.readiness(db, profile)
            target = SimpleNamespace(name=intake.business_name or intake.full_name)
    if room is None or target is None:
        return None
    pin = room.passcode or await asyncio.to_thread(
        client_room.read_passcode, room.link
    )
    values = precall.template_values(
        notice=notice,
        event=event,
        booking=booking,
        host=host,
        dealer=target,
        room_link=room.url,
        ready=ready,
        pin=pin,
        stop_link=precall.stop_url(notice),
        timezone_name=booking.timezone,
    )
    messages = booking.confirmation_messages or {}
    return {
        "room_url": room.url,
        "pin": pin,
        "block": precall.precall_block(booking, values),
        "email_template": {
            "subject": messages.get("email_subject"),
            "body": messages.get("email_body"),
        },
        "sms_template": precall.message_text(booking, "confirmation_sms"),
        "values": values,
    }


async def _deliver_document_request(
    db: AsyncSession,
    *,
    operation: BookingDeliveryOperation,
    notice: BookingNotification | None,
) -> tuple[str, str | None, str | None]:
    raw_dealer_id = (operation.payload or {}).get("document_request_dealer_id")
    if not raw_dealer_id or notice is None:
        return "skipped", None, "document_request_context_missing"
    try:
        dealer_id = UUID(str(raw_dealer_id))
    except ValueError:
        return "skipped", None, "document_request_dealer_invalid"

    from app.dealer_os.models import DealerBusiness
    from app.dealer_os.services import audit, client_room

    dealer = await db.get(DealerBusiness, dealer_id)
    actor = await db.get(User, operation.actor_user_id) if operation.actor_user_id else None
    room = await client_room.get_room(db, dealer) if dealer is not None else None
    if dealer is None or actor is None or room is None:
        return "unavailable", None, "document_request_context_unavailable"
    pin = await asyncio.to_thread(client_room.read_passcode, room.link)
    purpose = "review the underwriting document request"
    if pin:
        purpose += f" using access code {pin}"
    channel = "email" if notice.invitee_email else "sms"
    delivery = await consent_delivery.deliver_link_checked(
        db,
        channel=channel,
        to_email=notice.invitee_email,
        to_phone=notice.invitee_phone,
        business_name=dealer.name,
        purpose=purpose,
        path=room.url,
        rep_name=actor.name,
    )
    await audit.log_action(
        db,
        dealer.id,
        actor,
        "client_request.underwriting_review_documents",
        "dealer",
        entity_id=dealer.id,
        after={
            "delivered": delivery.ok,
            "email": delivery.email_ok,
            "sms": delivery.sms_ok,
            "purpose": purpose,
            "recipient": notice.invitee_email or notice.invitee_phone or "",
            "channel": channel,
        },
    )
    return (
        ("sent", delivery.provider_message_id, None)
        if delivery.ok
        else ("failed", delivery.provider_message_id, delivery.detail)
    )


async def _run_effect(
    db: AsyncSession,
    operation: BookingDeliveryOperation,
    effect: BookingDeliveryEffect,
) -> tuple[str, str | None, str | None]:
    appointment, event, notice, host, rep, booking = await _operation_context(
        db, operation
    )
    if appointment is None:
        return "skipped", None, "appointment_missing"
    if operation.operation_type != "cancel" and (
        appointment.status == "cancelled" or appointment.archived_at is not None
    ):
        return "skipped", None, "appointment_cancelled"

    key = effect.effect_key
    if key == "google":
        if event is None:
            return "unavailable", None, "calendar_event_missing"
        join = await booking_notify.push_to_google(
            db,
            event,
            invitee_email=appointment.invitee_email,
            invitee_name=appointment.invitee_name,
            rep_email=rep.email if rep else None,
            rep_name=rep.name if rep else None,
            want_meet=bool(
                operation.operation_type != "cancel"
                and booking is not None
                and booking.google_meet_enabled
                and appointment.meeting_mode == "video"
                and not appointment.join_url
            ),
            color_id=(operation.payload or {}).get("google_color_id"),
            send_updates=(operation.payload or {}).get(
                "google_send_updates", "all"
            ),
        )
        if join and not appointment.join_url:
            appointment.join_url = join
            if notice is not None:
                notice.join_url = join
            if event.description and "Join:" not in event.description:
                event.description = f"{event.description}\n\nJoin: {join}"
        needs_meet = bool(
            operation.operation_type != "cancel"
            and booking is not None
            and booking.google_meet_enabled
            and appointment.meeting_mode == "video"
        )
        if event.google_event_id and (not needs_meet or appointment.join_url):
            return "sent", event.google_event_id, None
        if operation.attempt_count < _MAX_OPERATION_ATTEMPTS:
            return (
                "pending",
                event.google_event_id,
                "google_meet_pending" if event.google_event_id else "google_calendar_pending",
            )
        return "action_required", event.google_event_id, "google_calendar_unavailable"

    if event is None or host is None or booking is None:
        return "unavailable", None, "booking_context_unavailable"
    sequence = int((operation.payload or {}).get("sequence") or 0)
    cancel = operation.operation_type == "cancel"
    create = operation.operation_type == "create"
    kit = (
        await _initial_delivery_kit(
            db,
            notice=notice,
            event=event,
            booking=booking,
            host=host,
        )
        if create
        else None
    )
    if key == "host_email":
        result = await asyncio.to_thread(
            booking_notify.notify_host,
            host,
            booking,
            appointment.starts_at,
            invitee_name=appointment.invitee_name,
            invitee_email=appointment.invitee_email or "not provided",
            invitee_phone=appointment.invitee_phone,
            notes=(operation.payload or {}).get("notes") or appointment.notes,
            join_url=appointment.join_url,
        )
        if result is None:
            return "unavailable", None, "host_email_provider_unavailable"
        return (
            ("sent", result.message_id, None)
            if result.ok
            else ("failed", result.message_id, result.detail)
        )
    if key == "client_email":
        if not appointment.invitee_email:
            return "skipped", None, "invitee_email_missing"
        if (
            not cancel
            and appointment.meeting_mode == "video"
            and booking.google_meet_enabled
            and not appointment.join_url
        ):
            exhausted = operation.attempt_count >= _MAX_OPERATION_ATTEMPTS
            if notice is not None and exhausted:
                notice.confirmation_email_status = "failed"
                notice.record_delivery_error("google_meet_url_unavailable")
            return (
                "action_required" if exhausted else "pending",
                None,
                "google_meet_url_unavailable" if exhausted else "waiting_for_google_meet",
            )
        result = await asyncio.to_thread(
            booking_notify.send_invitee_invite,
            host,
            booking,
            event,
            appointment.starts_at,
            invitee_name=appointment.invitee_name,
            invitee_email=appointment.invitee_email,
            join_url=appointment.join_url,
            cancel=cancel,
            sequence=sequence,
            precall_block=kit.get("block") if kit else None,
            template=kit.get("email_template") if kit else None,
            template_values=kit.get("values") if kit else None,
        )
        if notice is not None and not cancel:
            notice.confirmation_email_status = (
                "sent" if result and result.ok else "failed"
            )
        if notice is not None:
            if result and result.ok:
                notice.clear_delivery_error()
            else:
                notice.record_delivery_error(
                    result.detail if result else "email_provider_unavailable"
                )
        return (
            ("sent", result.message_id, None)
            if result and result.ok
            else (
                "failed",
                result.message_id if result else None,
                result.detail if result else "email_provider_unavailable",
            )
        )
    if key == "old_client_email":
        old_email = str((operation.payload or {}).get("old_email") or "").strip()
        old_start_raw = (operation.payload or {}).get("old_starts_at")
        if not old_email or not old_start_raw:
            return "skipped", None, "old_recipient_missing"
        old_start = datetime.fromisoformat(str(old_start_raw))
        result = await asyncio.to_thread(
            booking_notify.send_invitee_invite,
            host,
            booking,
            event,
            old_start,
            invitee_name=appointment.invitee_name,
            invitee_email=old_email,
            join_url=appointment.join_url,
            cancel=True,
            sequence=sequence,
        )
        return (
            ("sent", result.message_id, None)
            if result and result.ok
            else (
                "failed",
                result.message_id if result else None,
                result.detail if result else "email_provider_unavailable",
            )
        )
    if key == "rebooking_email":
        if not appointment.invitee_email:
            return "skipped", None, "invitee_email_missing"
        booking_url = str(
            (operation.payload or {}).get("rebooking_url") or ""
        ).strip()
        if not booking_url:
            return "unavailable", None, "public_booking_link_unavailable"
        from app.services.email.user_mailer import send_as_user

        result = await send_as_user(
            db,
            host.id,
            to_emails=[appointment.invitee_email],
            subject="Let's reschedule your Qualified Commercial appointment",
            body_text=(
                f"Hi {appointment.invitee_name},\n\n"
                "We missed you at the scheduled appointment. "
                "Choose a new time here:\n"
                f"{booking_url}"
            ),
        )
        return (
            ("sent", result.message_id, None)
            if result.ok
            else ("failed", result.message_id, result.detail)
        )
    if key == "rep_calendar":
        if (
            create
            and appointment.meeting_mode == "video"
            and booking.google_meet_enabled
            and not appointment.join_url
        ):
            exhausted = operation.attempt_count >= _MAX_OPERATION_ATTEMPTS
            return (
                "action_required" if exhausted else "pending",
                None,
                "google_meet_url_unavailable" if exhausted else "waiting_for_google_meet",
            )
        result = await asyncio.to_thread(
            booking_notify.send_rep_invite,
            host,
            booking,
            event,
            appointment.starts_at,
            rep=rep,
            join_url=appointment.join_url,
            cancel=cancel,
            sequence=sequence,
        )
        if result is None:
            return "unavailable", None, "representative_email_unavailable"
        return (
            ("sent", result.message_id, None)
            if result.ok
            else ("failed", result.message_id, result.detail)
        )
    if key == "client_sms":
        if notice is None or not notice.invitee_phone or not notice.sms_consent:
            return "skipped", None, "sms_consent_unavailable"
        if create or bool((operation.payload or {}).get("manual_confirmation")):
            if (
                appointment.meeting_mode == "video"
                and booking.google_meet_enabled
                and not appointment.join_url
            ):
                exhausted = operation.attempt_count >= _MAX_OPERATION_ATTEMPTS
                if exhausted:
                    notice.confirmation_sms_status = "failed"
                    notice.record_delivery_error("google_meet_url_unavailable")
                return (
                    "action_required" if exhausted else "pending",
                    None,
                    "google_meet_url_unavailable" if exhausted else "waiting_for_google_meet",
                )
            await booking_reminders.send_confirmation_sms(
                db,
                notice,
                event,
                timezone_name=appointment.timezone,
                template=kit.get("sms_template") if kit else None,
                values=kit.get("values") if kit else None,
                commit=False,
            )
            return (
                ("sent", None, None)
                if notice.confirmation_sms_status == "sent"
                else (
                    "failed",
                    None,
                    notice.last_error or "sms_provider_unavailable",
                )
            )
        when = appointment.starts_at.astimezone(
            booking_notify._tz(appointment.timezone)
        ).strftime("%b %d at %I:%M %p %Z")
        body = f"Qualified Commercial: your appointment on {when} was cancelled."
        result = await consent_delivery.send_sms_guarded(
            db,
            notice.invitee_phone,
            body,
            context="booking_cancellation",
        )
        if result.ok:
            notice.clear_delivery_error()
            return "sent", result.message_id, None
        notice.record_delivery_error(result.detail)
        return "failed", result.message_id, result.detail
    if key == "pin_delivery":
        if notice is None or not kit or not kit.get("pin"):
            return "skipped", None, "room_pin_unavailable"
        confirmation_states = {
            notice.confirmation_email_status,
            notice.confirmation_sms_status,
        }
        if "pending" in confirmation_states:
            return "pending", None, "waiting_for_booking_confirmation"
        if not confirmation_states.intersection(
            {"sent", "disabled", "blocked_no_consent"}
        ):
            return "action_required", None, "booking_confirmation_unavailable"
        if (
            notice.sms_consent
            and notice.invitee_phone
            and notice.confirmation_sms_status == "sent"
        ):
            notice.precall_pin_delivered_via = "sms"
            return "sent", None, None
        from app.dealer_os.services import precall

        channel = await precall.deliver_pin(
            db,
            notice=notice,
            booking=booking,
            values=kit["values"],
        )
        return (
            ("sent", None, None)
            if channel
            else ("failed", None, notice.last_error or "pin_delivery_failed")
        )
    if key == "document_request":
        return await _deliver_document_request(
            db,
            operation=operation,
            notice=notice,
        )
    if key.startswith("rep_notification_"):
        try:
            index = int(key.rsplit("_", 1)[1])
            raw_id = (operation.payload or {}).get("notification_ids", [])[index]
            notification_id = UUID(str(raw_id))
        except (IndexError, TypeError, ValueError):
            return "skipped", None, "notification_reference_missing"
        result = await deliver_deferred_notification_email(db, notification_id)
        if result is None:
            return "unavailable", None, "notification_recipient_unavailable"
        return (
            ("sent", result.message_id, None)
            if result.ok
            else ("failed", result.message_id, result.detail)
        )
    return "skipped", None, "unknown_effect"


async def process_operation(
    db: AsyncSession,
    operation_id: UUID,
    *,
    recover_stale: bool = False,
) -> bool:
    now = datetime.now(UTC)
    eligible = BookingDeliveryOperation.status == "pending"
    if recover_stale:
        eligible = or_(
            eligible,
            (
                BookingDeliveryOperation.status == "processing"
            )
            & (
                BookingDeliveryOperation.claimed_at
                <= now - _STALE_CLAIM_AFTER
            ),
        )
    operation = (
        await db.execute(
            select(BookingDeliveryOperation)
            .where(
                BookingDeliveryOperation.id == operation_id,
                eligible,
            )
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if operation is None:
        return False

    # Provider effects for one appointment must never overlap. In particular,
    # an immediate reschedule can be committed before the initial-create wake
    # starts. Let the oldest durable operation finish first; the scheduler will
    # retry the newer row after that operation reaches a terminal state.
    oldest_active_id = (
        await db.execute(
            select(BookingDeliveryOperation.id)
            .where(
                BookingDeliveryOperation.appointment_id
                == operation.appointment_id,
                BookingDeliveryOperation.status.in_(["pending", "processing"]),
            )
            .order_by(
                BookingDeliveryOperation.created_at.asc(),
                BookingDeliveryOperation.id.asc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if oldest_active_id is not None and oldest_active_id != operation.id:
        operation.status = "pending"
        operation.claimed_at = None
        operation.next_attempt_at = now + timedelta(seconds=5)
        await db.commit()
        return False
    if operation.status == "pending":
        operation.status = "processing"
        operation.attempt_count = int(operation.attempt_count or 0) + 1
    operation.claimed_at = now
    if operation.operation_type == "create":
        claimed_appointment = await db.get(
            DealerRepAppointment, operation.appointment_id
        )
        claimed_event = (
            await db.get(CalendarEvent, operation.event_id)
            if operation.event_id
            else None
        )
        if claimed_appointment is not None:
            operation.payload = {
                **(operation.payload or {}),
                "claimed_state_key": lifecycle_idempotency_key(
                    claimed_appointment, claimed_event
                ),
            }
    created_at = operation.created_at
    if created_at is not None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        booking_metrics.emit(
            "booking_delivery_job_age",
            max(0.0, (now - created_at).total_seconds()),
            unit="seconds",
            operation_type=operation.operation_type,
        )
    booking_metrics.emit(
        "booking_delivery_attempt",
        operation.attempt_count,
        operation_type=operation.operation_type,
    )
    await db.commit()

    effects = list(
        (
            await db.execute(
                select(BookingDeliveryEffect)
                .where(BookingDeliveryEffect.operation_id == operation.id)
            )
        )
        .scalars()
        .all()
    )
    effects.sort(
        key=lambda row: (
            _EFFECT_PRIORITY.get(
                row.effect_key,
                80 if row.effect_key.startswith("rep_notification_") else 100,
            ),
            str(row.id),
        )
    )
    for effect in effects:
        if effect.status in _TERMINAL_EFFECT_STATUSES:
            continue
        if effect.status == "processing":
            if stale_effect_recovery(effect.effect_key) == "retry":
                # Google event identity is deterministic from CalendarEvent.id.
                # Replaying the upsert after a crash updates the same provider
                # event and is required to unblock a pending Meet URL.
                effect.status = "pending"
                effect.error = "retrying_stale_idempotent_google_effect"
                effect.completed_at = None
                booking_metrics.emit(
                    "booking_provider_idempotent_replay",
                    operation_type=operation.operation_type,
                    effect="google",
                )
            else:
                # SES/SMS may have accepted before the worker died. Never
                # repeat a non-idempotent send with an ambiguous outcome.
                effect.status = "action_required"
                effect.error = "ambiguous_provider_outcome"
                effect.completed_at = datetime.now(UTC)
                await db.commit()
                continue
        effect.status = "processing"
        effect.attempt_count = int(effect.attempt_count or 0) + 1
        effect.last_attempt_at = datetime.now(UTC)
        await db.commit()
        try:
            effect_status, provider_id, error = await _run_effect(
                db, operation, effect
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "booking operation effect failed operation=%s effect=%s",
                operation.id,
                effect.effect_key,
            )
            retry_google = (
                effect.effect_key == "google"
                and operation.attempt_count < _MAX_OPERATION_ATTEMPTS
            )
            effect_status, provider_id, error = (
                "pending" if retry_google else "action_required",
                None,
                f"provider_exception:{type(exc).__name__}",
            )
            category = (
                "timeout"
                if isinstance(exc, TimeoutError) or "timeout" in str(exc).lower()
                else "exception"
            )
            booking_metrics.emit(
                "booking_provider_failure",
                operation_type=operation.operation_type,
                effect=effect.effect_key,
                category=category,
            )
        effect.status = effect_status
        effect.provider_message_id = provider_id
        effect.error = (error or "")[:1000] or None
        effect.completed_at = (
            None if effect_status == "pending" else datetime.now(UTC)
        )
        actor = (
            await db.get(User, operation.actor_user_id)
            if operation.actor_user_id
            else None
        )
        db.add(
            DealerRepAppointmentActivity(
                appointment_id=operation.appointment_id,
                event_type=f"delivery_{effect.effect_key}"[:40],
                body=(
                    f"{operation.operation_type} delivery: "
                    f"{effect.effect_key} {effect_status}"
                ),
                actor_user_id=operation.actor_user_id,
                actor_name=(
                    actor.name or actor.email or "Booking delivery worker"
                    if actor is not None
                    else "Booking delivery worker"
                ),
                after={
                    "operation_id": str(operation.id),
                    "operation_type": operation.operation_type,
                    "effect": effect.effect_key,
                    "status": effect_status,
                    "provider_message_id": provider_id,
                    "error": effect.error,
                },
            )
        )
        await db.commit()

    await db.refresh(operation)
    final_effects = list(
        (
            await db.execute(
                select(BookingDeliveryEffect).where(
                    BookingDeliveryEffect.operation_id == operation.id
                )
            )
        )
        .scalars()
        .all()
    )
    failed = [
        row
        for row in final_effects
        if row.status in _ACTION_REQUIRED_EFFECT_STATUSES
    ]
    pending = [row for row in final_effects if row.status == "pending"]
    operation.status = aggregate_operation_status(
        has_pending=bool(pending),
        has_failed=bool(failed),
    )
    operation.completed_at = (
        datetime.now(UTC) if not pending else None
    )
    operation.claimed_at = None if pending else operation.claimed_at
    operation.next_attempt_at = (
        datetime.now(UTC) + _retry_delay(operation.attempt_count)
        if pending
        else None
    )
    operation.last_error = (
        "; ".join(
            f"{row.effect_key}:{row.error or row.status}" for row in failed
        )[:2000]
        or None
    )
    if (
        operation.operation_type == "create"
        and not failed
        and not pending
    ):
        latest_appointment = await db.get(
            DealerRepAppointment, operation.appointment_id
        )
        latest_event = (
            await db.get(CalendarEvent, operation.event_id)
            if operation.event_id
            else None
        )
        latest_state_key = (
            lifecycle_idempotency_key(latest_appointment, latest_event)
            if latest_appointment is not None
            else None
        )
        if (
            latest_state_key
            and latest_state_key
            == (operation.payload or {}).get("claimed_state_key")
        ):
            # The create worker delivered the exact state represented by an
            # update queued just before it started. Suppress that redundant
            # operation so the client receives one confirmation, not an
            # immediate duplicate revised invitation.
            await db.execute(
                update(BookingDeliveryOperation)
                .where(
                    BookingDeliveryOperation.appointment_id
                    == operation.appointment_id,
                    BookingDeliveryOperation.id != operation.id,
                    BookingDeliveryOperation.status == "pending",
                    BookingDeliveryOperation.operation_type.in_(
                        ["update", "reschedule"]
                    ),
                    BookingDeliveryOperation.idempotency_key
                    == latest_state_key,
                )
                .values(
                    status="superseded",
                    next_attempt_at=None,
                    completed_at=datetime.now(UTC),
                    last_error="delivered_by_initial_create_operation",
                )
            )
    if failed and not pending:
        booking_metrics.emit(
            "booking_delivery_action_required",
            len(failed),
            operation_type=operation.operation_type,
        )
    if operation.operation_type == "create":
        notice = (
            await db.execute(
                select(BookingNotification).where(
                    BookingNotification.event_id == operation.event_id
                )
            )
        ).scalar_one_or_none()
        if notice is not None and not failed and not pending:
            notice.delivery_completed_at = datetime.now(UTC)
    await db.commit()
    return True


async def dispatch_due_operations(*, limit: int = 25) -> int:
    from app.db import SessionLocal

    processed = 0
    for _ in range(limit):
        async with SessionLocal() as claim_db:
            now = datetime.now(UTC)
            stale_before = now - _STALE_CLAIM_AFTER
            operation = (
                await claim_db.execute(
                    select(BookingDeliveryOperation)
                    .where(
                        or_(
                            (
                                BookingDeliveryOperation.status == "pending"
                            )
                            & (
                                BookingDeliveryOperation.next_attempt_at.is_(None)
                                | (BookingDeliveryOperation.next_attempt_at <= now)
                            ),
                            (
                                BookingDeliveryOperation.status == "processing"
                            )
                            & (BookingDeliveryOperation.claimed_at <= stale_before),
                        )
                    )
                    .order_by(
                        BookingDeliveryOperation.next_attempt_at.asc().nullsfirst(),
                        BookingDeliveryOperation.created_at.asc(),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if operation is None:
                break
            operation_id = operation.id
            was_stale_processing = operation.status == "processing"
            await claim_db.commit()
        async with SessionLocal() as work_db:
            if await process_operation(
                work_db,
                operation_id,
                # process_operation converts any in-flight effect to an
                # explicit ambiguous/action-required result.
                recover_stale=was_stale_processing,
            ):
                processed += 1
    return processed


async def wake_operation(operation_id: UUID) -> None:
    """Best-effort immediate drain; the periodic scheduler is authoritative."""

    from app.db import SessionLocal

    async with SessionLocal() as db:
        try:
            await process_operation(db, operation_id)
        except Exception:  # noqa: BLE001
            await db.rollback()
            log.exception("booking operation wake-up failed operation=%s", operation_id)
