"""The desk's side of a merchant-processing offer.

Everything here sits under a file's profile and behind the underwriting
gate, because the container lives in the Underwriting panel of the lead
page. The partner's terms PDF goes through the same presigned upload the
rest of the lead uses, marked as an offer document so nothing downstream
mistakes it for evidence; the offer row is created on upload-complete; the
desk corrects figures, picks the partner and presses Send. The client's side
is in application_profiles (the room), and the arithmetic, the extraction
and the partner email are in services/merchant_processing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.deps import CurrentUser
from app.models.application_profile import ApplicationProfile
from app.models.bucket import BucketFile
from app.models.lender import Lender
from app.models.merchant_processing_offer import MerchantProcessingOffer
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.routers.application_profiles import (
    _deliver_room_request,
    _delivery_overall,
    _profile_room_link,
    _require_underwriting_actor,
    _room_url,
)
from app.schemas.bucket import BucketFileUploadInitResponse
from app.services import application_profiles as profiles
from app.services import file_events
from app.services import merchant_processing as mp
from app.services.lender_products import MERCHANT_PROCESSING

router = APIRouter(prefix="/application-profiles", tags=["merchant-offers"])

TARGET = "merchant_processing_offer"


# ── shapes ──────────────────────────────────────────────────────────────────


class MerchantOfferUploadInit(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/pdf", max_length=160)
    size_bytes: int = Field(default=0, ge=0)


class MerchantOfferUploadComplete(BaseModel):
    file_id: UUID


class MerchantOfferPatch(BaseModel):
    terms: dict[str, Any] | None = None
    desk_terms: dict[str, Any] | None = None
    lender_id: UUID | None = None
    clear_lender: bool = False
    #: The desk's own annual figure; sets the basis to "manual".
    estimated_annual_savings: float | None = None


class MerchantOfferSend(BaseModel):
    confirm_no_saving: bool = False
    confirm_no_partner: bool = False


class MerchantOfferNotifyClient(BaseModel):
    recipient_email: EmailStr | None = None


class PartnerOption(BaseModel):
    id: UUID
    name: str
    email: str | None = None


class MerchantOfferRead(BaseModel):
    id: UUID
    status: str
    terms: dict[str, Any]
    desk_terms: dict[str, Any]
    terms_version: int
    estimated_monthly_savings: float | None = None
    estimated_annual_savings: float | None = None
    savings_basis: str | None = None
    savings_warning: str | None = None
    extraction_confidence: str | None = None
    extraction_error: str | None = None
    lender_id: UUID | None = None
    lender_name: str | None = None
    source_file_id: UUID | None = None
    source_file_name: str | None = None
    source_file_url: str | None = None
    sent_at: datetime | None = None
    sent_by_name: str | None = None
    client_response: str | None = None
    client_response_at: datetime | None = None
    client_response_reason: str | None = None
    client_response_name: str | None = None
    client_response_ip: str | None = None
    partner_email_status: str | None = None
    partner_email_error: str | None = None
    partner_email_at: datetime | None = None
    pro_forma: dict[str, Any] | None = None
    created_at: datetime


class MerchantOfferPanelRead(BaseModel):
    available: bool
    reason: str | None = None
    offer: MerchantOfferRead | None = None
    history_count: int = 0
    room_url: str | None = None
    partners: list[PartnerOption] = Field(default_factory=list)


class MerchantOfferNotifyResult(BaseModel):
    overall_status: str
    room_url: str


# ── loading ─────────────────────────────────────────────────────────────────


async def _profile(db: AsyncSession, profile_id: UUID, user: User) -> ApplicationProfile:
    profile = await profiles.load_profile(db, profile_id, user)
    _require_underwriting_actor(user)
    return profile


def _unavailable_reason(profile: ApplicationProfile) -> str | None:
    if profile.dealer_id is not None:
        return "This file's room is a Capital OS room; the processing offer is not available there yet."
    if profile.intake_id is None:
        return "This file has no intake room to drop the terms into."
    return None


async def _intake(db: AsyncSession, profile: ApplicationProfile) -> PublicUnderwritingIntake:
    reason = _unavailable_reason(profile)
    if reason:
        raise HTTPException(status.HTTP_409_CONFLICT, reason)
    intake = (
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(PublicUnderwritingIntake.id == profile.intake_id)
            .options(
                selectinload(PublicUnderwritingIntake.bucket),
                selectinload(PublicUnderwritingIntake.bucket_upload_link),
                selectinload(PublicUnderwritingIntake.latest_review),
            )
        )
    ).scalar_one_or_none()
    if intake is None or intake.bucket_upload_link_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This file has no intake room to drop the terms into.")
    return intake


async def _open_offer(db: AsyncSession, profile: ApplicationProfile) -> MerchantProcessingOffer:
    offer = await mp.current_offer(db, profile.id)
    if offer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No processing offer on this file")
    return offer


def _not_answered(offer: MerchantProcessingOffer) -> None:
    if offer.status in mp.RESPONDED_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "The client has already answered this offer. Withdraw it to start over.")


async def _partners(db: AsyncSession) -> list[PartnerOption]:
    rows = (
        await db.execute(select(Lender).where(Lender.is_active.is_(True)).order_by(Lender.name.asc()))
    ).scalars().all()
    return [
        PartnerOption(id=row.id, name=row.name, email=row.submission_email or row.contact_email)
        for row in rows
        if MERCHANT_PROCESSING in (row.products or [])
    ]


def _business_name(intake: PublicUnderwritingIntake | None) -> str:
    return (intake.business_name if intake else None) or (intake.full_name if intake else None) or "the business"


async def _desk_read(
    db: AsyncSession, offer: MerchantProcessingOffer, intake: PublicUnderwritingIntake | None
) -> MerchantOfferRead:
    from app.routers.buckets import _download_url
    from app.routers.dealer_ai_intake import _key_metrics

    file = await db.get(BucketFile, offer.source_file_id) if offer.source_file_id else None
    lender = await db.get(Lender, offer.lender_id) if offer.lender_id else None
    sender = await db.get(User, offer.sent_by_user_id) if offer.sent_by_user_id else None
    annual = float(offer.estimated_annual_savings) if offer.estimated_annual_savings is not None else None
    pro_forma = mp.pro_forma_dscr(_key_metrics(intake), annual) if intake is not None else None
    desk_terms = dict(offer.desk_terms or {})
    return MerchantOfferRead(
        id=offer.id,
        status=offer.status,
        terms=dict(offer.terms or {}),
        desk_terms=desk_terms,
        terms_version=offer.terms_version,
        estimated_monthly_savings=float(offer.estimated_monthly_savings) if offer.estimated_monthly_savings is not None else None,
        estimated_annual_savings=annual,
        savings_basis=offer.savings_basis,
        savings_warning=desk_terms.get("savings_warning"),
        extraction_confidence=offer.extraction_confidence,
        extraction_error=offer.extraction_error,
        lender_id=offer.lender_id,
        lender_name=lender.name if lender else None,
        source_file_id=offer.source_file_id,
        source_file_name=file.file_name if file else None,
        source_file_url=_download_url(file.s3_key, content_type=file.content_type) if file and file.deleted_at is None else None,
        sent_at=offer.sent_at,
        sent_by_name=(sender.name or sender.email) if sender else None,
        client_response=offer.client_response,
        client_response_at=offer.client_response_at,
        client_response_reason=offer.client_response_reason,
        client_response_name=offer.client_response_name,
        client_response_ip=offer.client_response_ip,
        partner_email_status=offer.partner_email_status,
        partner_email_error=offer.partner_email_error,
        partner_email_at=offer.partner_email_at,
        pro_forma=pro_forma,
        created_at=offer.created_at,
    )


async def _panel(db: AsyncSession, profile: ApplicationProfile) -> MerchantOfferPanelRead:
    reason = _unavailable_reason(profile)
    offer = await mp.current_offer(db, profile.id)
    partners = await _partners(db)
    if reason:
        return MerchantOfferPanelRead(available=False, reason=reason, offer=None, partners=partners)
    intake = await _intake(db, profile)
    room_url: str | None
    try:
        link = await _profile_room_link(db, profile)
        room_url = _room_url(link, query="tab=offer")
    except HTTPException:
        room_url = None
    return MerchantOfferPanelRead(
        available=True,
        reason=None,
        offer=await _desk_read(db, offer, intake) if offer else None,
        history_count=await mp.offer_count(db, profile.id),
        room_url=room_url,
        partners=partners,
    )


# ── routes ──────────────────────────────────────────────────────────────────


@router.get("/{profile_id}/merchant-offer", response_model=MerchantOfferPanelRead)
async def read_merchant_offer(
    profile_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MerchantOfferPanelRead:
    profile = await _profile(db, profile_id, user)
    return await _panel(db, profile)


@router.post("/{profile_id}/merchant-offer/upload-init", response_model=BucketFileUploadInitResponse)
async def merchant_offer_upload_init(
    profile_id: UUID,
    payload: MerchantOfferUploadInit,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketFileUploadInitResponse:
    """The partner's terms PDF into the lead's bucket, through the same
    presigned upload every other admin upload uses, marked as an offer
    document and never attached to a checklist slot."""
    from app.routers.dealer_ai_intake import DealerFileUploadInit, _start_upload

    profile = await _profile(db, profile_id, user)
    intake = await _intake(db, profile)
    is_pdf = payload.file_name.lower().endswith(".pdf") or payload.content_type == "application/pdf"
    if not is_pdf:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Drop the partner's terms as a PDF.")
    prior = await mp.current_offer(db, profile.id)
    if prior is not None and prior.status == mp.STATUS_ACCEPTED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The client already accepted an offer on this file. Withdraw it before dropping a new one.",
        )
    init = DealerFileUploadInit(
        requested_document_id=None,
        file_name=payload.file_name,
        content_type=payload.content_type or "application/pdf",
        size_bytes=payload.size_bytes,
    )
    result = await _start_upload(
        db,
        intake,
        init,
        request,
        actor_name=user.name or "Underwriting",
        actor_email=user.email,
        source_kind="internal_upload",
        actor=user,
        source_detail=mp.OFFER_SOURCE_DETAIL,
    )
    # An identical name and size dedups to an existing file. It is the same
    # bytes, and the desk has just said it is the offer document.
    file = await db.get(BucketFile, result.file_id)
    if file is not None and not mp.is_offer_document(file):
        file.source_detail = mp.OFFER_SOURCE_DETAIL
    await db.commit()
    return result


@router.post("/{profile_id}/merchant-offer/upload-complete", response_model=MerchantOfferPanelRead)
async def merchant_offer_upload_complete(
    profile_id: UUID,
    payload: MerchantOfferUploadComplete,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MerchantOfferPanelRead:
    from app.routers.dealer_ai_intake import DealerUploadComplete, _complete_upload

    profile = await _profile(db, profile_id, user)
    intake = await _intake(db, profile)
    prior = await mp.current_offer(db, profile.id)
    if prior is not None and prior.status == mp.STATUS_ACCEPTED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The client already accepted an offer on this file. Withdraw it before dropping a new one.",
        )
    file = await _complete_upload(
        db,
        intake,
        DealerUploadComplete(file_id=payload.file_id),
        request,
        actor_name=user.name or "Underwriting",
        actor_email=user.email,
    )
    if not mp.is_offer_document(file):
        file.source_detail = mp.OFFER_SOURCE_DETAIL
    if prior is not None:
        # The prior row keeps its file: dedup can make two rows share one.
        prior.status = mp.STATUS_SUPERSEDED
        await db.flush()
    offer = MerchantProcessingOffer(
        profile_id=profile.id,
        source_file_id=file.id,
        lender_id=prior.lender_id if prior is not None else None,
        status=mp.STATUS_UPLOADED,
        terms={},
        desk_terms={},
        created_by_user_id=user.id,
    )
    db.add(offer)
    await db.flush()
    review_type = (intake.bucket.ai_context or {}).get("review_type") if intake.bucket else None
    await mp.ensure_extracted(db, offer, file, review_type=review_type)
    await mp.sync_intake_state(db, offer)
    await profiles.log_profile_action(
        db, profile, user, "merchant_offer.uploaded",
        f"Dropped the processing partner's terms: {file.file_name}",
        target_type=TARGET, target_id=offer.id,
    )
    await db.commit()
    return await _panel(db, profile)


@router.patch("/{profile_id}/merchant-offer", response_model=MerchantOfferPanelRead)
async def patch_merchant_offer(
    profile_id: UUID,
    payload: MerchantOfferPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MerchantOfferPanelRead:
    profile = await _profile(db, profile_id, user)
    offer = await _open_offer(db, profile)
    _not_answered(offer)
    changed: list[str] = []
    if payload.terms is not None:
        offer.terms = {**(offer.terms or {}), **mp.clean_terms(payload.terms)}
        changed.append("terms")
    if payload.desk_terms is not None:
        offer.desk_terms = {**(offer.desk_terms or {}), **mp.clean_desk_terms(payload.desk_terms)}
        changed.append("desk_terms")
    if payload.clear_lender:
        offer.lender_id = None
        changed.append("lender")
    elif payload.lender_id is not None:
        lender = await db.get(Lender, payload.lender_id)
        if lender is None or not lender.is_active or MERCHANT_PROCESSING not in (lender.products or []):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick an active lender whose products include merchant processing.")
        offer.lender_id = lender.id
        changed.append("lender")
    if payload.estimated_annual_savings is not None:
        mp.apply_savings(offer, manual_annual=payload.estimated_annual_savings)
        changed.append("saving")
    elif "terms" in changed:
        mp.apply_savings(offer)
    if "terms" in changed or "saving" in changed:
        offer.terms_version = (offer.terms_version or 1) + 1
        if offer.status == mp.STATUS_UNREADABLE and (
            any(offer.terms.get(k) is not None for k in mp._NUMERIC_TERM_KEYS) or mp.positive_saving(offer)
        ):
            offer.status = mp.STATUS_EXTRACTED
            offer.extraction_error = None
    if not changed:
        return await _panel(db, profile)
    await mp.sync_intake_state(db, offer)
    await profiles.log_profile_action(
        db, profile, user, "merchant_offer.edited",
        f"Edited the processing offer ({', '.join(changed)})",
        target_type=TARGET, target_id=offer.id, metadata={"changed": changed, "terms_version": offer.terms_version},
    )
    await db.commit()
    return await _panel(db, profile)


@router.post("/{profile_id}/merchant-offer/send", response_model=MerchantOfferPanelRead)
async def send_merchant_offer(
    profile_id: UUID,
    payload: MerchantOfferSend,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MerchantOfferPanelRead:
    """Make the offer visible in the client's room. The figures the client
    sees are the ones on the row at this moment — the desk's confirmation is
    this click."""
    profile = await _profile(db, profile_id, user)
    reason = _unavailable_reason(profile)
    if reason:
        raise HTTPException(status.HTTP_409_CONFLICT, reason)
    offer = await _open_offer(db, profile)
    _not_answered(offer)
    if offer.status == mp.STATUS_UPLOADED:
        raise HTTPException(status.HTTP_409_CONFLICT, "The terms are still being read. Wait a moment, or enter the figures by hand.")
    if offer.estimated_annual_savings is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Enter the figures first: the offer has no saving to show.")
    if not mp.positive_saving(offer) and not payload.confirm_no_saving:
        raise HTTPException(status.HTTP_409_CONFLICT, "The proposed pricing does not save money on these figures. Tick 'send anyway' to send it regardless.")
    if offer.lender_id is None and not payload.confirm_no_partner:
        raise HTTPException(status.HTTP_409_CONFLICT, "Pick the processing partner first, or tick 'send without a partner' — nobody will be emailed when the client answers.")
    offer.status = mp.STATUS_SENT
    offer.sent_at = datetime.now(UTC)
    offer.sent_by_user_id = user.id
    await mp.sync_intake_state(db, offer)
    await profiles.log_profile_action(
        db, profile, user, "merchant_offer.sent",
        f"Sent the processing offer to the client (estimated annual saving {mp._usd(offer.estimated_annual_savings)})",
        target_type=TARGET, target_id=offer.id,
    )
    await file_events.emit(
        db,
        profile=profile,
        kind="offer.sent",
        visibility=file_events.VISIBILITY_CLIENT,
        title=f"Processing offer sent: estimated annual savings {mp._usd(offer.estimated_annual_savings)}",
        actor=user,
        target_type=TARGET,
        target_id=offer.id,
    )
    await db.commit()
    return await _panel(db, profile)


@router.post("/{profile_id}/merchant-offer/withdraw", response_model=MerchantOfferPanelRead)
async def withdraw_merchant_offer(
    profile_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MerchantOfferPanelRead:
    profile = await _profile(db, profile_id, user)
    offer = await _open_offer(db, profile)
    offer.status = mp.STATUS_WITHDRAWN
    await mp.sync_intake_state(db, offer)
    await profiles.log_profile_action(
        db, profile, user, "merchant_offer.withdrawn", "Withdrew the processing offer",
        target_type=TARGET, target_id=offer.id,
    )
    await db.commit()
    return await _panel(db, profile)


@router.post("/{profile_id}/merchant-offer/reanalyze", response_model=MerchantOfferPanelRead)
async def reanalyze_merchant_offer(
    profile_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MerchantOfferPanelRead:
    """Read the PDF again, now. The only caller of the analysis pipeline's
    force flag: one file, one click, by the desk."""
    from app.services.bucket_ai import analyze_bucket_file

    profile = await _profile(db, profile_id, user)
    intake = await _intake(db, profile)
    offer = await _open_offer(db, profile)
    _not_answered(offer)
    file = await db.get(BucketFile, offer.source_file_id) if offer.source_file_id else None
    if file is None or file.deleted_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The terms PDF is no longer on the file. Drop it again.")
    review_type = (intake.bucket.ai_context or {}).get("review_type") if intake.bucket else None
    await analyze_bucket_file(db, file, review_type=review_type, force=True)
    await profiles.log_profile_action(
        db, profile, user, "merchant_offer.reread", f"Read the processing partner's terms again: {file.file_name}",
        target_type=TARGET, target_id=offer.id,
    )
    await db.commit()
    return await _panel(db, profile)


@router.post("/{profile_id}/merchant-offer/notify-client", response_model=MerchantOfferNotifyResult)
async def notify_client_of_merchant_offer(
    profile_id: UUID,
    payload: MerchantOfferNotifyClient,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MerchantOfferNotifyResult:
    """Email the client their room link, landing on the offer tab. Email
    only — this path never texts."""
    profile = await _profile(db, profile_id, user)
    offer = await _open_offer(db, profile)
    if offer.status not in mp.CLIENT_VISIBLE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "Send the offer first; the room shows nothing until then.")
    link = await _profile_room_link(db, profile)
    email = profiles.normalized_email(str(payload.recipient_email) if payload.recipient_email else link.recipient_email)
    if not email:
        raise HTTPException(status.HTTP_409_CONFLICT, "There is no email address for the client on this room.")
    rows = await _deliver_room_request(
        db,
        profile=profile,
        user=user,
        link=link,
        action_kind="merchant_offer_notice",
        purpose="review your merchant processing savings offer",
        query="tab=offer",
        email=email,
        phone=None,
        send_email=True,
        send_sms=False,
    )
    overall = _delivery_overall(rows)
    await profiles.log_profile_action(
        db, profile, user, "merchant_offer.client_notified",
        f"Emailed the client their room link for the processing offer ({overall})",
        target_type=TARGET, target_id=offer.id,
    )
    await db.commit()
    return MerchantOfferNotifyResult(overall_status=overall, room_url=_room_url(link, query="tab=offer"))


@router.post("/{profile_id}/merchant-offer/partner-email/resend", response_model=MerchantOfferPanelRead)
async def resend_partner_email(
    profile_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MerchantOfferPanelRead:
    profile = await _profile(db, profile_id, user)
    intake = await _intake(db, profile)
    offer = await _open_offer(db, profile)
    if offer.status not in mp.RESPONDED_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "The client has not answered yet; there is nothing to tell the partner.")
    if offer.partner_email_status == "sent":
        raise HTTPException(status.HTTP_409_CONFLICT, "The partner has already been emailed.")
    await mp.notify_partner(db, offer, profile=profile, intake=intake, business_name=_business_name(intake), force=True)
    await profiles.log_profile_action(
        db, profile, user, "merchant_offer.partner_email_resent",
        f"Re-sent the partner email ({offer.partner_email_status}{': ' + offer.partner_email_error if offer.partner_email_error else ''})",
        target_type=TARGET, target_id=offer.id,
    )
    await db.commit()
    return await _panel(db, profile)
