"""File-scoped communications for the AI Intake workspace.

The secure room remains the canonical client chat. This router adds the two
provider-backed capabilities the operator workspace was missing: auditable SMS
delivery on that chat and mailbox-owned email threads for the application file.
"""

# FastAPI dependencies intentionally use callable defaults.
# ruff: noqa: B008

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.dealer_os.models import DealerRepInboxMessage, DealerRepInboxThread, DealerSmsConsent
from app.dealer_os.schemas import SmsConsentIn, SmsConsentOut, SmsDisclosureOut
from app.dealer_os.services import consent_delivery
from app.dealer_os.services import sms_consent as sms_consent_svc
from app.deps import CurrentUser
from app.enums import Role
from app.models.application_profile import ApplicationOwner, ApplicationProfile
from app.models.client import Client
from app.models.message_send import MessageSend
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.routers import application_profiles as profile_routes
from app.schemas.application_profile import FileCreditInviteRequest, VerificationInvitationCreate
from app.services import application_profiles as profiles
from app.services.messaging import outbox
from app.services.sms import is_opted_out, sms_available, unavailable_reason

router = APIRouter(prefix="/application-profiles", tags=["application-communications"])


def _require_operator(user: User) -> None:
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Underwriting role required")


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    return request.client.host[:64] if request.client else None


def _email(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    return value or None


def _subject_key(value: str) -> str:
    value = value.strip().lower()
    while re.match(r"^(re|fw|fwd)\s*:", value):
        value = re.sub(r"^(re|fw|fwd)\s*:\s*", "", value)
    return " ".join(value.split())[:200]


class ApplicationCommunicationContact(BaseModel):
    id: str
    kind: Literal["client", "owner"]
    name: str
    email: str | None = None
    phone: str | None = None
    is_primary: bool = False
    owner_id: UUID | None = None
    credit_required: bool = False


class ApplicationSmsState(BaseModel):
    phone: str | None = None
    can_send: bool = False
    delivery_enabled: bool = False
    transactional_consented: bool = False
    marketing_consented: bool = False
    opted_out: bool = False
    provider_available: bool = False
    blocked_reason: str | None = None
    grants: list[SmsConsentOut] = Field(default_factory=list)


class ApplicationSmsPreferencePatch(BaseModel):
    enabled: bool


class ApplicationEmailThreadRead(BaseModel):
    id: UUID
    subject: str
    owner_user_id: UUID | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    to_email: str
    cc_emails: list[str] = Field(default_factory=list)
    participant_names: list[str] = Field(default_factory=list)
    last_message_at: datetime | None = None
    unread_count: int = 0
    can_reply: bool = False
    created_at: datetime


class ApplicationEmailMessageRead(BaseModel):
    id: UUID
    thread_id: UUID
    direction: Literal["inbound", "outbound"]
    subject: str | None = None
    body: str
    sender: str | None = None
    recipient: str | None = None
    cc_emails: list[str] = Field(default_factory=list)
    provider: str | None = None
    delivery_status: str
    delivery_detail: str | None = None
    created_at: datetime


class ApplicationEmailThreadDetail(BaseModel):
    thread: ApplicationEmailThreadRead
    messages: list[ApplicationEmailMessageRead]


class ApplicationEmailCreate(BaseModel):
    to_contact_id: str = Field(min_length=1, max_length=80)
    cc_contact_ids: list[str] = Field(default_factory=list, max_length=8)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)


class ApplicationEmailReply(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class ApplicationCommunicationLinkOption(BaseModel):
    key: str
    kind: Literal["business_banking", "financial_form", "credit_authorization"]
    label: str
    form_kind: str | None = None
    owner_id: UUID | None = None
    recipient_contact_id: str | None = None
    enabled: bool = True
    disabled_reason: str | None = None


class ApplicationCommunicationLinkCreate(BaseModel):
    kind: Literal["business_banking", "financial_form", "credit_authorization"]
    form_kind: Literal["packet", "p_and_l", "balance_sheet", "debt_schedule", "pfs"] | None = None
    owner_id: UUID | None = None


class ApplicationCommunicationLinkRead(BaseModel):
    label: str
    url: str
    expires_at: datetime | None = None
    required_recipient_contact_id: str | None = None
    required_recipient_email: str | None = None
    exclusive_recipient: bool = False


async def _load_profile(db: AsyncSession, profile_id: UUID, user: User) -> ApplicationProfile:
    _require_operator(user)
    return await profiles.load_profile(db, profile_id, user)


async def _contacts(
    db: AsyncSession, profile: ApplicationProfile
) -> tuple[list[ApplicationCommunicationContact], list[ApplicationOwner]]:
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    client = await db.get(Client, profile.client_id) if profile.client_id else None
    owners = await profiles.owner_rows(db, profile)
    client_name = (intake.full_name if intake else None) or (client.name if client else None) or "Client"
    client_email = _email((intake.email if intake else None) or (client.email if client else None))
    client_phone = (intake.phone if intake else None) or (client.phone if client else None)
    rows = [
        ApplicationCommunicationContact(
            id="client",
            kind="client",
            name=client_name,
            email=client_email,
            phone=client_phone,
            is_primary=True,
        )
    ]
    seen = {client_email} if client_email else set()
    for owner in owners:
        owner_email = _email(owner.email)
        # One recipient choice per actual mailbox. Credit links still resolve
        # to this canonical contact when an owner shares the client's email.
        if owner_email and owner_email in seen:
            continue
        if owner_email:
            seen.add(owner_email)
        rows.append(
            ApplicationCommunicationContact(
                id=f"owner:{owner.id}",
                kind="owner",
                name=owner.full_name,
                email=owner_email,
                phone=owner.phone,
                owner_id=owner.id,
                credit_required=bool(owner.credit_required),
            )
        )
    return rows, owners


def _contact_for_owner(
    contacts: list[ApplicationCommunicationContact], owner: ApplicationOwner
) -> ApplicationCommunicationContact | None:
    exact = next((item for item in contacts if item.owner_id == owner.id), None)
    if exact:
        return exact
    owner_email = _email(owner.email)
    return next((item for item in contacts if item.email == owner_email), None)


async def _sms_state(db: AsyncSession, profile: ApplicationProfile) -> ApplicationSmsState:
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    client = await db.get(Client, profile.client_id) if profile.client_id else None
    phone = consent_delivery.normalize_phone((intake.phone if intake else None) or (client.phone if client else None))
    grants = list(
        (
            await db.execute(
                select(DealerSmsConsent)
                .where(DealerSmsConsent.profile_id == profile.id)
                .order_by(DealerSmsConsent.created_at.desc())
            )
        ).scalars().all()
    )
    provider_ready = sms_available()
    if not phone:
        return ApplicationSmsState(
            phone=None,
            delivery_enabled=profile.client_sms_delivery_enabled,
            provider_available=provider_ready,
            blocked_reason="No mobile number is on this file.",
            grants=grants,
        )
    opted_out = await is_opted_out(db, phone)
    transactional = await sms_consent_svc.consent_for(db, phone_e164=phone, kind="transactional")
    marketing = await sms_consent_svc.consent_for(db, phone_e164=phone, kind="marketing")
    reason = None
    if opted_out:
        reason = "This number opted out of text messages."
    elif transactional is None:
        reason = "Transactional SMS consent is required."
    elif not provider_ready:
        reason = unavailable_reason() or "SMS delivery is not configured."
    return ApplicationSmsState(
        phone=phone,
        can_send=bool(transactional and not opted_out and provider_ready),
        delivery_enabled=profile.client_sms_delivery_enabled,
        transactional_consented=transactional is not None,
        marketing_consented=marketing is not None,
        opted_out=opted_out,
        provider_available=provider_ready,
        blocked_reason=reason,
        grants=grants,
    )


@router.get("/{profile_id}/communications/contacts", response_model=list[ApplicationCommunicationContact])
async def list_application_communication_contacts(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[ApplicationCommunicationContact]:
    profile = await _load_profile(db, profile_id, user)
    contacts, _ = await _contacts(db, profile)
    return contacts


@router.get("/{profile_id}/communications/sms-disclosure", response_model=SmsDisclosureOut)
async def get_application_sms_disclosure(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> SmsDisclosureOut:
    await _load_profile(db, profile_id, user)
    return SmsDisclosureOut(**asdict(sms_consent_svc.disclosure()))


@router.get("/{profile_id}/communications/sms-consent", response_model=ApplicationSmsState)
async def get_application_sms_consent(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ApplicationSmsState:
    profile = await _load_profile(db, profile_id, user)
    return await _sms_state(db, profile)


@router.patch(
    "/{profile_id}/communications/sms-preference",
    response_model=ApplicationSmsState,
)
async def update_application_sms_preference(
    profile_id: UUID,
    payload: ApplicationSmsPreferencePatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationSmsState:
    profile = await _load_profile(db, profile_id, user)
    profile.client_sms_delivery_enabled = payload.enabled
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "sms.delivery_enabled" if payload.enabled else "sms.delivery_disabled",
        f"{'Enabled' if payload.enabled else 'Disabled'} persistent SMS delivery for client replies",
        metadata={"enabled": payload.enabled},
    )
    await db.commit()
    return await _sms_state(db, profile)


@router.post(
    "/{profile_id}/communications/sms-consent",
    response_model=ApplicationSmsState,
    status_code=status.HTTP_201_CREATED,
)
async def capture_application_sms_consent(
    profile_id: UUID,
    payload: SmsConsentIn,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationSmsState:
    profile = await _load_profile(db, profile_id, user)
    current = await _sms_state(db, profile)
    phone = consent_delivery.normalize_phone(payload.phone)
    if not current.phone or phone != current.phone:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Consent must be captured for the mobile number currently on this file.")
    if not (payload.transactional or payload.marketing):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Select at least one SMS consent type.")
    if not payload.accepted_legal:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The Terms and Privacy Policy must be accepted before SMS consent can be recorded.")
    for kind, enabled in (("transactional", payload.transactional), ("marketing", payload.marketing)):
        if enabled:
            await sms_consent_svc.record_consent(
                db,
                dealer_id=None,
                profile_id=profile.id,
                phone_e164=phone,
                kind=kind,
                method=payload.method,
                captured_by_user_id=user.id,
                captured_by_name=user.name,
                consenter_name=payload.consenter_name,
                ip_address=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
    await profiles.log_profile_action(db, profile, user, "sms.consent_captured", "Recorded client SMS consent", metadata={"phone": phone, "transactional": payload.transactional, "marketing": payload.marketing, "method": payload.method})
    await db.commit()
    return await _sms_state(db, profile)


async def _thread_read(
    db: AsyncSession,
    thread: DealerRepInboxThread,
    user: User,
    contacts: list[ApplicationCommunicationContact],
) -> ApplicationEmailThreadRead:
    owner = await db.get(User, thread.owner_user_id) if thread.owner_user_id else None
    emails = [_email(str(value)) for value in (thread.participant_emails or [])]
    emails = [value for value in emails if value]
    names_by_email = {item.email: item.name for item in contacts if item.email}
    return ApplicationEmailThreadRead(
        id=thread.id,
        subject=thread.subject,
        owner_user_id=thread.owner_user_id,
        owner_name=owner.name if owner else None,
        owner_email=owner.email if owner else None,
        to_email=emails[0] if emails else "",
        cc_emails=emails[1:],
        participant_names=[names_by_email.get(value, value) for value in emails],
        last_message_at=thread.last_message_at,
        unread_count=int(thread.unread_count or 0),
        can_reply=thread.owner_user_id == user.id,
        created_at=thread.created_at,
    )


async def _thread_detail(
    db: AsyncSession,
    profile: ApplicationProfile,
    thread: DealerRepInboxThread,
    user: User,
) -> ApplicationEmailThreadDetail:
    contacts, _ = await _contacts(db, profile)
    messages = list(
        (
            await db.execute(
                select(DealerRepInboxMessage)
                .where(DealerRepInboxMessage.thread_id == thread.id)
                .order_by(DealerRepInboxMessage.created_at.asc())
            )
        ).scalars().all()
    )
    send_ids = [item.message_send_id for item in messages if item.message_send_id]
    sends = {}
    if send_ids:
        sends = {row.id: row for row in (await db.execute(select(MessageSend).where(MessageSend.id.in_(send_ids)))).scalars().all()}
    return ApplicationEmailThreadDetail(
        thread=await _thread_read(db, thread, user, contacts),
        messages=[
            ApplicationEmailMessageRead(
                id=item.id,
                thread_id=item.thread_id,
                direction=item.direction,
                subject=item.subject,
                body=item.body,
                sender=item.sender,
                recipient=item.recipient,
                cc_emails=[str(value) for value in (item.cc_emails or [])],
                provider=item.provider,
                delivery_status=(sends[item.message_send_id].status if item.message_send_id in sends else item.delivery_status),
                delivery_detail=(sends[item.message_send_id].detail if item.message_send_id in sends else item.provider_error),
                created_at=item.created_at,
            )
            for item in messages
        ],
    )


async def _profile_thread(
    db: AsyncSession, profile: ApplicationProfile, thread_id: UUID
) -> DealerRepInboxThread:
    thread = (
        await db.execute(
            select(DealerRepInboxThread).where(
                DealerRepInboxThread.id == thread_id,
                DealerRepInboxThread.profile_id == profile.id,
                DealerRepInboxThread.channel == "email",
            )
        )
    ).scalar_one_or_none()
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File email thread not found.")
    return thread


async def _enforce_private_credit_recipient(
    db: AsyncSession,
    profile: ApplicationProfile,
    *,
    body: str,
    to_email: str,
    cc_emails: list[str],
) -> None:
    tokens = [value.rstrip(".,);]") for value in re.findall(r"/credit-consent#t=([^\s]+)", body)]
    if not tokens:
        return
    required: set[str] = set()
    for token in tokens:
        owner = (
            await db.execute(
                select(ApplicationOwner).where(
                    ApplicationOwner.profile_id == profile.id,
                    ApplicationOwner.invite_token_hash == profile_routes._hash_token(token),
                )
            )
        ).scalar_one_or_none()
        if owner is None or not _email(owner.email):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A private credit link in this draft is not valid for this file.")
        required.add(_email(owner.email))
    if len(required) != 1 or to_email not in required or cc_emails:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Private credit links must be emailed only to the matching owner, with no Cc recipients.")


async def _send_thread_email(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    thread: DealerRepInboxThread,
    user: User,
    to_email: str,
    cc_emails: list[str],
    subject: str,
    body: str,
) -> DealerRepInboxMessage:
    outcome = await outbox.deliver_email(
        db,
        outbox.Draft(to=to_email, cc=cc_emails, subject=subject, body_text=body),
        context="ai_intake_email",
        subject=outbox.Subject(
            owner_user_id=user.id,
            client_id=profile.client_id,
            profile_id=profile.id,
            intake_id=profile.intake_id,
        ),
        sender_user_id=user.id,
    )
    if outcome.provider_thread_id:
        thread.provider_thread_id = outcome.provider_thread_id
    now = datetime.now(UTC)
    message = DealerRepInboxMessage(
        thread_id=thread.id,
        owner_user_id=thread.owner_user_id,
        contact_id=None,
        dealer_id=thread.dealer_id,
        profile_id=profile.id,
        message_send_id=outcome.row.id if outcome.row else None,
        direction="outbound",
        channel="email",
        subject=subject,
        body=body,
        provider=(outcome.row.provider if outcome.row else None),
        provider_message_id=outcome.message_id,
        provider_error=None if outcome.ok else outcome.detail,
        delivery_status="sent" if outcome.ok else "failed",
        sender=user.email,
        recipient=to_email,
        cc_emails=cc_emails or None,
        read_at=now,
    )
    db.add(message)
    thread.last_message_at = now
    await db.flush()
    return message


@router.get("/{profile_id}/communications/email/threads", response_model=list[ApplicationEmailThreadRead])
async def list_application_email_threads(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[ApplicationEmailThreadRead]:
    profile = await _load_profile(db, profile_id, user)
    contacts, _ = await _contacts(db, profile)
    threads = list(
        (
            await db.execute(
                select(DealerRepInboxThread)
                .where(DealerRepInboxThread.profile_id == profile.id, DealerRepInboxThread.channel == "email")
                .order_by(DealerRepInboxThread.last_message_at.desc().nullslast(), DealerRepInboxThread.created_at.desc())
            )
        ).scalars().all()
    )
    return [await _thread_read(db, thread, user, contacts) for thread in threads]


@router.post(
    "/{profile_id}/communications/email/threads",
    response_model=ApplicationEmailThreadDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_application_email_thread(
    profile_id: UUID,
    payload: ApplicationEmailCreate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationEmailThreadDetail:
    profile = await _load_profile(db, profile_id, user)
    contacts, _ = await _contacts(db, profile)
    by_id = {item.id: item for item in contacts}
    to = by_id.get(payload.to_contact_id)
    if to is None or not to.email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose a current file contact with an email address.")
    cc_contacts = []
    for contact_id in dict.fromkeys(payload.cc_contact_ids):
        contact = by_id.get(contact_id)
        if contact is None or not contact.email:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Every Cc recipient must be a current file contact with an email address.")
        if contact.email != to.email:
            cc_contacts.append(contact)
    cc_emails = list(dict.fromkeys(item.email for item in cc_contacts if item.email))
    await _enforce_private_credit_recipient(db, profile, body=payload.body, to_email=to.email, cc_emails=cc_emails)
    await profile_routes._require_training_live_action(
        db,
        profile=profile,
        user=user,
        request=request,
        action="Start AI Intake email conversation",
        provider="Gmail / SES",
        recipient=to.email,
        effect="Send a live email from the application file",
    )
    now = datetime.now(UTC)
    thread = DealerRepInboxThread(
        owner_user_id=user.id,
        contact_id=None,
        dealer_id=profile.dealer_id,
        profile_id=profile.id,
        participant_emails=[to.email, *cc_emails],
        subject=payload.subject.strip(),
        subject_key=_subject_key(payload.subject),
        channel="email",
        source="ai_intake",
        last_message_at=now,
    )
    db.add(thread)
    await db.flush()
    await _send_thread_email(db, profile=profile, thread=thread, user=user, to_email=to.email, cc_emails=cc_emails, subject=payload.subject.strip(), body=payload.body.strip())
    await profiles.log_profile_action(db, profile, user, "email.thread_created", f"Started email conversation with {to.name}", target_type="email_thread", target_id=thread.id, metadata={"to": to.email, "cc": cc_emails, "subject": payload.subject.strip()})
    await db.commit()
    await db.refresh(thread)
    return await _thread_detail(db, profile, thread, user)


@router.get("/{profile_id}/communications/email/threads/{thread_id}", response_model=ApplicationEmailThreadDetail)
async def get_application_email_thread(
    profile_id: UUID, thread_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ApplicationEmailThreadDetail:
    profile = await _load_profile(db, profile_id, user)
    thread = await _profile_thread(db, profile, thread_id)
    return await _thread_detail(db, profile, thread, user)


@router.post(
    "/{profile_id}/communications/email/threads/{thread_id}/messages",
    response_model=ApplicationEmailThreadDetail,
    status_code=status.HTTP_201_CREATED,
)
async def reply_application_email_thread(
    profile_id: UUID,
    thread_id: UUID,
    payload: ApplicationEmailReply,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationEmailThreadDetail:
    profile = await _load_profile(db, profile_id, user)
    thread = await _profile_thread(db, profile, thread_id)
    if thread.owner_user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the mailbox owner can reply in this thread. Start a new email from your mailbox instead.")
    emails = [_email(str(value)) for value in (thread.participant_emails or [])]
    emails = [value for value in emails if value]
    if not emails:
        raise HTTPException(status.HTTP_409_CONFLICT, "This thread no longer has a valid recipient.")
    await _enforce_private_credit_recipient(db, profile, body=payload.body, to_email=emails[0], cc_emails=emails[1:])
    await profile_routes._require_training_live_action(
        db,
        profile=profile,
        user=user,
        request=request,
        action="Reply to AI Intake email conversation",
        provider="Gmail / SES",
        recipient=emails[0],
        effect="Send a live email reply from the application file",
    )
    await _send_thread_email(db, profile=profile, thread=thread, user=user, to_email=emails[0], cc_emails=emails[1:], subject=f"Re: {thread.subject}", body=payload.body.strip())
    await profiles.log_profile_action(db, profile, user, "email.thread_replied", "Replied in the file email conversation", target_type="email_thread", target_id=thread.id)
    await db.commit()
    return await _thread_detail(db, profile, thread, user)


_FORM_LABELS = {
    "packet": "Financial forms packet",
    "p_and_l": "Profit and loss statement",
    "balance_sheet": "Balance sheet",
    "debt_schedule": "Debt schedule",
    "pfs": "Personal financial statement",
}


@router.get("/{profile_id}/communications/links", response_model=list[ApplicationCommunicationLinkOption])
async def list_application_communication_links(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[ApplicationCommunicationLinkOption]:
    profile = await _load_profile(db, profile_id, user)
    contacts, owners = await _contacts(db, profile)
    options = [
        ApplicationCommunicationLinkOption(key="business_banking", kind="business_banking", label="Connect business banking")
    ]
    options.extend(
        ApplicationCommunicationLinkOption(key=f"financial_form:{kind}", kind="financial_form", form_kind=kind, label=label)
        for kind, label in _FORM_LABELS.items()
    )
    for owner in owners:
        if not owner.credit_required:
            continue
        contact = _contact_for_owner(contacts, owner)
        options.append(
            ApplicationCommunicationLinkOption(
                key=f"credit_authorization:{owner.id}",
                kind="credit_authorization",
                label=f"Credit authorization — {owner.full_name}",
                owner_id=owner.id,
                recipient_contact_id=contact.id if contact else None,
                enabled=bool(contact and contact.email and consent_delivery.normalize_phone(owner.phone)),
                disabled_reason=None if contact and contact.email and consent_delivery.normalize_phone(owner.phone) else "Add this owner's personal email and mobile number before creating the link.",
            )
        )
    return options


@router.post(
    "/{profile_id}/communications/links",
    response_model=ApplicationCommunicationLinkRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_application_communication_link(
    profile_id: UUID,
    payload: ApplicationCommunicationLinkCreate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationCommunicationLinkRead:
    profile = await _load_profile(db, profile_id, user)
    origin = get_settings().frontend_app_url.rstrip("/")
    if payload.kind == "business_banking":
        result = await profile_routes.create_bank_verification_invitation(
            profile_id,
            VerificationInvitationCreate(channel="none"),
            request,
            user,
            db,
        )
        return ApplicationCommunicationLinkRead(label="Connect business banking", url=f"{origin}{result.path}", expires_at=result.expires_at)
    if payload.kind == "financial_form":
        if not payload.form_kind:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose a financial form.")
        result = await profile_routes.mint_financial_form_link(profile_id, payload.form_kind, user, db)
        return ApplicationCommunicationLinkRead(label=_FORM_LABELS[payload.form_kind], url=str(result["url"]), expires_at=result.get("expires_at"))
    if payload.owner_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose the owner whose credit authorization is required.")
    owner = await profile_routes._owner_for_profile(db, profile, payload.owner_id)
    if not owner.credit_required:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "This owner does not require a credit authorization.")
    contacts, _ = await _contacts(db, profile)
    contact = _contact_for_owner(contacts, owner)
    if contact is None or not contact.email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Add this owner's personal email before creating a private credit link.")
    result = await profile_routes._mint_credit_invite(db, profile, owner, user, FileCreditInviteRequest(channel="none").channel, request)
    return ApplicationCommunicationLinkRead(
        label=f"Credit authorization — {owner.full_name}",
        url=f"{origin}{result.path}",
        required_recipient_contact_id=contact.id,
        required_recipient_email=contact.email,
        exclusive_recipient=True,
    )
