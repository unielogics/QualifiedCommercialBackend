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

Kept deliberately small + defensive (length caps, consent gate, a
best-effort per-IP throttle). No DB writes other than the Activity log.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, time as dt_time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.enums import CalendarEventKind, CalendarEventSource, CalendarEventStatus
from app.models.activity import Activity
from app.models.broker import Broker
from app.models.event import CalendarEvent
from app.models.user import User
from app.routers.fred import _build_summary, _current_spreads
from app.schemas.broker_settings import AgentBookingSettings, AgentSettingsData
from app.schemas.fred import FredSeriesSummary
from app.services import fred as fred_service

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
_THROTTLE_SECONDS = 20.0


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
    phone: str = Field(min_length=3, max_length=40)
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
    phone: str | None = Field(default=None, max_length=40)
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
# Public broker booking page
# ---------------------------------------------------------------------------


class PublicBookingSlot(BaseModel):
    starts_at: datetime
    label: str
    date_label: str


class PublicBookingProfile(BaseModel):
    slug: str
    agent_name: str
    title: str
    intro: str
    primary_color: str
    background_color: str
    duration_min: int
    timezone: str
    slots: list[PublicBookingSlot]


class PublicBookingCreate(BaseModel):
    starts_at: datetime
    full_name: str = Field(min_length=1, max_length=160)
    email: str = Field(min_length=5, max_length=320)
    phone: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=1000)


class PublicBookingCreateResult(BaseModel):
    ok: bool
    event_id: str


@router.get("/booking/{slug}", response_model=PublicBookingProfile)
async def public_booking_profile(
    slug: str,
    db: AsyncSession = Depends(get_db),
) -> PublicBookingProfile:
    broker, user, booking = await _load_public_booking(db, slug)
    slots = await _available_booking_slots(db, broker, booking)
    return PublicBookingProfile(
        slug=booking.slug or slug,
        agent_name=broker.display_name or user.name or "Qualified Commercial",
        title=booking.title or f"Book a meeting with {broker.display_name or user.name or 'Qualified Commercial'}",
        intro=booking.intro or "Choose a time that works for you. You will receive a confirmation after booking.",
        primary_color=booking.primary_color,
        background_color=booking.background_color,
        duration_min=booking.duration_min,
        timezone=booking.timezone,
        slots=slots,
    )


@router.post("/booking/{slug}", response_model=PublicBookingCreateResult)
async def public_booking_create(
    slug: str,
    payload: PublicBookingCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PublicBookingCreateResult:
    if "@" not in payload.email or "." not in payload.email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A valid email is required.")

    ip = (request.client.host if request.client else "?") or "?"
    now = time.monotonic()
    last = _LAST_SUBMIT.get(ip)
    if last is not None and (now - last) < _THROTTLE_SECONDS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Please wait a moment before submitting again.",
        )
    _LAST_SUBMIT[ip] = now

    broker, user, booking = await _load_public_booking(db, slug)
    starts_at = _to_utc_minute(payload.starts_at)
    slots = await _available_booking_slots(db, broker, booking)
    valid_slot = any(abs((slot.starts_at - starts_at).total_seconds()) < 1 for slot in slots)
    if not valid_slot:
        raise HTTPException(status.HTTP_409_CONFLICT, "That time is no longer available.")

    who = f"{payload.full_name} <{payload.email}>"
    description = (
        "Booked from the agent public booking page.\n"
        f"Name: {payload.full_name}\n"
        f"Email: {payload.email}\n"
        f"Phone: {payload.phone or '(not provided)'}\n\n"
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
        owner_user_id=broker.user_id,
        external_ref_kind="public_booking",
        external_ref_id=str(uuid.uuid4()),
    )
    db.add(ev)
    await db.flush()

    db.add(
        Activity(
            loan_id=None,
            actor_id=None,
            actor_label="public",
            kind="calendar.public_booking",
            summary=f"Public booking created for {broker.display_name}: {payload.full_name}",
            payload={
                "event_id": str(ev.id),
                "broker_id": str(broker.id),
                "broker_user_id": str(broker.user_id),
                "invitee_name": payload.full_name,
                "invitee_email": payload.email,
                "starts_at": starts_at.isoformat(),
                "duration_min": booking.duration_min,
                "source": "public_booking_page",
            },
        )
    )

    _notify_agent_of_booking(user, broker, payload, starts_at, booking)
    await db.flush()
    return PublicBookingCreateResult(ok=True, event_id=str(ev.id))


async def _load_public_booking(
    db: AsyncSession,
    slug: str,
) -> tuple[Broker, User, AgentBookingSettings]:
    rows = (
        await db.execute(
            select(Broker, User)
            .join(User, Broker.user_id == User.id)
            .where(User.deleted_at.is_(None))
        )
    ).all()
    for broker, user in rows:
        try:
            data = AgentSettingsData.model_validate(broker.settings_data or {})
        except Exception:
            log.warning("public-booking: broker settings failed validation for %s", broker.id)
            continue
        booking = data.booking
        if booking and booking.enabled and booking.slug == slug:
            return broker, user, booking
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking page not found.")


async def _available_booking_slots(
    db: AsyncSession,
    broker: Broker,
    booking: AgentBookingSettings,
) -> list[PublicBookingSlot]:
    tz = _booking_tz(booking.timezone)
    now_local = datetime.now(tz)
    earliest_local = _round_up_to_step(now_local + timedelta(hours=2), 15)
    window_end_local = (now_local + timedelta(days=15)).replace(hour=23, minute=59, second=0, microsecond=0)
    busy_rows = (
        await db.execute(
            select(CalendarEvent)
            .where(
                CalendarEvent.owner_user_id == broker.user_id,
                CalendarEvent.status != CalendarEventStatus.CANCELLED,
                CalendarEvent.starts_at >= now_local.astimezone(timezone.utc),
                CalendarEvent.starts_at <= window_end_local.astimezone(timezone.utc),
            )
            .order_by(CalendarEvent.starts_at)
        )
    ).scalars().all()
    busy = [
        (
            ev.starts_at.astimezone(tz),
            ev.starts_at.astimezone(tz) + timedelta(minutes=max(15, ev.duration_min or booking.duration_min)),
        )
        for ev in busy_rows
    ]

    start_min = _parse_hhmm(booking.start_time)
    end_min = _parse_hhmm(booking.end_time)
    duration = timedelta(minutes=booking.duration_min)
    slots: list[PublicBookingSlot] = []

    for offset in range(15):
        day = now_local.date() + timedelta(days=offset)
        if _js_weekday(day) not in booking.available_days:
            continue
        day_start = datetime.combine(day, dt_time(start_min // 60, start_min % 60), tzinfo=tz)
        day_end = datetime.combine(day, dt_time(end_min // 60, end_min % 60), tzinfo=tz)
        cursor = max(day_start, earliest_local if day == earliest_local.date() else day_start)
        cursor = _round_up_to_step(cursor, 15)
        while cursor + duration <= day_end:
            slot_end = cursor + duration
            if not any(cursor < busy_end and slot_end > busy_start for busy_start, busy_end in busy):
                starts_utc = cursor.astimezone(timezone.utc).replace(second=0, microsecond=0)
                slots.append(
                    PublicBookingSlot(
                        starts_at=starts_utc,
                        label=_slot_time_label(cursor),
                        date_label=_slot_date_label(cursor),
                    )
                )
                if len(slots) >= 80:
                    return slots
            cursor += duration
    return slots


def _booking_tz(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        try:
            return ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError:
            return timezone.utc


def _parse_hhmm(value: str) -> int:
    hours, minutes = [int(part) for part in value.split(":")]
    return hours * 60 + minutes


def _js_weekday(day) -> int:
    return (day.weekday() + 1) % 7


def _round_up_to_step(value: datetime, step_min: int) -> datetime:
    value = value.replace(second=0, microsecond=0)
    minute = value.minute
    remainder = minute % step_min
    if remainder:
        value += timedelta(minutes=step_min - remainder)
    return value


def _slot_time_label(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _slot_date_label(value: datetime) -> str:
    return f"{value.strftime('%a, %b')} {value.day}"


def _to_utc_minute(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0)


def _notify_agent_of_booking(
    user: User,
    broker: Broker,
    payload: PublicBookingCreate,
    starts_at: datetime,
    booking: AgentBookingSettings,
) -> None:
    try:
        from app.services.email.gmail_client import gmail_config, send_message

        cfg = gmail_config()
        if cfg is None:
            return
        tz = _booking_tz(booking.timezone)
        when = starts_at.astimezone(tz).strftime("%A, %B %d at %I:%M %p %Z")
        body = (
            f"New booking for {broker.display_name}\n\n"
            f"When: {when}\n"
            f"Duration: {booking.duration_min} minutes\n"
            f"Name: {payload.full_name}\n"
            f"Email: {payload.email}\n"
            f"Phone: {payload.phone or '(not provided)'}\n\n"
            f"Notes:\n{payload.notes or '(none)'}\n"
        )
        send_message(cfg, to=user.email, subject=f"New booked call — {payload.full_name}", body=body)
    except Exception:
        log.exception("public-booking: agent notification failed")


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
