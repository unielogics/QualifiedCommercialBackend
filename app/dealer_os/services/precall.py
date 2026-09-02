"""Pre-call prep: the draft file a booking opens, and the sequence that gets the
client to finish it before the call.

Every booking opens a draft dealer file with its secure room, so the client
can list who owns the business, connect the bank (Plaid) and authorize a soft
credit check (iSoftPull) before anyone talks to them. The draft IS the file:
when the rep converts the appointment, nothing is copied — the row is promoted
in place.

The nudge sequence reuses the booking-reminder rows and dispatcher rather than
adding an engine. Pre-call rows are ``kind='precall'`` with an absolute
``due_at`` (anchored to the booking, not the call), so rescheduling the call
never moves them, and every send re-checks readiness so a client who finished
is never nagged.

Consent basis is transactional, under the booking: email always, SMS only
with the transactional consent captured at booking, STOP halts SMS and the
email stop link halts email.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import CalendarEventStatus
from app.models.booking_notification import BookingNotification, BookingNotificationReminder
from app.models.booking_settings import BookingSettings
from app.models.event import CalendarEvent
from app.models.sms_message import SmsMessage
from app.models.user import User
from app.services import message_render
from app.services.email import ses_client
from app.services.notifications import notify_users
from app.services.sms import optout

from ..models import (
    DealerApplicationContact,
    DealerBusiness,
    DealerDocument,
    DealerOwner,
    DealerPlaidItem,
    DealerRepAppointment,
    DealerRepContact,
    DealerRepLead,
    DealerSourceConnection,
)
from . import client_room, consent_delivery
from . import sms_consent as sms_consent_svc
from .audit import log_action
from .targets import propose_targets

log = logging.getLogger(__name__)

# --- tunables ----------------------------------------------------------------

#: Owners at or above this share must authorize credit (mirrors the desk rule).
OWNER_CREDIT_THRESHOLD = 20.0
MAX_OWNERS = 5

#: Client-facing automated SMS only inside these local hours.
QUIET_START_HOUR = 9
QUIET_END_HOUR = 20
#: No two automated SMS to one number closer than this, except the day-of reminder.
SMS_SPACING_HOURS = 20
#: Nothing automated goes out inside the last hour before the call.
FINAL_CUTOFF_HOURS = 1

STEP_KEYS = ("nudge_1", "nudge_2")
#: Reminder rows are unique on (notification, channel, minutes_before). Pre-call
#: rows are booking-anchored and carry no minutes-before, so each step uses a
#: distinct negative marker to keep the constraint honest.
STEP_MARKERS = {"nudge_1": -1, "nudge_2": -2}

#: Appointment kinds that are not funding conversations.
SKIP_KINDS = frozenset({"signing", "lender_call"})

#: Where a booking came from. Only field-desk bookings open the Capital OS
#: draft at booking time; every other origin gets its file from the calendar
#: outcome (AI intake, funding loan, link existing) — "the right file based on
#: origin".
ORIGINS = ("field_desk", "calendar", "public", "intake")
DRAFT_ORIGINS = frozenset({"field_desk"})


def origin_for(explicit: str | None, role: str | None) -> str:
    """The origin of a rep-appointment booking.

    The surface says so when it can (the rep app sends field_desk, the
    operator calendar sends calendar). Without that, a field rep is booking
    for the field desk and anyone else is booking from the calendar.
    """
    if explicit in ORIGINS:
        return explicit
    return "field_desk" if (role or "").lower().endswith("field_rep") else "calendar"


def opens_draft(origin: str | None) -> bool:
    return origin in DRAFT_ORIGINS

#: Reasons a sequence stops. Stored on booking_notifications.precall_stop_reason.
STOP_REASONS = (
    "completed", "call_started", "cancelled", "sms_stop", "email_stop",
    "superseded", "host_disabled", "converted", "archived",
)

# --- default wording (host-overridable in booking settings) --------------------

DEFAULT_STEPS: dict[str, dict] = {
    "nudge_1": {
        "after_hours": 24,       # after booking
        "min_lead_hours": 36,    # only when the call is at least this far out
        "channel": "both",
        "email_subject": "Before your call with {rep}: {done} done",
        "email_body": (
            "Hi {first},\n\n"
            "A quick nudge before your call on {date}. Still needed: {missing}.\n\n"
            "It takes about 10 minutes and lets {rep} give you real numbers instead of estimates.\n\n"
            "Open your secure room: {room_link}\n"
            "(Your PIN was sent to you when you booked.)\n\n"
            "Qualified Commercial"
        ),
        "sms": "Qualified Commercial: before your call {date}, please {missing}: {room_link} (PIN sent earlier).",
    },
    "nudge_2": {
        "before_hours": 24,          # before the call
        "fallback_before_hours": 4,  # when the call is sooner than that
        "min_lead_hours": 6,
        "spacing_hours": 12,         # not within this of nudge_1
        "channel": "both",
        "email_subject": "Your call with {rep} is coming up — {done} done",
        "email_body": (
            "Hi {first},\n\n"
            "Your call with {rep} is {time}. If you can, finish these first so we can talk real numbers: {missing}.\n\n"
            "Open your secure room: {room_link}\n\n"
            "If you can't get to it, no problem — we'll do it together on the call.\n\n"
            "Qualified Commercial"
        ),
        "sms": "Qualified Commercial: your call with {rep} is {time}. Finish {missing} now so we can talk real numbers: {room_link}",
    },
}

DEFAULT_MESSAGES: dict[str, str] = {
    # Appended to the confirmation email (and its ICS description).
    "precall_block": (
        "Before your call — about 10 minutes:\n"
        "1. Confirm who owns {business}.\n"
        "2. Connect the business bank account (read-only, through Plaid) — or upload statements.\n"
        "3. Authorize a soft credit check for each owner with 20% or more (no impact on your score).\n"
        "Doing this first lets {rep} talk real numbers on the call.\n"
        "Open your secure room: {room_link}\n"
        "Your room PIN was sent to you separately."
    ),
    "confirmation_sms": (
        "Qualified Commercial: your call with {rep} is confirmed for {time}. "
        "Your secure room PIN is {pin}. Get ready before the call: {room_link}"
    ),
    "pin_email_subject": "Your secure room PIN",
    "pin_email_body": (
        "Hi {first},\n\n"
        "Your secure room PIN is {pin}. Keep it private — you will need it to open your room: {room_link}\n\n"
        "You can choose your own PIN the first time you open the room.\n\n"
        "Qualified Commercial"
    ),
    "pin_sms": "Qualified Commercial: your secure room PIN is {pin}. Keep it private.",
    # The {precall} placeholder inside existing reminders renders as this while
    # something is still needed, and as nothing once the checklist is done.
    "reminder_precall_line": "Still needed before your call: {missing} → {room_link}",
    "stop_footer": "You are receiving this because you booked a call with Qualified Commercial. Stop these emails: {stop_link}",
}


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "America/New_York")
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/New_York")


def _frontend_base() -> str:
    settings = get_settings()
    base = (getattr(settings, "frontend_app_url", "") or "").rstrip("/")
    if settings.app_env.lower() == "production" and (
        not base or "localhost" in base or "127.0.0.1" in base or base.startswith("http://")
    ):
        base = "https://app.qualifiedcommercial.com"
    return base


def _link_secret() -> bytes:
    """Key for the signed links in pre-call emails. Derived from the app's own
    secret so nothing extra is provisioned; a rotated secret simply retires old
    links, which is the right failure."""
    settings = get_settings()
    seed = (
        getattr(settings, "clerk_secret_key", "")
        or getattr(settings, "provider_secrets_encryption_key", "")
        or "qualified-commercial-precall"
    )
    return hashlib.sha256(f"precall-links:{seed}".encode()).digest()


def link_signature(notice_id: UUID | str, purpose: str) -> str:
    return hmac.new(_link_secret(), f"{purpose}:{notice_id}".encode(), hashlib.sha256).hexdigest()[:32]


def link_valid(notice_id: UUID | str, purpose: str, signature: str) -> bool:
    return hmac.compare_digest(link_signature(notice_id, purpose), (signature or "").strip())


def stop_url(notice: BookingNotification) -> str:
    """One-tap email stop link: {app}/prep/{notice_id}/{signature}/stop."""
    return f"{_frontend_base()}/prep/{notice.id}/{link_signature(notice.id, 'stop')}/stop"


# --- readiness ----------------------------------------------------------------


@dataclass
class OwnerState:
    id: UUID
    first_name: str
    last_name: str
    ownership_pct: float | None
    is_primary: bool
    required: bool
    has_email: bool
    has_phone: bool
    #: not_required | todo | sent | done | declined | failed
    credit_status: str
    #: Rows with a completed pull or an outstanding link are read-only to the client.
    editable: bool


@dataclass
class Readiness:
    ownership_complete: bool
    ownership_total: float
    contact_complete: bool
    owners: list[OwnerState] = field(default_factory=list)
    bank_complete: bool = False
    bank_detail: str = ""
    credit_complete: bool = False
    credit_required: int = 0
    credit_done: int = 0

    @property
    def ownership_step_complete(self) -> bool:
        return self.ownership_complete and self.contact_complete

    @property
    def complete(self) -> bool:
        return self.ownership_step_complete and self.bank_complete and self.credit_complete

    @property
    def done_count(self) -> int:
        return int(self.ownership_step_complete) + int(self.bank_complete) + int(self.credit_complete)

    @property
    def missing(self) -> list[str]:
        out: list[str] = []
        if not self.ownership_step_complete:
            out.append("confirm who owns the business")
        if not self.bank_complete:
            out.append("connect your business bank")
        if not self.credit_complete:
            out.append("authorize your soft credit check")
        return out

    @property
    def missing_phrase(self) -> str:
        parts = self.missing
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        return ", ".join(parts[:-1]) + " and " + parts[-1]

    @property
    def done_label(self) -> str:
        return f"{self.done_count} of 3"


def _owner_credit_status(owner: DealerOwner, required: bool) -> str:
    if not required:
        return "not_required"
    if owner.credit_pulled_at is not None:
        return "done"
    if owner.credit_workflow_status == "declined":
        return "declined"
    if owner.credit_provider_error_category:
        return "failed"
    if owner.invite_token_hash:
        return "sent"
    return "todo"


async def readiness(db: AsyncSession, dealer: DealerBusiness) -> Readiness:
    """What the client has and has not done, computed from the file itself.

    Mirrors the desk's ownership rule (``_owner_requirement_state``) so the room
    can never pass a state the credit endpoints reject: owners must total
    100.00% and every owner at or above 20% needs an email and a valid phone.
    """
    owners = list(
        (
            await db.execute(
                select(DealerOwner)
                .where(DealerOwner.dealer_id == dealer.id)
                .order_by(DealerOwner.is_primary.desc(), DealerOwner.ownership_pct.desc().nullslast(), DealerOwner.last_name)
            )
        ).scalars().all()
    )
    total = round(sum(float(o.ownership_pct or 0) for o in owners), 2)
    ownership_complete = bool(owners) and abs(total - 100.0) < 0.005
    states: list[OwnerState] = []
    required_count = done_count = 0
    contact_complete = True
    credit_terminal_all = True
    for o in owners:
        required = float(o.ownership_pct or 0) >= OWNER_CREDIT_THRESHOLD
        has_email = bool((o.email or "").strip()) and "@" in (o.email or "")
        has_phone = consent_delivery.normalize_phone(o.phone) is not None
        status_ = _owner_credit_status(o, required)
        if required:
            required_count += 1
            if not (has_email and has_phone):
                contact_complete = False
            if status_ == "done":
                done_count += 1
            elif status_ not in {"declined", "failed"}:
                credit_terminal_all = False
        states.append(
            OwnerState(
                id=o.id,
                first_name=o.first_name,
                last_name=o.last_name,
                ownership_pct=float(o.ownership_pct) if o.ownership_pct is not None else None,
                is_primary=bool(o.is_primary),
                required=required,
                has_email=has_email,
                has_phone=has_phone,
                credit_status=status_,
                editable=o.credit_pulled_at is None and not o.invite_token_hash,
            )
        )
    credit_complete = ownership_complete and contact_complete and required_count > 0 and credit_terminal_all

    plaid = (
        await db.execute(
            select(DealerPlaidItem.institution_name)
            .where(DealerPlaidItem.dealer_id == dealer.id, DealerPlaidItem.status == "active")
            .limit(1)
        )
    ).scalar_one_or_none()
    bank_complete = plaid is not None
    bank_detail = f"Connected: {plaid}" if plaid else ""
    if not bank_complete:
        statements = (
            await db.execute(
                select(func.count())
                .select_from(DealerDocument)
                .where(
                    DealerDocument.dealer_id == dealer.id,
                    or_(DealerDocument.kind == "bank_statement", DealerDocument.detected_kind == "bank_statement"),
                )
            )
        ).scalar_one()
        if statements:
            bank_complete = True
            bank_detail = f"{statements} bank statement{'s' if statements != 1 else ''} uploaded"

    return Readiness(
        ownership_complete=ownership_complete,
        ownership_total=total,
        contact_complete=contact_complete,
        owners=states,
        bank_complete=bank_complete,
        bank_detail=bank_detail,
        credit_complete=credit_complete,
        credit_required=required_count,
        credit_done=done_count,
    )


# --- the draft file -----------------------------------------------------------


async def next_case_ref(db: AsyncSession) -> str:
    """QC-{year}-{5 digits}; same derivation as the desk's helper."""
    year = datetime.now(UTC).year
    prefix = f"QC-{year}-"
    top = (
        await db.execute(
            select(func.max(DealerBusiness.case_ref)).where(DealerBusiness.case_ref.like(f"{prefix}%"))
        )
    ).scalar_one_or_none()
    n = 0
    if top:
        try:
            n = int(top.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            n = 0
    return f"{prefix}{n + 1:05d}"


def _amount(value: str | None) -> float | None:
    import re

    if not value:
        return None
    cleaned = re.sub(r"[^0-9.]", "", value)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _split_name(full: str) -> tuple[str, str]:
    parts = (full or "").strip().split()
    if not parts:
        return "Client", "Owner"
    if len(parts) == 1:
        return parts[0], "Owner"
    return parts[0], " ".join(parts[1:])


@dataclass
class DraftResult:
    dealer: DealerBusiness
    room: client_room.ClientRoom
    #: True when this call opened the file; False when the booking attached to
    #: an existing one (a rebook, or a booking on an open file).
    created: bool


async def _find_reusable_draft(
    db: AsyncSession, *, owner_user_id: UUID, email: str | None, phone: str | None
) -> DealerBusiness | None:
    """A public rebook by the same person should land on their existing draft."""
    since = datetime.now(UTC) - timedelta(days=90)
    conds = []
    if email:
        conds.append(func.lower(DealerBusiness.email) == email.strip().lower())
    if phone:
        conds.append(DealerBusiness.phone == phone)
    if not conds:
        return None
    return (
        await db.execute(
            select(DealerBusiness)
            .where(
                DealerBusiness.owner_user_id == owner_user_id,
                DealerBusiness.archived_at.is_(None),
                DealerBusiness.draft_source == "booking",
                DealerBusiness.created_at >= since,
                or_(*conds),
            )
            .order_by(DealerBusiness.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def create_draft_for_booking(
    db: AsyncSession,
    *,
    notice: BookingNotification,
    event: CalendarEvent,
    booking: BookingSettings,
    host: User,
    appointment: DealerRepAppointment | None = None,
    contact: DealerRepContact | None = None,
    dealer: DealerBusiness | None = None,
    booked_by: User | None = None,
    company: str | None = None,
    notes: str | None = None,
) -> DraftResult:
    """Open (or attach) the draft file for a booking and its secure room.

    Flushes, never commits: it rides in the booking transaction so a failed
    booking never leaves an orphan file. Returns the room with its plaintext
    PIN only when the room was minted here.
    """
    owner_user_id = (booked_by.id if booked_by is not None else None) or host.id
    created = False
    if dealer is None and appointment is not None and appointment.dealer_id:
        dealer = await db.get(DealerBusiness, appointment.dealer_id)
        if dealer is not None and dealer.archived_at is not None:
            dealer = None
    if dealer is None and contact is not None and contact.dealer_id:
        dealer = await db.get(DealerBusiness, contact.dealer_id)
        if dealer is not None and dealer.archived_at is not None:
            dealer = None
    if dealer is None:
        dealer = await _find_reusable_draft(
            db, owner_user_id=owner_user_id, email=notice.invitee_email, phone=notice.invitee_phone
        )
    if dealer is None:
        created = True
        business_name = (company or "").strip() or notice.invitee_name
        dealer = DealerBusiness(
            name=business_name[:180],
            legal_name=business_name[:180],
            email=notice.invitee_email,
            phone=notice.invitee_phone,
            address=notice.full_address,
            funding_goal=_amount(notice.requested_amount),
            client_requested_amount=_amount(notice.requested_amount),
            client_requested_program=(notice.program_name or "")[:80] or None,
            notes="\n\n".join(p for p in [notes, "Draft opened from a booked call."] if p),
            application_lifecycle="draft",
            status="draft",
            draft_source="booking",
            owner_user_id=owner_user_id,
            case_ref=await next_case_ref(db),
        )
        db.add(dealer)
        await db.flush()
        db.add(DealerSourceConnection(dealer_id=dealer.id, kind="uploads", status="active"))
        try:
            await propose_targets(db, dealer)
        except Exception:  # noqa: BLE001 — targets are advisory, never block a booking
            log.exception("precall: propose_targets failed for draft %s", dealer.id)
        actor = booked_by or host
        db.add(
            DealerRepLead(
                dealer_id=dealer.id,
                rep_user_id=owner_user_id,
                status="draft",
                status_history=[{
                    "at": datetime.now(UTC).isoformat(),
                    "from": None,
                    "to": "draft",
                    "by": str(actor.id),
                    "by_name": actor.name,
                    "source": "booking",
                }],
            )
        )
        first, last = _split_name(notice.invitee_name)
        db.add(
            DealerOwner(
                dealer_id=dealer.id,
                first_name=first[:80],
                last_name=last[:80],
                email=notice.invitee_email,
                phone=notice.invitee_phone,
                is_primary=True,
                ownership_pct=None,
            )
        )
        if contact is not None:
            db.add(
                DealerApplicationContact(
                    dealer_id=dealer.id, contact_id=contact.id, relationship="primary_contact", is_primary=True
                )
            )
        await db.flush()
        await log_action(
            db, dealer.id, booked_by or host, "draft.created_from_booking", "dealer",
            entity_id=dealer.id,
            after={"event_id": str(event.id), "booking_notification_id": str(notice.id), "invitee": notice.invitee_name},
        )

    if contact is not None and contact.dealer_id is None:
        contact.dealer_id = dealer.id
    if appointment is not None and appointment.dealer_id is None:
        appointment.dealer_id = dealer.id

    # The room. Fresh bucket for booking drafts: adoption matches on email
    # alone and could hand a stranger's intake room to a public booking.
    room = await client_room.ensure_room(db, dealer, adopt_intake=False)
    if created and room.passcode is None:
        # A reused bucket on a brand-new draft is impossible with adopt_intake
        # off, but a rotated code is the honest fallback if it ever happens.
        room = await client_room.rotate_passcode(db, dealer)

    notice.precall_dealer_id = dealer.id

    # Materialise the booking's transactional SMS consent on the file, so the
    # desk's consent-link texts (which key on dos_sms_consent) work later.
    if notice.sms_consent and notice.invitee_phone:
        try:
            await sms_consent_svc.record_consent(
                db,
                dealer_id=dealer.id,
                phone_e164=notice.invitee_phone,
                kind="transactional",
                method=notice.sms_consent_method or "self_web",
                captured_by_user_id=booked_by.id if booked_by is not None else None,
                captured_by_name=booked_by.name if booked_by is not None else "Public booking page",
                consenter_name=notice.invitee_name,
                ip_address=notice.sms_consent_ip,
                user_agent=notice.sms_consent_user_agent,
            )
        except Exception:  # noqa: BLE001 — the booking proof still exists on the notice
            log.exception("precall: could not record SMS consent grant on draft %s", dealer.id)
    await db.flush()
    return DraftResult(dealer=dealer, room=room, created=created)


async def promote_draft(
    db: AsyncSession, dealer: DealerBusiness, user: User | None, *, source: str
) -> bool:
    """Draft → active, in place. Nothing is copied: owners, bank consent,
    Plaid items, credit pulls and uploads are already on the row. Returns
    False when the file was not a draft."""
    if dealer.application_lifecycle != "draft":
        return False
    dealer.application_lifecycle = "active"
    if dealer.status == "draft":
        dealer.status = "active"
    has_uploads = (
        await db.execute(
            select(DealerSourceConnection.id).where(
                DealerSourceConnection.dealer_id == dealer.id, DealerSourceConnection.kind == "uploads"
            ).limit(1)
        )
    ).scalar_one_or_none()
    if has_uploads is None:
        db.add(DealerSourceConnection(dealer_id=dealer.id, kind="uploads", status="active"))
    try:
        await propose_targets(db, dealer)
    except Exception:  # noqa: BLE001
        log.exception("precall: propose_targets failed on promote %s", dealer.id)
    lead = (
        await db.execute(select(DealerRepLead).where(DealerRepLead.dealer_id == dealer.id).limit(1))
    ).scalar_one_or_none()
    if lead is None:
        db.add(DealerRepLead(dealer_id=dealer.id, rep_user_id=dealer.owner_user_id, status="draft"))
    await stop_sequences_for_dealer(db, dealer, reason="converted")
    await log_action(db, dealer.id, user, "draft.promoted", "dealer", entity_id=dealer.id, after={"source": source})
    await db.flush()
    return True


# --- the sequence -------------------------------------------------------------


def step_config(booking: BookingSettings, key: str) -> dict:
    """A step's effective settings: defaults with the host's overrides on top."""
    base = dict(DEFAULT_STEPS[key])
    override = (booking.precall_messages or {}).get(key) or {}
    for name, value in override.items():
        if value in (None, ""):
            continue
        base[name] = value
    return base


def message_text(booking: BookingSettings, key: str) -> str:
    override = ((booking.precall_messages or {}).get(key) or "").strip()
    if not override and key in {"confirmation_sms", "pin_email_subject", "pin_email_body"}:
        override = ((booking.confirmation_messages or {}).get(key) or "").strip()
    return override or DEFAULT_MESSAGES[key]


def _shift_into_window(when: datetime, tz: ZoneInfo) -> datetime:
    local = when.astimezone(tz)
    if local.hour < QUIET_START_HOUR:
        local = local.replace(hour=QUIET_START_HOUR, minute=0, second=0, microsecond=0)
    elif local.hour >= QUIET_END_HOUR:
        local = (local + timedelta(days=1)).replace(hour=QUIET_START_HOUR, minute=0, second=0, microsecond=0)
    return local.astimezone(UTC)


def _step_channels(booking: BookingSettings, cfg: dict, notice: BookingNotification) -> list[str]:
    wanted = (cfg.get("channel") or "both").lower()
    out: list[str] = []
    if wanted in {"email", "both"} and notice.invitee_email:
        out.append("email")
    if wanted in {"sms", "both"} and notice.invitee_phone and notice.sms_consent and booking.reminder_sms_enabled:
        out.append("sms")
    return out


def _plan_steps(
    booking: BookingSettings, notice: BookingNotification, starts_at: datetime, *, now: datetime, tz: ZoneInfo
) -> list[tuple[str, str, datetime]]:
    """(step_key, channel, due_at) for every row the sequence should hold."""
    cutoff = starts_at - timedelta(hours=FINAL_CUTOFF_HOURS)
    lead = (starts_at - now).total_seconds() / 3600
    rows: list[tuple[str, str, datetime]] = []

    n1 = step_config(booking, "nudge_1")
    n1_due: datetime | None = None
    if lead >= float(n1.get("min_lead_hours", 36)):
        n1_due = _shift_into_window(now + timedelta(hours=float(n1.get("after_hours", 24))), tz)
        if n1_due > cutoff:
            n1_due = None
    if n1_due is not None:
        for ch in _step_channels(booking, n1, notice):
            rows.append(("nudge_1", ch, n1_due))

    n2 = step_config(booking, "nudge_2")
    n2_due: datetime | None = None
    before = float(n2.get("before_hours", 24))
    fallback = float(n2.get("fallback_before_hours", 4))
    if lead >= before + 6:
        n2_due = starts_at - timedelta(hours=before)
    elif lead >= float(n2.get("min_lead_hours", 6)):
        n2_due = starts_at - timedelta(hours=fallback)
    if n2_due is not None:
        if n2_due <= now + timedelta(minutes=5):
            n2_due = None
        elif n1_due is not None and (n2_due - n1_due) < timedelta(hours=float(n2.get("spacing_hours", 12))):
            n2_due = None
    if n2_due is not None:
        for ch in _step_channels(booking, n2, notice):
            rows.append(("nudge_2", ch, n2_due))
    return rows


async def schedule(
    db: AsyncSession,
    *,
    notice: BookingNotification,
    booking: BookingSettings,
    event: CalendarEvent,
    dealer: DealerBusiness,
    timezone_name: str | None = None,
) -> list[BookingNotificationReminder]:
    """Insert the pre-call rows for a booking. Flushes, never commits."""
    if not booking.precall_enabled or dealer.is_training:
        return []
    now = datetime.now(UTC)
    tz = _tz(timezone_name or booking.timezone)
    rows: list[BookingNotificationReminder] = []
    for key, channel, due in _plan_steps(booking, notice, event.starts_at, now=now, tz=tz):
        row = BookingNotificationReminder(
            booking_notification_id=notice.id,
            kind="precall",
            step_key=key,
            channel=channel,
            minutes_before=STEP_MARKERS[key],
            due_at=due,
        )
        db.add(row)
        rows.append(row)
    await db.flush()
    return rows


async def _pending_rows(db: AsyncSession, notice: BookingNotification) -> list[BookingNotificationReminder]:
    return list(
        (
            await db.execute(
                select(BookingNotificationReminder).where(
                    BookingNotificationReminder.booking_notification_id == notice.id,
                    BookingNotificationReminder.kind == "precall",
                    BookingNotificationReminder.status == "pending",
                )
            )
        ).scalars().all()
    )


async def stop_sequence(
    db: AsyncSession, notice: BookingNotification, *, reason: str, channels: tuple[str, ...] = ("email", "sms")
) -> int:
    """Cancel pending pre-call rows. ``channels`` narrows it (STOP only halts SMS)."""
    n = 0
    for row in await _pending_rows(db, notice):
        if row.channel in channels:
            row.status = "cancelled"
            row.error = reason
            n += 1
    if set(channels) >= {"email", "sms"} and notice.precall_stopped_at is None:
        notice.precall_stopped_at = datetime.now(UTC)
        notice.precall_stop_reason = reason
    return n


async def stop_sequences_for_dealer(db: AsyncSession, dealer: DealerBusiness, *, reason: str) -> int:
    notices = (
        await db.execute(
            select(BookingNotification).where(
                BookingNotification.precall_dealer_id == dealer.id,
                BookingNotification.precall_stopped_at.is_(None),
            )
        )
    ).scalars().all()
    total = 0
    for notice in notices:
        total += await stop_sequence(db, notice, reason=reason)
    return total


async def retime_after_reschedule(
    db: AsyncSession, *, notice: BookingNotification, booking: BookingSettings, starts_at: datetime, dealer: DealerBusiness | None
) -> None:
    """Booking-anchored rows stay put; anything now inside the last hour is
    cancelled, and the day-before nudge is re-derived from the new time."""
    cutoff = starts_at - timedelta(hours=FINAL_CUTOFF_HOURS)
    pending = await _pending_rows(db, notice)
    for row in pending:
        if row.step_key == "nudge_2" or row.due_at > cutoff:
            row.status = "cancelled"
            row.error = "rescheduled"
    if dealer is None or notice.precall_stopped_at is not None or notice.precall_completed_at is not None:
        return
    now = datetime.now(UTC)
    n2 = step_config(booking, "nudge_2")
    lead = (starts_at - now).total_seconds() / 3600
    before = float(n2.get("before_hours", 24))
    fallback = float(n2.get("fallback_before_hours", 4))
    due: datetime | None = None
    if lead >= before + 6:
        due = starts_at - timedelta(hours=before)
    elif lead >= float(n2.get("min_lead_hours", 6)):
        due = starts_at - timedelta(hours=fallback)
    if due is None or due <= now + timedelta(minutes=5):
        return
    for ch in _step_channels(booking, n2, notice):
        db.add(
            BookingNotificationReminder(
                booking_notification_id=notice.id,
                kind="precall",
                step_key="nudge_2",
                channel=ch,
                # A re-derived nudge_2 needs its own marker so the old, cancelled
                # row does not collide on the schedule constraint.
                minutes_before=STEP_MARKERS["nudge_2"] - 10 * (1 + len([r for r in pending if r.step_key == "nudge_2" and r.channel == ch])),
                due_at=due,
            )
        )
    await db.flush()


# --- rendering ------------------------------------------------------------------


def template_values(
    *,
    notice: BookingNotification,
    event: CalendarEvent,
    booking: BookingSettings,
    host: User,
    dealer: DealerBusiness | None,
    room_link: str,
    ready: Readiness | None,
    pin: str | None = None,
    stop_link: str = "",
    timezone_name: str | None = None,
) -> dict[str, str]:
    tz = _tz(timezone_name or booking.timezone)
    local = event.starts_at.astimezone(tz)
    name = (notice.invitee_name or "").strip() or "there"
    first = name.split()[0] if name != "there" else "there"
    business = (dealer.name if dealer is not None else "") or "your business"
    missing = ready.missing_phrase if ready is not None else ""
    return {
        "{name}": name,
        "{first}": first,
        "{rep}": (host.name or "").strip() or "Qualified Commercial",
        "{business}": business,
        "{date}": local.strftime("%A, %B %-d"),
        "{time}": local.strftime("%A, %B %-d at %-I:%M %p %Z"),
        "{join_link}": (notice.join_url or "").strip(),
        "{room_link}": room_link,
        "{pin}": pin or "",
        "{missing}": missing,
        "{done}": ready.done_label if ready is not None else "",
        "{precall}": (
            message_render.render(message_text(booking, "reminder_precall_line"), {"{missing}": missing, "{room_link}": room_link})
            if ready is not None and not ready.complete and room_link
            else ""
        ),
        "{stop_link}": stop_link,
    }


def precall_block(booking: BookingSettings, values: dict[str, str]) -> str:
    return message_render.render_lines(message_text(booking, "precall_block"), values)


# --- delivery -------------------------------------------------------------------


async def deliver_pin(
    db: AsyncSession,
    *,
    notice: BookingNotification,
    booking: BookingSettings,
    values: dict[str, str],
) -> str | None:
    """Get the room PIN to the client on a different channel from the link:
    SMS when they consented, otherwise its own email. Returns the channel used."""
    if not values.get("{pin}"):
        return None
    if notice.sms_consent and notice.invitee_phone:
        body = message_render.with_stop_notice(message_render.render(message_text(booking, "pin_sms"), values))
        try:
            result = await consent_delivery.send_sms_guarded(db, notice.invitee_phone, body, context="precall_pin")
        except Exception:  # noqa: BLE001
            log.exception("precall: PIN SMS raised notification=%s", notice.id)
            result = None
        if result is not None and result.ok:
            notice.precall_pin_delivered_via = "sms"
            return "sms"
    if notice.invitee_email:
        result = await asyncio.to_thread(
            ses_client.send_email,
            to_email=notice.invitee_email,
            subject=message_render.render(message_text(booking, "pin_email_subject"), values),
            body_text=message_render.render_lines(message_text(booking, "pin_email_body"), values),
        )
        if result.ok:
            notice.precall_pin_delivered_via = "email"
            return "email"
        notice.last_error = (result.detail or "pin_email_failed")[:1000]
    return None


async def _recent_automated_sms(db: AsyncSession, phone: str, *, hours: int) -> bool:
    since = datetime.now(UTC) - timedelta(hours=hours)
    row = (
        await db.execute(
            select(SmsMessage.id)
            .where(
                SmsMessage.phone_e164 == phone,
                SmsMessage.direction == "outbound",
                SmsMessage.status.in_(["sent", "delivered", "queued", "accepted"]),
                SmsMessage.created_at >= since,
                or_(SmsMessage.context.like("booking_%"), SmsMessage.context.like("precall_%")),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


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
    """Send one due pre-call row, or resolve it without sending. Returns True on a send."""
    dealer = await db.get(DealerBusiness, notice.precall_dealer_id) if notice.precall_dealer_id else None
    if dealer is None or dealer.archived_at is not None:
        reminder.status = "cancelled"
        reminder.error = "draft_missing"
        return False
    if notice.precall_stopped_at is not None or not booking.precall_enabled:
        reminder.status = "cancelled"
        reminder.error = notice.precall_stop_reason or "host_disabled"
        return False
    if dealer.is_training:
        reminder.status = "skipped"
        reminder.error = "training"
        return False
    if event.status == CalendarEventStatus.CANCELLED or event.starts_at <= now + timedelta(hours=FINAL_CUTOFF_HOURS):
        reminder.status = "cancelled"
        reminder.error = "call_started" if event.status != CalendarEventStatus.CANCELLED else "cancelled"
        return False

    try:
        ready = await readiness(db, dealer)
    except Exception:  # noqa: BLE001 — never fail the tick on a broken file
        log.exception("precall: readiness failed dealer=%s", dealer.id)
        reminder.status = "skipped"
        reminder.error = "readiness_error"
        return False
    if ready.complete:
        await mark_complete(db, notice=notice, dealer=dealer, event=event)
        reminder.status = "skipped"
        reminder.error = "precall_complete"
        return False

    room = await client_room.ensure_room(db, dealer, adopt_intake=False)
    tz_name = booking.timezone
    stop_link = stop_url(notice) if reminder.channel == "email" else ""
    values = template_values(
        notice=notice, event=event, booking=booking, host=host, dealer=dealer,
        room_link=room.url, ready=ready, stop_link=stop_link, timezone_name=tz_name,
    )
    cfg = step_config(booking, reminder.step_key or "nudge_1")

    if reminder.channel == "sms":
        phone = notice.invitee_phone or ""
        if not notice.sms_consent or not phone:
            reminder.status = "skipped"
            reminder.error = "no_consent"
            return False
        if await optout.is_opted_out(db, phone):
            await stop_sequence(db, notice, reason="sms_stop", channels=("sms",))
            reminder.status = "cancelled"
            reminder.error = "sms_stop"
            return False
        local_hour = now.astimezone(_tz(tz_name)).hour
        if local_hour < QUIET_START_HOUR or local_hour >= QUIET_END_HOUR:
            # Leave it pending; the next tick inside the window sends it, and
            # the cutoff above cancels it if the call arrives first.
            return False
        if await _recent_automated_sms(db, phone, hours=SMS_SPACING_HOURS):
            reminder.due_at = now + timedelta(hours=1)
            return False
        body = message_render.with_stop_notice(message_render.render(cfg.get("sms"), values))
        try:
            result = await consent_delivery.send_sms_guarded(db, phone, body, context=f"precall_{reminder.step_key}")
        except Exception:  # noqa: BLE001
            log.exception("precall: SMS raised notification=%s", notice.id)
            reminder.status = "failed"
            reminder.sent_at = now
            reminder.error = "sms_provider_exception"
            return False
        reminder.rendered_body = body
    else:
        to = notice.invitee_email or ""
        if not to:
            reminder.status = "skipped"
            reminder.error = "no_email"
            return False
        subject = message_render.render(cfg.get("email_subject"), values) or f"Before your call with {values['{rep}']}"
        body = message_render.render_lines(cfg.get("email_body"), values)
        footer = _stop_footer(notice, booking, values)
        if footer:
            body = f"{body}\n\n{footer}"
        result = await asyncio.to_thread(ses_client.send_email, to_email=to, subject=subject, body_text=body)
        reminder.rendered_body = f"{subject}\n\n{body}"

    reminder.status = "sent" if result.ok else "failed"
    reminder.sent_at = now
    reminder.provider_message_id = getattr(result, "message_id", None)
    reminder.error = None if result.ok else (result.detail or "")[:1000]
    if not result.ok:
        notice.last_error = (result.detail or "")[:1000]
    await log_action(
        db, dealer.id, None, "precall.step_sent", "dealer", entity_id=dealer.id,
        after={"step": reminder.step_key, "channel": reminder.channel, "ok": bool(result.ok), "notification_id": str(notice.id)},
    )
    return bool(result.ok)


def _stop_footer(notice: BookingNotification, booking: BookingSettings, values: dict[str, str]) -> str:
    """Every automated pre-call email says why it arrived and how to stop it."""
    link = values.get("{stop_link}") or ""
    if not link:
        return "You are receiving this because you booked a call with Qualified Commercial. Reply to this email to stop these messages."
    return message_render.render(DEFAULT_MESSAGES["stop_footer"], values)


async def mark_complete(
    db: AsyncSession, *, notice: BookingNotification, dealer: DealerBusiness, event: CalendarEvent | None
) -> None:
    if notice.precall_completed_at is not None:
        return
    notice.precall_completed_at = datetime.now(UTC)
    for row in await _pending_rows(db, notice):
        row.status = "skipped"
        row.error = "precall_complete"
    recipients = {uid for uid in (notice.booked_by_user_id, dealer.owner_user_id) if uid}
    if recipients:
        try:
            when = ""
            if event is not None:
                when = event.starts_at.astimezone(_tz("America/New_York")).strftime("%a %b %-d")
            await notify_users(
                db,
                recipient_ids=recipients,
                event_type="precall_ready",
                category="calendar",
                priority="high",
                title=f"{notice.invitee_name} is ready for the call{(' on ' + when) if when else ''}",
                body="Owners ✓ · Bank ✓ · Credit ✓ — everything is on the draft file.",
                target_type="dealer",
                target_id=str(dealer.id),
                deep_link=f"/applications/{dealer.id}",
                meta={"dealer_id": str(dealer.id), "booking_notification_id": str(notice.id)},
                email=True,
                push=True,
            )
        except Exception:  # noqa: BLE001
            log.exception("precall: ready notification failed dealer=%s", dealer.id)
    await log_action(db, dealer.id, None, "precall.completed", "dealer", entity_id=dealer.id)


async def on_progress(db: AsyncSession, dealer: DealerBusiness) -> bool:
    """Called after a client finishes something in the room. Stops the nudges
    the moment the checklist is complete. Commits."""
    notices = list(
        (
            await db.execute(
                select(BookingNotification).where(
                    BookingNotification.precall_dealer_id == dealer.id,
                    BookingNotification.precall_completed_at.is_(None),
                )
            )
        ).scalars().all()
    )
    if not notices:
        return False
    try:
        ready = await readiness(db, dealer)
    except Exception:  # noqa: BLE001
        log.exception("precall: readiness failed on progress dealer=%s", dealer.id)
        return False
    if not ready.complete:
        return False
    for notice in notices:
        event = await db.get(CalendarEvent, notice.event_id)
        await mark_complete(db, notice=notice, dealer=dealer, event=event)
    await db.commit()
    return True


# --- rep-facing state -------------------------------------------------------------


async def notice_for_dealer(db: AsyncSession, dealer_id: UUID) -> BookingNotification | None:
    """The booking that most recently opened this file's pre-call flow."""
    return (
        await db.execute(
            select(BookingNotification)
            .where(BookingNotification.precall_dealer_id == dealer_id)
            .order_by(BookingNotification.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def status_for(notice: BookingNotification | None, ready: Readiness | None, booking_enabled: bool) -> str:
    if notice is None or notice.precall_dealer_id is None or not booking_enabled:
        return "disabled"
    if notice.precall_completed_at is not None or (ready is not None and ready.complete):
        return "complete"
    if notice.precall_stopped_at is not None:
        return "stopped"
    return "in_progress"


async def steps_for(db: AsyncSession, notice: BookingNotification) -> list[dict]:
    rows = list(
        (
            await db.execute(
                select(BookingNotificationReminder)
                .where(
                    BookingNotificationReminder.booking_notification_id == notice.id,
                    BookingNotificationReminder.kind == "precall",
                )
                .order_by(BookingNotificationReminder.due_at.asc())
            )
        ).scalars().all()
    )
    return [
        {
            "id": str(r.id),
            "step_key": r.step_key,
            "channel": r.channel,
            "due_at": r.due_at,
            "status": r.status,
            "sent_at": r.sent_at,
            "detail": r.error,
            "rendered_body": r.rendered_body,
        }
        for r in rows
    ]


# --- cleanup --------------------------------------------------------------------


async def archive_stale_drafts(db: AsyncSession, *, now: datetime | None = None, days: int = 30) -> int:
    """Tombstone booking drafts nobody touched: the call was cancelled or is
    long past, and the client added no ownership, bank, credit or documents.
    Reversible (archived_at only); consent and audit rows are never deleted."""
    now = now or datetime.now(UTC)
    horizon = now - timedelta(days=days)
    drafts = list(
        (
            await db.execute(
                select(DealerBusiness).where(
                    DealerBusiness.draft_source == "booking",
                    DealerBusiness.application_lifecycle == "draft",
                    DealerBusiness.archived_at.is_(None),
                    DealerBusiness.created_at < horizon,
                )
            )
        ).scalars().all()
    )
    archived = 0
    for dealer in drafts:
        events = list(
            (
                await db.execute(
                    select(CalendarEvent)
                    .join(BookingNotification, BookingNotification.event_id == CalendarEvent.id)
                    .where(BookingNotification.precall_dealer_id == dealer.id)
                )
            ).scalars().all()
        )
        live = [e for e in events if e.status != CalendarEventStatus.CANCELLED and e.starts_at > horizon]
        if live:
            continue
        touched = (
            await db.execute(
                select(DealerOwner.id).where(DealerOwner.dealer_id == dealer.id, DealerOwner.ownership_pct.isnot(None)).limit(1)
            )
        ).scalar_one_or_none()
        if touched is None:
            touched = (
                await db.execute(select(DealerPlaidItem.id).where(DealerPlaidItem.dealer_id == dealer.id).limit(1))
            ).scalar_one_or_none()
        if touched is None:
            touched = (
                await db.execute(select(DealerDocument.id).where(DealerDocument.dealer_id == dealer.id).limit(1))
            ).scalar_one_or_none()
        if touched is None:
            touched = (
                await db.execute(
                    select(DealerOwner.id).where(DealerOwner.dealer_id == dealer.id, DealerOwner.credit_pulled_at.isnot(None)).limit(1)
                )
            ).scalar_one_or_none()
        if touched is not None:
            continue
        dealer.archived_at = now
        dealer.notes = "\n\n".join(p for p in [dealer.notes, f"Archived automatically on {now:%Y-%m-%d}: booking draft with no client activity."] if p)
        await stop_sequences_for_dealer(db, dealer, reason="archived")
        await log_action(db, dealer.id, None, "draft.archived", "dealer", entity_id=dealer.id, after={"reason": "stale_booking_draft"})
        archived += 1
    return archived


async def resume_sequence(
    db: AsyncSession, *, notice: BookingNotification, event: CalendarEvent
) -> int:
    """Undo a host stop: future rows the stop cancelled go back to pending.

    Rows are flipped rather than re-inserted because the schedule constraint
    (notification, channel, marker) would refuse duplicates.
    """
    notice.precall_stopped_at = None
    notice.precall_stop_reason = None
    now = datetime.now(UTC)
    cutoff = event.starts_at - timedelta(hours=FINAL_CUTOFF_HOURS)
    rows = list(
        (
            await db.execute(
                select(BookingNotificationReminder).where(
                    BookingNotificationReminder.booking_notification_id == notice.id,
                    BookingNotificationReminder.kind == "precall",
                    BookingNotificationReminder.status == "cancelled",
                    BookingNotificationReminder.due_at > now,
                    BookingNotificationReminder.due_at <= cutoff,
                )
            )
        ).scalars().all()
    )
    for row in rows:
        row.status = "pending"
        row.error = None
    await db.flush()
    return len(rows)


async def send_kit(
    db: AsyncSession,
    *,
    notice: BookingNotification,
    event: CalendarEvent,
    booking: BookingSettings,
    host: User,
    dealer: DealerBusiness,
    channels: tuple[str, ...] = ("email", "sms"),
    pin: str | None = None,
) -> dict[str, bool]:
    """Re-send the room kit on demand (rep action). Email carries the
    checklist and link; SMS carries the link (and a fresh PIN when one was
    just rotated). Returns what went out."""
    ready = await readiness(db, dealer)
    room = await client_room.ensure_room(db, dealer, adopt_intake=False)
    values = template_values(
        notice=notice, event=event, booking=booking, host=host, dealer=dealer,
        room_link=room.url, ready=ready, pin=pin, stop_link=stop_url(notice),
    )
    out = {"email": False, "sms": False}
    if "email" in channels and notice.invitee_email:
        body = precall_block(booking, values)
        if pin:
            body = f"{body}\n\nYour room PIN is {pin}."
        body = f"{body}\n\n{_stop_footer(notice, booking, values)}"
        result = await asyncio.to_thread(
            ses_client.send_email,
            to_email=notice.invitee_email,
            subject=message_render.render("Your secure room for your call with {rep}", values),
            body_text=body,
        )
        out["email"] = bool(result.ok)
        if not result.ok:
            notice.last_error = (result.detail or "")[:1000]
    if "sms" in channels and notice.sms_consent and notice.invitee_phone:
        if await optout.is_opted_out(db, notice.invitee_phone):
            out["sms"] = False
        else:
            template = (
                "Qualified Commercial: your secure room for your call with {rep}: {room_link} PIN {pin}"
                if pin
                else step_config(booking, "nudge_1").get("sms")
            )
            body = message_render.with_stop_notice(message_render.render(template, values))
            try:
                result = await consent_delivery.send_sms_guarded(db, notice.invitee_phone, body, context="precall_kit")
                out["sms"] = bool(result.ok)
                if not result.ok:
                    notice.last_error = (result.detail or "")[:1000]
            except Exception:  # noqa: BLE001
                log.exception("precall: kit SMS raised notification=%s", notice.id)
    await log_action(
        db, dealer.id, None, "precall.kit_sent", "dealer", entity_id=dealer.id,
        after={**out, "rotated_pin": bool(pin), "notification_id": str(notice.id)},
    )
    return out
