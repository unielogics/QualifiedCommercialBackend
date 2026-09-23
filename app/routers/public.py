# ruff: noqa: B008, UP017, UP037
"""Public, UNauthenticated endpoints for the marketing site (QCWeb).

Two surfaces, both intentionally auth-free (the public site has no
logged-in user):

  * GET  /public/fred/series   — read-only last-N-day FRED series for
                                 the program-page rate charts. Mirrors
                                 the authed /fred/series shape but never
                                 triggers a refresh and exposes nothing
                                 beyond the already-public index values.
  * POST /public/investor-inquiry — full-screen "For Investors" form;
                                 emails the lead to franco@ and logs an
                                 Activity row so nothing is lost even if
                                 mail delivery is unconfigured.
  * GET  /public/financial-templates/{slug}.xlsx — the four financial
                                 templates the resources page links to
                                 (profit-and-loss, balance-sheet,
                                 business-debt-schedule,
                                 personal-financial-statement), generated
                                 from the form schemas and served as an
                                 attachment. Free, no form, cached a day.

Kept deliberately small + defensive (length caps, consent gate, a
best-effort per-IP throttle). No DB writes other than the Activity log.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import date, datetime, timezone, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.dealer_os.models import DealerRepAppointment
from app.dealer_os.schemas import BookingAvailabilityRead
from app.dealer_os.services import consent_delivery
from app.dealer_os.services import sms_consent as sms_consent_service
from app.enums import CalendarEventKind, CalendarEventSource, CalendarEventStatus
from app.models.activity import Activity
from app.models.booking_notification import BookingDeliveryOperation, BookingNotification
from app.models.booking_settings import BookingSettings, BookingSlugAlias
from app.models.event import CalendarEvent
from app.models.user import User
from app.routers.fred import _build_summary, _current_spreads
from app.schemas.fred import FredSeriesSummary
from app.schemas.phone import RequiredPhone
from app.services import booking_operations, booking_reminders
from app.services import fred as fred_service
from app.services.team_calendar import effective_booking_settings, lock_calendar_owner

log = logging.getLogger(__name__)

router = APIRouter(prefix="/public", tags=["public"])

INVESTOR_INBOX = "franco@qualifiedcommercial.com"
SUPPORT_INBOX = "support@qualifiedcommercial.com"
# Capital-partner applications notify both the founder (decisioning) and
# the support inbox (intake / audit trail).
CAPITAL_PARTNER_NOTIFY = ("franco@qualifiedcommercial.com", "support@qualifiedcommercial.com")

# Best-effort in-memory throttle (single-instance deploy — see scheduler
# note in app/services/scheduler.py). Maps client IP → last submit ts.
_LAST_SUBMIT: dict[str, float] = {}
_LAST_BOOKING_REPLAY: dict[str, float] = {}
_THROTTLE_SECONDS = 20.0


@router.get("/financial-templates/{slug}.xlsx")
async def financial_template_download(slug: str) -> Response:
    """A financial template, as an Excel workbook.

    Five slugs: the four single forms, and `financial-package`, which carries
    all four as tabs of one workbook for a borrower to forward to a bookkeeper
    or accountant.

    Generated from the same schemas the on-screen forms render, never a
    committed binary, so the spreadsheet a borrower downloads cannot drift from
    the form their advisor sends. Each single form's attachment filename is
    chosen so a filled copy uploaded back to a room routes to its checklist row
    by name before analysis runs; the package's deliberately does not, because
    one file answering four requests cannot pick a row (and a filename carrying
    the word "statement" would be read as a bank statement). Bytes are built
    once per process and cached a day at the edge; nothing here reads or writes
    the database.
    """
    from app.services import financial_templates_xlsx

    found = financial_templates_xlsx.workbook_for_slug(slug)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such template")
    filename, raw = found
    log.info("financial template download slug=%s bytes=%d", slug, len(raw))
    return Response(
        content=raw,
        media_type=financial_templates_xlsx.MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "public, max-age=86400",
        },
    )


@router.get("/fred/series", response_model=list[FredSeriesSummary])
async def public_fred_series(
    db: AsyncSession = Depends(get_db),
    days: int = 30,
) -> list[FredSeriesSummary]:
    """Unauthenticated bundled FRED summary for the public program-page
    rate charts. Read-only; returns whatever the daily refresh has
    populated (empty list-friendly if the table is bare)."""
    requested_days = max(1, min(days, 90))
    deepest = max(requested_days, 30)
    spreads = await _current_spreads(db)
    out: list[FredSeriesSummary] = []
    for series_id in fred_service.SERIES_IDS:
        history = await fred_service.get_history(db, series_id, days=deepest)
        out.append(
            _build_summary(series_id, history, spreads.get(series_id), requested_days)
        )
    return out


class InvestorInquiry(BaseModel):
    investor_type: str = Field(min_length=1, max_length=80)
    funding_to_deploy: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=4000)
    full_name: str = Field(min_length=1, max_length=160)
    phone: RequiredPhone
    consent: bool


class InvestorInquiryResult(BaseModel):
    ok: bool


@router.post("/investor-inquiry", response_model=InvestorInquiryResult)
async def investor_inquiry(
    payload: InvestorInquiry,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> InvestorInquiryResult:
    """Public 'For Investors' lead form. Emails franco@ and logs an
    Activity row. Consent is mandatory."""
    if payload.consent is not True:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Consent to be contacted is required.",
        )

    ip = (request.client.host if request.client else "?") or "?"
    now = time.monotonic()
    last = _LAST_SUBMIT.get(ip)
    if last is not None and (now - last) < _THROTTLE_SECONDS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Please wait a moment before submitting again.",
        )
    _LAST_SUBMIT[ip] = now

    subject = f"New investor inquiry — {payload.full_name}"
    mail_body = (
        f"Name: {payload.full_name}\n"
        f"Phone: {payload.phone}\n"
        f"Investor type: {payload.investor_type}\n"
        f"Funding to deploy: {payload.funding_to_deploy}\n"
        f"Subject: {payload.title}\n\n"
        f"{payload.body}\n"
    )

    sent = False
    try:
        from app.services.email.gmail_client import gmail_config, send_message

        cfg = gmail_config()
        if cfg is not None:
            send_message(cfg, to=INVESTOR_INBOX, subject=subject, body=mail_body)
            sent = True
        else:
            log.warning("investor-inquiry: gmail not configured — lead logged only")
    except Exception:
        log.exception("investor-inquiry: email send failed — lead still logged")

    # Always persist the lead so it's never lost, regardless of mail state.
    db.add(
        Activity(
            loan_id=None,
            actor_id=None,
            actor_label="public",
            kind="investor.inquiry",
            summary=f"Investor inquiry from {payload.full_name} ({payload.investor_type})",
            payload={
                "full_name": payload.full_name,
                "phone": payload.phone,
                "investor_type": payload.investor_type,
                "funding_to_deploy": payload.funding_to_deploy,
                "title": payload.title,
                "body": payload.body,
                "emailed": sent,
            },
        )
    )
    await db.flush()
    return InvestorInquiryResult(ok=True)


class SupportInquiry(BaseModel):
    """Public /support contact form. Same defensive shape as the
    investor inquiry — length caps + mandatory consent — plus an
    explicit `email` field so the inbox can simply hit Reply (the
    investor flow gates on phone instead)."""

    full_name: str = Field(min_length=1, max_length=160)
    email: str = Field(min_length=5, max_length=160)
    phone: RequiredPhone
    topic: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=4000)
    consent: bool


class SupportInquiryResult(BaseModel):
    ok: bool


@router.post("/support-inquiry", response_model=SupportInquiryResult)
async def support_inquiry(
    payload: SupportInquiry,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SupportInquiryResult:
    """Public /support contact form on qualifiedcommercial.com. Mirrors
    `/public/investor-inquiry`: emails support@ via the existing Gmail
    relay (best-effort), always logs an Activity row so the inquiry is
    never lost when mail is misconfigured, enforces a 20-second per-IP
    throttle to deter abuse."""
    if payload.consent is not True:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Consent to be contacted is required.",
        )
    # Lightweight email sanity (no pydantic[email] dep — the recipient
    # is a human, format errors surface on the reply path).
    if "@" not in payload.email or "." not in payload.email:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A valid email is required so we can reply.",
        )

    ip = (request.client.host if request.client else "?") or "?"
    now = time.monotonic()
    last = _LAST_SUBMIT.get(ip)
    if last is not None and (now - last) < _THROTTLE_SECONDS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Please wait a moment before submitting again.",
        )
    _LAST_SUBMIT[ip] = now

    subject = f"New support inquiry — {payload.full_name}"
    mail_body = (
        f"Name: {payload.full_name}\n"
        f"Email: {payload.email}\n"
        f"Phone: {payload.phone or '(not provided)'}\n"
        f"Topic: {payload.topic}\n"
        f"Subject: {payload.title}\n\n"
        f"{payload.body}\n"
    )

    sent = False
    try:
        from app.services.email.gmail_client import gmail_config, send_message

        cfg = gmail_config()
        if cfg is not None:
            send_message(cfg, to=SUPPORT_INBOX, subject=subject, body=mail_body)
            sent = True
        else:
            log.warning("support-inquiry: gmail not configured — lead logged only")
    except Exception:
        log.exception("support-inquiry: email send failed — lead still logged")

    db.add(
        Activity(
            loan_id=None,
            actor_id=None,
            actor_label="public",
            kind="support.inquiry",
            summary=f"Support inquiry from {payload.full_name} ({payload.topic})",
            payload={
                "full_name": payload.full_name,
                "email": payload.email,
                "phone": payload.phone,
                "topic": payload.topic,
                "title": payload.title,
                "body": payload.body,
                "emailed": sent,
            },
        )
    )
    await db.flush()
    return SupportInquiryResult(ok=True)


# ---------------------------------------------------------------------------
# Public booking page
# ---------------------------------------------------------------------------


def _booking_asset_get_url(s3_key: str | None) -> str | None:
    if not s3_key:
        return None
    from app.config import get_settings as get_app_config

    cfg = get_app_config()
    if not cfg.s3_bucket:
        return None
    import boto3

    try:
        return boto3.client("s3", region_name=cfg.aws_region).generate_presigned_url(
            "get_object",
            Params={"Bucket": cfg.s3_bucket, "Key": s3_key},
            ExpiresIn=3600,
        )
    except Exception:
        log.exception("public-booking: failed to sign booking image key=%s", s3_key)
        return None


class PublicBookingSlot(BaseModel):
    starts_at: datetime
    label: str
    date_label: str


class PublicBookingProfile(BaseModel):
    slug: str
    agent_name: str
    host_name: str
    host_role: str
    title: str
    intro: str
    primary_color: str
    background_color: str
    duration_min: int
    timezone: str
    google_meet_enabled: bool = True
    meeting_mode: Literal["video", "phone"] = "video"
    logo_url: str | None = None
    profile_photo_url: str | None = None
    slots: list[PublicBookingSlot]
    page_start_date: date
    page_end_date: date
    next_start_date: date | None = None
    window_end_date: date
    #: The exact consent sentence stored as proof when the box is ticked. The
    #: page must render this, not its own paraphrase.
    sms_disclosure_text: str = ""
    precall_enabled: bool = False
    booking_questions: dict[str, bool] = Field(default_factory=dict)
    precall_default_variant: Literal["dealer", "real_estate", "main_street", "mca_refinance"] = "main_street"
    precall_allowed_variants: list[
        Literal["dealer", "real_estate", "main_street", "mca_refinance"]
    ] = Field(default_factory=list)
    precall_allow_vertical_choice: bool = False


class PublicBookingCreate(BaseModel):
    creation_idempotency_key: str | None = Field(
        default=None,
        min_length=36,
        max_length=36,
        pattern=(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
            r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
        ),
    )
    starts_at: datetime
    full_name: str = Field(min_length=1, max_length=160)
    email: str = Field(min_length=5, max_length=320)
    phone: RequiredPhone
    business_name: str | None = Field(default=None, max_length=180)
    requested_amount: float | None = Field(default=None, gt=0)
    vertical: Literal["dealer", "real_estate", "main_street", "mca_refinance"] | None = None
    preferred_bank_method: Literal["plaid", "statements", "decide_later"] | None = None
    notes: str | None = Field(default=None, max_length=1000)
    transactional_sms_consent: bool = False
    #: Campaign hint carried by the link (e.g. the rep product booklet appends
    #: ?source=field_desk_product). Recorded for the desk; never decides a file.
    source: str | None = Field(default=None, max_length=64)


class PublicBookingCreateResult(BaseModel):
    ok: bool
    event_id: str
    appointment_id: str | None = None
    #: The client's secure room, when the booking opened a draft file.
    room_url: str | None = None
    pin_delivered_via: str | None = None
    delivery_state: dict[str, str] | None = None


async def _public_booking_replay_result(
    db: AsyncSession,
    appointment: DealerRepAppointment,
    *,
    include_sensitive: bool,
) -> PublicBookingCreateResult:
    """Return the original local booking for an exact retried submission."""

    notice = (
        await db.execute(
            select(BookingNotification).where(
                BookingNotification.event_id == appointment.calendar_event_id
            )
        )
    ).scalar_one_or_none()
    room_url = None
    if include_sensitive and notice is not None and notice.precall_dealer_id:
        from app.dealer_os.models import DealerBusiness
        from app.dealer_os.services import client_room

        dealer = await db.get(DealerBusiness, notice.precall_dealer_id)
        room = await client_room.get_room(db, dealer) if dealer is not None else None
        room_url = room.url if room is not None else None
    elif include_sensitive and notice is not None and notice.precall_intake_id:
        from app.dealer_os.services import application_precall
        from app.models.public_underwriting_intake import PublicUnderwritingIntake

        intake = await db.get(PublicUnderwritingIntake, notice.precall_intake_id)
        if intake is not None:
            room = await application_precall.room_for_intake(db, intake)
            room_url = room.url
    operation = await booking_operations.find_by_idempotency_key(
        db, f"booking:create:{appointment.id}"
    )
    return PublicBookingCreateResult(
        ok=True,
        event_id=str(appointment.calendar_event_id),
        appointment_id=str(appointment.id),
        room_url=room_url,
        pin_delivered_via=(
            notice.precall_pin_delivered_via
            if include_sensitive and notice
            else None
        ),
        delivery_state=(
            await booking_operations.queued_results(db, operation)
            if operation is not None
            else None
        ),
    )


def _assert_public_booking_replay_matches(
    appointment: DealerRepAppointment,
    *,
    owner_user_id: uuid.UUID,
    starts_at: datetime,
    duration_min: int,
    email: str | None,
    phone: str | None,
    request_fingerprint: str | None,
) -> None:
    if booking_operations.creation_replay_matches(
        appointment,
        owner_user_id=owner_user_id,
        dealer_id=None,
        starts_at=starts_at,
        duration_min=duration_min,
        invitee_email=email,
        invitee_phone=phone,
        request_fingerprint=request_fingerprint,
    ):
        return
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "code": "idempotency_key_reused",
            "message": "That booking request key was already used for different booking details.",
        },
    )


@router.get("/booking/{slug}", response_model=PublicBookingProfile)
async def public_booking_profile(
    slug: str,
    db: AsyncSession = Depends(get_db),
    start_date: date | None = None,
    days: int | None = None,
) -> PublicBookingProfile:
    user, booking = await _load_public_booking(db, slug)
    from app.dealer_os.deps import is_rep

    field_desk_page = is_rep(user)
    try:
        availability = await _available_booking_slots(
            db,
            user,
            booking,
            start_date=start_date,
            days=days,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    host_name = user.name or "Qualified Commercial"
    return PublicBookingProfile(
        slug=booking.slug or slug,
        agent_name=host_name,
        host_name=host_name,
        host_role=user.role.value if hasattr(user.role, "value") else str(user.role),
        title=booking.title or f"Book a meeting with {host_name}",
        intro=booking.intro or "Choose a time that works for you. You will receive a confirmation after booking.",
        primary_color=booking.primary_color,
        background_color=booking.background_color,
        duration_min=booking.duration_min,
        timezone=booking.timezone,
        google_meet_enabled=bool(booking.google_meet_enabled),
        meeting_mode="video" if booking.google_meet_enabled else "phone",
        logo_url=_booking_asset_get_url(booking.logo_s3_key),
        profile_photo_url=_booking_asset_get_url(booking.profile_photo_s3_key),
        slots=[
            PublicBookingSlot(
                starts_at=slot.starts_at,
                label=slot.label,
                date_label=slot.date_label,
            )
            for slot in availability.slots
        ],
        page_start_date=availability.page_start_date,
        page_end_date=availability.page_end_date,
        next_start_date=availability.next_start_date,
        window_end_date=availability.window_end_date,
        sms_disclosure_text=sms_consent_service.text_for("transactional"),
        precall_enabled=bool(booking.precall_enabled),
        booking_questions=dict(booking.booking_questions or {}),
        precall_default_variant=(
            "dealer" if field_desk_page else booking.precall_default_variant or "main_street"
        ),
        precall_allowed_variants=(
            ["dealer"] if field_desk_page else list(booking.precall_allowed_variants or [])
        ),
        precall_allow_vertical_choice=(
            False if field_desk_page else bool(booking.precall_allow_vertical_choice)
        ),
    )


@router.post("/booking/{slug}", response_model=PublicBookingCreateResult)
async def public_booking_create(
    slug: str,
    payload: PublicBookingCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PublicBookingCreateResult:
    ip = (request.client.host if request.client else "?") or "?"
    now = time.monotonic()
    # A caller-token retry must be allowed to reach the idempotency ledger
    # even when it arrives inside the anti-spam window (double-click or a
    # response timeout). Legacy requests without an unguessable caller token
    # remain throttled before any replay lookup can expose sensitive data.
    defer_throttle = bool(payload.creation_idempotency_key)
    if not defer_throttle:
        last = _LAST_SUBMIT.get(ip)
        if last is not None and (now - last) < _THROTTLE_SECONDS:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Please wait a moment before submitting again.",
            )
        _LAST_SUBMIT[ip] = now

    user, booking = await _load_public_booking(db, slug)
    from app.dealer_os.deps import is_rep

    host_is_rep = is_rep(user)
    questions = booking.booking_questions or {}
    if questions.get("business_name") and not (payload.business_name or "").strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Business name is required.")
    normalized_phone = consent_delivery.normalize_phone(payload.phone)
    if questions.get("phone") and normalized_phone is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A valid phone number is required.")
    if payload.phone and normalized_phone is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Enter a valid phone number.")
    if questions.get("requested_amount") and payload.requested_amount is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Requested amount is required.")
    if questions.get("bank_statement") and payload.preferred_bank_method is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose a banking evidence method.")
    selected_variant = "dealer" if host_is_rep else booking.precall_default_variant or "main_street"
    if booking.precall_enabled and not host_is_rep:
        if (
            payload.vertical
            and not booking.precall_allow_vertical_choice
            and payload.vertical != selected_variant
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "This booking page does not allow the application type to be changed.",
            )
        selected_variant = (
            payload.vertical
            if booking.precall_allow_vertical_choice and payload.vertical
            else booking.precall_default_variant or "main_street"
        )
        allowed_variants = set(
            booking.precall_allowed_variants or [booking.precall_default_variant]
        )
        if selected_variant not in allowed_variants:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "That application type is not available on this booking page.",
            )
    starts_at = _to_utc_minute(payload.starts_at)
    request_fingerprint = booking_operations.creation_request_fingerprint(
        {
            "surface": "public",
            "owner_user_id": user.id,
            "starts_at": starts_at,
            "duration_min": booking.duration_min,
            "origin": public_booking_origin(host_is_rep),
            "selected_variant": selected_variant,
            "meeting_mode": (
                "video" if booking.google_meet_enabled else "phone"
            ),
            "payload": payload.model_dump(
                mode="json", exclude={"creation_idempotency_key"}
            ),
        }
    )
    creation_key = booking_operations.creation_idempotency_key(
        owner_user_id=user.id,
        starts_at=starts_at,
        duration_min=booking.duration_min,
        invitee_email=payload.email,
        invitee_phone=normalized_phone,
        origin=public_booking_origin(host_is_rep),
        scope=f"public:{booking.slug or slug}:{selected_variant}",
        caller_token=payload.creation_idempotency_key,
    )
    existing = (
        await db.execute(
            select(DealerRepAppointment).where(
                DealerRepAppointment.creation_idempotency_key == creation_key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        _assert_public_booking_replay_matches(
            existing,
            owner_user_id=user.id,
            starts_at=starts_at,
            duration_min=booking.duration_min,
            email=payload.email,
            phone=normalized_phone,
            request_fingerprint=(
                request_fingerprint
                if payload.creation_idempotency_key
                else None
            ),
        )
        booking_operations.record_idempotency_replay(
            operation_type="create",
            appointment_id=existing.id,
            surface="public_booking",
        )
        replay_guard = f"{ip}:{creation_key}"
        last_replay = _LAST_BOOKING_REPLAY.get(replay_guard)
        if (
            last_replay is not None
            and (now - last_replay) < _THROTTLE_SECONDS
        ):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Please wait before checking this booking again.",
            )
        _LAST_BOOKING_REPLAY[replay_guard] = now
        return await _public_booking_replay_result(
            db,
            existing,
            include_sensitive=False,
        )
    if defer_throttle:
        last = _LAST_SUBMIT.get(ip)
        if last is not None and (now - last) < _THROTTLE_SECONDS:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Please wait a moment before submitting again.",
            )
        _LAST_SUBMIT[ip] = now
    try:
        availability = await _available_booking_slots(
            db,
            user,
            booking,
            start_date=starts_at.astimezone(_booking_tz(booking.timezone)).date(),
            days=1,
        )
    except ValueError:
        availability = None
    valid_slot = availability is not None and any(
        abs((slot.starts_at - starts_at).total_seconds()) < 1
        for slot in availability.slots
    )
    if not valid_slot:
        raise HTTPException(status.HTTP_409_CONFLICT, "That time is no longer available.")
    # Never hold the owner row lock while consulting Google. The full
    # provider-backed slot check above runs first; under the lock we repeat
    # only local collision detection so simultaneous QC requests serialize.
    from app.dealer_os.router import _appointment_slot_is_available

    await lock_calendar_owner(db, user.id)
    existing = (
        await db.execute(
            select(DealerRepAppointment).where(
                DealerRepAppointment.creation_idempotency_key == creation_key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        _assert_public_booking_replay_matches(
            existing,
            owner_user_id=user.id,
            starts_at=starts_at,
            duration_min=booking.duration_min,
            email=payload.email,
            phone=normalized_phone,
            request_fingerprint=(
                request_fingerprint
                if payload.creation_idempotency_key
                else None
            ),
        )
        booking_operations.record_idempotency_replay(
            operation_type="create",
            appointment_id=existing.id,
            surface="public_booking_locked",
        )
        return await _public_booking_replay_result(
            db,
            existing,
            include_sensitive=False,
        )
    if not await _appointment_slot_is_available(
        db,
        user,
        booking,
        starts_at=starts_at,
        duration_min=booking.duration_min,
        check_google=False,
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "That time is no longer available.")

    who = f"{payload.full_name} <{payload.email}>"
    description = (
        "Booked from the agent public booking page.\n"
        f"Name: {payload.full_name}\n"
        f"Email: {payload.email}\n"
        f"Phone: {payload.phone or '(not provided)'}\n\n"
        f"Business: {payload.business_name or '(not provided)'}\n"
        f"Requested amount: {payload.requested_amount if payload.requested_amount is not None else '(not provided)'}\n"
        f"Application type: {selected_variant}\n"
        f"Bank evidence preference: {payload.preferred_bank_method or '(not provided)'}\n\n"
        f"Notes:\n{payload.notes or '(none)'}"
    )
    ev = CalendarEvent(
        loan_id=None,
        kind=CalendarEventKind.CALL,
        title=f"Booked call: {payload.full_name}",
        description=description,
        who=who[:160],
        starts_at=starts_at,
        duration_min=booking.duration_min,
        status=CalendarEventStatus.PENDING,
        source=CalendarEventSource.AUTO,
        owner_user_id=user.id,
        external_ref_kind="public_booking",
        external_ref_id=str(uuid.uuid4()),
    )
    db.add(ev)
    await db.flush()
    from app.dealer_os.services import booking_appointments

    notice = await booking_reminders.register_booking(
        db,
        event=ev,
        booking=booking,
        invitee_name=payload.full_name,
        invitee_email=payload.email,
        invitee_phone=payload.phone,
        sms_consent=payload.transactional_sms_consent,
        sms_consent_method="self_web" if payload.transactional_sms_consent else None,
        sms_consent_ip=ip,
        sms_consent_user_agent=request.headers.get("user-agent"),
        # A booking on a rep's own page is that rep's: it lands on their
        # calendar and they get the staff reminders.
        booked_by_user_id=user.id if host_is_rep else None,
        program_name="General funding discussion / Not decided yet",
        requested_amount=(str(payload.requested_amount) if payload.requested_amount is not None else None),
    )
    appointment = await booking_appointments.create_booking_appointment(
        db,
        event=ev,
        host=user,
        booking=booking,
        origin=public_booking_origin(host_is_rep),
        invitee_name=payload.full_name,
        invitee_email=payload.email,
        invitee_phone=payload.phone,
        company=payload.business_name,
        notes=payload.notes,
        requested_amount=(str(payload.requested_amount) if payload.requested_amount is not None else None),
        booked_by_user_id=user.id if host_is_rep else None,
        contact_source="public_booking",
        creation_idempotency_key=creation_key,
        meeting_mode="video" if booking.google_meet_enabled else "phone",
        creation_request_fingerprint=request_fingerprint,
    )
    draft = await _open_public_booking_draft(
        db,
        notice=notice,
        event=ev,
        booking=booking,
        host=user,
        appointment=appointment,
        request=request,
        variant=selected_variant,
        company=payload.business_name,
        application_data={
            "requested_amount": payload.requested_amount,
            "preferred_bank_method": payload.preferred_bank_method,
            "vertical": selected_variant,
            "booking_slug": booking.slug,
        },
    )

    db.add(
        Activity(
            loan_id=None,
            actor_id=None,
            actor_label="public",
            kind="calendar.public_booking",
            summary=f"Public booking created for {user.name or user.email}: {payload.full_name}",
            payload={
                "event_id": str(ev.id),
                "host_user_id": str(user.id),
                "invitee_name": payload.full_name,
                "invitee_email": payload.email,
                "starts_at": starts_at.isoformat(),
                "duration_min": booking.duration_min,
                "source": "public_booking_page",
                # The link's hint (e.g. ?source=field_desk_product) is kept as a
                # campaign label for the desk; it never decides anything.
                "campaign": (payload.source or "")[:64] or None,
            },
        )
    )

    delivery_operation = await _deliver_booking(
        db,
        user,
        payload,
        starts_at,
        booking,
        ev,
        notice,
        draft=draft,
        appointment=appointment,
    )
    # Appointment and every provider effect commit together. Provider work
    # happens only after this durable local transaction.
    await db.commit()
    await db.refresh(ev)
    delivery_state = await booking_operations.queued_results(db, delivery_operation)
    asyncio.create_task(booking_operations.wake_operation(delivery_operation.id))
    return PublicBookingCreateResult(
        ok=True,
        event_id=str(ev.id),
        appointment_id=str(appointment.id),
        room_url=draft.room.url if draft is not None else None,
        pin_delivered_via=notice.precall_pin_delivered_via,
        delivery_state=delivery_state,
    )


async def _load_public_booking(
    db: AsyncSession,
    slug: str,
) -> tuple[User, BookingSettings]:
    row = (
        await db.execute(
            select(User, BookingSettings)
            .join(BookingSettings, BookingSettings.user_id == User.id)
            .where(
                User.deleted_at.is_(None),
                BookingSettings.enabled.is_(True),
                BookingSettings.slug == slug,
            )
        )
    ).first()
    if row:
        return row[0], await effective_booking_settings(db, row[1])
    alias_row = (
        await db.execute(
            select(User, BookingSettings)
            .join(BookingSlugAlias, BookingSlugAlias.user_id == User.id)
            .join(BookingSettings, BookingSettings.user_id == User.id)
            .where(
                User.deleted_at.is_(None),
                BookingSettings.enabled.is_(True),
                BookingSlugAlias.slug == slug,
            )
        )
    ).first()
    if alias_row:
        return alias_row[0], await effective_booking_settings(db, alias_row[1])
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking page not found.")


async def _available_booking_slots(
    db: AsyncSession,
    user: User,
    booking: BookingSettings,
    *,
    start_date: date | None = None,
    days: int | None = None,
) -> BookingAvailabilityRead:
    """Use the same authoritative implementation as Field Desk availability.

    That shared query includes orphan ``DealerRepAppointment`` rows with no
    CalendarEvent mirror, preventing public and authenticated pages from
    offering different times after a partial provider failure.
    """

    from app.dealer_os.router import _booking_slots

    return await _booking_slots(
        db,
        user,
        booking,
        duration_min=booking.duration_min,
        start_date=start_date,
        days=days,
    )


def _booking_tz(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        try:
            return ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError:
            return timezone.utc


def _to_utc_minute(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0)


def public_booking_origin(host_is_rep: bool) -> str:
    """field_desk when the page belongs to a rep; public for the team's page.

    Decided from the host alone — a server fact. The link's ?source= is an
    unauthenticated query parameter and must never open a file on its own.
    """
    return "field_desk" if host_is_rep else "public"


async def _open_public_booking_draft(
    db: AsyncSession,
    *,
    notice,
    event: CalendarEvent,
    booking: BookingSettings,
    host: User,
    appointment=None,
    request: Request,
    variant: str,
    company: str | None,
    application_data: dict,
):
    """Open the explicit preparation target for this public booking."""
    from sqlalchemy.exc import SQLAlchemyError

    from app.dealer_os.deps import is_rep
    from app.dealer_os.services import application_precall, precall

    origin = public_booking_origin(is_rep(host))
    if not booking.precall_enabled:
        return None
    try:
        if precall.opens_draft(origin):
            result = await precall.create_draft_for_booking(
                db,
                notice=notice,
                event=event,
                booking=booking,
                host=host,
                appointment=appointment,
                company=company,
                notes=f"Booked from {host.name or 'the rep'}'s public booking page.",
            )
            ready = await precall.readiness(db, result.dealer)
            if not ready.complete:
                await precall.schedule(db, notice=notice, booking=booking, event=event, dealer=result.dealer)
        else:
            result = await application_precall.create_draft_for_booking(
                db,
                notice=notice,
                event=event,
                booking=booking,
                host=host,
                request=request,
                appointment=appointment,
                variant=variant,
                company=company,
                notes=f"Booked from {host.name or 'Qualified Commercial'}'s public booking page.",
                application_data=application_data,
            )
            ready = await application_precall.readiness(db, result.profile)
            if not ready.complete:
                await application_precall.schedule(db, notice=notice, booking=booking, event=event)
        return result
    except SQLAlchemyError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("public-booking: could not open the draft file for %s", notice.id)
        notice.last_error = "precall_draft_creation_failed"
        await db.flush()
        return None


async def _deliver_booking(
    db: AsyncSession,
    user: User,
    payload: PublicBookingCreate,
    starts_at: datetime,
    booking: BookingSettings,
    ev: CalendarEvent,
    notice,
    draft=None,
    appointment=None,
) -> "BookingDeliveryOperation":
    """Persist public-booking provider effects; never call a provider here.

    This function intentionally runs before the route's single local commit.
    The scheduler (and best-effort wake-up) executes Google, SES and SMS later
    with one durable idempotency boundary per effect.
    """

    del starts_at, booking, notice, draft
    if appointment is None:
        raise RuntimeError("public booking delivery requires an appointment")
    return await booking_operations.enqueue(
        db,
        appointment=appointment,
        event=ev,
        actor_user_id=user.id,
        operation_type="create",
        idempotency_key=f"booking:create:{appointment.id}",
        delivery_payload={"notes": payload.notes},
    )


# ---------------------------------------------------------------------------
# Pre-call prep: signed links in the client's emails


class PrepLinkState(BaseModel):
    ok: bool
    invitee_first: str = ""
    host_name: str = ""
    starts_at: datetime | None = None
    stopped: bool = False
    completed: bool = False


def _prep_notice_or_404(notice, signature: str, notice_id: str) -> None:
    from app.dealer_os.services import precall

    if notice is None or not precall.link_valid(notice_id, "stop", signature):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This link is no longer valid.")


@router.get("/prep/{notice_id}/{signature}", response_model=PrepLinkState)
async def public_prep_state(notice_id: uuid.UUID, signature: str, db: AsyncSession = Depends(get_db)) -> PrepLinkState:
    """PUBLIC. What the one-tap stop page shows before the client confirms."""
    from app.models.booking_notification import BookingNotification

    notice = await db.get(BookingNotification, notice_id)
    _prep_notice_or_404(notice, signature, str(notice_id))
    event = await db.get(CalendarEvent, notice.event_id)
    host = await db.get(User, event.owner_user_id) if event is not None else None
    name = (notice.invitee_name or "").strip()
    return PrepLinkState(
        ok=True,
        invitee_first=name.split()[0] if name else "",
        host_name=(host.name if host is not None else "") or "Qualified Commercial",
        starts_at=event.starts_at if event is not None else None,
        stopped=notice.precall_stopped_at is not None,
        completed=notice.precall_completed_at is not None,
    )


@router.post("/prep/{notice_id}/{signature}/stop", response_model=PrepLinkState)
async def public_prep_stop(notice_id: uuid.UUID, signature: str, db: AsyncSession = Depends(get_db)) -> PrepLinkState:
    """PUBLIC. The client stops the pre-call emails and texts for this booking.
    Meeting confirmations and reminders are unaffected; only the prep nudges
    halt."""
    from app.dealer_os.services import precall
    from app.dealer_os.services.audit import log_action
    from app.models.booking_notification import BookingNotification

    notice = await db.get(BookingNotification, notice_id)
    _prep_notice_or_404(notice, signature, str(notice_id))
    if notice.precall_stopped_at is None:
        await precall.stop_sequence(db, notice, reason="email_stop")
        if notice.precall_dealer_id:
            await log_action(
                db, notice.precall_dealer_id, None, "precall.stopped", "dealer",
                entity_id=notice.precall_dealer_id, after={"via": "email_stop_link"},
            )
        await db.commit()
    return await public_prep_state(notice_id, signature, db)


# ---------------------------------------------------------------------------
# Capital partner (lender) application
# ---------------------------------------------------------------------------
#
# Public "Become a Lending Partner" form at
# qualifiedcommercial.com/lenders/apply. Persists to the dedicated
# `capital_partner_applications` table (so super-admin can review +
# approve/deny in QCDashboard), and emails franco@ + support@ to
# notify the team that a new application is ready to review.


class CapitalPartnerApplicationIn(BaseModel):
    """Public lender-application submission. Long and intentionally
    structured — we'd rather collect everything once than chase the
    prospect twice. All numeric fields are nullable (some firms won't
    publish hard underwriting boxes upfront)."""

    # Company
    company_name: str = Field(min_length=1, max_length=160)
    legal_entity_type: str | None = Field(default=None, max_length=40)
    formation_state: str | None = Field(default=None, max_length=40)
    ein: str | None = Field(default=None, max_length=20)
    years_in_business: int | None = Field(default=None, ge=0, le=200)
    website: str | None = Field(default=None, max_length=240)

    # Lending appetite
    loan_types: list[str] = Field(default_factory=list, max_length=20)
    loan_size_min: int | None = Field(default=None, ge=0, le=10_000_000_000)
    loan_size_max: int | None = Field(default=None, ge=0, le=10_000_000_000)
    geographic_states: list[str] = Field(default_factory=list, max_length=60)
    asset_classes: list[str] = Field(default_factory=list, max_length=20)

    # Capital & volume
    capital_source: str | None = Field(default=None, max_length=80)
    aum_band: str | None = Field(default=None, max_length=40)
    monthly_origination_band: str | None = Field(default=None, max_length=40)

    # Underwriting box
    max_ltv: float | None = Field(default=None, ge=0.0, le=1.5)
    max_ltc: float | None = Field(default=None, ge=0.0, le=1.5)
    min_dscr: float | None = Field(default=None, ge=0.0, le=10.0)
    min_fico: int | None = Field(default=None, ge=300, le=900)
    rate_range: str | None = Field(default=None, max_length=80)

    # Contact + submission
    contact_name: str = Field(min_length=1, max_length=160)
    contact_title: str | None = Field(default=None, max_length=80)
    contact_email: str = Field(min_length=5, max_length=320)
    contact_phone: str | None = Field(default=None, max_length=40)
    submission_email: str | None = Field(default=None, max_length=320)
    submission_portal_url: str | None = Field(default=None, max_length=320)
    average_response_time: str | None = Field(default=None, max_length=80)
    notes: str | None = Field(default=None, max_length=4000)

    consent: bool


class CapitalPartnerApplicationResult(BaseModel):
    ok: bool
    id: str


@router.post(
    "/capital-partner-application",
    response_model=CapitalPartnerApplicationResult,
)
async def capital_partner_application(
    payload: CapitalPartnerApplicationIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CapitalPartnerApplicationResult:
    """Public "Become a Lending Partner" form. Persists to
    capital_partner_applications (pending) and notifies the team."""
    from app.models.capital_partner_application import CapitalPartnerApplication

    if payload.consent is not True:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Consent to be contacted is required.",
        )
    if "@" not in payload.contact_email or "." not in payload.contact_email:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A valid contact email is required so we can reply.",
        )

    ip = (request.client.host if request.client else "?") or "?"
    now = time.monotonic()
    last = _LAST_SUBMIT.get(ip)
    if last is not None and (now - last) < _THROTTLE_SECONDS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Please wait a moment before submitting again.",
        )
    _LAST_SUBMIT[ip] = now

    app_row = CapitalPartnerApplication(
        company_name=payload.company_name,
        legal_entity_type=payload.legal_entity_type,
        formation_state=payload.formation_state,
        ein=payload.ein,
        years_in_business=payload.years_in_business,
        website=payload.website,
        loan_types=payload.loan_types,
        loan_size_min=payload.loan_size_min,
        loan_size_max=payload.loan_size_max,
        geographic_states=payload.geographic_states,
        asset_classes=payload.asset_classes,
        capital_source=payload.capital_source,
        aum_band=payload.aum_band,
        monthly_origination_band=payload.monthly_origination_band,
        max_ltv=payload.max_ltv,
        max_ltc=payload.max_ltc,
        min_dscr=payload.min_dscr,
        min_fico=payload.min_fico,
        rate_range=payload.rate_range,
        contact_name=payload.contact_name,
        contact_title=payload.contact_title,
        contact_email=payload.contact_email,
        contact_phone=payload.contact_phone,
        submission_email=payload.submission_email,
        submission_portal_url=payload.submission_portal_url,
        average_response_time=payload.average_response_time,
        notes=payload.notes,
        status="pending",
        consent=payload.consent,
        ip_address=ip if ip != "?" else None,
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
    )
    db.add(app_row)
    await db.flush()
    await db.refresh(app_row)

    # Best-effort email notification to the founder + support inbox.
    subject = f"New capital partner application — {payload.company_name}"
    body_summary = _format_capital_partner_summary(payload, app_row.id)
    sent_to: list[str] = []
    try:
        from app.services.email.gmail_client import gmail_config, send_message

        cfg = gmail_config()
        if cfg is not None:
            for to_email in CAPITAL_PARTNER_NOTIFY:
                try:
                    send_message(cfg, to=to_email, subject=subject, body=body_summary)
                    sent_to.append(to_email)
                except Exception:
                    log.exception(
                        "capital-partner-application: send to %s failed", to_email
                    )
        else:
            log.warning(
                "capital-partner-application: gmail not configured — lead logged only"
            )
    except Exception:
        log.exception("capital-partner-application: email send block failed")

    # Audit-trail Activity row (paired with the dedicated DB row so the
    # firehose log still shows every public submission).
    db.add(
        Activity(
            loan_id=None,
            actor_id=None,
            actor_label="public",
            kind="capital_partner.application",
            summary=f"Capital partner application from {payload.company_name}",
            payload={
                "application_id": str(app_row.id),
                "company_name": payload.company_name,
                "contact_name": payload.contact_name,
                "contact_email": payload.contact_email,
                "loan_types": payload.loan_types,
                "emailed_to": sent_to,
            },
        )
    )
    await db.flush()
    return CapitalPartnerApplicationResult(ok=True, id=str(app_row.id))


def _format_capital_partner_summary(
    p: "CapitalPartnerApplicationIn", app_id: object
) -> str:
    """Render a plain-text email summary of an application. Operator
    clicks the QCDashboard link at the bottom to review/approve/deny."""
    lines: list[str] = [
        f"Application ID: {app_id}",
        "",
        "--- Company ---",
        f"Company: {p.company_name}",
    ]
    if p.legal_entity_type:
        lines.append(f"Entity type: {p.legal_entity_type}")
    if p.formation_state:
        lines.append(f"Formation state: {p.formation_state}")
    if p.years_in_business is not None:
        lines.append(f"Years in business: {p.years_in_business}")
    if p.website:
        lines.append(f"Website: {p.website}")

    lines += ["", "--- Lending appetite ---"]
    lines.append(f"Loan types: {', '.join(p.loan_types) or '(unspecified)'}")
    if p.loan_size_min is not None or p.loan_size_max is not None:
        lo = f"${p.loan_size_min:,.0f}" if p.loan_size_min is not None else "?"
        hi = f"${p.loan_size_max:,.0f}" if p.loan_size_max is not None else "?"
        lines.append(f"Loan size: {lo} – {hi}")
    lines.append(
        f"States: {', '.join(p.geographic_states) or '(unspecified)'}"
    )
    lines.append(
        f"Asset classes: {', '.join(p.asset_classes) or '(unspecified)'}"
    )

    lines += ["", "--- Capital & volume ---"]
    if p.capital_source:
        lines.append(f"Capital source: {p.capital_source}")
    if p.aum_band:
        lines.append(f"AUM band: {p.aum_band}")
    if p.monthly_origination_band:
        lines.append(f"Monthly origination band: {p.monthly_origination_band}")

    box_bits: list[str] = []
    if p.max_ltv is not None:
        box_bits.append(f"max LTV {p.max_ltv * 100:.1f}%")
    if p.max_ltc is not None:
        box_bits.append(f"max LTC {p.max_ltc * 100:.1f}%")
    if p.min_dscr is not None:
        box_bits.append(f"min DSCR {p.min_dscr:.2f}x")
    if p.min_fico is not None:
        box_bits.append(f"min FICO {p.min_fico}")
    if p.rate_range:
        box_bits.append(f"rates {p.rate_range}")
    if box_bits:
        lines += ["", "--- Underwriting box ---", "; ".join(box_bits)]

    lines += [
        "",
        "--- Contact ---",
        f"Name: {p.contact_name}",
    ]
    if p.contact_title:
        lines.append(f"Title: {p.contact_title}")
    lines.append(f"Email: {p.contact_email}")
    if p.contact_phone:
        lines.append(f"Phone: {p.contact_phone}")
    if p.submission_email:
        lines.append(f"Submission email: {p.submission_email}")
    if p.submission_portal_url:
        lines.append(f"Submission portal: {p.submission_portal_url}")
    if p.average_response_time:
        lines.append(f"Average response time: {p.average_response_time}")
    if p.notes:
        lines += ["", "--- Notes ---", p.notes]

    lines += [
        "",
        "Review in QCDashboard:",
        f"  https://app.qualifiedcommercial.com/admin/capital-partner-applications/{app_id}",
    ]
    return "\n".join(lines)
