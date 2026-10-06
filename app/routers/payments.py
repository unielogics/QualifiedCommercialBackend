from __future__ import annotations

# FastAPI dependency declarations intentionally call Depends/Query in defaults.
# ruff: noqa: B008
import hashlib
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.application_profile import ApplicationProfile, ApplicationRoomDelivery
from app.models.broker import Broker
from app.models.bucket import BucketFile
from app.models.client import Client
from app.models.loan import Loan
from app.models.payments import (
    AchMandate,
    BankDirectFeeReceipt,
    FeeObligation,
    PaymentDebitNotice,
    PaymentInstallment,
    PaymentRefund,
    PaymentTransfer,
    PrivateFundingPaymentPlan,
)
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.routers import application_profiles as profile_routes
from app.schemas.payments import (
    BankDirectReceiptCreate,
    FeeAllocationPatch,
    FeeAllocationResponse,
    FeeObligationCreate,
    FeeReleaseRequest,
    FundingConfirmationCreate,
    PaymentPermissions,
    PaymentQueueItem,
    PaymentQueueResponse,
    PaymentQueueTotals,
    PaymentReviewRequest,
    PaymentSummary,
    PlanVersionAction,
    PrivatePlanCreate,
    PrivatePlanFromTermSheetCreate,
    PrivateSchedulePreview,
    RefundCreate,
    ServicingAuthorityCreate,
    TransferRetryRequest,
)
from app.services import ach_fee_workflow, plaid_transfer
from app.services import application_profiles as profiles
from app.services import payments as pay

router = APIRouter(tags=["payments"])


def _write_gate(*, private: bool = False, provider: bool = False) -> None:
    settings = get_settings()
    enabled = (
        settings.payments_enabled and settings.private_funding_payments_enabled
        if private
        else settings.payments_enabled
    )
    if not enabled:
        label = "Private-funding payments" if private else "ACH payments"
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"{label} are not enabled")
    ach_fee_workflow.require_legal_approval()
    if provider and not plaid_transfer.enabled():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Dedicated Plaid payment credentials are not configured",
        )


class FeeAgreementPrepareRequest(BaseModel):
    expected_allocation_version: int | None = Field(default=None, ge=1)
    include_origination_fee: bool = True
    include_consulting_fee: bool = False
    consulting_milestone_confirmed: bool = False
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class FeeAuthorizationSendRequest(BaseModel):
    scheduled_debit_date: date
    submission_window: Literal["business_day_et"] = "business_day_et"
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class AchMandateRevokeRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=240)


def _permissions(user: CurrentUser) -> PaymentPermissions:
    settings = get_settings()
    manager = user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC}
    workflow_ready = (
        settings.payments_enabled
        and not ach_fee_workflow.legal_approval_required()
    )
    provider_ready = (
        workflow_ready
        and plaid_transfer.enabled()
    )
    return PaymentPermissions(
        can_view=True,
        can_edit_allocation=manager and workflow_ready,
        can_prepare_obligation=manager and workflow_ready,
        can_prepare_fee_agreement=manager and workflow_ready,
        can_confirm_funding=manager and workflow_ready,
        can_send_authorization=manager and provider_ready,
        can_release_ach=manager and provider_ready,
        # Existing proof retrieval and revocation are safety controls, not
        # new money movement. They remain available while ACH creation is
        # paused by a feature, provider, or legal kill gate.
        can_manage_mandate_proof=manager,
        can_revoke_mandate=manager,
        can_retry=manager and provider_ready,
        can_refund=(
            user.role == Role.SUPER_ADMIN
            and provider_ready
            and settings.payment_refunds_enabled
        ),
        can_reconcile_bank_direct=manager and workflow_ready,
        can_manage_private_schedule=(
            manager
            and workflow_ready
            and settings.private_funding_payments_enabled
        ),
        can_manage_servicing_authority=(
            user.role == Role.SUPER_ADMIN
            and workflow_ready
            and settings.private_funding_payments_enabled
        ),
        can_waive=user.role == Role.SUPER_ADMIN and workflow_ready,
        can_request_review=(user.role in pay.READ_ROLES and workflow_ready),
    )


def _enforce_waiver_change_permission(
    *,
    user: User,
    latest: object | None,
    requested_waived_cents: int,
) -> None:
    allocation = getattr(latest, "allocation", None) or {}
    previous_waived_cents = int(allocation.get("waived_cents", 0) or 0)
    if (
        user.role != Role.SUPER_ADMIN
        and requested_waived_cents != previous_waived_cents
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only Super Admin may create or change a fee waiver",
        )


async def _summary(db: AsyncSession, profile: ApplicationProfile, user: CurrentUser) -> PaymentSummary:
    summary = await pay.build_summary(
        db,
        profile=profile,
        permissions=_permissions(user),
    )
    if user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        return summary

    # Assigned brokers and field representatives may follow collection status,
    # but bank identity, payer identity, proof artifacts, notice snapshots, and
    # agreement hashes are manager-only payment operations data.
    funding_source = (
        summary.funding_source.model_copy(
            update={
                "institution_name": None,
                "account_name": None,
                "account_mask": None,
                "account_subtype": None,
            }
        )
        if summary.funding_source
        else None
    )
    mandate = (
        summary.mandate.model_copy(
            update={
                "payer_name": "Client",
                "certificate_available": False,
                "can_revoke": False,
                "can_resend_proof": False,
                "artifact": None,
            }
        )
        if summary.mandate
        else None
    )
    fee_agreement = None
    if summary.fee_agreement:
        fee_agreement = {
            key: summary.fee_agreement.get(key)
            for key in (
                "id",
                "status",
                "template_version",
                "prepared_at",
                "sent_at",
                "signed_at",
                "countersigned_at",
                "proof_email_status",
                "current",
            )
        }
    return summary.model_copy(
        update={
            "funding_source": funding_source,
            "mandate": mandate,
            "debit_notice": None,
            "fee_agreement": fee_agreement,
            "agreement_documents": [],
            "servicing_authorities": [],
        }
    )


async def _profile_for_obligation(db: AsyncSession, obligation_id: UUID, user: CurrentUser) -> tuple[FeeObligation, ApplicationProfile]:
    obligation = await db.get(FeeObligation, obligation_id)
    if not obligation:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Fee obligation not found")
    profile = await profiles.load_profile(db, obligation.application_profile_id, user)
    return obligation, profile


async def _profile_or_obligation(db: AsyncSession, value: UUID, user: CurrentUser) -> ApplicationProfile:
    profile = await db.get(ApplicationProfile, value)
    if profile:
        return await profiles.load_profile(db, profile.id, user)
    obligation = await db.get(FeeObligation, value)
    if obligation:
        return await profiles.load_profile(db, obligation.application_profile_id, user)
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Application file not found")


async def _send_authorization_email_once(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    user: CurrentUser,
    idempotency_key: str,
    action_kind: str,
    purpose: str,
    path: str = "/buckets/request/{token}?tab=payments",
) -> tuple[ApplicationRoomDelivery, object, str, bool]:
    """Claim, commit, and deliver one secure-room email per durable key.

    The claim commits before the provider call. A lost response can therefore
    never cause the same key to send twice; a deliberate resend needs a fresh
    key. A claim left in ``sending`` is surfaced as uncertain for review.
    """

    link = await profile_routes._profile_room_link(db, profile)
    await pay.assert_payment_room_identity(db, link=link, profile=profile)
    email = profiles.normalized_email(link.recipient_email)
    if not email:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The application room needs a valid client email",
        )
    await pay._lock_profile(db, profile.id)
    existing = (
        await db.execute(
            select(ApplicationRoomDelivery).where(
                ApplicationRoomDelivery.idempotency_key == idempotency_key
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing, link, email, True
    row = ApplicationRoomDelivery(
        profile_id=profile.id,
        bucket_id=profile.primary_bucket_id,
        action_kind=action_kind,
        channel="email",
        recipient_email=email,
        status="sending",
        detail="Delivery claimed; awaiting provider result",
        provider_result={"accepted": None},
        initiation_source="payments",
        idempotency_key=idempotency_key,
        created_by_user_id=user.id,
    )
    db.add(row)
    await db.commit()

    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    client = await db.get(Client, profile.client_id) if profile.client_id else None
    business_name = profile_routes._business_label(profile, intake, client)
    try:
        result = await profile_routes.consent_delivery.deliver_link_checked(
            db,
            channel="email",
            to_email=email,
            to_phone=None,
            business_name=business_name,
            purpose=purpose,
            path=path.format(token=link.token),
            rep_name=user.name,
            origin=get_settings().frontend_app_url,
        )
        row.status = "sent" if result.email_ok else "failed"
        row.detail = result.detail
        row.provider_result = {"accepted": bool(result.email_ok)}
    except Exception as exc:  # noqa: BLE001 - preserve a durable uncertain result
        row.status = "failed"
        row.detail = "Payment authorization delivery failed before a definite result"
        row.provider_result = {
            "accepted": False,
            "error_type": type(exc).__name__,
        }
    await db.commit()
    return row, link, email, False


@router.get("/application-profiles/{profile_id}/payments/summary", response_model=PaymentSummary)
async def payment_summary(
    profile_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentSummary:
    pay.require_reader(user)
    profile = await profiles.load_profile(db, profile_id, user)
    return await _summary(db, profile, user)


@router.post(
    "/application-profiles/{profile_id}/payments/request-review",
    response_model=PaymentSummary,
)
async def request_payment_review(
    profile_id: UUID,
    payload: PaymentReviewRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PaymentSummary:
    _write_gate()
    pay.require_reader(user)
    profile = await profiles.load_profile(db, profile_id, user)
    key = idempotency_header or payload.idempotency_key
    if not key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Idempotency-Key header is required")
    await pay.request_payment_review(
        db,
        profile=profile,
        actor=user,
        idempotency_key=key,
        reason=payload.reason,
    )
    await db.commit()
    return await _summary(db, profile, user)


@router.put(
    "/application-profiles/{profile_id}/payments/fee-allocation",
    response_model=FeeAllocationResponse,
)
async def put_fee_allocation(
    profile_id: UUID,
    payload: FeeAllocationPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> FeeAllocationResponse:
    del idempotency_key  # the version and immutable allocation rows make retries idempotent
    _write_gate()
    pay.require_manager(user)
    profile = await profiles.load_profile(db, profile_id, user)
    # Serialize the role-sensitive waiver comparison with allocation writes.
    # Without this profile lock, a Loan Executive could read an old waiver,
    # race a Super Admin update, and then overwrite the newly waived amount
    # while still passing the comparison below. The save service takes this
    # same lock, so holding it here makes the authorization decision and the
    # immutable allocation-version write one transaction boundary.
    await pay._lock_profile(db, profile.id)
    latest = await pay.latest_allocation(db, profile.id, for_update=True)
    new_waived = pay._patch_allocation_dict(payload)["waived_cents"]
    _enforce_waiver_change_permission(
        user=user,
        latest=latest,
        requested_waived_cents=new_waived,
    )
    row = await pay.save_fee_allocation(db, profile=profile, payload=payload, actor=user)
    await db.commit()
    await db.refresh(row)
    economics = pay.economics_snapshot(profile)
    return pay.allocation_response(row, current_gross_cents=economics.gross_fee_cents)


@router.post(
    "/application-profiles/{profile_id}/payments/fee-obligations",
    response_model=PaymentSummary,
)
async def prepare_fee_obligation(
    profile_id: UUID,
    payload: FeeObligationCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentSummary:
    _write_gate()
    pay.require_manager(user)
    profile = await profiles.load_profile(db, profile_id, user)
    # FeeObligationCreate retains direct-allocation fields for compatibility,
    # so enforce the same Super Admin-only waiver rule as the allocation
    # endpoint. Hold the profile lock through obligation preparation to keep a
    # concurrent waiver edit from changing the authorization decision.
    await pay._lock_profile(db, profile.id)
    latest = await pay.latest_allocation(db, profile.id, for_update=True)
    direct_allocation = pay._allocation_dict(payload)
    if any(direct_allocation.values()):
        _enforce_waiver_change_permission(
            user=user,
            latest=latest,
            requested_waived_cents=direct_allocation["waived_cents"],
        )
    await pay.create_fee_obligation(db, profile=profile, payload=payload, actor=user)
    await db.commit()
    return await _summary(db, profile, user)


@router.post(
    "/application-profiles/{profile_id}/payments/fee-agreements/prepare",
    response_model=PaymentSummary,
)
async def prepare_success_fee_agreement(
    profile_id: UUID,
    payload: FeeAgreementPrepareRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PaymentSummary:
    _write_gate()
    pay.require_manager(user)
    key = idempotency_header or payload.idempotency_key
    if not key:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Idempotency-Key header is required",
        )
    profile = await profiles.load_profile(db, profile_id, user)
    requested = await ach_fee_workflow.prepare_success_fee_agreement(
        db,
        profile=profile,
        actor=user,
        include_origination_fee=payload.include_origination_fee,
        include_consulting_fee=payload.include_consulting_fee,
        consulting_milestone_confirmed=payload.consulting_milestone_confirmed,
        expected_allocation_version=payload.expected_allocation_version,
        idempotency_key=key,
    )
    delivery_key = (
        f"fee-agreement:{requested.id}:"
        f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}"
    )
    delivery, _link, email, replay = await _send_authorization_email_once(
        db,
        profile=profile,
        user=user,
        idempotency_key=delivery_key,
        action_kind="success_fee_agreement_signature_request",
        purpose="review and sign the deal-specific Success Fee Agreement",
        path=(
            f"/buckets/request/{{token}}?tab=agreements&request={requested.id}"
        ),
    )
    source = dict(requested.requirement_source or {})
    source["signature_request_delivery_id"] = str(delivery.id)
    source["signature_request_delivery_status"] = delivery.status
    source["signature_request_recipient"] = email
    requested.requirement_source = source
    if delivery.status != "sent" and not replay:
        await pay.log_event(
            db,
            profile_id=profile.id,
            actor_id=user.id,
            event_type="success_fee_agreement.delivery_failed",
            entity_type="bucket_requested_document",
            entity_id=requested.id,
            summary="Success Fee Agreement signature request was not accepted",
            metadata={"delivery_id": str(delivery.id)},
        )
    await db.commit()
    return await _summary(db, profile, user)


@router.post("/fee-obligations/{obligation_id}/send-authorization", response_model=PaymentSummary)
async def send_fee_authorization(
    obligation_id: UUID,
    payload: FeeAuthorizationSendRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PaymentSummary:
    _write_gate(provider=True)
    pay.require_manager(user)
    obligation, profile = await _profile_for_obligation(db, obligation_id, user)
    if obligation.client_ach_cents <= 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "No client ACH amount is allocated")
    if not await pay._fee_agreement_is_current(db, obligation):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The signed fee agreement artifact is missing, changed, or no longer verified",
        )
    if obligation.status in {"processing", "collected", "cancelled", "superseded"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "Authorization cannot be requested in the current state")
    request_key = idempotency_key or payload.idempotency_key
    if not request_key or not (8 <= len(request_key) <= 128):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Idempotency-Key header (8-128 characters) is required",
        )
    notice = await ach_fee_workflow.prepare_debit_notice(
        db,
        profile=profile,
        obligation=obligation,
        actor=user,
        scheduled_debit_date=payload.scheduled_debit_date,
        submission_window=payload.submission_window,
        idempotency_key=request_key,
    )
    del request
    delivery_key = (
        f"fee-ach:{obligation.id}:"
        f"{hashlib.sha256(request_key.encode('utf-8')).hexdigest()[:32]}"
    )
    delivery, link, email, replay = await _send_authorization_email_once(
        db,
        profile=profile,
        user=user,
        idempotency_key=delivery_key,
        action_kind="payment_authorization_request",
        purpose="review and authorize the QC fee payment request",
    )
    if delivery.status == "sending":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The prior delivery result is uncertain; review it before using a new key",
        )
    if delivery.status != "sent":
        if not replay:
            await pay.log_event(
                db,
                profile_id=profile.id,
                actor_id=user.id,
                event_type="ach_authorization.delivery_failed",
                entity_type="fee_obligation",
                entity_id=obligation.id,
                summary="ACH authorization request email was not accepted",
                metadata={"recipient": email, "delivery_id": str(delivery.id)},
            )
            await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The authorization email was not accepted; no request was marked sent")
    if obligation.authorization_sent_at is None:
        obligation.status = "awaiting_authorization"
        obligation.authorization_sent_at = datetime.now(UTC)
        obligation.record_version += 1
        await pay.log_event(
            db,
            profile_id=profile.id,
            actor_id=user.id,
            event_type="ach_authorization.requested",
            entity_type="fee_obligation",
            entity_id=obligation.id,
            summary="Sent secure ACH authorization request",
            metadata={
                "recipient": email,
                "room_link_id": str(link.id),
                "delivery_id": str(delivery.id),
                "debit_notice_id": str(notice.id),
                "scheduled_debit_at": notice.scheduled_debit_at.isoformat(),
            },
        )
    await db.commit()
    return await _summary(db, profile, user)


@router.post(
    "/application-profiles/{profile_or_obligation_id}/payments/funding-confirmations",
    response_model=PaymentSummary,
)
async def post_funding_confirmation(
    profile_or_obligation_id: UUID,
    payload: FundingConfirmationCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentSummary:
    _write_gate()
    pay.require_manager(user)
    profile = await _profile_or_obligation(db, profile_or_obligation_id, user)
    await pay.confirm_actual_funding(db, profile=profile, payload=payload, actor=user)
    await db.commit()
    return await _summary(db, profile, user)


@router.post("/fee-obligations/{obligation_id}/release", response_model=PaymentSummary)
async def release_fee_ach(
    obligation_id: UUID,
    payload: FeeReleaseRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PaymentSummary:
    _write_gate(provider=True)
    pay.require_manager(user)
    obligation, profile = await _profile_for_obligation(db, obligation_id, user)
    if payload.expected_version and payload.expected_version != obligation.record_version:
        raise HTTPException(status.HTTP_409_CONFLICT, "Fee obligation changed; reload before release")
    key = idempotency_header or payload.idempotency_key
    if not key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Idempotency-Key header is required")
    await pay.prepare_fee_transfer(
        db,
        obligation_id=obligation.id,
        amount_cents=payload.amount_cents,
        idempotency_key=key,
        actor=user,
    )
    await db.commit()
    return await _summary(db, profile, user)


@router.get("/ach-mandates/{mandate_id}/certificate")
async def download_ach_mandate_proof(
    mandate_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    pay.require_reader(user)
    mandate = await db.get(AchMandate, mandate_id)
    if mandate is None or not mandate.certificate_s3_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ACH authorization proof not found")
    await profiles.load_profile(db, mandate.application_profile_id, user)
    await ach_fee_workflow.extend_mandate_retention(
        db, mandate, anchor=datetime.now(UTC)
    )
    await db.commit()
    proof_file = (
        await db.get(BucketFile, mandate.certificate_bucket_file_id)
        if mandate.certificate_bucket_file_id
        else None
    )
    if proof_file is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ACH authorization proof has no protected file record",
        )
    url = await ach_fee_workflow.verified_protected_download_url(
        proof_file,
        download_filename="QC-one-time-ACH-authorization.pdf",
    )
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ach-mandates/{mandate_id}/resend-proof", response_model=PaymentSummary)
async def resend_ach_mandate_proof(
    mandate_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentSummary:
    pay.require_manager(user)
    mandate = await db.get(AchMandate, mandate_id)
    if mandate is None or mandate.fee_obligation_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ACH authorization not found")
    profile = await profiles.load_profile(db, mandate.application_profile_id, user)
    proof_message = await ach_fee_workflow.ensure_mandate_proof_queued(
        db,
        mandate=mandate,
        profile=profile,
        force_new=True,
    )
    # Persist the new audited attempt before the provider handoff so a process
    # interruption cannot produce an unrecorded customer copy.
    await db.commit()
    await ach_fee_workflow.deliver_mandate_proof(
        db,
        mandate=mandate,
        profile=profile,
        recorded_row=proof_message,
    )
    await ach_fee_workflow.extend_mandate_retention(
        db, mandate, anchor=datetime.now(UTC)
    )
    await db.commit()
    return await _summary(db, profile, user)


@router.post(
    "/ach-mandates/{mandate_id}/resend-debit-notice",
    response_model=PaymentSummary,
)
async def resend_ach_debit_notice(
    mandate_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentSummary:
    """Send a fresh audited copy of the already-bound exact debit notice."""

    pay.require_manager(user)
    mandate = (
        await db.execute(
            select(AchMandate)
            .where(AchMandate.id == mandate_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if mandate is None or mandate.fee_obligation_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ACH authorization not found")
    profile = await profiles.load_profile(db, mandate.application_profile_id, user)
    notice = (
        await db.execute(
            select(PaymentDebitNotice)
            .where(
                PaymentDebitNotice.fee_obligation_id == mandate.fee_obligation_id,
                PaymentDebitNotice.mandate_id == mandate.id,
                PaymentDebitNotice.superseded_at.is_(None),
                PaymentDebitNotice.revoked_at.is_(None),
            )
            .order_by(PaymentDebitNotice.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if notice is None or not notice.notice_bucket_file_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The exact protected debit notice is not available to resend",
        )
    notice_message = await ach_fee_workflow.ensure_debit_notice_queued(
        db,
        notice=notice,
        mandate=mandate,
        profile=profile,
        force_new=True,
    )
    if notice_message is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The exact protected debit notice could not be queued",
        )
    await db.commit()
    await ach_fee_workflow.deliver_debit_notice(
        db,
        notice=notice,
        mandate=mandate,
        profile=profile,
        recorded_row=notice_message,
    )
    await ach_fee_workflow.extend_mandate_retention(
        db,
        mandate,
        anchor=max(datetime.now(UTC), notice.scheduled_debit_at),
    )
    await db.commit()
    return await _summary(db, profile, user)


@router.post("/ach-mandates/{mandate_id}/revoke", response_model=PaymentSummary)
async def revoke_ach_mandate(
    mandate_id: UUID,
    payload: AchMandateRevokeRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentSummary:
    pay.require_manager(user)
    mandate = (
        await db.execute(
            select(AchMandate).where(AchMandate.id == mandate_id).with_for_update()
        )
    ).scalar_one_or_none()
    if mandate is None or mandate.fee_obligation_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ACH authorization not found")
    profile = await profiles.load_profile(db, mandate.application_profile_id, user)
    await ach_fee_workflow.revoke_mandate(
        db,
        mandate=mandate,
        actor_user_id=user.id,
        reason=payload.reason,
    )
    # The revocation and its queued notification ledger row must be durable
    # before any external provider receives the confirmation email.
    await db.commit()
    await ach_fee_workflow.deliver_revocation_confirmation(db, mandate)
    await db.commit()
    return await _summary(db, profile, user)


@router.post(
    "/application-profiles/{profile_id}/payments/bank-direct-receipts",
    response_model=PaymentSummary,
)
async def post_bank_direct_receipt(
    profile_id: UUID,
    payload: BankDirectReceiptCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentSummary:
    _write_gate()
    pay.require_manager(user)
    profile = await profiles.load_profile(db, profile_id, user)
    obligation = await pay.current_obligation(db, profile.id, for_update=True)
    if not obligation:
        raise HTTPException(status.HTTP_409_CONFLICT, "Prepare a fee obligation first")
    payload = payload.model_copy(update={"obligation_id": obligation.id})
    await pay.record_bank_direct_receipt(db, obligation=obligation, payload=payload, actor=user)
    await db.commit()
    return await _summary(db, profile, user)


@router.post("/payment-transfers/{transfer_id}/retry", response_model=PaymentSummary)
async def retry_payment_transfer(
    transfer_id: UUID,
    payload: TransferRetryRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PaymentSummary:
    _write_gate(provider=True)
    pay.require_manager(user)
    transfer = await db.get(PaymentTransfer, transfer_id)
    if not transfer:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transfer not found")
    profile = await profiles.load_profile(db, transfer.application_profile_id, user)
    key = idempotency_header or payload.idempotency_key
    if not key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Idempotency-Key header is required")
    await pay.retry_transfer(
        db,
        transfer_id=transfer.id,
        idempotency_key=key,
        actor=user,
    )
    await db.commit()
    return await _summary(db, profile, user)


@router.post("/payment-transfers/{transfer_id}/resume", response_model=PaymentSummary)
async def resume_payment_transfer(
    transfer_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentSummary:
    _write_gate(provider=True)
    pay.require_manager(user)
    transfer = await db.get(PaymentTransfer, transfer_id)
    if not transfer:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transfer not found")
    profile = await profiles.load_profile(db, transfer.application_profile_id, user)
    await pay.resume_action_required_transfer(
        db,
        transfer_id=transfer.id,
        profile_id=profile.id,
        actor_id=user.id,
        bank_repair_completed=False,
    )
    await db.commit()
    return await _summary(db, profile, user)


@router.post("/payment-transfers/{transfer_id}/refunds", response_model=PaymentSummary)
async def create_payment_refund(
    transfer_id: UUID,
    payload: RefundCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PaymentSummary:
    _write_gate(provider=True)
    if not get_settings().payment_refunds_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ACH refunds are not enabled until production return/refund controls are verified",
        )
    pay.require_super_admin(user)
    transfer = await db.get(PaymentTransfer, transfer_id)
    if not transfer:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transfer not found")
    profile = await profiles.load_profile(db, transfer.application_profile_id, user)
    key = idempotency_header or payload.idempotency_key
    if not key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Idempotency-Key header is required")
    await pay.create_refund_intent(
        db,
        transfer=transfer,
        amount_cents=pay._cents(payload.amount),
        reason=payload.reason,
        idempotency_key=key,
        actor=user,
    )
    await db.commit()
    return await _summary(db, profile, user)


@router.post(
    "/application-profiles/{profile_id}/payments/private-plans",
    response_model=PaymentSummary,
)
async def post_private_plan(
    profile_id: UUID,
    payload: PrivatePlanCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentSummary:
    _write_gate(private=True)
    pay.require_manager(user)
    profile = await profiles.load_profile(db, profile_id, user)
    await pay.create_private_plan(db, profile=profile, payload=payload, actor=user)
    await db.commit()
    return await _summary(db, profile, user)


@router.get(
    "/application-profiles/{profile_id}/payments/private-plan-preview",
    response_model=PrivateSchedulePreview,
)
async def get_private_plan_preview(
    profile_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    production_term_sheet_id: UUID | None = Query(default=None),
) -> PrivateSchedulePreview:
    pay.require_reader(user)
    profile = await profiles.load_profile(db, profile_id, user)
    return await pay.private_schedule_preview(
        db,
        profile_id=profile.id,
        production_term_sheet_id=production_term_sheet_id,
    )


@router.post(
    "/application-profiles/{profile_id}/payments/private-plans/from-term-sheet",
    response_model=PaymentSummary,
)
async def post_private_plan_from_term_sheet(
    profile_id: UUID,
    payload: PrivatePlanFromTermSheetCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentSummary:
    _write_gate(private=True)
    pay.require_manager(user)
    profile = await profiles.load_profile(db, profile_id, user)
    await pay.create_private_plan_from_term_sheet(
        db,
        profile=profile,
        payload=payload,
        actor=user,
    )
    await db.commit()
    return await _summary(db, profile, user)


@router.post(
    "/application-profiles/{profile_id}/payments/servicing-authorities",
    response_model=PaymentSummary,
)
async def post_servicing_authority(
    profile_id: UUID,
    payload: ServicingAuthorityCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentSummary:
    _write_gate(private=True)
    pay.require_super_admin(user)
    profile = await profiles.load_profile(db, profile_id, user)
    await pay.create_servicing_authority(db, profile=profile, payload=payload, actor=user)
    await db.commit()
    return await _summary(db, profile, user)


@router.post("/private-payment-plans/{plan_id}/activate", response_model=PaymentSummary)
async def activate_private_plan(
    plan_id: UUID,
    payload: PlanVersionAction,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentSummary:
    _write_gate(private=True)
    pay.require_manager(user)
    plan = await db.get(PrivateFundingPaymentPlan, plan_id)
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment plan not found")
    profile = await profiles.load_profile(db, plan.application_profile_id, user)
    await pay.activate_private_plan(
        db,
        plan_id=plan.id,
        expected_record_version=payload.expected_record_version,
        actor=user,
    )
    await db.commit()
    return await _summary(db, profile, user)


@router.post(
    "/private-payment-plans/{plan_id}/send-authorization",
    response_model=PaymentSummary,
)
async def send_private_plan_authorization(
    plan_id: UUID,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PaymentSummary:
    _write_gate(private=True, provider=True)
    pay.require_manager(user)
    if not idempotency_key or not (8 <= len(idempotency_key) <= 128):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Idempotency-Key header (8-128 characters) is required",
        )
    plan = await db.get(PrivateFundingPaymentPlan, plan_id)
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment plan not found")
    profile = await profiles.load_profile(db, plan.application_profile_id, user)
    if plan.status not in {"draft", "paused"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Authorization can only be requested for a draft or paused schedule",
        )
    delivery_key = (
        f"private-ach:{plan.id}:"
        f"{hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()[:32]}"
    )
    delivery, link, email, replay = await _send_authorization_email_once(
        db,
        profile=profile,
        user=user,
        idempotency_key=delivery_key,
        action_kind="private_payment_authorization",
        purpose="connect a business bank account and authorize the fixed payment schedule",
    )
    if delivery.status == "sending":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The prior delivery result is uncertain; review it before using a new key",
        )
    delivered = delivery.status == "sent"
    if not replay:
        await pay.log_event(
            db,
            profile_id=profile.id,
            actor_id=user.id,
            event_type=(
                "private_plan.authorization_requested"
                if delivered
                else "private_plan.authorization_delivery_failed"
            ),
            entity_type="private_funding_payment_plan",
            entity_id=plan.id,
            summary=(
                "Sent secure private-schedule ACH authorization request"
                if delivered
                else "Private-schedule authorization email was not accepted"
            ),
            metadata={
                "recipient": email,
                "room_link_id": str(link.id),
                "delivery_id": str(delivery.id),
                "idempotency_key_sha256": hashlib.sha256(
                    idempotency_key.encode("utf-8")
                ).hexdigest(),
            },
        )
        await db.commit()
    if not delivered:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "The authorization email was not accepted; no request was marked sent",
        )
    return await _summary(db, profile, user)


@router.post("/private-payment-plans/{plan_id}/pause", response_model=PaymentSummary)
async def pause_private_plan(
    plan_id: UUID,
    payload: PlanVersionAction,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentSummary:
    _write_gate(private=True)
    pay.require_manager(user)
    plan = await db.get(PrivateFundingPaymentPlan, plan_id)
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment plan not found")
    profile = await profiles.load_profile(db, plan.application_profile_id, user)
    await pay.pause_private_plan(
        db,
        plan_id=plan.id,
        expected_record_version=payload.expected_record_version,
        actor=user,
        reason=payload.reason,
    )
    await db.commit()
    return await _summary(db, profile, user)


@router.post("/private-payment-plans/{plan_id}/cancel", response_model=PaymentSummary)
async def cancel_private_plan(
    plan_id: UUID,
    payload: PlanVersionAction,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaymentSummary:
    _write_gate(private=True)
    pay.require_manager(user)
    plan = await db.get(PrivateFundingPaymentPlan, plan_id)
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment plan not found")
    profile = await profiles.load_profile(db, plan.application_profile_id, user)
    await pay.cancel_private_plan(
        db,
        plan_id=plan.id,
        expected_record_version=payload.expected_record_version,
        actor=user,
        reason=payload.reason,
    )
    await db.commit()
    return await _summary(db, profile, user)


@router.get("/payments", response_model=PaymentQueueResponse)
async def payments_queue(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    queue: str = Query(default="all"),
    status_filter: str | None = Query(default=None, alias="status"),
    q: str = Query(default="", max_length=200),
    owner_id: UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> PaymentQueueResponse:
    pay.require_manager(user)
    obligation_stmt = select(FeeObligation).where(FeeObligation.superseded_at.is_(None))
    if queue == "private_funding":
        obligation_rows: list[FeeObligation] = []
    else:
        obligation_rows = (await db.execute(obligation_stmt.order_by(FeeObligation.updated_at.desc()))).scalars().all()
    plan_stmt = select(PrivateFundingPaymentPlan)
    if queue == "origination_fees":
        plan_rows: list[PrivateFundingPaymentPlan] = []
    else:
        plan_rows = (await db.execute(plan_stmt.order_by(PrivateFundingPaymentPlan.updated_at.desc()))).scalars().all()
    all_items: list[PaymentQueueItem] = []
    totals = PaymentQueueTotals()

    async def resolved_owner(
        profile_id: UUID,
        client_id: UUID | None,
        loan_id: UUID | None,
    ) -> tuple[UUID | None, str | None]:
        resolved_id: UUID | None = None
        if loan_id:
            loan = await db.get(Loan, loan_id)
            if loan:
                resolved_id = loan.assigned_owner_id
                if resolved_id is None and loan.broker_id:
                    broker = await db.get(Broker, loan.broker_id)
                    resolved_id = broker.user_id if broker else None
        if resolved_id is None and client_id:
            client = await db.get(Client, client_id)
            if client:
                resolved_id = client.current_agent_id
                if resolved_id is None and client.broker_id:
                    broker = await db.get(Broker, client.broker_id)
                    resolved_id = broker.user_id if broker else None
        if resolved_id is None:
            profile = await db.get(ApplicationProfile, profile_id)
            intake = (
                await db.get(PublicUnderwritingIntake, profile.intake_id)
                if profile and profile.intake_id
                else None
            )
            if intake:
                resolved_id = intake.assigned_underwriter_user_id or intake.broker_id
        owner = await db.get(User, resolved_id) if resolved_id else None
        return resolved_id, owner.name if owner else None

    requested_status = (
        "awaiting_client_authorization"
        if status_filter == "awaiting_authorization"
        else status_filter
    )
    search_text = q.strip().casefold()

    def search_matches(*values: object) -> bool:
        if not search_text:
            return True
        return any(
            search_text in str(value).casefold()
            for value in values
            if value is not None
        )

    for row in obligation_rows:
        resolved_owner_id, owner_name = await resolved_owner(
            row.application_profile_id, row.client_id, row.loan_id
        )
        if owner_id and resolved_owner_id != owner_id:
            continue
        if not search_matches(
            row.business_name_snapshot,
            row.client_name_snapshot,
            row.client_email_snapshot,
            row.agreement_reference,
            row.id,
            row.application_profile_id,
            row.client_id,
            row.loan_id,
            row.intake_id,
            owner_name,
        ):
            continue
        transfers = (
            await db.execute(
                select(PaymentTransfer).where(PaymentTransfer.fee_obligation_id == row.id).order_by(PaymentTransfer.created_at.desc())
            )
        ).scalars().all()
        latest = transfers[0] if transfers else None
        transfer_ids = [item.id for item in transfers]
        refunded = int((await db.execute(
            select(func.coalesce(func.sum(PaymentRefund.amount_cents), 0)).where(
                PaymentRefund.transfer_id.in_(transfer_ids),
                PaymentRefund.status.in_(pay.REFUND_COMPLETED_STATUSES),
            )
        )).scalar_one()) if transfer_ids else 0
        ach_collected = max(
            0,
            sum(
                item.amount_cents
                for item in transfers
                if item.status == pay.COLLECTED_TRANSFER_STATUS
            ) - refunded,
        )
        receipts = (
            await db.execute(
                select(BankDirectFeeReceipt).where(
                    BankDirectFeeReceipt.obligation_id == row.id
                )
            )
        ).scalars().all()
        bank_received = sum(
            receipt.amount_cents
            for receipt in receipts
            if receipt.receipt_type == "bank_direct"
        )
        external_received = sum(
            receipt.amount_cents
            for receipt in receipts
            if receipt.receipt_type == "external_manual"
        )
        bank_outstanding = max(0, row.bank_direct_cents - bank_received)
        external_outstanding = max(0, row.external_cents - external_received)
        outstanding = max(
            0,
            row.client_ach_cents
            + row.bank_direct_cents
            + row.external_cents
            - ach_collected
            - bank_received
            - external_received,
        )
        active_mandate = (
            await db.execute(
                select(AchMandate)
                .where(
                    AchMandate.fee_obligation_id == row.id,
                    AchMandate.status == "active",
                    AchMandate.revoked_at.is_(None),
                )
                .order_by(AchMandate.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        confirmation = await pay._current_confirmation(db, row.application_profile_id)
        readiness, _, _ = await pay.fee_release_readiness(db, row)
        matches: set[str] = set()
        if latest and latest.status in pay.PROCESSING_TRANSFER_STATUSES:
            derived_status = "processing"
            next_action = "Monitor ACH processing"
        elif latest and latest.status in {"returned", "failed", "action_required"}:
            derived_status = "action_required"
            next_action = "Resolve the ACH return or payment error"
        elif bank_outstanding or external_outstanding:
            derived_status = "bank_direct_outstanding"
            next_action = "Reconcile the outstanding external receipt"
        elif outstanding == 0 and row.client_ach_cents > 0 and any(
            item.status == pay.COLLECTED_TRANSFER_STATUS for item in transfers
        ):
            derived_status = "funds_available"
            next_action = "Collection complete"
        elif outstanding == 0:
            derived_status = "externally_reconciled"
            next_action = "External fee receipts reconciled"
        elif row.client_ach_cents and not active_mandate:
            derived_status = "awaiting_client_authorization"
            next_action = "Send or await client ACH authorization"
        elif row.client_ach_cents and not confirmation:
            derived_status = "ready_for_funding_confirmation"
            next_action = "Record authoritative actual funding"
        elif row.client_ach_cents and readiness.ready_for_release:
            derived_status = "ready_for_release"
            next_action = "Review and release the exact ACH amount"
        else:
            derived_status = "action_required"
            next_action = readiness.blockers[0] if readiness.blockers else "Review payment readiness"
        matches.add(derived_status)
        if refunded:
            matches.add("refunded")
        if bank_outstanding:
            matches.add("bank_direct_outstanding")
        if derived_status == "awaiting_client_authorization":
            totals.awaiting_authorization += 1
        elif derived_status == "ready_for_funding_confirmation":
            totals.ready_for_funding_confirmation += 1
        elif derived_status == "ready_for_release":
            totals.ready_for_release += 1
        elif derived_status == "processing":
            totals.processing += 1
        elif derived_status == "funds_available":
            totals.funds_available += 1
        elif derived_status == "externally_reconciled":
            totals.externally_reconciled += 1
        elif derived_status == "action_required":
            totals.action_required += 1
        if refunded:
            totals.refunded += 1
        if bank_outstanding:
            totals.bank_direct_outstanding += 1
        if requested_status and requested_status not in matches:
            continue
        all_items.append(PaymentQueueItem(
            id=row.id,
            profile_id=row.application_profile_id,
            client_id=row.client_id,
            loan_id=row.loan_id,
            intake_id=row.intake_id,
            display_name=row.business_name_snapshot or row.client_name_snapshot or "Application file",
            reference=row.agreement_reference,
            owner_id=resolved_owner_id,
            owner_name=owner_name,
            kind="origination_fee",
            status=derived_status,
            amount=row.gross_fee_cents / 100,
            outstanding_amount=outstanding / 100,
            next_action=next_action,
            updated_at=row.updated_at,
        ))
    for row in plan_rows:
        plan_profile = await db.get(ApplicationProfile, row.application_profile_id)
        plan_business_name = None
        plan_client_name = None
        plan_client_email = None
        if plan_profile:
            plan_business_name, plan_client_name, plan_client_email = (
                await pay._profile_identity(db, plan_profile)
            )
        resolved_owner_id, owner_name = await resolved_owner(
            row.application_profile_id, row.client_id, row.loan_id
        )
        if owner_id and resolved_owner_id != owner_id:
            continue
        if not search_matches(
            plan_business_name,
            plan_client_name,
            plan_client_email,
            row.creditor_name,
            row.payee_name,
            row.agreement_reference,
            row.id,
            row.application_profile_id,
            row.client_id,
            row.loan_id,
            plan_profile.intake_id if plan_profile else None,
            owner_name,
        ):
            continue
        installments = (
            await db.execute(
                select(PaymentInstallment).where(PaymentInstallment.plan_id == row.id)
            )
        ).scalars().all()
        installment_ids = [installment.id for installment in installments]
        transfers = (
            await db.execute(
                select(PaymentTransfer)
                .where(PaymentTransfer.installment_id.in_(installment_ids))
                .order_by(PaymentTransfer.created_at.desc())
            )
        ).scalars().all() if installment_ids else []
        collected = sum(
            transfer.amount_cents
            for transfer in transfers
            if transfer.status == pay.COLLECTED_TRANSFER_STATUS
        )
        if any(
            transfer.status in {"returned", "failed", "action_required"}
            for transfer in transfers
        ):
            derived_status = "action_required"
            next_action = "Resolve the private-payment transfer issue"
        elif any(
            transfer.status in pay.PROCESSING_TRANSFER_STATUSES for transfer in transfers
        ):
            derived_status = "processing"
            next_action = "Monitor scheduled ACH processing"
        else:
            derived_status = row.status
            next_action = {
                "draft": "Send the client authorization request",
                "paused": "Review and resume with a replacement if needed",
                "active": "Monitor the next scheduled installment",
                "completed": "Schedule complete",
                "cancelled": "Schedule permanently cancelled",
                "superseded": "Review the replacement schedule",
            }.get(row.status, "Review fixed-payment schedule")
        matches = {derived_status}
        if row.status == "active" and row.next_due_date:
            totals.upcoming_private_installments += 1
        if derived_status == "processing":
            totals.processing += 1
        elif derived_status == "action_required":
            totals.action_required += 1
        if requested_status and requested_status not in matches:
            continue
        all_items.append(PaymentQueueItem(
            id=row.id,
            profile_id=row.application_profile_id,
            client_id=row.client_id,
            loan_id=row.loan_id,
            intake_id=plan_profile.intake_id if plan_profile else None,
            display_name=plan_business_name or plan_client_name or row.creditor_name,
            reference=row.agreement_reference,
            owner_id=resolved_owner_id,
            owner_name=owner_name,
            kind="private_funding",
            status=derived_status,
            amount=row.total_amount_cents / 100,
            outstanding_amount=max(0, row.total_amount_cents - collected) / 100,
            next_action=next_action,
            next_due_at=row.next_due_date,
            updated_at=row.updated_at,
        ))
    all_items.sort(key=lambda item: item.updated_at, reverse=True)
    total = len(all_items)
    start = (page - 1) * page_size
    return PaymentQueueResponse(
        items=all_items[start : start + page_size],
        total=total,
        server_now=datetime.now(UTC),
        totals=totals,
    )
