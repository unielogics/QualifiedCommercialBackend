"""PIN-protected client-room endpoints for ACH authorization.

Browser values are treated only as intent.  Fee amounts, ACH class,
obligation hashes, and certificate contents are derived again on the server.
"""

from __future__ import annotations

# FastAPI dependencies are intentionally declared as callable defaults.
# ruff: noqa: B008
import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models.bucket import BucketDocumentSignature, BucketFile, BucketRequestedDocument
from app.models.message_send import MessageSend
from app.models.payments import (
    AchMandate,
    FeeObligationLine,
    PaymentFundingSource,
    PaymentInstallment,
    PaymentRefund,
    PaymentTransfer,
    PrivateFundingPaymentPlan,
)
from app.routers.application_profiles import _public_application_room
from app.schemas.payments import (
    AchMandateCreate,
    PaymentDebitNoticeRead,
)
from app.services import ach_authorization, ach_fee_workflow, plaid_transfer
from app.services import application_profiles as profiles
from app.services import payments as pay

router = APIRouter(tags=["public payments"])


class RoomPaymentAccess(BaseModel):
    passcode: str = Field(min_length=6, max_length=16)


class RoomPaymentLinkRequest(RoomPaymentAccess):
    owner_type: Literal["business", "consumer"]
    purpose: Literal["fee", "private_schedule"] = "fee"
    business_account_attested: Literal[True]


class RoomPaymentExchange(RoomPaymentLinkRequest):
    public_token: str = Field(min_length=1)
    plaid_account_id: str = Field(min_length=1, max_length=128)


class RoomPaymentAuthorize(RoomPaymentAccess):
    obligation_id: UUID
    funding_source_id: UUID
    typed_name: str = Field(min_length=2, max_length=180)
    authorized_amount_cents: int = Field(gt=0)
    authorization_terms_sha256: str = Field(min_length=64, max_length=64)
    scheduled_debit_at: datetime
    consent: Literal[True]


class RoomPrivatePlanAuthorize(RoomPaymentAccess):
    funding_source_id: UUID
    typed_name: str = Field(min_length=2, max_length=180)
    consent: Literal[True]


class RoomPaymentRepairComplete(RoomPaymentAccess):
    transfer_id: UUID


class RoomMandateRevoke(RoomPaymentAccess):
    reason: str | None = Field(default=None, max_length=240)


def _enabled() -> None:
    if not get_settings().payments_enabled or not plaid_transfer.enabled():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Online ACH authorization is not enabled")
    ach_fee_workflow.require_legal_approval()


def _private_enabled() -> None:
    _enabled()
    if not get_settings().private_funding_payments_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Private-funding payments are not enabled")


async def _current_private_plan(db: AsyncSession, profile_id: UUID, *, for_update: bool = False):
    stmt = (
        select(PrivateFundingPaymentPlan)
        .where(PrivateFundingPaymentPlan.application_profile_id == profile_id)
        .order_by(PrivateFundingPaymentPlan.version.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def _room(db: AsyncSession, token: str, passcode: str, request: Request):
    link, profile = await _public_application_room(
        db, token, passcode, request, allow_dealer=True
    )
    await pay.assert_payment_room_identity(db, link=link, profile=profile)
    return link, profile


async def _repairable_transfer(
    db: AsyncSession,
    *,
    purpose: Literal["fee", "private_schedule"],
    target_id: UUID,
) -> PaymentTransfer | None:
    if purpose == "fee":
        target_clause = PaymentTransfer.fee_obligation_id == target_id
    else:
        installment_ids = select(PaymentInstallment.id).where(
            PaymentInstallment.plan_id == target_id
        )
        target_clause = PaymentTransfer.installment_id.in_(installment_ids)
    return (
        await db.execute(
            select(PaymentTransfer)
            .where(
                target_clause,
                PaymentTransfer.status == "action_required",
                PaymentTransfer.plaid_authorization_id.is_not(None),
                func.upper(PaymentTransfer.provider_failure_code).in_(
                    pay.PLAID_LINK_REPAIR_CODES
                ),
            )
            .order_by(PaymentTransfer.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _state(db: AsyncSession, profile) -> dict:
    settings = get_settings()
    fee_agreement = await ach_fee_workflow.fee_agreement_state(db, profile)
    if fee_agreement and fee_agreement.get("artifact"):
        artifact = dict(fee_agreement["artifact"])
        artifact["download_route"] = (
            "/application-profiles/public/room/{token}/payments/fee-agreements/"
            f"{fee_agreement['id']}/certificate"
        )
        artifact["download_method"] = "POST"
        fee_agreement = {**fee_agreement, "artifact": artifact}
    obligation = await pay.current_obligation(db, profile.id)
    if obligation and obligation.status == "draft":
        obligation = None
    agreement_ready = bool(
        obligation and await pay._fee_agreement_is_current(db, obligation)
    )
    business_name, client_name, client_email = await pay._profile_identity(db, profile)
    funding_confirmation = await pay._current_confirmation(db, profile.id)
    funding_source = (
        await db.execute(
            select(PaymentFundingSource)
            .where(
                PaymentFundingSource.application_profile_id == profile.id,
                PaymentFundingSource.status == "verified",
            )
            .order_by(PaymentFundingSource.verified_at.desc().nullslast(), PaymentFundingSource.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    mandate = None
    debit_notice = None
    transfer = None
    mandate_proof_file = None
    line_rows: list[FeeObligationLine] = []
    collected_cents = 0
    refunded_cents = 0
    if obligation:
        debit_notice = await ach_fee_workflow.current_debit_notice(db, obligation.id)
        await ach_fee_workflow.sync_notice_delivery(db, debit_notice)
        line_rows = (
            await db.execute(
                select(FeeObligationLine)
                .where(FeeObligationLine.obligation_id == obligation.id)
                .order_by(FeeObligationLine.line_type)
            )
        ).scalars().all()
        mandate = (
            await db.execute(
                select(AchMandate)
                .where(AchMandate.fee_obligation_id == obligation.id)
                .order_by(AchMandate.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        await ach_fee_workflow.sync_mandate_proof_delivery(db, mandate)
        if mandate and mandate.certificate_bucket_file_id:
            mandate_proof_file = await db.get(BucketFile, mandate.certificate_bucket_file_id)
        transfers = (
            await db.execute(
                select(PaymentTransfer)
                .where(PaymentTransfer.fee_obligation_id == obligation.id)
                .order_by(PaymentTransfer.created_at.desc())
            )
        ).scalars().all()
        transfer = transfers[0] if transfers else None
        collected_cents = sum(row.amount_cents for row in transfers if row.status == "funds_available")
        if transfers:
            refunded_cents = int((await db.execute(
                select(func.coalesce(func.sum(PaymentRefund.amount_cents), 0))
                .where(
                    PaymentRefund.transfer_id.in_([row.id for row in transfers]),
                    PaymentRefund.status.in_(pay.REFUND_COMPLETED_STATUSES),
                )
            )).scalar_one())
    mandate_current = await pay.fee_mandate_is_current(
        db,
        mandate=mandate,
        obligation=obligation,
        source=funding_source,
        notice=debit_notice,
    )

    plan = (
        await db.execute(
            select(PrivateFundingPaymentPlan)
            .where(PrivateFundingPaymentPlan.application_profile_id == profile.id)
            .order_by(PrivateFundingPaymentPlan.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    installments = []
    plan_mandate = None
    plan_transfer = None
    if plan:
        installments = (
            await db.execute(
                select(PaymentInstallment)
                .where(PaymentInstallment.plan_id == plan.id)
                .order_by(PaymentInstallment.sequence)
            )
        ).scalars().all()
        plan_mandate = (
            await db.execute(
                select(AchMandate)
                .where(AchMandate.private_plan_id == plan.id)
                .order_by(AchMandate.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if installments:
            plan_transfer = (
                await db.execute(
                    select(PaymentTransfer)
                    .where(
                        PaymentTransfer.installment_id.in_(
                            [row.id for row in installments]
                        )
                    )
                    .order_by(PaymentTransfer.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    plan_mandate_current = bool(
        plan
        and plan_mandate
        and funding_source
        and pay._mandate_is_current(plan_mandate)
        and pay._mandate_matches_source(plan_mandate, funding_source)
        and plan_mandate.obligation_sha256 == plan.schedule_sha256
        and str(plan_mandate.ach_class).upper() == "CCD"
        and str(funding_source.ach_class).upper() == "CCD"
        and pay._business_account_attested(funding_source)
    )

    remaining_ach = obligation.client_ach_cents if obligation else 0
    governing_ids = {
        line.governing_agreement_document_id
        for line in line_rows
        if line.governing_agreement_document_id is not None
    }
    governing_files = {
        file.id: file
        for file in (
            (
                await db.execute(
                    select(BucketFile).where(BucketFile.id.in_(governing_ids))
                )
            ).scalars().all()
            if governing_ids
            else []
        )
    }
    line_payload = []
    for line in sorted(line_rows, key=lambda row: 0 if row.line_type == "origination" else 1):
        exact = getattr(line, "client_ach_cents", None)
        collection = int(exact) if exact is not None else min(remaining_ach, line.amount_cents)
        remaining_ach = max(0, remaining_ach - collection)
        governing_file = governing_files.get(line.governing_agreement_document_id)
        agreement_reference = (
            obligation.agreement_reference
            if obligation and line.line_type == "origination"
            else (
                f"{governing_file.file_name} ({governing_file.id})"
                if governing_file is not None
                else "Consulting and Fee Schedule Addendum"
            )
        )
        line_payload.append({
            "id": str(line.id),
            "label": "Origination fee" if line.line_type == "origination" else "Consulting fee",
            "amount_cents": line.amount_cents,
            "collection_amount_cents": collection,
            "agreement_reference": agreement_reference,
        })
    authorization_terms = None
    if (
        obligation
        and debit_notice
        and funding_source
        and (funding_source.account_mask or "").strip()
        and obligation.agreement_sha256
        and debit_notice.debit_window_start_at
        and debit_notice.debit_window_end_at
    ):
        obligation_sha = await pay.fee_obligation_sha256(db, obligation)
        exact_text = ach_authorization.one_time_fee_authorization_text(
            amount_cents=obligation.client_ach_cents,
            scheduled_debit_at=debit_notice.scheduled_debit_at,
            debit_window_start_at=debit_notice.debit_window_start_at,
            debit_window_end_at=debit_notice.debit_window_end_at,
            account_mask=funding_source.account_mask,
            revocation_cutoff_at=debit_notice.revocation_cutoff_at,
            agreement_sha256=obligation.agreement_sha256,
            obligation_sha256=obligation_sha,
            business_name=business_name,
            # The exact terms remain stable while the authenticated business
            # customer may designate a different officer/controller below.
            # The typed signer is captured as separate signature evidence.
            payer_name=None,
        )
        authorization_terms = {
            "authorization_type": "one_time",
            "type": "one_time_business_ccd",
            "mandate_type": "one_time_business_ccd",
            "originator": "Qualified Commercial LLC",
            "originator_name": "Qualified Commercial LLC",
            "customer_name": client_name,
            "business_name": business_name,
            "amount_cents": obligation.client_ach_cents,
            "currency": obligation.currency,
            "account_mask": funding_source.account_mask,
            "ach_class": "CCD",
            "scheduled_debit_at": debit_notice.scheduled_debit_at,
            "scheduled_debit_date": debit_notice.scheduled_debit_at.astimezone(
                ach_fee_workflow.FIRM_TIMEZONE
            ).date(),
            "debit_window_start_at": debit_notice.debit_window_start_at,
            "debit_window_end_at": debit_notice.debit_window_end_at,
            "submission_window": "business_day_et",
            "notice_business_days": debit_notice.notice_business_days,
            "advance_notice_business_days": debit_notice.notice_business_days,
            "revocation_cutoff_at": debit_notice.revocation_cutoff_at,
            "revocation_email": "support@qualifiedcommercial.com",
            "authorization_text": exact_text,
            "authorization_text_version": ach_authorization.ONE_TIME_CCD_AUTHORIZATION_TEXT_VERSION,
            "authorization_text_sha256": hashlib.sha256(exact_text.encode("utf-8")).hexdigest(),
            "terms_version": ach_authorization.ONE_TIME_CCD_AUTHORIZATION_TEXT_VERSION,
            "terms_sha256": hashlib.sha256(exact_text.encode("utf-8")).hexdigest(),
            "agreement_sha256": obligation.agreement_sha256,
            "obligation_sha256": obligation_sha,
        }
    return {
        "available": bool(obligation or plan or fee_agreement),
        "payments_enabled": bool(
            settings.payments_enabled
            and plaid_transfer.enabled()
            and not ach_fee_workflow.legal_approval_required()
        ),
        "ach_authorization_enabled": bool(
            settings.payments_enabled
            and plaid_transfer.enabled()
            and not ach_fee_workflow.legal_approval_required()
        ),
        "legal_approval_required": ach_fee_workflow.legal_approval_required(),
        "fee_agreement": fee_agreement,
        "private_funding_payments_enabled": bool(
            settings.payments_enabled
            and settings.private_funding_payments_enabled
            and plaid_transfer.enabled()
            and not ach_fee_workflow.legal_approval_required()
        ),
        "business_name": business_name,
        "customer_identity": {
            "legal_name": client_name,
            "customer_name": client_name,
            "client_name": client_name,
            "business_name": business_name,
            "email": client_email,
        },
        "authorization_terms": authorization_terms,
        "obligation": ({
            "id": str(obligation.id),
            "status": obligation.status,
            "currency": obligation.currency,
            "lines": line_payload,
            "client_ach_cents": obligation.client_ach_cents,
            "accepted_amount": obligation.accepted_amount,
            "origination_points": obligation.origination_points,
            "origination_fee_cents": obligation.origination_fee_cents,
            "consulting_fee_cents": obligation.consulting_fee_cents,
            "gross_fee_cents": obligation.gross_fee_cents,
            "agreement_reference": obligation.agreement_reference,
            "agreement_sha256": obligation.agreement_sha256,
            "agreement_document_id": (
                str(obligation.agreement_document_id)
                if obligation.agreement_document_id
                else None
            ),
            "calculation": {
                "basis": "accepted_amount",
                "formula": "accepted amount × origination percentage",
            },
            "collected_cents": max(0, collected_cents - refunded_cents),
            "outstanding_cents": max(0, obligation.client_ach_cents - collected_cents + refunded_cents),
            "agreement_ready": agreement_ready,
        } if obligation else None),
        "funding_confirmation": ({
            "actual_funding_date": funding_confirmation.actual_funding_date,
            "actual_funded_amount": funding_confirmation.actual_funded_amount,
            "funding_party_name": funding_confirmation.funding_party_name,
        } if funding_confirmation else None),
        "funding_source": ({
            "id": str(funding_source.id),
            "status": "connected",
            "owner_type": funding_source.owner_type,
            "institution_name": funding_source.institution_name,
            "account_name": funding_source.account_name,
            "account_mask": funding_source.account_mask,
            "ach_class": funding_source.ach_class,
            "business_account_attested": pay._business_account_attested(funding_source),
        } if funding_source else None),
        "mandate": ({
            "id": str(mandate.id),
            "status": "signed" if mandate_current else mandate.status,
            "current": mandate_current,
            "ach_class": mandate.ach_class.lower(),
            "authorized_amount_cents": mandate.authorized_amount_cents,
            "payer_name": mandate.payer_name,
            "signed_at": mandate.signed_at,
            "authorization_type": mandate.authorization_type,
            "scheduled_debit_at": mandate.scheduled_debit_at,
            "revocation_cutoff_at": mandate.revocation_cutoff_at,
            "proof_copy_delivery_status": mandate.proof_copy_delivery_status,
            "proof_email_status": mandate.proof_copy_delivery_status,
            "proof_copy_sent_at": mandate.proof_copy_sent_at,
            "proof_delivered_at": mandate.proof_copy_delivered_at,
            "proof_copy_last_error": mandate.proof_copy_last_error,
            "certificate_available": bool(mandate.certificate_s3_key),
            "can_resend_proof": bool(mandate.certificate_s3_key),
            "can_revoke": bool(
                mandate_current
                and not (
                    transfer
                    and (
                        transfer.claimed_at
                        or transfer.submitted_at
                        or transfer.plaid_transfer_id
                    )
                )
            ),
            "artifact": ({
                "name": mandate_proof_file.file_name,
                "sha256": mandate_proof_file.content_hash,
                "retention_class": mandate_proof_file.retention_class,
                "protected_until": mandate_proof_file.protected_until,
                "legal_hold": mandate_proof_file.legal_hold,
            } if mandate_proof_file and mandate_proof_file.deleted_at is None else None),
            "certificate_path": (
                f"/application-profiles/public/room/{{token}}/payments/mandates/{mandate.id}/certificate"
                if mandate.certificate_s3_key else None
            ),
            "resend_proof_path": f"/application-profiles/public/room/{{token}}/payments/mandates/{mandate.id}/resend-proof",
            "revoke_path": f"/application-profiles/public/room/{{token}}/payments/mandates/{mandate.id}/revoke",
        } if mandate else None),
        "debit_notice": (
            PaymentDebitNoticeRead.model_validate(debit_notice).model_dump(mode="json")
            if debit_notice is not None
            else None
        ),
        "transfer": ({
            "id": str(transfer.id),
            "status": transfer.status,
            "amount_cents": transfer.amount_cents,
            "submitted_at": transfer.submitted_at,
            "funds_available_at": transfer.funds_available_at,
            "returned_at": transfer.returned_at,
            "repair_needed": bool(
                transfer.status == "action_required"
                and transfer.plaid_authorization_id
                and (transfer.provider_failure_code or "").upper()
                in pay.PLAID_LINK_REPAIR_CODES
            ),
            "resume_eligible": pay._same_intent_resume_eligible(transfer),
        } if transfer else None),
        "private_plan": ({
            "id": str(plan.id),
            "status": plan.status,
            "cadence": plan.cadence,
            "total_amount_cents": plan.total_amount_cents,
            "creditor_name": plan.creditor_name,
            "payee_name": plan.payee_name,
            "settlement_destination_ref": plan.settlement_destination_ref,
            "agreement_reference": plan.agreement_reference,
            "agreement_sha256": plan.agreement_sha256,
            "agreement_executed_at": plan.agreement_executed_at,
            "production_package_id": str(plan.production_package_id),
            "production_package_revision_id": (
                str(plan.production_package_revision_id)
                if plan.production_package_revision_id
                else None
            ),
            "production_term_sheet_id": str(plan.production_term_sheet_id),
            "production_term_sheet_version": plan.production_term_sheet_version,
            "schedule_sha256": plan.schedule_sha256,
            "next_due_date": plan.next_due_date,
            "mandate": ({
                "id": str(plan_mandate.id),
                "status": "signed" if plan_mandate_current else plan_mandate.status,
                "current": plan_mandate_current,
                "authorized_amount_cents": plan_mandate.authorized_amount_cents,
                "payer_name": plan_mandate.payer_name,
                "signed_at": plan_mandate.signed_at,
                "certificate_available": bool(plan_mandate.certificate_s3_key),
            } if plan_mandate else None),
            "transfer": ({
                "id": str(plan_transfer.id),
                "status": plan_transfer.status,
                "amount_cents": plan_transfer.amount_cents,
                "submitted_at": plan_transfer.submitted_at,
                "funds_available_at": plan_transfer.funds_available_at,
                "returned_at": plan_transfer.returned_at,
                "repair_needed": bool(
                    plan_transfer.status == "action_required"
                    and plan_transfer.plaid_authorization_id
                    and (plan_transfer.provider_failure_code or "").upper()
                    in pay.PLAID_LINK_REPAIR_CODES
                ),
                "resume_eligible": pay._same_intent_resume_eligible(plan_transfer),
            } if plan_transfer else None),
            "installments": [{
                "id": str(row.id), "sequence": row.sequence, "due_date": row.due_date,
                "amount_cents": row.amount_cents, "status": row.status,
            } for row in installments],
            "can_revoke_future_authorization": bool(
                plan.status == "active" and plan_mandate and plan_mandate.status == "active"
            ),
        } if plan else None),
        "client_name": client_name,
        "client_email": client_email,
    }


@router.post("/application-profiles/public/room/{token}/payments/state")
async def public_payment_state(
    token: str, payload: RoomPaymentAccess, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    _link, profile = await _room(db, token, payload.passcode, request)
    return await _state(db, profile)


@router.post("/application-profiles/public/room/{token}/payments/link-token")
async def public_payment_link_token(
    token: str, payload: RoomPaymentLinkRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    if payload.purpose == "private_schedule":
        _private_enabled()
    else:
        _enabled()
    _link, profile = await _room(db, token, payload.passcode, request)
    target_id: UUID
    plan = None
    obligation = None
    if payload.purpose == "private_schedule":
        if payload.owner_type != "business":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Private schedules require a business account")
        plan = await _current_private_plan(db, profile.id)
        if not plan:
            raise HTTPException(status.HTTP_409_CONFLICT, "There is no private schedule awaiting authorization")
        target_id = plan.id
    else:
        if payload.owner_type != "business":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "One-time fee debits require a business account",
            )
        obligation = await pay.current_obligation(db, profile.id)
        if not obligation or obligation.status not in {"awaiting_authorization", "authorized", "returned"}:
            raise HTTPException(status.HTTP_409_CONFLICT, "There is no active fee authorization request")
        if obligation.client_ach_cents <= 0:
            raise HTTPException(status.HTTP_409_CONFLICT, "No client ACH amount is allocated")
        target_id = obligation.id
    repair_transfer = await _repairable_transfer(
        db,
        purpose=payload.purpose,
        target_id=target_id,
    )
    if payload.purpose == "private_schedule":
        if not repair_transfer and plan.status not in {"draft", "paused"}:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "There is no private schedule awaiting authorization",
            )
    link_token = await plaid_transfer.create_link_token(
        client_user_id=f"payment:{payload.purpose}:{profile.id}:{target_id}",
        authorization_id=(
            repair_transfer.plaid_authorization_id if repair_transfer else None
        ),
    )
    return {
        "link_token": link_token,
        "owner_type": payload.owner_type,
        "purpose": payload.purpose,
        "repair_mode": bool(repair_transfer),
        "exchange_required": not bool(repair_transfer),
        "transfer_id": str(repair_transfer.id) if repair_transfer else None,
    }


@router.post(
    "/application-profiles/public/room/{token}/payments/repair-complete"
)
async def public_payment_repair_complete(
    token: str,
    payload: RoomPaymentRepairComplete,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _enabled()
    _link, profile = await _room(db, token, payload.passcode, request)
    await pay.resume_action_required_transfer(
        db,
        transfer_id=payload.transfer_id,
        profile_id=profile.id,
        actor_id=None,
        bank_repair_completed=True,
    )
    await db.commit()
    return await _state(db, profile)


async def _exchange_or_recover_payment_source(
    db: AsyncSession,
    *,
    profile,
    link,
    payload: RoomPaymentExchange,
) -> tuple[PaymentFundingSource, str | None]:
    """Durably retain a Plaid exchange before any fallible account validation.

    The public token is one-time. Its hash locates a pending or already
    verified encrypted exchange on retry without retaining the token itself.
    If the first durable write fails, remove the provider Item so a live
    credential is not orphaned.
    """

    public_token_sha256 = hashlib.sha256(
        payload.public_token.encode("utf-8")
    ).hexdigest()
    existing = (
        await db.execute(
            select(PaymentFundingSource)
            .where(
                PaymentFundingSource.application_profile_id == profile.id,
                PaymentFundingSource.metadata_json[
                    "exchange_public_token_sha256"
                ].astext
                == public_token_sha256,
            )
            .order_by(PaymentFundingSource.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        metadata = existing.metadata_json or {}
        if existing.status == "verified" and existing.revoked_at is None:
            attestation = metadata.get("business_account_attestation")
            verified_match = bool(
                existing.plaid_account_id == payload.plaid_account_id
                and existing.owner_type == payload.owner_type == "business"
                and str(existing.ach_class).upper() == "CCD"
                and metadata.get("payment_purpose") == payload.purpose
                and metadata.get("room_link_id") == str(link.id)
                and metadata.get("account_type") == "depository"
                and isinstance(attestation, dict)
                and payload.business_account_attested is True
                and attestation.get("attested") is True
                and attestation.get("room_link_id") == str(link.id)
                and existing.plaid_item_id
                and existing.access_token_ciphertext
                and existing.account_mask
                and existing.account_subtype in {"checking", "savings"}
                and existing.verified_at
            )
            if verified_match:
                return existing, None
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The completed payment connection does not match this account or authorization",
            )
        if existing.status != "pending":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This payment connection can no longer be resumed; restart bank connection",
            )
        if (
            existing.owner_type != payload.owner_type
            or metadata.get("room_link_id") != str(link.id)
            or metadata.get("payment_purpose") != payload.purpose
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The saved payment connection does not match this secure room",
            )
        access_token = plaid_transfer.decrypt_access_token(
            existing.access_token_ciphertext
        )
        if access_token and existing.plaid_item_id:
            return existing, access_token
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The saved payment connection cannot be recovered; restart bank connection",
        )

    access_token, item_id = await plaid_transfer.exchange_public_token(
        payload.public_token
    )
    source = PaymentFundingSource(
        application_profile_id=profile.id,
        client_id=profile.client_id,
        status="pending",
        owner_type=payload.owner_type,
        ach_class="CCD" if payload.owner_type == "business" else "WEB",
        plaid_item_id=item_id,
        plaid_account_id=payload.plaid_account_id,
        access_token_ciphertext=plaid_transfer.encrypt_access_token(access_token),
        metadata_json={
            "payment_only": True,
            "payment_purpose": payload.purpose,
            "exchange_public_token_sha256": public_token_sha256,
            "room_link_id": str(link.id),
        },
    )
    db.add(source)
    try:
        await db.flush()
        await db.commit()
    except Exception:
        try:
            await plaid_transfer.remove_item(access_token)
        except Exception:  # noqa: BLE001 - preserve the persistence failure
            pass
        raise
    return source, access_token


@router.post("/application-profiles/public/room/{token}/payments/exchange")
async def public_payment_exchange(
    token: str, payload: RoomPaymentExchange, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    if payload.purpose == "private_schedule":
        _private_enabled()
    else:
        _enabled()
    _link, profile = await _room(db, token, payload.passcode, request)
    if payload.purpose == "private_schedule":
        if payload.owner_type != "business":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Private schedules require a business account")
        plan = await _current_private_plan(db, profile.id)
        if not plan or plan.status not in {"draft", "paused"}:
            raise HTTPException(status.HTTP_409_CONFLICT, "There is no private schedule awaiting authorization")
    else:
        if payload.owner_type != "business":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "One-time fee debits require a business account",
            )
        obligation = await pay.current_obligation(db, profile.id)
        if not obligation or obligation.status not in {"awaiting_authorization", "authorized", "returned"}:
            raise HTTPException(status.HTTP_409_CONFLICT, "There is no active fee authorization request")
    source, access_token = await _exchange_or_recover_payment_source(
        db,
        profile=profile,
        link=_link,
        payload=payload,
    )
    if access_token is None:
        return await _state(db, profile)
    accounts = await plaid_transfer.accounts(access_token)
    account = next((row for row in accounts if row["account_id"] == payload.plaid_account_id), None)
    if not account:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The selected payment account was not returned by Plaid")
    if account.get("type") != "depository" or account.get("subtype") not in {"checking", "savings"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose an eligible checking or savings account")
    account_mask = str(account.get("mask") or "").strip()[-8:]
    if not account_mask:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The selected account did not provide a usable masked account number",
        )
    # Serialize source replacement per file. The provider exchange happens
    # before this lock so no database lock is held while waiting on Plaid.
    await pay._lock_profile(db, profile.id)
    previous = (
        await db.execute(select(PaymentFundingSource).where(
            PaymentFundingSource.application_profile_id == profile.id,
            PaymentFundingSource.status == "verified",
        ).with_for_update())
    ).scalars().all()
    revoked_source_ids = [row.id for row in previous]
    source_mandates = (
        await db.execute(
            select(AchMandate)
            .where(
                AchMandate.funding_source_id.in_(revoked_source_ids),
                AchMandate.status == "active",
                AchMandate.revoked_at.is_(None),
            )
            .with_for_update()
        )
    ).scalars().all() if revoked_source_ids else []
    now = datetime.now(UTC)
    source_mandate_ids = [mandate.id for mandate in source_mandates]
    if source_mandate_ids:
        mandate_transfers = (
            await db.execute(
                select(PaymentTransfer)
                .where(PaymentTransfer.mandate_id.in_(source_mandate_ids))
                .with_for_update()
            )
        ).scalars().all()
        if any(
            row.claimed_at
            or row.plaid_transfer_id
            or row.submitted_at
            or row.status in {
                "submitting", "submitted", "pending", "posted", "settled", "funds_available"
            }
            for row in mandate_transfers
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A debit is already being submitted; the payment account cannot be replaced",
            )
    for mandate in source_mandates:
        mandate.status = "superseded"
        mandate.revoked_at = now
        mandate.terminated_at = now
        mandate.termination_reason = "Payment account replaced"
        await ach_fee_workflow.extend_mandate_retention(db, mandate, anchor=now)
    for row in previous:
        row.status = "revoked"
        row.revoked_at = now
    await pay.cancel_unclaimed_transfer_intents(
        db,
        mandate_ids=source_mandate_ids,
        reason="payment_source_reconnected",
    )
    await pay.cancel_unclaimed_transfer_intents(
        db,
        funding_source_ids=revoked_source_ids,
        reason="payment_source_replaced",
    )
    source.status = "verified"
    source.owner_type = payload.owner_type
    source.ach_class = "CCD" if payload.owner_type == "business" else "WEB"
    source.plaid_account_id = payload.plaid_account_id
    source.account_name = str(
        account.get("official_name") or account.get("name") or "Bank account"
    )
    source.account_mask = account_mask
    source.account_subtype = str(account.get("subtype") or "")
    source.verified_at = now
    source.revoked_at = None
    source.metadata_json = {
        **(source.metadata_json or {}),
        "payment_only": True,
        "account_type": account.get("type"),
        "business_account_attestation": {
            "attested": payload.business_account_attested,
            "attested_at": now.isoformat(),
            "room_link_id": str(_link.id),
            "recipient_email": _link.recipient_email,
            "ip_address": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
        },
    }
    await db.flush()
    # A reconnect creates a new immutable funding-source version. Historical
    # mandates and transfers retain the exact account/classification evidence
    # that was used when they were authorized.
    await pay.log_event(
        db, profile_id=profile.id, actor_id=None, event_type="payment_source.connected",
        entity_type="payment_funding_source", entity_id=source.id,
        summary="Client connected a dedicated ACH payment account",
        metadata={"owner_type": payload.owner_type, "ach_class": source.ach_class},
    )
    await db.commit()
    return await _state(db, profile)


@router.post("/application-profiles/public/room/{token}/payments/authorize")
async def public_payment_authorize(
    token: str, payload: RoomPaymentAuthorize, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    _enabled()
    _link, profile = await _room(db, token, payload.passcode, request)
    # Serialize authorization with payment-account replacement.  Both flows
    # take the profile lock before any source/mandate rows.
    await pay._lock_profile(db, profile.id)
    obligation = await pay.current_obligation(db, profile.id, for_update=True)
    if not obligation or obligation.id != payload.obligation_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "The fee obligation changed; refresh before authorizing")
    if obligation.status not in {"awaiting_authorization", "authorized", "returned"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "This fee request can no longer be authorized")
    snapshot_blockers = await pay.fee_obligation_snapshot_blockers(db, obligation)
    if snapshot_blockers:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This fee request changed; ask QC to prepare a new request: "
            + "; ".join(snapshot_blockers),
        )
    if payload.authorized_amount_cents != obligation.client_ach_cents:
        raise HTTPException(status.HTTP_409_CONFLICT, "The authorization amount changed; refresh before signing")
    source = (
        await db.execute(
            select(PaymentFundingSource)
            .where(PaymentFundingSource.id == payload.funding_source_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        not source
        or source.application_profile_id != profile.id
        or source.status != "verified"
        or source.revoked_at
        or source.owner_type != "business"
        or str(source.ach_class).upper() != "CCD"
        or not pay._business_account_attested(source)
        or not (source.account_mask or "").strip()
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Connect a verified business account eligible for CCD debits",
        )
    notice = await ach_fee_workflow.current_debit_notice(db, obligation.id)
    if (
        notice is None
        or notice.revoked_at is not None
        or notice.amount_cents != obligation.client_ach_cents
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "QC must schedule the exact debit date and notice before authorization",
        )
    lines = (
        await db.execute(
            select(FeeObligationLine)
            .where(FeeObligationLine.obligation_id == obligation.id)
            .order_by(FeeObligationLine.line_type)
        )
    ).scalars().all()
    obligation_sha = await pay.fee_obligation_sha256(db, obligation)
    if not obligation.agreement_document_id or not obligation.agreement_sha256:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The exact signed fee agreement is unavailable",
        )
    if not await pay.fee_lines_have_current_governing_agreements(
        db, obligation, lines=list(lines)
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Each fee line must be bound to its exact signed governing agreement",
        )
    business_name, client_name, _client_email = await pay._profile_identity(db, profile)
    # The authenticated room identifies the customer record.  Capture the
    # actual authorized business-account signer separately: an officer or
    # controller may legitimately differ from the CRM contact name.
    if len(" ".join(payload.typed_name.split())) < 2:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Enter the authorized business-account signer's full legal name",
        )
    signer_email = profiles.normalized_email(_link.recipient_email)
    if not signer_email:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The authenticated payment-room recipient needs a valid email address",
        )
    # Bind proof and notice delivery to the authenticated room recipient who
    # is actually signing, not to a potentially different CRM contact.
    notice.recipient_name = payload.typed_name.strip()
    notice.recipient_email = signer_email
    authorization_text = ach_authorization.one_time_fee_authorization_text(
        amount_cents=obligation.client_ach_cents,
        scheduled_debit_at=notice.scheduled_debit_at,
        debit_window_start_at=notice.debit_window_start_at,
        debit_window_end_at=notice.debit_window_end_at,
        account_mask=source.account_mask,
        revocation_cutoff_at=notice.revocation_cutoff_at,
        agreement_sha256=obligation.agreement_sha256,
        obligation_sha256=obligation_sha,
        business_name=business_name,
        payer_name=None,
    )
    authorization_sha = ach_fee_workflow.require_authorization_terms_binding(
        submitted_sha256=payload.authorization_terms_sha256,
        submitted_scheduled_debit_at=payload.scheduled_debit_at,
        exact_text=authorization_text,
        notice=notice,
    )
    active = (
        await db.execute(
            select(AchMandate)
            .where(
                AchMandate.fee_obligation_id == obligation.id,
                AchMandate.status == "active",
                AchMandate.revoked_at.is_(None),
            )
            .order_by(AchMandate.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        active
        and active.funding_source_id == source.id
        and active.authorized_amount_cents == obligation.client_ach_cents
        and active.obligation_sha256 == obligation_sha
        and active.typed_name.casefold() == payload.typed_name.strip().casefold()
        and active.authorization_type == "one_time_business_ccd"
        and active.scheduled_debit_at == notice.scheduled_debit_at
        and active.authorization_text_sha256 == authorization_sha
    ):
        notice_message = await ach_fee_workflow.ensure_debit_notice_queued(
            db,
            notice=notice,
            mandate=active,
            profile=profile,
        )
        proof_message = await ach_fee_workflow.ensure_mandate_proof_queued(
            db,
            mandate=active,
            profile=profile,
        )
        await db.commit()
        await ach_fee_workflow.deliver_debit_notice(
            db,
            notice=notice,
            mandate=active,
            profile=profile,
            recorded_row=notice_message,
        )
        await ach_fee_workflow.deliver_mandate_proof(
            db,
            mandate=active,
            profile=profile,
            recorded_row=proof_message,
        )
        await db.commit()
        return await _state(db, profile)
    if notice.status not in {"draft", "delivery_failed"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This exact debit notice is already bound; refresh before authorizing",
        )
    mandate = await pay.create_ach_mandate(db, profile=profile, payload=AchMandateCreate(
        funding_source_id=source.id,
        fee_obligation_id=obligation.id,
        authorized_amount_cents=obligation.client_ach_cents,
        authorization_text_version=ach_authorization.ONE_TIME_CCD_AUTHORIZATION_TEXT_VERSION,
        authorization_type="one_time_business_ccd",
        authorization_text_snapshot=authorization_text,
        authorization_text_sha256=authorization_sha,
        obligation_sha256=obligation_sha,
        agreement_document_id=obligation.agreement_document_id,
        agreement_sha256=obligation.agreement_sha256,
        typed_name=payload.typed_name.strip(),
        payer_name=payload.typed_name.strip(),
        payer_email=signer_email,
        scheduled_debit_at=notice.scheduled_debit_at,
        debit_window_start_at=notice.debit_window_start_at,
        debit_window_end_at=notice.debit_window_end_at,
        notice_business_days=notice.notice_business_days,
        revocation_method=(
            "Secure application room or support@qualifiedcommercial.com before the stated cutoff"
        ),
        revocation_cutoff_at=notice.revocation_cutoff_at,
        signer_session_id=str(_link.id),
    ), ip_address=(request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else None)),
       user_agent=(request.headers.get("user-agent") or "")[:512] or None)
    mandate.signature_sha256 = ach_authorization.signature_hash(
        typed_name=mandate.typed_name, obligation_sha256=obligation_sha,
        signed_at=mandate.signed_at, ip_address=mandate.ip_address,
    )
    fee_lines: list[tuple[str, int, str | None]] = []
    remaining = obligation.client_ach_cents
    for row in sorted(lines, key=lambda item: 0 if item.line_type == "origination" else 1):
        exact = getattr(row, "client_ach_cents", None)
        amount = int(exact) if exact is not None else min(remaining, row.amount_cents)
        remaining = max(0, remaining - amount)
        if amount:
            agreement_reference = (
                obligation.agreement_reference
                if row.line_type == "origination"
                else (
                    "Consulting and Fee Schedule Addendum "
                    f"{row.governing_agreement_document_id}"
                )
            )
            fee_lines.append((
                "Origination fee" if row.line_type == "origination" else "Consulting fee",
                amount,
                agreement_reference,
            ))
    pdf = await asyncio.to_thread(
        ach_authorization.render_certificate_pdf,
        mandate=mandate, funding_source=source, business_name=business_name,
        client_name=client_name, client_email=signer_email, fee_lines=fee_lines,
        accepted_amount=obligation.accepted_amount,
        origination_points=obligation.origination_points,
        origination_fee_cents=obligation.origination_fee_cents,
        consulting_fee_cents=obligation.consulting_fee_cents,
        agreement_reference=obligation.agreement_reference,
        agreement_sha256=obligation.agreement_sha256,
    )
    await ach_fee_workflow.store_mandate_proof(
        db, mandate=mandate, profile=profile, pdf=pdf
    )
    await ach_fee_workflow.bind_and_queue_debit_notice(
        db,
        notice=notice,
        mandate=mandate,
        profile=profile,
        funding_source=source,
        authorization_text=authorization_text,
    )
    proof_message = await ach_fee_workflow.ensure_mandate_proof_queued(
        db, mandate=mandate, profile=profile, pdf=pdf
    )
    notice_message = (
        await db.get(MessageSend, notice.message_send_id)
        if notice.message_send_id
        else None
    )
    # The mandate, protected artifacts, and both queued outbox claims become
    # durable before any email provider handoff.
    await db.commit()
    await ach_fee_workflow.deliver_debit_notice(
        db,
        notice=notice,
        mandate=mandate,
        profile=profile,
        recorded_row=notice_message,
    )
    await ach_fee_workflow.deliver_mandate_proof(
        db,
        mandate=mandate,
        profile=profile,
        pdf=pdf,
        recorded_row=proof_message,
    )
    await db.commit()
    return await _state(db, profile)


@router.post("/application-profiles/public/room/{token}/payments/private-plans/{plan_id}/authorize")
async def public_private_plan_authorize(
    token: str,
    plan_id: UUID,
    payload: RoomPrivatePlanAuthorize,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _private_enabled()
    _link, profile = await _room(db, token, payload.passcode, request)
    await pay._lock_profile(db, profile.id)
    plan = await _current_private_plan(db, profile.id, for_update=True)
    if not plan or plan.id != plan_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "The private schedule changed; refresh before authorizing")
    if plan.status not in {"draft", "paused"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "This private schedule can no longer be authorized")
    if not await pay._private_agreement_is_current(db, plan=plan):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The executed stage-two agreement changed; ask QC to replace this schedule",
        )
    source = (
        await db.execute(
            select(PaymentFundingSource)
            .where(PaymentFundingSource.id == payload.funding_source_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        not source
        or source.application_profile_id != profile.id
        or source.status != "verified"
        or source.revoked_at
        or source.owner_type != "business"
        or source.ach_class != "CCD"
        or not pay._business_account_attested(source)
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "Connect an eligible business checking account")
    installments = (
        await db.execute(
            select(PaymentInstallment)
            .where(PaymentInstallment.plan_id == plan.id)
            .order_by(PaymentInstallment.sequence)
        )
    ).scalars().all()
    if not installments or sum(row.amount_cents for row in installments) != plan.total_amount_cents:
        raise HTTPException(status.HTTP_409_CONFLICT, "The fixed schedule is incomplete; ask QC to review it")
    active = (
        await db.execute(
            select(AchMandate)
            .where(
                AchMandate.private_plan_id == plan.id,
                AchMandate.status == "active",
                AchMandate.revoked_at.is_(None),
            )
            .order_by(AchMandate.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        active
        and active.funding_source_id == source.id
        and active.authorized_amount_cents == plan.total_amount_cents
        and active.obligation_sha256 == plan.schedule_sha256
        and active.typed_name.casefold() == payload.typed_name.strip().casefold()
    ):
        proof_message = await ach_fee_workflow.ensure_mandate_proof_queued(
            db,
            mandate=active,
            profile=profile,
        )
        await db.commit()
        await ach_fee_workflow.deliver_mandate_proof(
            db,
            mandate=active,
            profile=profile,
            recorded_row=proof_message,
        )
        await db.commit()
        return await _state(db, profile)
    business_name, client_name, client_email = await pay._profile_identity(db, profile)
    mandate = await pay.create_ach_mandate(
        db,
        profile=profile,
        payload=AchMandateCreate(
            funding_source_id=source.id,
            private_plan_id=plan.id,
            authorized_amount_cents=plan.total_amount_cents,
            authorization_text_version=ach_authorization.PRIVATE_SCHEDULE_AUTHORIZATION_TEXT_VERSION,
            obligation_sha256=plan.schedule_sha256,
            typed_name=payload.typed_name.strip(),
            payer_name=payload.typed_name.strip(),
            payer_email=client_email,
        ),
        ip_address=(
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else None)
        ),
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
    )
    mandate.signature_sha256 = ach_authorization.signature_hash(
        typed_name=mandate.typed_name,
        obligation_sha256=plan.schedule_sha256,
        signed_at=mandate.signed_at,
        ip_address=mandate.ip_address,
    )
    pdf = await asyncio.to_thread(
        ach_authorization.render_private_schedule_certificate_pdf,
        mandate=mandate,
        funding_source=source,
        business_name=business_name,
        client_name=client_name,
        client_email=client_email,
        creditor_name=plan.creditor_name,
        payee_name=plan.payee_name,
        settlement_destination_ref=plan.settlement_destination_ref,
        agreement_reference=plan.agreement_reference,
        production_term_sheet_id=plan.production_term_sheet_id,
        production_term_sheet_version=plan.production_term_sheet_version,
        schedule_sha256=plan.schedule_sha256,
        cadence=plan.cadence,
        installments=[
            (row.sequence, row.due_date.isoformat(), row.amount_cents) for row in installments
        ],
    )
    await ach_fee_workflow.store_mandate_proof(
        db, mandate=mandate, profile=profile, pdf=pdf
    )
    proof_message = await ach_fee_workflow.ensure_mandate_proof_queued(
        db,
        mandate=mandate,
        profile=profile,
        pdf=pdf,
    )
    # Make the protected proof and audited delivery attempt durable before the
    # provider handoff.
    await db.commit()
    await ach_fee_workflow.deliver_mandate_proof(
        db,
        mandate=mandate,
        profile=profile,
        pdf=pdf,
        recorded_row=proof_message,
    )
    await db.commit()
    return await _state(db, profile)


@router.post("/application-profiles/public/room/{token}/payments/mandates/{mandate_id}/certificate")
async def public_payment_certificate(
    token: str, mandate_id: UUID, payload: RoomPaymentAccess, request: Request,
    db: AsyncSession = Depends(get_db),
):
    _link, profile = await _room(db, token, payload.passcode, request)
    mandate = await db.get(AchMandate, mandate_id)
    if not mandate or mandate.application_profile_id != profile.id or not mandate.certificate_s3_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Authorization certificate not found")
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
            "Authorization certificate has no protected file record",
        )
    url = await ach_fee_workflow.verified_protected_download_url(
        proof_file,
        download_filename="QC-ACH-authorization.pdf",
    )
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/application-profiles/public/room/{token}/payments/fee-agreements/{agreement_id}/certificate"
)
async def public_fee_agreement_certificate(
    token: str,
    agreement_id: UUID,
    payload: RoomPaymentAccess,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    link, profile = await _room(db, token, payload.passcode, request)
    requested = await db.get(BucketRequestedDocument, agreement_id)
    if (
        requested is None
        or requested.bucket_id != link.bucket_id
        or requested.signature_kind != "success_fee_agreement"
        or profile.primary_bucket_id != requested.bucket_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Signed fee agreement not found")
    signature = (
        await db.execute(
            select(BucketDocumentSignature)
            .where(BucketDocumentSignature.requested_document_id == requested.id)
            .order_by(BucketDocumentSignature.signed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    file = (
        await db.get(BucketFile, signature.result_file_id)
        if signature and signature.result_file_id
        else None
    )
    if file is None or file.deleted_at is not None or not file.s3_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Signed fee agreement not found")
    file.protected_until = ach_fee_workflow.extend_retention(
        file.protected_until, anchor=datetime.now(UTC)
    )
    await db.commit()
    url = await ach_fee_workflow.verified_protected_download_url(
        file,
        download_filename="QC-Deal-Specific-Success-Fee-Agreement.pdf",
    )
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/application-profiles/public/room/{token}/payments/mandates/{mandate_id}/resend-proof"
)
async def public_resend_mandate_proof(
    token: str,
    mandate_id: UUID,
    payload: RoomPaymentAccess,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _link, profile = await _room(db, token, payload.passcode, request)
    mandate = await db.get(AchMandate, mandate_id)
    if (
        not mandate
        or mandate.application_profile_id != profile.id
        or mandate.fee_obligation_id is None
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ACH authorization not found")
    proof_message = await ach_fee_workflow.ensure_mandate_proof_queued(
        db,
        mandate=mandate,
        profile=profile,
        force_new=True,
    )
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
    return await _state(db, profile)


@router.post(
    "/application-profiles/public/room/{token}/payments/mandates/{mandate_id}/revoke"
)
async def public_revoke_fee_mandate(
    token: str,
    mandate_id: UUID,
    payload: RoomMandateRevoke,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _link, profile = await _room(db, token, payload.passcode, request)
    mandate = (
        await db.execute(
            select(AchMandate)
            .where(
                AchMandate.id == mandate_id,
                AchMandate.application_profile_id == profile.id,
                AchMandate.fee_obligation_id.is_not(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if mandate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ACH authorization not found")
    await ach_fee_workflow.revoke_mandate(
        db,
        mandate=mandate,
        actor_user_id=None,
        reason=payload.reason,
    )
    # Persist the revocation and durable notification claim before email I/O.
    await db.commit()
    await ach_fee_workflow.deliver_revocation_confirmation(db, mandate)
    await db.commit()
    return await _state(db, profile)


@router.post("/application-profiles/public/room/{token}/payments/private-plans/{plan_id}/revoke")
async def public_revoke_private_plan(
    token: str, plan_id: UUID, payload: RoomPaymentAccess, request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _link, profile = await _room(db, token, payload.passcode, request)
    plan = (
        await db.execute(select(PrivateFundingPaymentPlan).where(
            PrivateFundingPaymentPlan.id == plan_id,
            PrivateFundingPaymentPlan.application_profile_id == profile.id,
        ).with_for_update())
    ).scalar_one_or_none()
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Private payment schedule not found")
    if plan.status in {"cancelled", "completed", "superseded"}:
        return await _state(db, profile)
    now = datetime.now(UTC)
    mandates = (
        await db.execute(select(AchMandate).where(
            AchMandate.private_plan_id == plan.id, AchMandate.status == "active"
        ).with_for_update())
    ).scalars().all()
    mandate_ids = [mandate.id for mandate in mandates]
    for mandate in mandates:
        mandate.status = "revoked"
        mandate.revoked_at = now
    await pay.cancel_unclaimed_transfer_intents(
        db,
        mandate_ids=mandate_ids,
        reason="schedule_authorization_revoked",
    )
    plan.status = "paused"
    plan.record_version += 1
    await pay.log_event(
        db, profile_id=profile.id, actor_id=None, event_type="private_plan.authorization_revoked",
        entity_type="private_payment_plan", entity_id=plan.id,
        summary="Client revoked future scheduled-payment authorization",
    )
    await db.commit()
    return await _state(db, profile)
