"""Domain rules for ACH origination fees and private-funding schedules.

This module never calls Plaid.  It commits durable intents and consumes
provider events.  ``plaid_transfer.py`` owns network I/O and may safely run
outside the row-locking transactions used here.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import Role
from app.models.application_profile import ApplicationProfile, ApplicationRoomDelivery
from app.models.bucket import BucketDocumentSignature, BucketFile, BucketRequestedDocument
from app.models.client import Client
from app.models.document import Document
from app.models.payments import (
    AchMandate,
    ActualFundingConfirmation,
    BankDirectFeeReceipt,
    FeeAllocationVersion,
    FeeObligation,
    FeeObligationLine,
    PaymentAuditEvent,
    PaymentDebitNotice,
    PaymentFundingSource,
    PaymentInstallment,
    PaymentRefund,
    PaymentServicingAuthority,
    PaymentTransfer,
    PaymentTransferEvent,
    PlaidTransferCursor,
    PrivateFundingPaymentPlan,
)
from app.models.production_package import ProductionPackage, ProductionTermSheet
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.schemas.payments import (
    AchMandateCreate,
    AchMandateResponse,
    ActualFundingConfirmationResponse,
    AgreementDocumentCandidate,
    BankDirectReceiptCreate,
    DealEconomicsSnapshot,
    FeeAllocationInput,
    FeeAllocationPatch,
    FeeAllocationResponse,
    FeeObligationCreate,
    FeeObligationLineResponse,
    FeeObligationResponse,
    FundingConfirmationCreate,
    PaymentDebitNoticeRead,
    PaymentFundingSourceCreate,
    PaymentFundingSourceResponse,
    PaymentPermissions,
    PaymentReadiness,
    PaymentSummary,
    PaymentSummaryTotals,
    PaymentTimelineItem,
    PaymentTransferResponse,
    PrivateFundingPlanResponse,
    PrivatePlanCreate,
    PrivatePlanFromTermSheetCreate,
    PrivateSchedulePreview,
    ServicingAuthorityCreate,
    ServicingAuthorityResponse,
)
from app.services import notifications
from app.services import production_term_structure as term_structure
from app.services.deal_economics import calculate_deal_earnings

MANAGE_ROLES = {Role.SUPER_ADMIN, Role.LOAN_EXEC}
READ_ROLES = MANAGE_ROLES | {Role.BROKER, Role.FIELD_REP}
PRIVATE_FUNDER_TYPES = {
    "private_fund",
    "private_capital",
    "private_credit",
    "family_office",
    "balance_sheet",
    "balance_sheet_lender",
}
PROCESSING_TRANSFER_STATUSES = {
    "authorizing",
    "submitting",
    "submitted",
    "pending",
    "posted",
    "settled",
}
COLLECTED_TRANSFER_STATUS = "funds_available"
RETRYABLE_RETURN_CODES = {"R01", "R09"}
PLAID_LINK_REPAIR_CODES = {
    "ITEM_LOGIN_REQUIRED",
    "INVALID_UPDATED_USERNAME",
    "USER_ACTION_REQUIRED",
    "MFA_REQUIRED",
}
FINAL_TRANSFER_STATUSES = {"funds_available", "cancelled", "returned", "failed"}
REFUND_RESERVED_STATUSES = {
    "pending",
    "submitting",
    "submitted",
    "posted",
    "settled",
    "completed",
    "action_required",
}
REFUND_COMPLETED_STATUSES = {"settled", "completed"}
FIRM_TIMEZONE = ZoneInfo("America/New_York")


def require_reader(user: User) -> None:
    if user.role not in READ_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Payments access required")


def require_manager(user: User) -> None:
    if user.role not in MANAGE_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Loan Executive access required")


def require_super_admin(user: User) -> None:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super Admin access required")


def _now() -> datetime:
    return datetime.now(UTC)


def _firm_today() -> date:
    return datetime.now(FIRM_TIMEZONE).date()


def _mandate_is_current(mandate: AchMandate | None, *, at: datetime | None = None) -> bool:
    """Return whether a signed mandate may still authorize a new provider handoff."""

    if mandate is None or mandate.status != "active" or mandate.revoked_at is not None:
        return False
    now = at or _now()
    return mandate.expires_at is None or mandate.expires_at > now


def _servicing_authority_is_effective(
    authority: PaymentServicingAuthority | None,
    *,
    profile_id: UUID,
    on_date: date,
) -> bool:
    return bool(
        authority
        and authority.application_profile_id == profile_id
        and authority.status == "active"
        and authority.effective_from <= on_date
        and (authority.effective_to is None or authority.effective_to >= on_date)
    )


def _cents(value: Decimal | float | int | None) -> int:
    if value is None:
        return 0
    return int((Decimal(str(value)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _funding_source_snapshot(source: PaymentFundingSource) -> dict[str, Any]:
    source_metadata = source.metadata_json if isinstance(source.metadata_json, dict) else {}
    business_attestation = source_metadata.get("business_account_attestation")
    if not isinstance(business_attestation, dict):
        business_attestation = None
    return {
        "funding_source_id": str(source.id),
        "owner_type": source.owner_type,
        "ach_class": source.ach_class,
        "account_name": source.account_name,
        "account_mask": source.account_mask,
        "account_subtype": source.account_subtype,
        "institution_name": source.institution_name,
        "holder_name": source.holder_name,
        "verified_at": source.verified_at.isoformat() if source.verified_at else None,
        "business_account_attestation": business_attestation,
    }


def _business_account_attested(source: PaymentFundingSource | None) -> bool:
    """Require affirmative, auditable customer ownership/authority evidence for CCD."""

    if source is None or not isinstance(source.metadata_json, dict):
        return False
    attestation = source.metadata_json.get("business_account_attestation")
    return bool(
        isinstance(attestation, dict)
        and attestation.get("attested") is True
        and str(attestation.get("attested_at") or "").strip()
        and str(attestation.get("room_link_id") or "").strip()
    )


def _mandate_matches_source(
    mandate: AchMandate | None, source: PaymentFundingSource | None
) -> bool:
    if not mandate or not source:
        return False
    snapshot = _funding_source_snapshot(source)
    return bool(
        mandate.funding_source_id == source.id
        and str(mandate.ach_class).upper() == str(source.ach_class).upper()
        and mandate.funding_source_snapshot == snapshot
        and mandate.funding_source_sha256 == _canonical_hash(snapshot)
    )


async def fee_mandate_is_current(
    db: AsyncSession,
    *,
    mandate: AchMandate | None,
    obligation: FeeObligation | None,
    source: PaymentFundingSource | None,
    notice: PaymentDebitNotice | None,
) -> bool:
    """Canonical usability check shared by client display and staff release gates."""

    if not mandate or not obligation or not source or not notice:
        return False
    now = _now()
    if (
        mandate.status != "active"
        or mandate.revoked_at is not None
        or (mandate.expires_at is not None and mandate.expires_at <= now)
        or mandate.authorization_type != "one_time_business_ccd"
        or mandate.authorized_amount_cents != obligation.client_ach_cents
        or mandate.obligation_sha256 != await fee_obligation_sha256(db, obligation)
        or mandate.agreement_document_id != obligation.agreement_document_id
        or mandate.agreement_sha256 != obligation.agreement_sha256
        or not mandate.authorization_text_sha256
        or notice.revoked_at is not None
        or notice.mandate_id != mandate.id
        or notice.amount_cents != obligation.client_ach_cents
        or notice.authorization_text_sha256 != mandate.authorization_text_sha256
        or notice.scheduled_debit_at != mandate.scheduled_debit_at
        or not _business_account_attested(source)
        or not _mandate_matches_source(mandate, source)
    ):
        return False
    return True


def _provider_refund_idempotency_key(refund_id: UUID) -> str:
    return f"qcr-{refund_id}"


def _refund_operation_fingerprint(
    *,
    transfer_id: UUID,
    amount_cents: int,
    reason: str,
    idempotency_key: str,
) -> str:
    """Identify one approved refund while still permitting equal later refunds."""

    return _canonical_hash({
        "transfer_id": str(transfer_id),
        "amount_cents": amount_cents,
        "reason": " ".join(reason.split()).casefold(),
        "idempotency_key": idempotency_key,
    })


async def _lock_profile(db: AsyncSession, profile_id: UUID) -> None:
    """Serialize profile-scoped financial version creation, including first rows."""

    locked = (
        await db.execute(
            select(ApplicationProfile.id)
            .where(ApplicationProfile.id == profile_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application file not found")


def _allocation_dict(payload: FeeAllocationInput) -> dict[str, int]:
    return {
        "client_ach_cents": payload.client_ach_cents,
        "bank_direct_cents": payload.bank_direct_cents,
        "external_cents": payload.external_cents,
        "deferred_cents": payload.deferred_cents,
        "waived_cents": payload.waived_cents,
    }


def _amount_or_cents(
    *,
    amount: Decimal | float | int | None,
    cents: int | None,
    label: str,
) -> int:
    """Accept the normalized dollar API and the backwards-compatible cents API."""

    if amount is not None and cents is not None and _cents(amount) != cents:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{label} was supplied with conflicting dollar and cent values",
        )
    return cents if cents is not None else _cents(amount)


def _patch_allocation_dict(payload: FeeAllocationPatch) -> dict[str, int]:
    return {
        "client_ach_cents": _amount_or_cents(
            amount=payload.client_ach_amount,
            cents=payload.client_ach_cents,
            label="Client ACH allocation",
        ),
        "bank_direct_cents": _amount_or_cents(
            amount=payload.bank_direct_amount,
            cents=payload.bank_direct_cents,
            label="Bank-direct allocation",
        ),
        "external_cents": _amount_or_cents(
            amount=payload.external_amount,
            cents=payload.external_cents,
            label="External allocation",
        ),
        "deferred_cents": _amount_or_cents(
            amount=payload.deferred_amount,
            cents=payload.deferred_cents,
            label="Deferred allocation",
        ),
        "waived_cents": _amount_or_cents(
            amount=payload.waived_amount,
            cents=payload.waived_cents,
            label="Waived allocation",
        ),
    }


def _component_client_ach_split(
    *,
    client_ach_cents: int,
    origination_fee_cents: int,
    consulting_fee_cents: int,
    origination_client_ach_cents: int | None,
    consulting_client_ach_cents: int | None,
) -> tuple[int, int]:
    """Validate the exact component allocation, with origination-first legacy fallback."""

    origination = origination_client_ach_cents
    consulting = consulting_client_ach_cents
    if origination is None and consulting is None:
        origination = min(client_ach_cents, origination_fee_cents)
        consulting = client_ach_cents - origination
    elif origination is None:
        origination = client_ach_cents - int(consulting or 0)
    elif consulting is None:
        consulting = client_ach_cents - origination
    if origination < 0 or consulting < 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Component ACH allocations cannot exceed the total client ACH allocation",
        )
    if origination + consulting != client_ach_cents:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Origination and consulting ACH allocations must equal the client ACH allocation",
        )
    if origination > origination_fee_cents:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Origination ACH allocation cannot exceed the origination fee",
        )
    if consulting > consulting_fee_cents:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Consulting ACH allocation cannot exceed the consulting fee",
        )
    return origination, consulting


def _patch_component_client_ach(
    payload: FeeAllocationPatch,
    *,
    client_ach_cents: int,
    origination_fee_cents: int,
    consulting_fee_cents: int,
) -> tuple[int, int]:
    origination = None
    if payload.origination_client_ach_amount is not None or payload.origination_client_ach_cents is not None:
        origination = _amount_or_cents(
            amount=payload.origination_client_ach_amount,
            cents=payload.origination_client_ach_cents,
            label="Origination client ACH allocation",
        )
    consulting = None
    if payload.consulting_client_ach_amount is not None or payload.consulting_client_ach_cents is not None:
        consulting = _amount_or_cents(
            amount=payload.consulting_client_ach_amount,
            cents=payload.consulting_client_ach_cents,
            label="Consulting client ACH allocation",
        )
    return _component_client_ach_split(
        client_ach_cents=client_ach_cents,
        origination_fee_cents=origination_fee_cents,
        consulting_fee_cents=consulting_fee_cents,
        origination_client_ach_cents=origination,
        consulting_client_ach_cents=consulting,
    )


async def assert_payment_room_identity(
    db: AsyncSession,
    *,
    link: Any,
    profile: ApplicationProfile,
) -> None:
    """Limit payment authority to the canonical room owned by the client identity."""

    from app.dealer_os.services import client_room
    from app.services import application_profiles as profile_service

    _business_name, _client_name, client_email = await _profile_identity(db, profile)
    current_link = await client_room.active_link(db, link.bucket_id)
    if (
        not client_room.link_is_usable(link)
        or current_link is None
        or current_link.id != link.id
        or not profile_service.normalized_email(link.recipient_email)
        or profile_service.normalized_email(link.recipient_email)
        != profile_service.normalized_email(client_email)
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This secure room invitation is not authorized for payment actions",
        )


def validate_allocation(gross_fee_cents: int, payload: FeeAllocationInput) -> dict[str, int]:
    allocation = _allocation_dict(payload)
    if sum(allocation.values()) != gross_fee_cents:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Fee allocation must equal gross fee ({gross_fee_cents} cents)",
        )
    return allocation


def _select_obligation_allocation(
    *,
    allocation: dict[str, int],
    full_origination_cents: int,
    full_consulting_cents: int,
    include_origination: bool,
    include_consulting: bool,
    origination_client_ach_cents: int,
    consulting_client_ach_cents: int,
) -> tuple[dict[str, int], int, int]:
    """Reduce a saved forecast allocation to the fee lines eligible now.

    An unearned component may only be omitted when it has no client ACH
    allocation and its remaining dollars were explicitly deferred or waived.
    This avoids guessing which portion of a bank-direct/manual allocation
    belongs to a fee that is not yet collectible.
    """

    selected_origination = full_origination_cents if include_origination else 0
    selected_consulting = full_consulting_cents if include_consulting else 0
    selected_gross = selected_origination + selected_consulting
    full_gross = full_origination_cents + full_consulting_cents
    allocated = sum(allocation.values())
    if allocated not in {selected_gross, full_gross}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The saved allocation does not match the selected fee components; update the allocation first",
        )
    reduced = dict(allocation)
    exclusions = []
    if not include_origination and full_origination_cents:
        if origination_client_ach_cents:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Set origination client ACH to None before excluding the origination fee",
            )
        exclusions.append(("origination", full_origination_cents))
        origination_client_ach_cents = 0
    if not include_consulting and full_consulting_cents:
        if consulting_client_ach_cents:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Set consulting client ACH to None and defer or waive that fee until it is earned",
            )
        exclusions.append(("consulting", full_consulting_cents))
        consulting_client_ach_cents = 0
    if allocated == full_gross:
        for label, excluded_cents in exclusions:
            remaining = excluded_cents
            for key in ("deferred_cents", "waived_cents"):
                amount = min(remaining, reduced[key])
                reduced[key] -= amount
                remaining -= amount
            if remaining:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"Allocate the excluded {label} fee to Deferred or Waived before preparing collection",
                )
    if sum(reduced.values()) != selected_gross:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Fee allocation does not balance the currently selected fee lines",
        )
    return reduced, origination_client_ach_cents, consulting_client_ach_cents


async def latest_allocation(
    db: AsyncSession, profile_id: UUID, *, for_update: bool = False
) -> FeeAllocationVersion | None:
    stmt = (
        select(FeeAllocationVersion)
        .where(FeeAllocationVersion.application_profile_id == profile_id)
        .order_by(FeeAllocationVersion.version.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


def allocation_response(row: FeeAllocationVersion, *, current_gross_cents: int) -> FeeAllocationResponse:
    values = row.allocation or {}
    assigned = sum(int(values.get(key, 0) or 0) for key in (
        "client_ach_cents", "bank_direct_cents", "external_cents", "deferred_cents", "waived_cents"
    ))
    client_ach_cents = int(values.get("client_ach_cents", 0) or 0)
    origination_client_ach_cents = int(row.origination_client_ach_cents or 0)
    consulting_client_ach_cents = int(row.consulting_client_ach_cents or 0)
    if origination_client_ach_cents + consulting_client_ach_cents != client_ach_cents:
        # Compatibility for any aggregate-only records created before component
        # allocation was introduced.
        origination_client_ach_cents = client_ach_cents
        consulting_client_ach_cents = 0
    return FeeAllocationResponse(
        id=row.id,
        version=row.version,
        collection_mode=row.collection_mode,
        gross_fee=row.gross_fee_cents / 100,
        client_ach_amount=client_ach_cents / 100,
        origination_client_ach_amount=origination_client_ach_cents / 100,
        consulting_client_ach_amount=consulting_client_ach_cents / 100,
        origination_client_ach_cents=origination_client_ach_cents,
        consulting_client_ach_cents=consulting_client_ach_cents,
        bank_direct_amount=int(values.get("bank_direct_cents", 0) or 0) / 100,
        external_amount=int(values.get("external_cents", 0) or 0) / 100,
        deferred_amount=int(values.get("deferred_cents", 0) or 0) / 100,
        waived_amount=int(values.get("waived_cents", 0) or 0) / 100,
        unallocated_amount=(row.gross_fee_cents - assigned) / 100,
        is_balanced=assigned == row.gross_fee_cents,
        is_current=row.gross_fee_cents == current_gross_cents,
        updated_at=row.updated_at,
    )


async def save_fee_allocation(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    payload: FeeAllocationPatch,
    actor: User,
) -> FeeAllocationVersion:
    await _lock_profile(db, profile.id)
    economics = economics_snapshot(profile)
    allocation = _patch_allocation_dict(payload)
    if sum(allocation.values()) != economics.gross_fee_cents:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Fee allocation must equal gross expected fees ({economics.gross_fee_cents / 100:.2f})",
        )
    origination_client_ach_cents, consulting_client_ach_cents = _patch_component_client_ach(
        payload,
        client_ach_cents=allocation["client_ach_cents"],
        origination_fee_cents=economics.origination_fee_cents,
        consulting_fee_cents=_cents(economics.consulting_fee),
    )
    latest = await latest_allocation(db, profile.id, for_update=True)
    if (
        latest
        and latest.allocation == allocation
        and latest.collection_mode == payload.collection_mode
        and latest.origination_client_ach_cents == origination_client_ach_cents
        and latest.consulting_client_ach_cents == consulting_client_ach_cents
    ):
        return latest
    if payload.expected_record_version and (
        latest is None or latest.version != payload.expected_record_version
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "Fee allocation changed; reload before saving")
    obligation = await current_obligation(db, profile.id, for_update=True)
    if obligation and obligation.status in {"processing", "partially_collected", "collected"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "Collected or processing fee economics cannot be changed")
    # A stale non-processing obligation is superseded below. This permits a
    # balanced allocation for the new economics before staff explicitly
    # prepares the replacement immutable obligation.
    active_mandates: list[AchMandate] = []
    if obligation:
        active_mandates = (
            await db.execute(
                select(AchMandate).where(
                    AchMandate.fee_obligation_id == obligation.id,
                    AchMandate.status == "active",
                    AchMandate.revoked_at.is_(None),
                ).with_for_update()
            )
        ).scalars().all()
        terminated_at = _now()
        for mandate in active_mandates:
            mandate.status = "superseded"
            mandate.revoked_at = terminated_at
            mandate.revoked_by_user_id = actor.id
            mandate.terminated_at = terminated_at
            mandate.termination_reason = "Fee allocation changed"
            from app.services import ach_fee_workflow

            await ach_fee_workflow.extend_mandate_retention(
                db, mandate, anchor=terminated_at
            )
        await cancel_unclaimed_transfer_intents(
            db,
            mandate_ids=[mandate.id for mandate in active_mandates],
            reason="fee_allocation_changed",
        )
        # A prepared obligation is a signed financial snapshot.  Allocation
        # edits never rewrite that snapshot (or its lines) in place.  Retire it
        # and require an explicit prepare action to create the next version.
        obligation.status = "superseded"
        obligation.superseded_at = _now()
        obligation.superseded_by_user_id = actor.id
        obligation.record_version += 1
    row = FeeAllocationVersion(
        application_profile_id=profile.id,
        # A newly edited allocation is only a draft until the operator prepares
        # a fresh immutable fee obligation.
        obligation_id=None,
        version=(latest.version + 1) if latest else 1,
        collection_mode=payload.collection_mode,
        gross_fee_cents=economics.gross_fee_cents,
        origination_client_ach_cents=origination_client_ach_cents,
        consulting_client_ach_cents=consulting_client_ach_cents,
        allocation=allocation,
        allocation_sha256=_canonical_hash(allocation),
        reason=payload.reason,
        created_by_user_id=actor.id,
    )
    db.add(row)
    await db.flush()
    await log_event(
        db,
        profile_id=profile.id,
        actor_id=actor.id,
        event_type="fee_allocation.saved",
        entity_type="fee_allocation",
        entity_id=row.id,
        summary=f"Saved fee allocation v{row.version}",
        metadata={
            "allocation": allocation,
            "origination_client_ach_cents": origination_client_ach_cents,
            "consulting_client_ach_cents": consulting_client_ach_cents,
            "mandates_voided": len(active_mandates),
            "superseded_obligation_id": str(obligation.id) if obligation else None,
        },
    )
    return row


async def log_event(
    db: AsyncSession,
    *,
    profile_id: UUID,
    actor_id: UUID | None,
    event_type: str,
    entity_type: str,
    entity_id: UUID | None,
    summary: str,
    metadata: dict[str, Any] | None = None,
) -> PaymentAuditEvent:
    row = PaymentAuditEvent(
        application_profile_id=profile_id,
        actor_user_id=actor_id,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary,
        metadata_json=metadata or {},
        created_at=_now(),
    )
    db.add(row)
    return row


async def request_payment_review(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    actor: User,
    idempotency_key: str,
    reason: str | None = None,
) -> PaymentAuditEvent:
    """Create one review request across browser retries and notify managers."""

    event_id = uuid5(
        NAMESPACE_URL,
        f"qualified-commercial:payment-review:{profile.id}:{idempotency_key}",
    )
    existing = await db.get(PaymentAuditEvent, event_id)
    if existing:
        return existing
    row = PaymentAuditEvent(
        id=event_id,
        application_profile_id=profile.id,
        actor_user_id=actor.id,
        event_type="payment.review_requested",
        entity_type="application_profile",
        entity_id=profile.id,
        summary="Requested payment review",
        metadata_json={"reason": reason, "idempotency_key": idempotency_key},
        created_at=_now(),
    )
    db.add(row)
    managers = await notifications.users_with_roles(
        db,
        Role.SUPER_ADMIN,
        Role.LOAN_EXEC,
    )
    await notifications.notify_users(
        db,
        recipient_ids={manager.id for manager in managers},
        actor_user_id=actor.id,
        event_type="payment.review_requested",
        category="payments",
        priority="high",
        title="Payment review requested",
        body=(reason or "An assigned team member requested review of this file's payment setup."),
        target_type="application_profile",
        target_id=str(profile.id),
        meta={"profile_id": str(profile.id), "audit_event_id": str(event_id)},
        batch_key=f"payment-review:{event_id}",
        push=True,
    )
    return row


async def current_obligation(db: AsyncSession, profile_id: UUID, *, for_update: bool = False) -> FeeObligation | None:
    stmt = select(FeeObligation).where(
        FeeObligation.application_profile_id == profile_id,
        FeeObligation.superseded_at.is_(None),
        FeeObligation.status != "cancelled",
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def fee_obligation_sha256(db: AsyncSession, obligation: FeeObligation) -> str:
    """Canonical hash signed by the client for one immutable obligation."""

    lines = (
        await db.execute(
            select(FeeObligationLine)
            .where(FeeObligationLine.obligation_id == obligation.id)
            .order_by(FeeObligationLine.line_type)
        )
    ).scalars().all()
    return _canonical_hash({
        "obligation_id": str(obligation.id),
        "version": obligation.version,
        "client_ach_cents": obligation.client_ach_cents,
        "accepted_amount": obligation.accepted_amount,
        "funded_amount": obligation.funded_amount,
        "origination_points": obligation.origination_points,
        "lines": [
            {
                "type": line.line_type,
                "amount_cents": line.amount_cents,
                "client_ach_cents": line.client_ach_cents,
            }
            for line in lines
        ],
        "agreement_reference": obligation.agreement_reference,
        "agreement_sha256": obligation.agreement_sha256,
    })


async def _profile_identity(db: AsyncSession, profile: ApplicationProfile) -> tuple[str | None, str | None, str | None]:
    if profile.client_id:
        client = await db.get(Client, profile.client_id)
        if client:
            return client.name, client.name, client.email
    if profile.intake_id:
        intake = await db.get(PublicUnderwritingIntake, profile.intake_id)
        if intake:
            return intake.business_name, intake.full_name, intake.email
    return None, None, None


def _is_sha256(value: str | None) -> bool:
    raw = str(value or "").strip().lower()
    return len(raw) == 64 and all(character in "0123456789abcdef" for character in raw)


async def agreement_document_candidates(
    db: AsyncSession,
    profile: ApplicationProfile,
) -> list[AgreementDocumentCandidate]:
    """Return only immutable, owned BucketFiles that can back fee consent."""

    if profile.primary_bucket_id is None:
        return []
    files = (
        await db.execute(
            select(BucketFile)
            .where(
                BucketFile.bucket_id == profile.primary_bucket_id,
                BucketFile.deleted_at.is_(None),
            )
            .order_by(BucketFile.created_at.desc())
        )
    ).scalars().all()
    files = [file for file in files if _is_sha256(file.content_hash)]
    if not files:
        return []
    signature_rows = (
        await db.execute(
            select(BucketDocumentSignature, BucketRequestedDocument)
            .join(
                BucketRequestedDocument,
                BucketRequestedDocument.id == BucketDocumentSignature.requested_document_id,
            )
            .where(
                BucketDocumentSignature.result_file_id.in_([file.id for file in files]),
                BucketDocumentSignature.signed_at.is_not(None),
                BucketDocumentSignature.esign_consent.is_(True),
            )
            .order_by(BucketDocumentSignature.signed_at.desc())
        )
    ).all()
    signatures: dict[UUID, tuple[BucketDocumentSignature, BucketRequestedDocument]] = {}
    for signature, requested in signature_rows:
        if signature.result_file_id not in signatures:
            signatures[signature.result_file_id] = (signature, requested)
    return [
        AgreementDocumentCandidate(
            id=file.id,
            name=file.file_name,
            sha256=str(file.content_hash).lower(),
            system_signed=file.id in signatures,
            signed_at=(signatures[file.id][0].signed_at if file.id in signatures else None),
            signature_kind=(signatures[file.id][1].signature_kind if file.id in signatures else None),
            requires_staff_attestation=file.id not in signatures,
        )
        for file in files
    ]


async def _resolve_fee_agreement(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    payload: FeeObligationCreate,
    actor: User,
) -> tuple[BucketFile, str, str, dict[str, Any], dict[str, Any]]:
    if payload.agreement_document_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Select a signed agreement document from this application file",
        )
    file = await db.get(BucketFile, payload.agreement_document_id)
    if (
        file is None
        or profile.primary_bucket_id is None
        or file.bucket_id != profile.primary_bucket_id
        or file.deleted_at is not None
        or not _is_sha256(file.content_hash)
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Agreement must be a non-deleted, hash-verified file owned by this application",
        )
    signature_row = (
        await db.execute(
            select(BucketDocumentSignature, BucketRequestedDocument)
            .join(
                BucketRequestedDocument,
                BucketRequestedDocument.id == BucketDocumentSignature.requested_document_id,
            )
            .where(
                BucketDocumentSignature.result_file_id == file.id,
                BucketDocumentSignature.signed_at.is_not(None),
                BucketDocumentSignature.esign_consent.is_(True),
            )
            .order_by(BucketDocumentSignature.signed_at.desc())
            .limit(1)
        )
    ).first()
    if signature_row is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Use the deal-specific Success Fee Agreement prepare and signing flow",
        )
    signature, requested = signature_row if signature_row else (None, None)
    if requested is None or requested.signature_kind != "success_fee_agreement":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Only an executed deal-specific Success Fee Agreement may govern fee collection",
        )
    # A cryptographically valid historical PDF is not authority to collect
    # fees calculated from newer economics or a newer allocation.  The
    # document-signing flow snapshots both, so every entry point (including
    # the compatibility obligation endpoint) must revalidate that snapshot.
    from app.services import ach_fee_workflow

    if not await ach_fee_workflow.prepared_agreement_is_current(
        db, profile=profile, requested=requested
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This Success Fee Agreement no longer matches the current deal economics or fee allocation. Prepare and sign a replacement agreement.",
        )
    source = requested.requirement_source or {}
    signed_terms = source.get("snapshot") if isinstance(source, dict) else None
    if not isinstance(signed_terms, dict):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The executed Success Fee Agreement has no verifiable fee snapshot",
        )
    snapshot: dict[str, Any] = {
        "artifact_type": "bucket_file",
        "bucket_file_id": str(file.id),
        "bucket_id": str(file.bucket_id),
        "file_name": file.file_name,
        "content_sha256": str(file.content_hash).lower(),
        "system_signed": signature is not None,
        "signature_id": str(signature.id) if signature else None,
        "signed_at": signature.signed_at.isoformat() if signature and signature.signed_at else None,
        "signature_kind": requested.signature_kind if requested else None,
        "attested_signed": False,
        "attested_by_user_id": None,
        "attested_at": None,
    }
    return (
        file,
        f"bucket-file:{file.id}",
        str(file.content_hash).lower(),
        snapshot,
        signed_terms,
    )


def _require_obligation_matches_signed_fee_terms(
    *,
    payload: FeeObligationCreate,
    signed_terms: dict[str, Any],
    origination_fee_cents: int,
    consulting_fee_cents: int,
    gross_fee_cents: int,
    allocation: dict[str, int],
    origination_client_ach_cents: int,
    consulting_client_ach_cents: int,
) -> dict[str, Any]:
    """Bind compatibility obligation creation to the exact executed agreement.

    The signing workflow normally creates the obligation itself.  This guard
    keeps the compatibility endpoint idempotent without allowing a caller to
    reuse that signed PDF for different components, economics, or allocation.
    The returned subset is persisted with the obligation as durable evidence
    of the comparison made here.
    """

    integer_fields = (
        "origination_fee_cents",
        "consulting_fee_cents",
        "gross_fee_cents",
        "client_ach_cents",
        "origination_client_ach_cents",
        "consulting_client_ach_cents",
        "bank_direct_cents",
        "external_cents",
        "deferred_cents",
        "waived_cents",
    )
    try:
        expected = {
            "include_origination_fee": bool(
                signed_terms.get("include_origination_fee")
            ),
            "include_consulting_fee": bool(
                signed_terms.get("include_consulting_fee")
            ),
            "consulting_milestone_confirmed": bool(
                signed_terms.get("consulting_milestone_confirmed")
            ),
            **{
                field: int(signed_terms.get(field) or 0)
                for field in integer_fields
            },
        }
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The executed Success Fee Agreement has an invalid fee snapshot. Prepare and sign a replacement agreement.",
        ) from exc
    actual = {
        "include_origination_fee": payload.include_origination_fee,
        "include_consulting_fee": payload.include_consulting_fee,
        "consulting_milestone_confirmed": payload.consulting_milestone_confirmed,
        "origination_fee_cents": origination_fee_cents,
        "consulting_fee_cents": consulting_fee_cents,
        "gross_fee_cents": gross_fee_cents,
        "client_ach_cents": allocation["client_ach_cents"],
        "origination_client_ach_cents": origination_client_ach_cents,
        "consulting_client_ach_cents": consulting_client_ach_cents,
        "bank_direct_cents": allocation["bank_direct_cents"],
        "external_cents": allocation["external_cents"],
        "deferred_cents": allocation["deferred_cents"],
        "waived_cents": allocation["waived_cents"],
    }
    if actual != expected:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The requested fee components or allocation do not exactly match the executed Success Fee Agreement. Prepare and sign a replacement agreement.",
        )
    return expected


async def _fee_agreement_is_current(db: AsyncSession, obligation: FeeObligation) -> bool:
    snapshot = obligation.agreement_snapshot or {}
    if (
        obligation.agreement_document_id is None
        or snapshot.get("artifact_type") != "bucket_file"
        or str(snapshot.get("bucket_file_id") or "") != str(obligation.agreement_document_id)
        or str(snapshot.get("content_sha256") or "").lower()
        != str(obligation.agreement_sha256 or "").lower()
    ):
        return False
    profile = await db.get(ApplicationProfile, obligation.application_profile_id)
    file = await db.get(BucketFile, obligation.agreement_document_id)
    if (
        profile is None
        or file is None
        or profile.primary_bucket_id is None
        or file.bucket_id != profile.primary_bucket_id
        or file.deleted_at is not None
        or str(file.content_hash or "").lower() != str(obligation.agreement_sha256 or "").lower()
    ):
        return False
    if snapshot.get("system_signed") and snapshot.get("signature_kind") == "success_fee_agreement":
        signature_id = snapshot.get("signature_id")
        if not signature_id:
            return False
        try:
            signature = await db.get(BucketDocumentSignature, UUID(str(signature_id)))
        except ValueError:
            return False
        return bool(
            signature
            and signature.result_file_id == file.id
            and signature.signed_at
            and signature.esign_consent
        )
    return False


async def fee_lines_have_current_governing_agreements(
    db: AsyncSession,
    obligation: FeeObligation,
    *,
    lines: list[FeeObligationLine] | None = None,
) -> bool:
    """Validate each fee component against its exact signed agreement.

    Origination is governed by the deal-specific Success Fee Agreement.
    Consulting is governed by the separately executed Consulting and Fee
    Schedule Addendum. Both artifacts must remain hash verified and belong to
    the same application bucket.
    """

    profile = await db.get(ApplicationProfile, obligation.application_profile_id)
    if profile is None or profile.primary_bucket_id is None:
        return False
    if lines is None:
        lines = list(
            (
                await db.execute(
                    select(FeeObligationLine).where(
                        FeeObligationLine.obligation_id == obligation.id
                    )
                )
            ).scalars().all()
        )
    expected_components = {
        component
        for component, amount in (
            ("origination", obligation.origination_fee_cents),
            ("consulting", obligation.consulting_fee_cents),
        )
        if amount
    }
    if {line.line_type for line in lines} != expected_components:
        return False
    expected_signature_kind = {
        "origination": "success_fee_agreement",
        "consulting": "contract_consulting_addendum",
    }
    for line in lines:
        if (
            line.agreement_component_scope != line.line_type
            or line.governing_agreement_document_id is None
            or not _is_sha256(line.governing_agreement_sha256)
        ):
            return False
        file = await db.get(BucketFile, line.governing_agreement_document_id)
        if (
            file is None
            or file.bucket_id != profile.primary_bucket_id
            or file.deleted_at is not None
            or str(file.content_hash or "").lower()
            != str(line.governing_agreement_sha256).lower()
        ):
            return False
        signature_exists = (
            await db.execute(
                select(BucketDocumentSignature.id)
                .join(
                    BucketRequestedDocument,
                    BucketRequestedDocument.id
                    == BucketDocumentSignature.requested_document_id,
                )
                .where(
                    BucketDocumentSignature.result_file_id == file.id,
                    BucketDocumentSignature.signed_at.is_not(None),
                    BucketDocumentSignature.esign_consent.is_(True),
                    BucketRequestedDocument.signature_kind
                    == expected_signature_kind[line.line_type],
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if signature_exists is None:
            return False
    return True


def economics_snapshot(profile: ApplicationProfile) -> DealEconomicsSnapshot:
    earnings = calculate_deal_earnings(
        accepted_amount=profile.underwriting_accepted_amount,
        origination_points=profile.forecast_fee_points,
        consulting_fee=profile.forecast_consulting_fee,
    )
    return DealEconomicsSnapshot(
        approved_amount=profile.underwriting_approved_amount,
        accepted_amount=earnings.accepted_amount,
        funded_amount=profile.underwriting_funded_amount,
        origination_points=earnings.origination_points,
        consulting_fee=earnings.consulting_fee,
        origination_fee_cents=_cents(earnings.origination_earnings),
        gross_fee_cents=_cents(earnings.total),
        estimated_close_date=profile.estimated_close_date,
    )


async def create_fee_obligation(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    payload: FeeObligationCreate,
    actor: User,
) -> FeeObligation:
    await _lock_profile(db, profile.id)
    existing = await current_obligation(db, profile.id, for_update=True)
    if existing and existing.status in {"processing", "partially_collected", "collected"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "A collected or processing obligation cannot be replaced")

    economics = economics_snapshot(profile)
    full_origination_cents = economics.origination_fee_cents
    full_consulting_cents = _cents(economics.consulting_fee)
    origination_cents = full_origination_cents if payload.include_origination_fee else 0
    consulting_cents = full_consulting_cents if payload.include_consulting_fee else 0
    gross = origination_cents + consulting_cents
    if gross <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The selected fee components total zero")
    (
        agreement_file,
        agreement_reference,
        agreement_sha256,
        agreement_snapshot,
        signed_fee_terms,
    ) = (
        await _resolve_fee_agreement(
            db,
            profile=profile,
            payload=payload,
            actor=actor,
        )
    )
    consulting_agreement_file: BucketFile | None = None
    if consulting_cents:
        from app.services import ach_fee_workflow

        consulting_agreement_file, _, _ = (
            await ach_fee_workflow._current_signed_consulting_addendum(db, profile)
        )
    latest_plan = await latest_allocation(db, profile.id, for_update=True)
    direct_allocation = _allocation_dict(payload)
    if any(direct_allocation.values()):
        allocation = direct_allocation
        collection_mode = "split"
        direct_origination_ach = payload.origination_client_ach_cents
        direct_consulting_ach = payload.consulting_client_ach_cents
        if (
            direct_origination_ach is None
            and direct_consulting_ach is None
            and latest_plan
            and int(latest_plan.allocation.get("client_ach_cents", 0) or 0)
            == allocation["client_ach_cents"]
        ):
            direct_origination_ach = latest_plan.origination_client_ach_cents
            direct_consulting_ach = latest_plan.consulting_client_ach_cents
        origination_client_ach_cents, consulting_client_ach_cents = _component_client_ach_split(
            client_ach_cents=allocation["client_ach_cents"],
            origination_fee_cents=full_origination_cents,
            consulting_fee_cents=full_consulting_cents,
            origination_client_ach_cents=direct_origination_ach,
            consulting_client_ach_cents=direct_consulting_ach,
        )
    elif latest_plan:
        allocation = {key: int(value or 0) for key, value in latest_plan.allocation.items()}
        collection_mode = latest_plan.collection_mode
        origination_client_ach_cents, consulting_client_ach_cents = _component_client_ach_split(
            client_ach_cents=allocation["client_ach_cents"],
            origination_fee_cents=full_origination_cents,
            consulting_fee_cents=full_consulting_cents,
            origination_client_ach_cents=latest_plan.origination_client_ach_cents,
            consulting_client_ach_cents=latest_plan.consulting_client_ach_cents,
        )
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Save a balanced fee allocation first")
    allocation, origination_client_ach_cents, consulting_client_ach_cents = _select_obligation_allocation(
        allocation=allocation,
        full_origination_cents=full_origination_cents,
        full_consulting_cents=full_consulting_cents,
        include_origination=payload.include_origination_fee,
        include_consulting=payload.include_consulting_fee,
        origination_client_ach_cents=origination_client_ach_cents,
        consulting_client_ach_cents=consulting_client_ach_cents,
    )
    if payload.include_consulting_fee and not payload.consulting_milestone_confirmed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Confirm the consulting-fee earning milestone before adding it to an obligation",
        )
    agreement_snapshot["signed_fee_terms"] = _require_obligation_matches_signed_fee_terms(
        payload=payload,
        signed_terms=signed_fee_terms,
        origination_fee_cents=origination_cents,
        consulting_fee_cents=consulting_cents,
        gross_fee_cents=gross,
        allocation=allocation,
        origination_client_ach_cents=origination_client_ach_cents,
        consulting_client_ach_cents=consulting_client_ach_cents,
    )

    # Preparing an obligation is semantically idempotent.  A browser retry
    # after a lost response must return the immutable snapshot that was just
    # created, not supersede it and invalidate the client's future mandate.
    existing_line_agreements_current = bool(
        existing
        and await fee_lines_have_current_governing_agreements(db, existing)
    )
    if existing and (
        existing.accepted_amount == profile.underwriting_accepted_amount
        and existing.funded_amount == profile.underwriting_funded_amount
        and existing.origination_points == profile.forecast_fee_points
        and existing.origination_fee_cents == origination_cents
        and existing.consulting_fee_cents == consulting_cents
        and existing.gross_fee_cents == gross
        and existing.client_ach_cents == allocation["client_ach_cents"]
        and existing.origination_client_ach_cents == origination_client_ach_cents
        and existing.consulting_client_ach_cents == consulting_client_ach_cents
        and existing.bank_direct_cents == allocation["bank_direct_cents"]
        and existing.external_cents == allocation["external_cents"]
        and existing.deferred_cents == allocation["deferred_cents"]
        and existing.waived_cents == allocation["waived_cents"]
        and existing.agreement_document_id == agreement_file.id
        and existing.agreement_reference == agreement_reference
        and existing.agreement_sha256 == agreement_sha256
        and bool(existing.consulting_milestone_confirmed_at)
        == bool(payload.include_consulting_fee)
        and existing_line_agreements_current
    ):
        return existing

    version = 1
    if existing:
        active_mandates = (
            await db.execute(
                select(AchMandate)
                .where(
                    AchMandate.fee_obligation_id == existing.id,
                    AchMandate.status == "active",
                    AchMandate.revoked_at.is_(None),
                )
                .with_for_update()
            )
        ).scalars().all()
        await cancel_unclaimed_transfer_intents(
            db,
            mandate_ids=[mandate.id for mandate in active_mandates],
            reason="fee_obligation_replaced",
        )
        terminated_at = _now()
        for mandate in active_mandates:
            mandate.status = "superseded"
            mandate.revoked_at = terminated_at
            mandate.revoked_by_user_id = actor.id
            mandate.terminated_at = terminated_at
            mandate.termination_reason = "Fee obligation replaced"
            from app.services import ach_fee_workflow

            await ach_fee_workflow.extend_mandate_retention(
                db, mandate, anchor=terminated_at
            )
        version = existing.version + 1
        existing.superseded_at = terminated_at
        existing.superseded_by_user_id = actor.id
        existing.status = "superseded"
        existing.record_version += 1
        await db.flush()
    business_name, client_name, client_email = await _profile_identity(db, profile)
    obligation = FeeObligation(
        application_profile_id=profile.id,
        client_id=profile.client_id,
        loan_id=profile.loan_id,
        intake_id=profile.intake_id,
        production_package_id=None,
        version=version,
        status="prepared",
        accepted_amount=profile.underwriting_accepted_amount,
        funded_amount=profile.underwriting_funded_amount,
        origination_points=profile.forecast_fee_points,
        origination_fee_cents=origination_cents,
        consulting_fee_cents=consulting_cents,
        gross_fee_cents=gross,
        origination_client_ach_cents=origination_client_ach_cents,
        consulting_client_ach_cents=consulting_client_ach_cents,
        **allocation,
        business_name_snapshot=business_name,
        client_name_snapshot=client_name,
        client_email_snapshot=client_email,
        economics_snapshot=economics.model_dump(mode="json"),
        agreement_document_id=agreement_file.id,
        agreement_reference=agreement_reference,
        agreement_sha256=agreement_sha256,
        agreement_snapshot=agreement_snapshot,
        consulting_milestone_confirmed_at=_now() if payload.include_consulting_fee else None,
        consulting_milestone_confirmed_by_user_id=actor.id if payload.include_consulting_fee else None,
        created_by_user_id=actor.id,
    )
    db.add(obligation)
    await db.flush()
    if origination_cents:
        db.add(FeeObligationLine(
            obligation_id=obligation.id,
            line_type="origination",
            amount_cents=origination_cents,
            client_ach_cents=origination_client_ach_cents,
            governing_agreement_document_id=agreement_file.id,
            governing_agreement_sha256=agreement_sha256,
            agreement_component_scope="origination",
            earning_milestone="actual financing funding",
            calculation_snapshot={
                "accepted_amount": str(economics.accepted_amount or "0"),
                "origination_points": str(economics.origination_points or "0"),
            },
        ))
    if consulting_cents:
        if consulting_agreement_file is None or not _is_sha256(
            consulting_agreement_file.content_hash
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The signed Consulting and Fee Schedule Addendum is unavailable",
            )
        db.add(FeeObligationLine(
            obligation_id=obligation.id,
            line_type="consulting",
            amount_cents=consulting_cents,
            client_ach_cents=consulting_client_ach_cents,
            governing_agreement_document_id=consulting_agreement_file.id,
            governing_agreement_sha256=str(
                consulting_agreement_file.content_hash
            ).lower(),
            agreement_component_scope="consulting",
            earning_milestone=(
                "the milestone defined in the Consulting and Fee Schedule Addendum, "
                "confirmed by staff"
            ),
            calculation_snapshot={"fixed_fee": str(economics.consulting_fee or "0")},
            earned_confirmed_at=obligation.consulting_milestone_confirmed_at,
            earned_confirmed_by_user_id=actor.id,
        ))
    from app.services import ach_fee_workflow

    ach_fee_workflow.protect_bucket_file(
        agreement_file,
        retention_class="payment_agreement",
        anchor=agreement_file.created_at or _now(),
        entity_type="fee_obligation",
        entity_id=obligation.id,
        immutable_ref=agreement_sha256,
    )
    if consulting_agreement_file is not None and consulting_agreement_file.content_hash:
        ach_fee_workflow.protect_bucket_file(
            consulting_agreement_file,
            retention_class="payment_agreement",
            anchor=consulting_agreement_file.created_at or _now(),
            entity_type="fee_obligation",
            entity_id=obligation.id,
            immutable_ref=consulting_agreement_file.content_hash,
        )
    allocation_row = FeeAllocationVersion(
        application_profile_id=profile.id,
        obligation_id=obligation.id,
        version=(latest_plan.version + 1) if latest_plan else 1,
        collection_mode=collection_mode,
        gross_fee_cents=gross,
        origination_client_ach_cents=origination_client_ach_cents,
        consulting_client_ach_cents=consulting_client_ach_cents,
        allocation=allocation,
        allocation_sha256=_canonical_hash(allocation),
        reason=payload.reason,
        created_by_user_id=actor.id,
    )
    db.add(allocation_row)
    if latest_plan and latest_plan.obligation_id is None:
        latest_plan.obligation_id = obligation.id
    await log_event(
        db,
        profile_id=profile.id,
        actor_id=actor.id,
        event_type="fee_obligation.prepared",
        entity_type="fee_obligation",
        entity_id=obligation.id,
        summary=f"Prepared fee obligation v{version}",
        metadata={
            "gross_fee_cents": gross,
            "allocation": allocation,
            "origination_client_ach_cents": origination_client_ach_cents,
            "consulting_client_ach_cents": consulting_client_ach_cents,
        },
    )
    return obligation


async def confirm_actual_funding(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    payload: FundingConfirmationCreate,
    actor: User,
    trusted_production_attestation: bool = False,
) -> ActualFundingConfirmation:
    await _lock_profile(db, profile.id)
    if payload.actual_funding_date > _firm_today():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Actual funding date cannot be in the future",
        )
    if payload.source == "manual":
        if not (payload.funding_reference or "").strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Manual funding confirmation requires a transaction reference",
            )
        if not (payload.note or "").strip() and payload.evidence_document_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Manual funding confirmation requires a supporting note or evidence",
            )
        if payload.evidence_document_id is not None:
            evidence = await db.get(Document, payload.evidence_document_id)
            if evidence is None or profile.loan_id is None or evidence.loan_id != profile.loan_id:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "Funding evidence must belong to this application file",
                )
    else:
        if not trusted_production_attestation:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Production funding attestations may only be recorded by the stage-two execution workflow",
            )
        package = (
            await db.get(ProductionPackage, payload.production_package_id)
            if payload.production_package_id
            else None
        )
        if (
            package is None
            or package.profile_id != profile.id
            or package.stage != 2
            or package.status != "executed"
            or package.executed_at is None
            or package.frozen_revision_id is None
            or not package.executed_pdf_s3_key
            or not _is_sha256(package.executed_pdf_sha256)
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Production funding confirmation requires this file's executed stage-two package",
            )
    existing = (
        await db.execute(
            select(ActualFundingConfirmation).where(
                ActualFundingConfirmation.application_profile_id == profile.id,
                ActualFundingConfirmation.superseded_at.is_(None),
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if existing and (
        existing.production_package_id == payload.production_package_id
        and existing.actual_funding_date == payload.actual_funding_date
        and existing.actual_funded_amount == payload.actual_funded_amount
        and existing.funding_party_name == payload.funding_party_name.strip()
        and existing.funding_reference == payload.funding_reference
        and existing.note == payload.note
        and existing.evidence_document_id == payload.evidence_document_id
        and existing.source == payload.source
    ):
        profile.underwriting_funded_amount = payload.actual_funded_amount
        return existing
    version = 1
    if existing:
        version = existing.version + 1
        existing.superseded_at = _now()
        existing.superseded_by_user_id = actor.id
        await db.flush()
    row = ActualFundingConfirmation(
        application_profile_id=profile.id,
        production_package_id=payload.production_package_id,
        version=version,
        actual_funding_date=payload.actual_funding_date,
        actual_funded_amount=payload.actual_funded_amount,
        funding_party_name=payload.funding_party_name.strip(),
        funding_reference=payload.funding_reference,
        note=payload.note,
        evidence_document_id=payload.evidence_document_id,
        source=payload.source,
        confirmed_by_user_id=actor.id,
        confirmed_at=_now(),
    )
    db.add(row)
    profile.underwriting_funded_amount = payload.actual_funded_amount
    await db.flush()
    await log_event(
        db,
        profile_id=profile.id,
        actor_id=actor.id,
        event_type="funding.confirmed",
        entity_type="funding_confirmation",
        entity_id=row.id,
        summary="Recorded actual funding confirmation",
        metadata={"amount": str(payload.actual_funded_amount), "funding_date": str(payload.actual_funding_date)},
    )
    return row


async def create_funding_source(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    payload: PaymentFundingSourceCreate,
) -> PaymentFundingSource:
    existing = (
        await db.execute(
            select(PaymentFundingSource).where(
                PaymentFundingSource.application_profile_id == profile.id,
                PaymentFundingSource.plaid_account_id == payload.plaid_account_id,
                PaymentFundingSource.status == "verified",
                PaymentFundingSource.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    ach_class = "CCD" if payload.owner_type == "business" else "WEB"
    row = PaymentFundingSource(
        application_profile_id=profile.id,
        client_id=profile.client_id,
        status="verified",
        owner_type=payload.owner_type,
        ach_class=ach_class,
        plaid_item_id=payload.plaid_item_id,
        plaid_account_id=payload.plaid_account_id,
        access_token_ciphertext=payload.access_token_ciphertext,
        account_name=payload.account_name,
        account_mask=payload.account_mask,
        account_subtype=payload.account_subtype,
        institution_name=payload.institution_name,
        holder_name=payload.holder_name,
        verified_at=_now(),
        metadata_json=payload.metadata_json,
    )
    db.add(row)
    await db.flush()
    return row


async def create_ach_mandate(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    payload: AchMandateCreate,
    ip_address: str | None,
    user_agent: str | None,
) -> AchMandate:
    source = await db.get(PaymentFundingSource, payload.funding_source_id)
    if not source or source.application_profile_id != profile.id or source.status != "verified":
        raise HTTPException(status.HTTP_409_CONFLICT, "A verified payment bank account is required")
    if payload.fee_obligation_id:
        obligation = (
            await db.execute(
                select(FeeObligation)
                .where(FeeObligation.id == payload.fee_obligation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not obligation or obligation.application_profile_id != profile.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Fee obligation not found")
        if (
            source.owner_type != "business"
            or str(source.ach_class).upper() != "CCD"
            or not _business_account_attested(source)
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "One-time fee debits require a verified and customer-attested business CCD account",
            )
        if not (source.account_mask or "").strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "The business account must have a verified masked account number",
            )
        if payload.authorized_amount_cents != obligation.client_ach_cents:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "The one-time authorization amount must exactly match the client ACH allocation",
            )
        if payload.authorization_type != "one_time_business_ccd":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Fee collection requires an exact one-time business CCD authorization",
            )
        if (
            not payload.authorization_text_snapshot
            or not payload.authorization_text_sha256
            or hashlib.sha256(payload.authorization_text_snapshot.encode("utf-8")).hexdigest()
            != payload.authorization_text_sha256
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "The exact authorization text and digest are required",
            )
        if (
            payload.agreement_document_id != obligation.agreement_document_id
            or payload.agreement_sha256 != obligation.agreement_sha256
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The authorization must reference the exact signed fee agreement",
            )
        if not all(
            (
                payload.scheduled_debit_at,
                payload.debit_window_start_at,
                payload.debit_window_end_at,
                payload.revocation_cutoff_at,
            )
        ) or payload.notice_business_days is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "The exact scheduled debit window and advance-notice terms are required",
            )
        if not (
            payload.debit_window_start_at
            <= payload.scheduled_debit_at
            <= payload.debit_window_end_at
        ) or payload.revocation_cutoff_at > payload.scheduled_debit_at:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "The scheduled debit and revocation cutoff do not match the authorized window",
            )
    if payload.private_plan_id:
        plan = (
            await db.execute(
                select(PrivateFundingPaymentPlan)
                .where(PrivateFundingPaymentPlan.id == payload.private_plan_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not plan or plan.application_profile_id != profile.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Private payment plan not found")
        if (
            source.owner_type != "business"
            or source.ach_class != "CCD"
            or not _business_account_attested(source)
        ):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Private-funding schedules require a business account")
        if payload.authorized_amount_cents < plan.total_amount_cents:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Authorized amount is below plan total")
    target_filter = (
        AchMandate.fee_obligation_id == payload.fee_obligation_id
        if payload.fee_obligation_id
        else AchMandate.private_plan_id == payload.private_plan_id
    )
    mandates = (
        await db.execute(
            select(AchMandate).where(target_filter).with_for_update()
        )
    ).scalars().all()
    active = [mandate for mandate in mandates if mandate.status == "active"]
    await cancel_unclaimed_transfer_intents(
        db,
        mandate_ids=[old.id for old in active],
        reason="mandate_superseded",
    )
    for old in active:
        old.status = "superseded"
        old.revoked_at = _now()
        old.terminated_at = old.revoked_at
        old.termination_reason = "Superseded by a new ACH authorization"
        from app.services import ach_fee_workflow

        await ach_fee_workflow.extend_mandate_retention(
            db, old, anchor=old.revoked_at
        )
    version = max((m.version for m in mandates), default=0) + 1
    source_snapshot = _funding_source_snapshot(source)
    row = AchMandate(
        application_profile_id=profile.id,
        funding_source_id=source.id,
        fee_obligation_id=payload.fee_obligation_id,
        private_plan_id=payload.private_plan_id,
        status="active",
        version=version,
        ach_class=source.ach_class,
        authorized_amount_cents=payload.authorized_amount_cents,
        authorization_text_version=payload.authorization_text_version,
        authorization_type=payload.authorization_type,
        authorization_text_snapshot=payload.authorization_text_snapshot,
        authorization_text_sha256=payload.authorization_text_sha256,
        obligation_sha256=payload.obligation_sha256,
        agreement_document_id=payload.agreement_document_id,
        agreement_sha256=payload.agreement_sha256,
        funding_source_snapshot=source_snapshot,
        funding_source_sha256=_canonical_hash(source_snapshot),
        typed_name=payload.typed_name,
        payer_name=payload.payer_name,
        payer_email=payload.payer_email,
        signature_sha256=payload.signature_sha256,
        certificate_s3_key=payload.certificate_s3_key,
        certificate_sha256=payload.certificate_sha256,
        certificate_bucket_file_id=payload.certificate_bucket_file_id,
        scheduled_debit_at=payload.scheduled_debit_at,
        debit_window_start_at=payload.debit_window_start_at,
        debit_window_end_at=payload.debit_window_end_at,
        notice_business_days=payload.notice_business_days,
        revocation_method=payload.revocation_method,
        revocation_cutoff_at=payload.revocation_cutoff_at,
        signer_session_id=payload.signer_session_id,
        ip_address=ip_address,
        user_agent=user_agent,
        signed_at=_now(),
        expires_at=payload.expires_at,
    )
    db.add(row)
    await db.flush()
    if payload.fee_obligation_id:
        obligation.status = "authorized"
        obligation.record_version += 1
    await log_event(
        db,
        profile_id=profile.id,
        actor_id=None,
        event_type="ach_mandate.signed",
        entity_type="ach_mandate",
        entity_id=row.id,
        summary="Client signed ACH authorization",
        metadata={"target": "fee" if payload.fee_obligation_id else "private_plan", "amount_cents": payload.authorized_amount_cents},
    )
    return row


async def _current_confirmation(db: AsyncSession, profile_id: UUID) -> ActualFundingConfirmation | None:
    return (
        await db.execute(
            select(ActualFundingConfirmation).where(
                ActualFundingConfirmation.application_profile_id == profile_id,
                ActualFundingConfirmation.superseded_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def fee_obligation_snapshot_blockers(
    db: AsyncSession, obligation: FeeObligation
) -> list[str]:
    """Return reasons an obligation no longer matches its signed economic snapshot."""

    blockers: list[str] = []
    if obligation.superseded_at is not None or obligation.status in {"cancelled", "superseded"}:
        blockers.append("The fee obligation is no longer current")
    if not await _fee_agreement_is_current(db, obligation):
        blockers.append("The signed fee agreement artifact is missing, changed, or no longer verified")
    if not await fee_lines_have_current_governing_agreements(db, obligation):
        blockers.append(
            "A fee component is missing its exact signed governing agreement"
        )

    profile = await db.get(ApplicationProfile, obligation.application_profile_id)
    if profile is None:
        blockers.append("Application file is unavailable")
    else:
        current = economics_snapshot(profile)
        if obligation.origination_fee_cents and (
            _cents(obligation.accepted_amount) != _cents(current.accepted_amount)
            or Decimal(str(obligation.origination_points or 0)).quantize(Decimal("0.0001"))
            != Decimal(str(current.origination_points or 0)).quantize(Decimal("0.0001"))
            or obligation.origination_fee_cents != current.origination_fee_cents
        ):
            blockers.append("Accepted amount or origination percentage changed; prepare a new fee obligation")
        if obligation.consulting_fee_cents != (
            _cents(current.consulting_fee) if obligation.consulting_fee_cents else 0
        ):
            blockers.append("Consulting fee changed; prepare a new fee obligation")

    allocation = await latest_allocation(db, obligation.application_profile_id)
    expected_allocation = {
        "client_ach_cents": obligation.client_ach_cents,
        "bank_direct_cents": obligation.bank_direct_cents,
        "external_cents": obligation.external_cents,
        "deferred_cents": obligation.deferred_cents,
        "waived_cents": obligation.waived_cents,
    }
    if (
        allocation is None
        or allocation.obligation_id != obligation.id
        or allocation.gross_fee_cents != obligation.gross_fee_cents
        or {
            key: int((allocation.allocation or {}).get(key, 0) or 0)
            for key in expected_allocation
        }
        != expected_allocation
        or allocation.origination_client_ach_cents
        != obligation.origination_client_ach_cents
        or allocation.consulting_client_ach_cents
        != obligation.consulting_client_ach_cents
    ):
        blockers.append("Fee allocation changed; prepare a new fee obligation")
    return blockers


async def fee_release_readiness(
    db: AsyncSession,
    obligation: FeeObligation,
    *,
    ignore_transfer_id: UUID | None = None,
) -> tuple[PaymentReadiness, PaymentFundingSource | None, AchMandate | None]:
    blockers: list[str] = []
    if obligation.client_ach_cents <= 0:
        blockers.append("No fee amount is allocated to client ACH")
    agreement_current = await _fee_agreement_is_current(db, obligation)
    snapshot_blockers = await fee_obligation_snapshot_blockers(db, obligation)
    blockers.extend(snapshot_blockers)
    if obligation.consulting_fee_cents and not obligation.consulting_milestone_confirmed_at:
        blockers.append("Consulting fee earning milestone is not confirmed")
    confirmation = await _current_confirmation(db, obligation.application_profile_id)
    if not confirmation:
        blockers.append("Actual funding is not confirmed")
    mandate = (
        await db.execute(
            select(AchMandate).where(
                AchMandate.fee_obligation_id == obligation.id,
                AchMandate.status == "active",
                AchMandate.revoked_at.is_(None),
            ).order_by(AchMandate.version.desc()).limit(1)
        )
    ).scalar_one_or_none()
    source = await db.get(PaymentFundingSource, mandate.funding_source_id) if mandate else None
    if not mandate:
        blockers.append("Client ACH authorization is required")
    elif mandate.expires_at and mandate.expires_at <= _now():
        blockers.append("Client ACH authorization expired")
    elif mandate.authorized_amount_cents != obligation.client_ach_cents:
        blockers.append("Authorization amount does not exactly match the client ACH allocation")
    elif mandate.obligation_sha256 != await fee_obligation_sha256(db, obligation):
        blockers.append("Client authorization does not match the current fee obligation")
    elif mandate.authorization_type != "one_time_business_ccd":
        blockers.append("Client authorization is not an exact one-time business CCD mandate")
    elif (
        mandate.agreement_document_id != obligation.agreement_document_id
        or mandate.agreement_sha256 != obligation.agreement_sha256
    ):
        blockers.append("Client authorization does not reference the exact signed agreement")
    if not source or source.status != "verified" or source.revoked_at:
        blockers.append("Verified payment bank account is required")
    elif source.owner_type != "business" or str(source.ach_class).upper() != "CCD":
        blockers.append("A verified business CCD payment account is required")
    elif not _business_account_attested(source):
        blockers.append("The client must attest that the connected account is an authorized business account")
    elif mandate and not _mandate_matches_source(mandate, source):
        blockers.append(
            "Payment account evidence or classification changed; client must authorize again"
        )
    notice = None
    notice_message = None
    notice_delivered = False
    proof_delivered = False
    if mandate:
        from app.services import ach_fee_workflow

        proof_message = await ach_fee_workflow.sync_mandate_proof_delivery(db, mandate)
        proof_status = (
            proof_message.status
            if proof_message is not None
            else (mandate.proof_copy_delivery_status or "")
        ).lower()
        proof_delivered = bool(
            mandate.proof_copy_sent_at
            and mandate.proof_copy_message_send_id
            and proof_status in {"sent", "delivered"}
        )
        if not proof_delivered:
            blockers.append(
                "The executed ACH authorization copy was not accepted for customer delivery"
            )
        notice = await ach_fee_workflow.current_debit_notice(db, obligation.id)
        notice_message = await ach_fee_workflow.sync_notice_delivery(db, notice)
        notice_delivery_status = (
            notice_message.status
            if notice_message is not None
            else (notice.delivery_status if notice is not None else None)
        )
        notice_delivered = bool(
            notice
            and notice.provider_accepted_at
            and notice_delivery_status in {"sent", "delivered"}
        )
        if notice and (
            notice.mandate_id != mandate.id
            or notice.amount_cents != obligation.client_ach_cents
            or notice.authorization_text_sha256 != mandate.authorization_text_sha256
            or notice.scheduled_debit_at != mandate.scheduled_debit_at
        ):
            blockers.append("Advance debit notice does not match the signed authorization")
        else:
            blockers.extend(
                ach_fee_workflow.notice_release_blockers(notice, notice_message)
            )
    transfer_filters = [PaymentTransfer.fee_obligation_id == obligation.id]
    if ignore_transfer_id is not None:
        current_transfer = await db.get(PaymentTransfer, ignore_transfer_id)
        transfer_filters.extend(
            [
                PaymentTransfer.id != ignore_transfer_id,
                PaymentTransfer.attempt_group_id
                != (
                    current_transfer.attempt_group_id
                    if current_transfer is not None
                    else obligation.id
                ),
            ]
        )
    existing = (
        await db.execute(
            select(PaymentTransfer.id).where(*transfer_filters).limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        blockers.append("A transfer already exists for this obligation")
    return PaymentReadiness(
        fee_obligation_current=(
            not snapshot_blockers
        ),
        agreement_signed=agreement_current,
        fee_agreement_signed=agreement_current,
        consulting_fee_earned=not obligation.consulting_fee_cents or bool(obligation.consulting_milestone_confirmed_at),
        client_authorized=bool(mandate and mandate.status == "active" and not mandate.revoked_at),
        funding_confirmed=confirmation is not None,
        amount_covered=bool(mandate and mandate.authorized_amount_cents == obligation.client_ach_cents),
        account_eligible=bool(
            source
            and source.status == "verified"
            and not source.revoked_at
            and source.owner_type == "business"
            and str(source.ach_class).upper() == "CCD"
            and _business_account_attested(source)
        ),
        authorization_proof_delivered=proof_delivered,
        debit_notice_delivered=notice_delivered,
        no_existing_claim=existing is None,
        ready_for_release=not blockers,
        blockers=blockers,
    ), source, mandate


async def prepare_fee_transfer(
    db: AsyncSession,
    *,
    obligation_id: UUID,
    amount_cents: int | None,
    idempotency_key: str,
    actor: User,
) -> PaymentTransfer:
    obligation = (
        await db.execute(select(FeeObligation).where(FeeObligation.id == obligation_id).with_for_update())
    ).scalar_one_or_none()
    if not obligation:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Fee obligation not found")
    replay = (
        await db.execute(select(PaymentTransfer).where(PaymentTransfer.idempotency_key == idempotency_key))
    ).scalar_one_or_none()
    if replay:
        if replay.fee_obligation_id != obligation.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "Idempotency key was used for another payment")
        return replay
    readiness, source, mandate = await fee_release_readiness(db, obligation)
    if not readiness.ready_for_release or source is None or mandate is None:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": "ACH collection is not ready", "blockers": readiness.blockers})
    amount = amount_cents or obligation.client_ach_cents
    if amount != obligation.client_ach_cents:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Release amount must match the authorized client ACH allocation")
    row = PaymentTransfer(
        application_profile_id=obligation.application_profile_id,
        fee_obligation_id=obligation.id,
        funding_source_id=source.id,
        mandate_id=mandate.id,
        idempotency_key=idempotency_key,
        attempt_no=1,
        status="authorizing",
        amount_cents=amount,
        ach_class=source.ach_class,
        released_by_user_id=actor.id,
    )
    db.add(row)
    obligation.status = "processing"
    obligation.record_version += 1
    await db.flush()
    from app.services import ach_fee_workflow

    notice = await ach_fee_workflow.current_debit_notice(db, obligation.id)
    if notice is None or notice.mandate_id != mandate.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The exact advance debit notice is unavailable",
        )
    notice.transfer_id = row.id
    notice.status = "consumed"
    await ach_fee_workflow.extend_mandate_retention(db, mandate, anchor=_now())
    await log_event(
        db,
        profile_id=obligation.application_profile_id,
        actor_id=actor.id,
        event_type="fee_transfer.released",
        entity_type="payment_transfer",
        entity_id=row.id,
        summary="Released origination-fee ACH collection",
        metadata={"amount_cents": amount},
    )
    return row


@dataclass(frozen=True)
class TransferDispatch:
    transfer_id: UUID
    application_profile_id: UUID
    amount_cents: int
    ach_class: str
    idempotency_key: str
    plaid_account_id: str
    access_token_ciphertext: str


def _transfer_is_locally_cancellable(transfer: PaymentTransfer) -> bool:
    return bool(
        transfer.status == "authorizing"
        and transfer.plaid_transfer_id is None
        and transfer.submitted_at is None
    )


def _same_intent_resume_eligible(transfer: PaymentTransfer) -> bool:
    code = (transfer.provider_failure_code or "").strip().upper()
    return bool(
        transfer.status == "action_required"
        and transfer.provider_failure_retryable
        and transfer.plaid_transfer_id is None
        and transfer.submitted_at is None
        and code not in PLAID_LINK_REPAIR_CODES
        and code not in RETRYABLE_RETURN_CODES
    )


async def cancel_unclaimed_transfer_intents(
    db: AsyncSession,
    *,
    mandate_ids: list[UUID] | None = None,
    funding_source_ids: list[UUID] | None = None,
    reason: str = "authorization_no_longer_valid",
) -> int:
    """Cancel local intents that have not begun provider handoff.

    The dispatcher changes an intent to ``submitting`` immediately before any
    Plaid call.  Only ``authorizing`` rows are therefore safe to cancel here;
    a submitting/provider-backed debit cannot be represented as recalled.
    """

    filters = []
    if mandate_ids:
        filters.append(PaymentTransfer.mandate_id.in_(mandate_ids))
    if funding_source_ids:
        filters.append(PaymentTransfer.funding_source_id.in_(funding_source_ids))
    if not filters:
        return 0
    rows = (
        await db.execute(
            select(PaymentTransfer)
            .where(
                or_(*filters),
                PaymentTransfer.status == "authorizing",
                PaymentTransfer.plaid_transfer_id.is_(None),
                PaymentTransfer.submitted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalars().all()
    now = _now()
    for transfer in rows:
        if not _transfer_is_locally_cancellable(transfer):
            continue
        transfer.status = "cancelled"
        transfer.cancelled_at = now
        transfer.claimed_at = None
        transfer.provider_failure_code = reason[:64]
        transfer.provider_failure_message = "Payment authorization changed before provider handoff"
        if transfer.installment_id:
            installment = await db.get(PaymentInstallment, transfer.installment_id)
            if installment and installment.status == "processing":
                installment.status = "action_required"
        if transfer.fee_obligation_id:
            obligation = await db.get(FeeObligation, transfer.fee_obligation_id)
            if obligation and obligation.status == "processing":
                obligation.status = "prepared"
                obligation.record_version += 1
    return sum(1 for transfer in rows if transfer.status == "cancelled")


async def transfer_dispatch_readiness(
    db: AsyncSession,
    transfer: PaymentTransfer,
) -> tuple[PaymentFundingSource | None, AchMandate | None, str | None]:
    """Revalidate authorization at the last database boundary before Plaid."""

    # Lock the profile and all financial snapshots while making the final
    # authorization decision.  The dispatcher commits ``submitting`` in the
    # same transaction before any provider call.
    await _lock_profile(db, transfer.application_profile_id)
    mandate = (
        await db.execute(
            select(AchMandate).where(AchMandate.id == transfer.mandate_id).with_for_update()
        )
    ).scalar_one_or_none()
    source = (
        await db.execute(
            select(PaymentFundingSource)
            .where(PaymentFundingSource.id == transfer.funding_source_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not _mandate_is_current(mandate):
        return source, mandate, "ACH mandate is revoked, expired, or inactive"
    if (
        source is None
        or source.application_profile_id != transfer.application_profile_id
        or source.status != "verified"
        or source.revoked_at is not None
        or not _mandate_matches_source(mandate, source)
    ):
        return source, mandate, "Payment funding source is revoked, inactive, or mismatched"
    if not source.plaid_account_id or not source.access_token_ciphertext:
        return source, mandate, "Payment funding source is incomplete"
    if transfer.fee_obligation_id:
        obligation = (
            await db.execute(
                select(FeeObligation)
                .where(FeeObligation.id == transfer.fee_obligation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            obligation is None
            or obligation.application_profile_id != transfer.application_profile_id
            or mandate.fee_obligation_id != obligation.id
            or obligation.superseded_at is not None
            or obligation.status == "cancelled"
        ):
            return source, mandate, "Fee obligation is no longer current"
        readiness, current_source, current_mandate = await fee_release_readiness(
            db,
            obligation,
            ignore_transfer_id=transfer.id,
        )
        if (
            not readiness.ready_for_release
            or current_source is None
            or current_mandate is None
            or current_source.id != source.id
            or current_mandate.id != mandate.id
        ):
            return source, mandate, "; ".join(readiness.blockers) or "Fee release is no longer authorized"
    elif transfer.installment_id:
        installment = await db.get(PaymentInstallment, transfer.installment_id)
        plan = await db.get(PrivateFundingPaymentPlan, installment.plan_id) if installment else None
        if (
            installment is None
            or plan is None
            or plan.application_profile_id != transfer.application_profile_id
            or plan.status != "active"
            or mandate.private_plan_id != plan.id
            or str(source.ach_class).upper() != "CCD"
            or str(mandate.ach_class).upper() != "CCD"
            or not _business_account_attested(source)
            or mandate.obligation_sha256 != plan.schedule_sha256
        ):
            return source, mandate, "Private-funding schedule is no longer active or authorized"
        private_blocker = await _private_plan_runtime_blocker(
            db,
            plan=plan,
            mandate=mandate,
            source=source,
        )
        if private_blocker:
            return source, mandate, private_blocker
    else:
        return source, mandate, "Transfer has no supported payment target"
    return source, mandate, None


async def resume_action_required_transfer(
    db: AsyncSession,
    *,
    transfer_id: UUID,
    profile_id: UUID,
    actor_id: UUID | None,
    bank_repair_completed: bool,
) -> PaymentTransfer:
    """Resume the same durable intent after a repair or transient provider error.

    Bank-Link repair re-runs authorization/create and therefore clears the
    prior non-approved authorization id. A staff resume for a transient
    transfer/create failure preserves the approved authorization id so Plaid's
    authorization-scoped idempotency remains in force.
    """

    await _lock_profile(db, profile_id)
    transfer = (
        await db.execute(
            select(PaymentTransfer)
            .where(PaymentTransfer.id == transfer_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not transfer or transfer.application_profile_id != profile_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment transfer not found")
    if transfer.status in {"authorizing", "submitting"}:
        return transfer
    if (
        transfer.status != "action_required"
        or transfer.plaid_transfer_id is not None
        or transfer.submitted_at is not None
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This payment intent cannot be resumed",
        )
    code = (transfer.provider_failure_code or "").strip().upper()
    repairable = code in PLAID_LINK_REPAIR_CODES
    if bank_repair_completed and not repairable:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This payment does not require a Plaid Link bank repair",
        )
    if not bank_repair_completed and repairable:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The client must complete the bank repair before this payment can resume",
        )
    if not bank_repair_completed and not _same_intent_resume_eligible(transfer):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This provider result is not eligible for a same-intent resume",
        )
    _source, _mandate, blocker = await transfer_dispatch_readiness(db, transfer)
    if blocker:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"message": "Payment intent is no longer ready", "blockers": [blocker]},
        )
    prior_authorization_id = transfer.plaid_authorization_id
    if bank_repair_completed:
        transfer.plaid_authorization_id = None
    transfer.status = "authorizing"
    transfer.claimed_at = None
    transfer.provider_status = None
    transfer.provider_failure_code = None
    transfer.provider_failure_message = None
    transfer.provider_failure_retryable = False
    if transfer.fee_obligation_id:
        obligation = await db.get(FeeObligation, transfer.fee_obligation_id)
        if obligation:
            obligation.status = "processing"
    if transfer.installment_id:
        installment = await db.get(PaymentInstallment, transfer.installment_id)
        if installment:
            installment.status = "processing"
    await log_event(
        db,
        profile_id=profile_id,
        actor_id=actor_id,
        event_type=(
            "transfer.bank_repair_completed"
            if bank_repair_completed
            else "transfer.submission_resumed"
        ),
        entity_type="payment_transfer",
        entity_id=transfer.id,
        summary=(
            "Client completed required bank repair; payment authorization will be retried"
            if bank_repair_completed
            else "Resumed the same payment intent after a transient provider failure"
        ),
        metadata={
            "prior_authorization_id": prior_authorization_id,
            "same_local_intent": True,
        },
    )
    return transfer


async def materialize_due_installment_transfers(
    db: AsyncSession, *, through_date: date | None = None, limit: int = 100
) -> int:
    """Turn due fixed installments into durable transfer intents.

    This function is safe to run repeatedly.  The installment row lock and the
    unique idempotency key ensure one intent per scheduled installment.
    """
    due = through_date or _firm_today()
    installments = (
        await db.execute(
            select(PaymentInstallment)
            .join(PrivateFundingPaymentPlan, PrivateFundingPaymentPlan.id == PaymentInstallment.plan_id)
            .where(
                PaymentInstallment.status == "scheduled",
                PaymentInstallment.due_date <= due,
                PrivateFundingPaymentPlan.status == "active",
            )
            .order_by(PaymentInstallment.due_date, PaymentInstallment.sequence)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    created = 0
    for installment in installments:
        plan = await db.get(PrivateFundingPaymentPlan, installment.plan_id)
        if not plan:
            continue
        mandate = (
            await db.execute(
                select(AchMandate).where(
                    AchMandate.private_plan_id == plan.id,
                    AchMandate.status == "active",
                    AchMandate.revoked_at.is_(None),
                    AchMandate.ach_class == "CCD",
                ).order_by(AchMandate.version.desc()).limit(1)
            )
        ).scalar_one_or_none()
        source = await db.get(PaymentFundingSource, mandate.funding_source_id) if mandate else None
        runtime_blocker = (
            await _private_plan_runtime_blocker(
                db,
                plan=plan,
                mandate=mandate,
                source=source,
            )
            if mandate and source
            else "A current business ACH authorization is required"
        )
        if (
            not _mandate_is_current(mandate)
            or not source
            or source.status != "verified"
            or source.revoked_at
            or runtime_blocker
        ):
            installment.status = "action_required"
            plan.status = "paused"
            plan.paused_at = _now()
            plan.record_version += 1
            continue
        idempotency_key = f"private:{plan.id}:v{plan.version}:installment:{installment.sequence}:attempt:1"
        existing = (
            await db.execute(
                select(PaymentTransfer.id).where(PaymentTransfer.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing:
            installment.status = "processing"
            continue
        db.add(PaymentTransfer(
            application_profile_id=plan.application_profile_id,
            installment_id=installment.id,
            funding_source_id=source.id,
            mandate_id=mandate.id,
            idempotency_key=idempotency_key,
            attempt_no=1,
            status="authorizing",
            amount_cents=installment.amount_cents,
            ach_class="CCD",
        ))
        installment.status = "processing"
        installment.claimed_at = _now()
        created += 1
    return created


async def retry_transfer(
    db: AsyncSession,
    *,
    transfer_id: UUID,
    idempotency_key: str,
    actor: User,
) -> PaymentTransfer:
    original = (
        await db.execute(select(PaymentTransfer).where(PaymentTransfer.id == transfer_id).with_for_update())
    ).scalar_one_or_none()
    if not original:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transfer not found")
    next_attempt = original.attempt_no + 1
    existing_attempt = (
        await db.execute(
            select(PaymentTransfer).where(
                PaymentTransfer.attempt_group_id == original.attempt_group_id,
                PaymentTransfer.attempt_no == next_attempt,
            )
        )
    ).scalar_one_or_none()
    if existing_attempt:
        return existing_attempt
    replay = (
        await db.execute(select(PaymentTransfer).where(PaymentTransfer.idempotency_key == idempotency_key))
    ).scalar_one_or_none()
    if replay:
        if replay.attempt_group_id != original.attempt_group_id:
            raise HTTPException(status.HTTP_409_CONFLICT, "Idempotency key was used for another payment")
        return replay
    code = (original.provider_failure_code or "").upper()
    if original.status not in {"failed", "returned", "action_required"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a failed or returned transfer can be retried")
    if original.fee_obligation_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A returned one-time fee debit requires a new exact mandate, date, and advance notice",
        )
    if code not in RETRYABLE_RETURN_CODES:
        raise HTTPException(status.HTTP_409_CONFLICT, "This return reason is not eligible for retry")
    if original.attempt_no >= 3:
        raise HTTPException(status.HTTP_409_CONFLICT, "Maximum of two manual retries reached")
    mandate = await db.get(AchMandate, original.mandate_id)
    source = await db.get(PaymentFundingSource, original.funding_source_id)
    if not _mandate_is_current(mandate):
        raise HTTPException(status.HTTP_409_CONFLICT, "A current ACH authorization is required")
    if not source or source.status != "verified" or source.revoked_at:
        raise HTTPException(status.HTTP_409_CONFLICT, "A verified bank account is required")
    if original.installment_id:
        installment = await db.get(PaymentInstallment, original.installment_id)
        plan = await db.get(PrivateFundingPaymentPlan, installment.plan_id) if installment else None
        if not installment or not plan or plan.status != "active" or mandate.private_plan_id != plan.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "Private-funding schedule is not active")
    row = PaymentTransfer(
        application_profile_id=original.application_profile_id,
        fee_obligation_id=original.fee_obligation_id,
        installment_id=original.installment_id,
        funding_source_id=source.id,
        mandate_id=mandate.id,
        attempt_group_id=original.attempt_group_id,
        retry_of_transfer_id=original.id,
        idempotency_key=idempotency_key,
        attempt_no=next_attempt,
        status="authorizing",
        amount_cents=original.amount_cents,
        ach_class=original.ach_class,
        released_by_user_id=actor.id,
    )
    db.add(row)
    if original.installment_id:
        installment = await db.get(PaymentInstallment, original.installment_id)
        if installment:
            installment.status = "processing"
    if original.fee_obligation_id:
        obligation = await db.get(FeeObligation, original.fee_obligation_id)
        if obligation:
            obligation.status = "processing"
            obligation.record_version += 1
    await db.flush()
    return row


async def claim_due_transfers(
    db: AsyncSession,
    limit: int = 25,
    stale_after: timedelta = timedelta(minutes=10),
    *,
    include_private: bool = True,
) -> list[TransferDispatch]:
    stale_before = _now() - stale_after
    # A process can exit after committing the durable ``submitting`` claim but
    # before persisting the provider result. Reclaim only old, unresolved
    # intents. The dispatcher resolves a persisted authorization at Plaid
    # before it repeats transfer/create, whose authorization id is itself the
    # provider's idempotency boundary.
    filters = [
        or_(
            PaymentTransfer.status == "authorizing",
            and_(
                PaymentTransfer.status == "submitting",
                PaymentTransfer.plaid_transfer_id.is_(None),
                PaymentTransfer.submitted_at.is_(None),
                PaymentTransfer.claimed_at.is_not(None),
                PaymentTransfer.claimed_at <= stale_before,
            ),
        )
    ]
    if not include_private:
        # Private installments remain queued while the phase-two feature is
        # dark and never enter the durable provider-handoff state.
        filters.append(PaymentTransfer.installment_id.is_(None))
    rows = (
        await db.execute(
            select(PaymentTransfer)
            .where(*filters)
            .order_by(PaymentTransfer.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    result: list[TransferDispatch] = []
    for transfer in rows:
        source, _mandate, blocker = await transfer_dispatch_readiness(db, transfer)
        if blocker or source is None:
            transfer.status = "cancelled"
            transfer.cancelled_at = _now()
            transfer.claimed_at = None
            transfer.provider_failure_code = "authorization_no_longer_valid"
            transfer.provider_failure_message = blocker or "Payment authorization is unavailable"
            if transfer.installment_id:
                installment = await db.get(PaymentInstallment, transfer.installment_id)
                if installment and installment.status == "processing":
                    installment.status = "action_required"
            if transfer.fee_obligation_id:
                obligation = await db.get(FeeObligation, transfer.fee_obligation_id)
                if obligation and obligation.status == "processing":
                    obligation.status = "prepared"
                    obligation.record_version += 1
            continue
        # This row-lock transaction is the revocation race boundary. Mandate,
        # source, obligation, and private-plan validity were rechecked above.
        # Revocation may cancel authorizing rows, but cannot claim a debit once
        # this durable submitting state commits.
        transfer.status = "submitting"
        transfer.claimed_at = _now()
        result.append(TransferDispatch(
            transfer_id=transfer.id,
            application_profile_id=transfer.application_profile_id,
            amount_cents=transfer.amount_cents,
            ach_class=transfer.ach_class.lower(),
            idempotency_key=transfer.idempotency_key,
            plaid_account_id=source.plaid_account_id,
            access_token_ciphertext=source.access_token_ciphertext,
        ))
    return result


async def record_transfer_submission(
    db: AsyncSession,
    *,
    transfer_id: UUID,
    plaid_authorization_id: str,
    plaid_transfer_id: str,
    provider_status: str,
    metadata: dict[str, Any] | None = None,
) -> PaymentTransfer:
    row = (
        await db.execute(select(PaymentTransfer).where(PaymentTransfer.id == transfer_id).with_for_update())
    ).scalar_one()
    if row.plaid_transfer_id and row.plaid_transfer_id != plaid_transfer_id:
        raise RuntimeError("Transfer already linked to a different Plaid transfer")
    row.plaid_authorization_id = plaid_authorization_id
    row.plaid_transfer_id = plaid_transfer_id
    row.provider_status = provider_status
    row.status = _normalized_transfer_status(provider_status)
    row.submitted_at = row.submitted_at or _now()
    row.provider_metadata = metadata or {}
    row.provider_failure_retryable = False
    return row


async def record_transfer_authorization(
    db: AsyncSession,
    *,
    transfer_id: UUID,
    plaid_authorization_id: str,
) -> PaymentTransfer:
    """Persist the provider id before transfer/create crosses the network."""

    row = (
        await db.execute(
            select(PaymentTransfer)
            .where(PaymentTransfer.id == transfer_id)
            .with_for_update()
        )
    ).scalar_one()
    if (
        row.plaid_authorization_id
        and row.plaid_authorization_id != plaid_authorization_id
    ):
        raise RuntimeError("Transfer already linked to a different Plaid authorization")
    row.plaid_authorization_id = plaid_authorization_id
    return row


async def record_transfer_submission_failure(
    db: AsyncSession,
    *,
    transfer_id: UUID,
    code: str,
    message: str,
    retryable: bool,
) -> PaymentTransfer:
    row = (
        await db.execute(select(PaymentTransfer).where(PaymentTransfer.id == transfer_id).with_for_update())
    ).scalar_one()
    if row.status in FINAL_TRANSFER_STATUSES:
        return row
    row.status = "action_required" if retryable else "failed"
    row.provider_failure_code = code[:64]
    row.provider_failure_message = message[:4000]
    row.provider_failure_retryable = retryable
    row.claimed_at = None
    if row.fee_obligation_id:
        obligation = await db.get(FeeObligation, row.fee_obligation_id)
        if obligation and obligation.status == "processing":
            obligation.status = "authorized"
            obligation.record_version += 1
    if row.installment_id:
        installment = await db.get(PaymentInstallment, row.installment_id)
        if installment:
            installment.status = "action_required"
            plan = await db.get(PrivateFundingPaymentPlan, installment.plan_id)
            if plan and plan.status == "active":
                plan.status = "paused"
                plan.paused_at = _now()
                plan.record_version += 1
    return row


def _normalized_transfer_status(value: str | None) -> str:
    raw = (value or "").strip().lower()
    aliases = {
        "pending": "pending",
        "posted": "posted",
        "settled": "settled",
        "funds_available": "funds_available",
        "failed": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "returned": "returned",
    }
    return aliases.get(raw, raw or "pending")


def _should_apply_transfer_status(current: str, new: str) -> bool:
    """Keep provider lifecycle monotonic while allowing a later ACH return."""

    current = _normalized_transfer_status(current)
    new = _normalized_transfer_status(new)
    if current == new:
        return True
    if current in {"returned", "cancelled", "failed"}:
        return False
    if current == "funds_available":
        return new == "returned"
    rank = {
        "authorizing": 0,
        "submitting": 1,
        "submitted": 2,
        "pending": 3,
        "posted": 4,
        "settled": 5,
        "funds_available": 6,
    }
    if new in {"returned", "cancelled", "failed"}:
        return True
    return rank.get(new, -1) >= rank.get(current, -1)


def _provider_failure_details(
    event: dict[str, Any], *, default_code: str
) -> tuple[str, str | None]:
    failure = event.get("failure_reason")
    if isinstance(failure, dict):
        code = failure.get("failure_code") or failure.get("code")
        message = failure.get("description") or failure.get("message")
    else:
        code = failure or event.get("return_code")
        message = event.get("failure_message")
    return str(code or default_code)[:64], str(message)[:4000] if message else None


def _derived_fee_collection_status(
    obligation: FeeObligation,
    *,
    ach_collected_cents: int,
    receipt_collected_cents: int,
    has_processing_transfer: bool,
    has_returned_transfer: bool,
) -> str:
    """Derive the obligation lifecycle from every collectible fee source."""

    net_collectible = max(
        0,
        obligation.gross_fee_cents
        - obligation.deferred_cents
        - obligation.waived_cents,
    )
    collected = max(0, ach_collected_cents) + max(0, receipt_collected_cents)
    if net_collectible > 0 and collected >= net_collectible:
        return "collected"
    if collected > 0:
        return "partially_collected"
    if has_processing_transfer:
        return "processing"
    if has_returned_transfer:
        return "returned"
    return obligation.status


async def _refresh_fee_obligation_collection_status(
    db: AsyncSession,
    obligation_id: UUID,
) -> FeeObligation | None:
    """Lock and refresh collection state after a transfer or receipt changes."""

    obligation = (
        await db.execute(
            select(FeeObligation)
            .where(FeeObligation.id == obligation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if obligation is None:
        return None
    ach_collected = int((await db.execute(
        select(func.coalesce(func.sum(PaymentTransfer.amount_cents), 0)).where(
            PaymentTransfer.fee_obligation_id == obligation.id,
            PaymentTransfer.status == COLLECTED_TRANSFER_STATUS,
        )
    )).scalar_one())
    receipt_collected = int((await db.execute(
        select(func.coalesce(func.sum(BankDirectFeeReceipt.amount_cents), 0)).where(
            BankDirectFeeReceipt.obligation_id == obligation.id,
        )
    )).scalar_one())
    transfer_states = set((await db.execute(
        select(PaymentTransfer.status).where(
            PaymentTransfer.fee_obligation_id == obligation.id,
        )
    )).scalars().all())
    derived = _derived_fee_collection_status(
        obligation,
        ach_collected_cents=ach_collected,
        receipt_collected_cents=receipt_collected,
        has_processing_transfer=bool(transfer_states & PROCESSING_TRANSFER_STATUSES),
        has_returned_transfer="returned" in transfer_states,
    )
    if obligation.status != derived:
        obligation.status = derived
        obligation.record_version += 1
    return obligation


async def apply_plaid_transfer_event(db: AsyncSession, event: dict[str, Any]) -> PaymentTransferEvent:
    event_id = str(event.get("event_id") or event.get("id") or "").strip()
    if not event_id:
        raise ValueError("Plaid transfer event has no event_id")
    existing = (
        await db.execute(select(PaymentTransferEvent).where(PaymentTransferEvent.plaid_event_id == event_id))
    ).scalar_one_or_none()
    if existing:
        return existing
    plaid_transfer_id = str(event.get("transfer_id") or "").strip() or None
    transfer = None
    if plaid_transfer_id:
        transfer = (
            await db.execute(
                select(PaymentTransfer)
                .where(PaymentTransfer.plaid_transfer_id == plaid_transfer_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
    event_type = str(event.get("event_type") or event.get("type") or "unknown")
    event_time_raw = event.get("timestamp") or event.get("event_timestamp")
    event_time = None
    if isinstance(event_time_raw, datetime):
        event_time = event_time_raw
    elif isinstance(event_time_raw, str):
        try:
            event_time = datetime.fromisoformat(event_time_raw.replace("Z", "+00:00"))
        except ValueError:
            event_time = None
    row = PaymentTransferEvent(
        transfer_id=transfer.id if transfer else None,
        plaid_event_id=event_id,
        plaid_transfer_id=plaid_transfer_id,
        event_type=event_type,
        event_timestamp=event_time,
        raw_event=event,
        created_at=_now(),
    )
    db.add(row)
    refund_id = str(event.get("refund_id") or "").strip()
    if refund_id:
        refund = (
            await db.execute(
                select(PaymentRefund)
                .where(PaymentRefund.plaid_refund_id == refund_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if refund:
            refund_status = event_type.removeprefix("refund.").lower()
            refund.status = refund_status
            if refund_status == "settled":
                refund.completed_at = event_time or _now()
            refund_transfer = await db.get(PaymentTransfer, refund.transfer_id)
            if refund_transfer:
                await log_event(
                    db,
                    profile_id=refund_transfer.application_profile_id,
                    actor_id=None,
                    event_type=f"refund.{refund_status}",
                    entity_type="payment_refund",
                    entity_id=refund.id,
                    summary=f"ACH refund {refund_status.replace('_', ' ')}",
                    metadata={"amount_cents": refund.amount_cents, "plaid_event_id": event_id},
                )
        # A refund event describes the outgoing credit. It must not regress
        # or overwrite the lifecycle state of the original collected debit.
        return row
    if transfer:
        new_status = _normalized_transfer_status(str(event.get("transfer_status") or event_type))
        if not _should_apply_transfer_status(transfer.status, new_status):
            return row
        transfer.status = new_status
        transfer.provider_status = new_status
        mandate = await db.get(AchMandate, transfer.mandate_id)
        if mandate is not None:
            from app.services import ach_fee_workflow

            await ach_fee_workflow.extend_mandate_retention(
                db, mandate, anchor=event_time or _now()
            )
        if new_status == "funds_available":
            transfer.funds_available_at = event_time or _now()
            if transfer.fee_obligation_id:
                await _refresh_fee_obligation_collection_status(
                    db, transfer.fee_obligation_id
                )
            if transfer.installment_id:
                installment = await db.get(PaymentInstallment, transfer.installment_id)
                if installment:
                    installment.status = "completed"
                    installment.completed_at = event_time or _now()
                    plan = await db.get(PrivateFundingPaymentPlan, installment.plan_id)
                    if plan:
                        next_installment = (
                            await db.execute(
                                select(PaymentInstallment)
                                .where(
                                    PaymentInstallment.plan_id == plan.id,
                                    PaymentInstallment.status == "scheduled",
                                )
                                .order_by(PaymentInstallment.sequence)
                                .limit(1)
                            )
                        ).scalar_one_or_none()
                        plan.next_due_date = next_installment.due_date if next_installment else None
                        if next_installment is None:
                            plan.status = "completed"
                        plan.record_version += 1
        elif new_status == "returned":
            transfer.returned_at = event_time or _now()
            (
                transfer.provider_failure_code,
                transfer.provider_failure_message,
            ) = _provider_failure_details(event, default_code="returned")
            if transfer.fee_obligation_id:
                await _refresh_fee_obligation_collection_status(
                    db, transfer.fee_obligation_id
                )
            if transfer.installment_id:
                installment = await db.get(PaymentInstallment, transfer.installment_id)
                if installment:
                    installment.status = "returned"
                    plan = await db.get(PrivateFundingPaymentPlan, installment.plan_id)
                    if plan:
                        plan.status = "paused"
                        plan.paused_at = event_time or _now()
                        plan.record_version += 1
        elif new_status == "cancelled":
            transfer.cancelled_at = event_time or _now()
            if transfer.installment_id:
                installment = await db.get(PaymentInstallment, transfer.installment_id)
                if installment:
                    installment.status = "cancelled"
        elif new_status == "failed":
            (
                transfer.provider_failure_code,
                transfer.provider_failure_message,
            ) = _provider_failure_details(event, default_code="failed")
        await log_event(
            db,
            profile_id=transfer.application_profile_id,
            actor_id=None,
            event_type=f"transfer.{new_status}",
            entity_type="payment_transfer",
            entity_id=transfer.id,
            summary=f"ACH transfer {new_status.replace('_', ' ')}",
            metadata={
                "amount_cents": transfer.amount_cents,
                "plaid_event_id": event_id,
                "provider_transfer_id": plaid_transfer_id,
            },
        )
    return row


async def get_plaid_transfer_cursor(db: AsyncSession, environment: str) -> str | None:
    row = await db.get(PlaidTransferCursor, environment)
    return row.cursor if row else None


async def set_plaid_transfer_cursor(
    db: AsyncSession,
    environment: str,
    cursor: str | None,
    *,
    error: str | None = None,
) -> PlaidTransferCursor:
    row = await db.get(PlaidTransferCursor, environment, with_for_update=True)
    if row is None:
        row = PlaidTransferCursor(environment=environment)
        db.add(row)
    row.cursor = cursor
    row.last_synced_at = _now()
    row.last_error = error
    return row


async def create_refund_intent(
    db: AsyncSession,
    *,
    transfer: PaymentTransfer,
    amount_cents: int,
    reason: str,
    idempotency_key: str,
    actor: User,
) -> PaymentRefund:
    locked_transfer = (
        await db.execute(
            select(PaymentTransfer)
            .where(PaymentTransfer.id == transfer.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked_transfer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transfer not found")
    transfer = locked_transfer
    if transfer.installment_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Private-schedule installment refunds are not supported by this fee-refund workflow",
        )
    normalized_reason = " ".join(reason.split()).casefold()
    operation_fingerprint = _canonical_hash({
        "transfer_id": str(transfer.id),
        "amount_cents": amount_cents,
        "reason": normalized_reason,
    })
    existing_operation = (
        await db.execute(
            select(PaymentRefund).where(
                PaymentRefund.transfer_id == transfer.id,
                PaymentRefund.operation_fingerprint == operation_fingerprint,
            )
        )
    ).scalar_one_or_none()
    if existing_operation:
        return existing_operation
    replay = (
        await db.execute(select(PaymentRefund).where(PaymentRefund.idempotency_key == idempotency_key))
    ).scalar_one_or_none()
    if replay:
        if replay.transfer_id != transfer.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "Idempotency key was used for another refund")
        return replay
    if transfer.status != "funds_available":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only funds-available transfers can be refunded")
    refunded = (
        await db.execute(
            select(func.coalesce(func.sum(PaymentRefund.amount_cents), 0)).where(
                PaymentRefund.transfer_id == transfer.id,
                PaymentRefund.status.not_in({"failed", "cancelled"}),
            )
        )
    ).scalar_one()
    if amount_cents > transfer.amount_cents - int(refunded):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Refund exceeds the unrefunded collected amount")
    refund_id = uuid4()
    row = PaymentRefund(
        id=refund_id,
        transfer_id=transfer.id,
        idempotency_key=idempotency_key,
        operation_fingerprint=operation_fingerprint,
        # Plaid limits idempotency keys to 50 characters.  Keep the caller's
        # full key for API replay and use this server-derived stable key at the
        # provider boundary.
        provider_idempotency_key=_provider_refund_idempotency_key(refund_id),
        amount_cents=amount_cents,
        status="pending",
        reason=reason,
        requested_by_user_id=actor.id,
    )
    db.add(row)
    await db.flush()
    await log_event(
        db,
        profile_id=transfer.application_profile_id,
        actor_id=actor.id,
        event_type="refund.requested",
        entity_type="payment_refund",
        entity_id=row.id,
        summary="Requested ACH refund",
        metadata={"amount_cents": amount_cents},
    )
    return row


async def record_bank_direct_receipt(
    db: AsyncSession,
    *,
    obligation: FeeObligation,
    payload: BankDirectReceiptCreate,
    actor: User,
) -> BankDirectFeeReceipt:
    locked_obligation = (
        await db.execute(
            select(FeeObligation)
            .where(FeeObligation.id == obligation.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked_obligation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Fee obligation not found")
    obligation = locked_obligation
    amount_cents = _cents(payload.amount)
    existing = (
        await db.execute(
            select(BankDirectFeeReceipt).where(
                BankDirectFeeReceipt.obligation_id == obligation.id,
                BankDirectFeeReceipt.reference == payload.reference,
            )
        )
    ).scalar_one_or_none()
    if existing:
        if (
            existing.amount_cents == amount_cents
            and existing.receipt_type == payload.receipt_type
            and existing.received_on == payload.received_on
            and existing.note == payload.note
            and existing.evidence_document_id == payload.evidence_document_id
        ):
            return existing
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This receipt reference is already recorded with different details",
        )
    allocation_cents = (
        obligation.bank_direct_cents
        if payload.receipt_type == "bank_direct"
        else obligation.external_cents
    )
    received = (
        await db.execute(
            select(func.coalesce(func.sum(BankDirectFeeReceipt.amount_cents), 0)).where(
                BankDirectFeeReceipt.obligation_id == obligation.id,
                BankDirectFeeReceipt.receipt_type == payload.receipt_type,
            )
        )
    ).scalar_one()
    if int(received) + amount_cents > allocation_cents:
        label = "bank-direct" if payload.receipt_type == "bank_direct" else "external/manual"
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Receipt exceeds {label} allocation",
        )
    row = BankDirectFeeReceipt(
        obligation_id=obligation.id,
        amount_cents=amount_cents,
        receipt_type=payload.receipt_type,
        received_on=payload.received_on,
        reference=payload.reference,
        note=payload.note,
        evidence_document_id=payload.evidence_document_id,
        recorded_by_user_id=actor.id,
    )
    db.add(row)
    await db.flush()
    await _refresh_fee_obligation_collection_status(db, obligation.id)
    await log_event(
        db,
        profile_id=obligation.application_profile_id,
        actor_id=actor.id,
        event_type=(
            "bank_direct.received"
            if payload.receipt_type == "bank_direct"
            else "external_manual.received"
        ),
        entity_type="fee_receipt",
        entity_id=row.id,
        summary=(
            "Recorded funding-source fee receipt"
            if payload.receipt_type == "bank_direct"
            else "Recorded external/manual fee receipt"
        ),
        metadata={
            "amount_cents": amount_cents,
            "reference": payload.reference,
            "receipt_type": payload.receipt_type,
        },
    )
    return row


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def banking_holidays(year: int) -> set[date]:
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _last_weekday(year, 5, 0),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 10, 0, 2),
        _observed(date(year, 11, 11)),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    if year >= 2021:
        holidays.add(_observed(date(year, 6, 19)))
    return holidays


def next_banking_day(day: date) -> date:
    candidate = day
    # New Year's Day can be observed on December 31 of the prior calendar
    # year. Include the following year's holiday set so that spillover is not
    # accidentally treated as a banking day.
    while candidate.weekday() >= 5 or candidate in (
        banking_holidays(candidate.year) | banking_holidays(candidate.year + 1)
    ):
        candidate += timedelta(days=1)
    return candidate


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def generate_schedule(payload: PrivatePlanCreate) -> list[tuple[date, int]]:
    today = _firm_today()
    if payload.first_due_date <= today:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "First payment date must be a future business date",
        )
    if any(day <= today for day in payload.custom_due_dates):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Every custom payment date must be in the future",
        )
    if payload.cadence == "custom":
        raw_dates = sorted(payload.custom_due_dates)
        if len(set(raw_dates)) != len(raw_dates):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Custom due dates must be unique")
    else:
        raw_dates = []
        current = payload.first_due_date
        for index in range(payload.installment_count):
            if payload.cadence == "business_daily":
                if index:
                    current += timedelta(days=1)
                current = next_banking_day(current)
            elif payload.cadence == "weekly":
                current = payload.first_due_date + timedelta(days=7 * index)
            elif payload.cadence == "biweekly":
                current = payload.first_due_date + timedelta(days=14 * index)
            elif payload.cadence == "monthly":
                current = _add_months(payload.first_due_date, index)
            elif payload.cadence == "semimonthly":
                if index == 0:
                    current = payload.first_due_date
                elif current.day < 15:
                    current = date(current.year, current.month, 15)
                else:
                    nxt = _add_months(date(current.year, current.month, 1), 1)
                    current = nxt
            raw_dates.append(current)
    dates = [next_banking_day(day) for day in raw_dates]
    if any(dates[index] <= dates[index - 1] for index in range(1, len(dates))):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Schedule produces duplicate or non-increasing banking dates")
    total_amount_cents = _cents(payload.total_amount)
    base, remainder = divmod(total_amount_cents, payload.installment_count)
    if base <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Installment amount must be at least one cent")
    amounts = [base] * payload.installment_count
    amounts[-1] += remainder
    return list(zip(dates, amounts, strict=True))


def _term_sheet_schedule(
    sheet: ProductionTermSheet,
    payload: PrivatePlanCreate,
) -> tuple[list[tuple[date, int]], dict[str, Any]]:
    """Derive the fixed collection schedule from the exact current term sheet."""

    terms = term_structure.public_values(
        {
            "approved_amount": sheet.approved_amount,
            "term_months": sheet.term_months,
            "rate_pct": sheet.rate_pct,
            "monthly_debt_service": sheet.monthly_debt_service,
            "debt_service_is_level_payment": sheet.debt_service_is_level_payment,
            "facility_type": sheet.facility_type,
            "funding_party_kind": sheet.funding_party_kind,
            "extra": sheet.extra or {},
        }
    )
    repayment = str(terms.get("repayment_structure") or "")
    if repayment == "interest_only_then_amortizing":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Step-up repayment terms are not eligible for fixed ACH schedules",
        )
    if terms.get("rate_structure") != "fixed" and not terms.get(
        "lender_payment_override"
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Variable-payment terms are not eligible for fixed ACH schedules",
        )
    frequency = str(terms.get("payment_frequency") or "")
    cadence_map = {
        "daily": "business_daily",
        "weekly": "weekly",
        "biweekly": "biweekly",
        "monthly": "monthly",
    }
    expected_cadence = cadence_map.get(frequency)
    if frequency == "custom":
        custom_label = str(terms.get("custom_payment_frequency") or "").strip().lower()
        if custom_label.replace("-", "").replace(" ", "") in {
            "semimonthly",
            "twiceamonth",
        }:
            expected_cadence = "semimonthly"
    if expected_cadence is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The current term sheet does not contain a supported fixed payment cadence",
        )
    first_raw = terms.get("first_payment_date")
    try:
        expected_first = (
            first_raw
            if isinstance(first_raw, date)
            else date.fromisoformat(str(first_raw))
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The current term sheet needs a first payment date",
        ) from None
    expected_count = int(terms.get("payment_count") or 0)
    expected_total = _cents(terms.get("total_repayment"))
    expected_periodic = _cents(terms.get("periodic_payment"))
    if expected_count <= 0 or expected_total <= 0 or expected_periodic <= 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The current term sheet needs payment count, periodic payment, and total repayment",
        )
    mismatches: list[str] = []
    if payload.cadence != expected_cadence:
        mismatches.append(f"cadence must be {expected_cadence}")
    if payload.installment_count != expected_count:
        mismatches.append(f"payment count must be {expected_count}")
    if payload.first_due_date != expected_first:
        mismatches.append(f"first payment date must be {expected_first.isoformat()}")
    if _cents(payload.total_amount) != expected_total:
        mismatches.append(f"total repayment must be {expected_total / 100:.2f}")
    if payload.installment_amount is not None and _cents(payload.installment_amount) != expected_periodic:
        mismatches.append(f"periodic payment must be {expected_periodic / 100:.2f}")
    if payload.custom_due_dates:
        mismatches.append("custom dates must be recorded in a new Production Term Sheet")
    if mismatches:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "message": "Private payment schedule must exactly match the current Production Term Sheet",
                "mismatches": mismatches,
            },
        )
    dated = generate_schedule(payload)
    final_amount = expected_total - expected_periodic * (expected_count - 1)
    if final_amount <= 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Production Term Sheet payment totals do not form a valid fixed schedule",
        )
    amounts = [expected_periodic] * expected_count
    amounts[-1] = final_amount
    schedule = [
        (due, amounts[index]) for index, (due, _amount) in enumerate(dated)
    ]
    return schedule, {
        "repayment_structure": repayment,
        "payment_frequency": frequency,
        "periodic_payment_cents": expected_periodic,
        "payment_count": expected_count,
        "total_repayment_cents": expected_total,
        "first_payment_date": expected_first.isoformat(),
    }


def _private_funder_type(sheet: ProductionTermSheet) -> str:
    extra = sheet.extra if isinstance(sheet.extra, dict) else {}
    return str(extra.get("funder_type") or "").strip().lower().replace(" ", "_").replace("-", "_")


async def _executed_stage_two_package(
    db: AsyncSession,
    *,
    profile_id: UUID,
    sheet: ProductionTermSheet,
) -> ProductionPackage | None:
    if sheet.consumed_by_package_id is None:
        return None
    package = await db.get(ProductionPackage, sheet.consumed_by_package_id)
    if (
        package is None
        or package.profile_id != profile_id
        or package.stage != 2
        or package.term_sheet_id != sheet.id
        or package.status != "executed"
        or package.executed_at is None
        or package.frozen_revision_id is None
        or not package.executed_pdf_s3_key
        or not _is_sha256(package.executed_pdf_sha256)
    ):
        return None
    return package


async def _private_agreement_is_current(
    db: AsyncSession,
    *,
    plan: PrivateFundingPaymentPlan,
) -> bool:
    sheet = await db.get(ProductionTermSheet, plan.production_term_sheet_id)
    if sheet is None:
        return False
    package = await _executed_stage_two_package(
        db,
        profile_id=plan.application_profile_id,
        sheet=sheet,
    )
    return bool(
        package
        and package.id == plan.production_package_id
        and package.frozen_revision_id == plan.production_package_revision_id
        and str(package.executed_pdf_sha256 or "").lower()
        == str(plan.agreement_sha256 or "").lower()
        and package.executed_at == plan.agreement_executed_at
    )


def preview_private_term_sheet(sheet: ProductionTermSheet | None) -> PrivateSchedulePreview:
    """Return the exact fixed schedule derivable from the current term sheet."""

    if sheet is None:
        return PrivateSchedulePreview(blockers=["No current Production Term Sheet is available"])
    blockers: list[str] = []
    funder_type = _private_funder_type(sheet)
    if funder_type not in PRIVATE_FUNDER_TYPES:
        blockers.append("Term sheet is not classified as an eligible private funding source")
    terms = term_structure.public_values(
        {
            "approved_amount": sheet.approved_amount,
            "term_months": sheet.term_months,
            "rate_pct": sheet.rate_pct,
            "monthly_debt_service": sheet.monthly_debt_service,
            "debt_service_is_level_payment": sheet.debt_service_is_level_payment,
            "facility_type": sheet.facility_type,
            "funding_party_kind": sheet.funding_party_kind,
            "extra": sheet.extra or {},
        }
    )
    if str(terms.get("repayment_structure") or "") == "interest_only_then_amortizing":
        blockers.append("Step-up repayment terms cannot use a fixed ACH schedule")
    if terms.get("rate_structure") != "fixed" and not terms.get("lender_payment_override"):
        blockers.append("Variable-payment terms cannot use a fixed ACH schedule")
    frequency = str(terms.get("payment_frequency") or "")
    cadence_map = {
        "daily": "business_daily",
        "weekly": "weekly",
        "biweekly": "biweekly",
        "monthly": "monthly",
    }
    cadence = cadence_map.get(frequency)
    if frequency == "custom":
        label = str(terms.get("custom_payment_frequency") or "").strip().lower()
        if label.replace("-", "").replace(" ", "") in {"semimonthly", "twiceamonth"}:
            cadence = "semimonthly"
    if cadence is None:
        blockers.append("Term sheet needs a supported fixed payment frequency")
    try:
        first_raw = terms.get("first_payment_date")
        first_due = first_raw if isinstance(first_raw, date) else date.fromisoformat(str(first_raw))
    except (TypeError, ValueError):
        first_due = None
        blockers.append("Term sheet needs a first payment date")
    count = int(terms.get("payment_count") or 0)
    total_cents = _cents(terms.get("total_repayment"))
    periodic_cents = _cents(terms.get("periodic_payment"))
    if count <= 0:
        blockers.append("Term sheet needs a positive payment count")
    if total_cents <= 0:
        blockers.append("Term sheet needs total repayment")
    if periodic_cents <= 0:
        blockers.append("Term sheet needs a periodic payment amount")
    installments: list[dict[str, Any]] = []
    if not blockers and cadence and first_due:
        derived = PrivatePlanCreate(
            production_term_sheet_id=sheet.id,
            cadence=cadence,
            total_amount_cents=total_cents,
            installment_amount_cents=periodic_cents,
            installment_count=count,
            first_due_date=first_due,
            agreement_reference="preview-only",
        )
        try:
            dates = generate_schedule(derived)
            final_amount = total_cents - periodic_cents * (count - 1)
            if final_amount <= 0:
                blockers.append("Periodic payment and total repayment do not form a valid schedule")
            else:
                installments = [
                    {
                        "sequence": index,
                        "due_date": due,
                        "amount_cents": periodic_cents if index < count else final_amount,
                    }
                    for index, (due, _amount) in enumerate(dates, 1)
                ]
        except HTTPException as exc:
            blockers.append(str(exc.detail))
    return PrivateSchedulePreview(
        eligible=not blockers,
        blockers=blockers,
        production_term_sheet_id=sheet.id,
        production_term_sheet_version=sheet.version,
        funder_type=funder_type or None,
        funding_party_name=sheet.funding_party_name,
        creditor_name=sheet.funding_party_name,
        cadence=cadence,
        total_amount_cents=total_cents or None,
        installment_amount_cents=periodic_cents or None,
        installment_count=count or None,
        first_due_date=first_due,
        installments=installments,
    )


async def private_schedule_preview(
    db: AsyncSession,
    *,
    profile_id: UUID,
    production_term_sheet_id: UUID | None = None,
) -> PrivateSchedulePreview:
    if production_term_sheet_id:
        sheet = await db.get(ProductionTermSheet, production_term_sheet_id)
        if sheet is not None and sheet.profile_id != profile_id:
            sheet = None
    else:
        sheet = (
            await db.execute(
                select(ProductionTermSheet)
                .where(
                    ProductionTermSheet.profile_id == profile_id,
                    ProductionTermSheet.status == "current",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    if sheet is not None and sheet.status != "current":
        return PrivateSchedulePreview(
            production_term_sheet_id=sheet.id,
            production_term_sheet_version=sheet.version,
            blockers=["Selected Production Term Sheet is not current"],
        )
    preview = preview_private_term_sheet(sheet)
    if sheet is not None and preview.eligible:
        package = await _executed_stage_two_package(
            db,
            profile_id=profile_id,
            sheet=sheet,
        )
        if package is None:
            preview.eligible = False
            preview.blockers.append(
                "Execute the stage-two production agreement for this exact term sheet"
            )
    return preview


async def create_private_plan_from_term_sheet(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    payload: PrivatePlanFromTermSheetCreate,
    actor: User,
) -> PrivateFundingPaymentPlan:
    preview = await private_schedule_preview(
        db,
        profile_id=profile.id,
        production_term_sheet_id=payload.production_term_sheet_id,
    )
    if not preview.eligible:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"message": "Current term sheet cannot produce a fixed schedule", "blockers": preview.blockers},
        )
    derived = PrivatePlanCreate(
        production_term_sheet_id=preview.production_term_sheet_id,
        creditor_name=preview.creditor_name,
        cadence=preview.cadence,
        total_amount_cents=preview.total_amount_cents,
        installment_amount_cents=preview.installment_amount_cents,
        installment_count=preview.installment_count,
        first_due_date=preview.first_due_date,
        servicing_authority_id=payload.servicing_authority_id,
        agreement_reference=payload.agreement_reference,
        servicing_authority_reference=payload.servicing_authority_reference,
        reason=payload.reason,
    )
    return await create_private_plan(db, profile=profile, payload=derived, actor=actor)


async def _private_plan_runtime_blocker(
    db: AsyncSession,
    *,
    plan: PrivateFundingPaymentPlan,
    mandate: AchMandate,
    source: PaymentFundingSource,
) -> str | None:
    """Revalidate every fact that may authorize a future scheduled debit."""

    if plan.status != "active":
        return "Private-funding schedule is not active"
    if not _mandate_is_current(mandate) or mandate.private_plan_id != plan.id:
        return "Private-funding ACH mandate is no longer current"
    if mandate.obligation_sha256 != plan.schedule_sha256:
        return "Private-funding authorization does not match this schedule version"
    if (
        source.application_profile_id != plan.application_profile_id
        or source.status != "verified"
        or source.revoked_at is not None
        or source.owner_type != "business"
        or not _business_account_attested(source)
        or not _mandate_matches_source(mandate, source)
        or str(source.ach_class).upper() != "CCD"
        or str(mandate.ach_class).upper() != "CCD"
    ):
        return "Current verified business payment account is required"
    current_sheet = (
        await db.execute(
            select(ProductionTermSheet).where(
                ProductionTermSheet.profile_id == plan.application_profile_id,
                ProductionTermSheet.status == "current",
            )
        )
    ).scalar_one_or_none()
    if (
        current_sheet is None
        or current_sheet.id != plan.production_term_sheet_id
        or current_sheet.version != plan.production_term_sheet_version
    ):
        return "Production Term Sheet changed; replace and reauthorize the schedule"
    if not await _private_agreement_is_current(db, plan=plan):
        return "Executed stage-two production agreement changed or is unavailable"
    confirmation = await _current_confirmation(db, plan.application_profile_id)
    if confirmation is None or confirmation.id != plan.funding_confirmation_id:
        return "Actual funding confirmation changed; review the schedule"
    qc_names = {"qualified commercial", "qualified commercial llc"}
    is_external = (
        plan.funding_party_kind.strip().casefold() not in qc_names
        and plan.creditor_name.strip().casefold() not in qc_names
    )
    if is_external:
        authority = (
            await db.get(PaymentServicingAuthority, plan.servicing_authority_id)
            if plan.servicing_authority_id
            else None
        )
        if not _servicing_authority_is_effective(
            authority,
            profile_id=plan.application_profile_id,
            on_date=_firm_today(),
        ):
            return "Servicing authority is missing, inactive, or expired"
        if (
            authority.creditor_name.strip().casefold()
            != plan.creditor_name.strip().casefold()
            or authority.payee_name.strip().casefold()
            != str(plan.payee_name or "").strip().casefold()
            or authority.settlement_destination_ref.strip()
            != str(plan.settlement_destination_ref or "").strip()
        ):
            return "Servicing authority identity or settlement destination changed"
    return None


async def create_servicing_authority(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    payload: ServicingAuthorityCreate,
    actor: User,
) -> PaymentServicingAuthority:
    if payload.effective_to and payload.effective_to < payload.effective_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Authority end date precedes start date")
    row = PaymentServicingAuthority(
        application_profile_id=profile.id,
        **payload.model_dump(),
        created_by_user_id=actor.id,
    )
    db.add(row)
    await db.flush()
    return row


async def _supersede_private_plan(
    db: AsyncSession,
    *,
    plan: PrivateFundingPaymentPlan,
) -> None:
    """Stop an old schedule without pretending an in-flight debit was recalled."""

    mandates = (
        await db.execute(
            select(AchMandate)
            .where(
                AchMandate.private_plan_id == plan.id,
                AchMandate.status == "active",
            )
            .with_for_update()
        )
    ).scalars().all()
    await cancel_unclaimed_transfer_intents(
        db,
        mandate_ids=[mandate.id for mandate in mandates],
        reason="private_plan_superseded",
    )
    now = _now()
    for mandate in mandates:
        mandate.status = "superseded"
        mandate.revoked_at = now
    installments = (
        await db.execute(
            select(PaymentInstallment)
            .where(
                PaymentInstallment.plan_id == plan.id,
                PaymentInstallment.status.in_({"scheduled", "action_required", "processing"}),
            )
            .with_for_update()
        )
    ).scalars().all()
    for installment in installments:
        in_flight = (
            await db.execute(
                select(PaymentTransfer.id)
                .where(
                    PaymentTransfer.installment_id == installment.id,
                    PaymentTransfer.status.in_({"submitting", "pending", "posted", "settled"}),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if in_flight is None:
            installment.status = "cancelled"
    plan.status = "superseded"
    plan.cancelled_at = now
    plan.next_due_date = None
    plan.record_version += 1


async def create_private_plan(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    payload: PrivatePlanCreate,
    actor: User,
) -> PrivateFundingPaymentPlan:
    await _lock_profile(db, profile.id)
    if payload.production_term_sheet_id:
        sheet = await db.get(ProductionTermSheet, payload.production_term_sheet_id)
    else:
        sheet = (
            await db.execute(
                select(ProductionTermSheet).where(
                    ProductionTermSheet.profile_id == profile.id,
                    ProductionTermSheet.status == "current",
                ).limit(1)
            )
        ).scalar_one_or_none()
    if not sheet or sheet.profile_id != profile.id or sheet.status != "current":
        raise HTTPException(status.HTTP_409_CONFLICT, "The current Production Term Sheet is required")
    funder_type = _private_funder_type(sheet)
    if funder_type not in PRIVATE_FUNDER_TYPES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Production Term Sheet must explicitly classify an eligible private funding source",
        )
    package = await _executed_stage_two_package(
        db,
        profile_id=profile.id,
        sheet=sheet,
    )
    if package is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Execute the stage-two production agreement for this exact term sheet before preparing payments",
        )
    agreement_reference = f"production-package:{package.id}"
    agreement_sha256 = str(package.executed_pdf_sha256).lower()
    creditor_name = (payload.creditor_name or sheet.funding_party_name).strip()
    authority = None
    payee_name = payload.payee_name.strip() if payload.payee_name else None
    settlement_destination_ref = (
        payload.settlement_destination_ref.strip()
        if payload.settlement_destination_ref
        else None
    )
    if payload.servicing_authority_id:
        authority = await db.get(PaymentServicingAuthority, payload.servicing_authority_id)
        if not authority or authority.application_profile_id != profile.id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Servicing authority must belong to this application file",
            )
        if authority.creditor_name.strip().casefold() != creditor_name.casefold():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Servicing authority creditor must match the payment schedule creditor",
            )
        if payee_name and authority.payee_name.strip().casefold() != payee_name.casefold():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Servicing authority payee does not match the payment schedule",
            )
        if (
            settlement_destination_ref
            and authority.settlement_destination_ref.strip()
            != settlement_destination_ref
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Servicing authority settlement destination does not match the payment schedule",
            )
        payee_name = authority.payee_name
        settlement_destination_ref = authority.settlement_destination_ref
    confirmation = await _current_confirmation(db, profile.id)
    schedule, term_schedule = _term_sheet_schedule(sheet, payload)
    latest = (
        await db.execute(
            select(PrivateFundingPaymentPlan)
            .where(PrivateFundingPaymentPlan.application_profile_id == profile.id)
            .order_by(PrivateFundingPaymentPlan.version.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    version = (latest.version + 1) if latest else 1
    snapshot = {
        "funder_type": funder_type,
        "funding_party_name": sheet.funding_party_name,
        "funding_party_kind": sheet.funding_party_kind,
        "production_term_sheet_id": str(sheet.id),
        "production_term_sheet_version": sheet.version,
        "term_schedule": term_schedule,
        "cadence": payload.cadence,
        "installments": [{"due_date": str(due), "amount_cents": amount} for due, amount in schedule],
        "reason": payload.reason,
        "agreement_reference": agreement_reference,
        "agreement_package_id": str(package.id),
        "agreement_package_revision_id": str(package.frozen_revision_id),
        "agreement_sha256": agreement_sha256,
        "agreement_executed_at": package.executed_at.isoformat(),
        "servicing_authority_reference": payload.servicing_authority_reference,
        "creditor_name": creditor_name,
        "payee_name": payee_name,
        "settlement_destination_ref": settlement_destination_ref,
    }
    schedule_sha256 = _canonical_hash(snapshot)
    if latest and latest.status in {"draft", "authorized", "active", "paused"} and (
        latest.production_term_sheet_id == sheet.id
        and latest.production_term_sheet_version == sheet.version
        and latest.schedule_sha256 == schedule_sha256
        and latest.agreement_reference == agreement_reference
        and latest.production_package_id == package.id
        and latest.production_package_revision_id == package.frozen_revision_id
        and latest.agreement_sha256 == agreement_sha256
        and latest.servicing_authority_id == payload.servicing_authority_id
        and latest.creditor_name == creditor_name
        and latest.payee_name == payee_name
        and latest.settlement_destination_ref == settlement_destination_ref
    ):
        return latest
    live_plans = (
        await db.execute(
            select(PrivateFundingPaymentPlan)
            .where(
                PrivateFundingPaymentPlan.application_profile_id == profile.id,
                PrivateFundingPaymentPlan.status.in_({"draft", "authorized", "active", "paused"}),
            )
            .with_for_update()
        )
    ).scalars().all()
    for prior in live_plans:
        await _supersede_private_plan(db, plan=prior)
    row = PrivateFundingPaymentPlan(
        application_profile_id=profile.id,
        client_id=profile.client_id,
        loan_id=profile.loan_id,
        production_term_sheet_id=sheet.id,
        production_term_sheet_version=sheet.version,
        production_package_id=package.id,
        production_package_revision_id=package.frozen_revision_id,
        agreement_sha256=agreement_sha256,
        agreement_executed_at=package.executed_at,
        funding_party_kind=sheet.funding_party_kind,
        creditor_name=creditor_name,
        payee_name=payee_name,
        settlement_destination_ref=settlement_destination_ref,
        agreement_reference=agreement_reference,
        funding_confirmation_id=confirmation.id if confirmation else None,
        servicing_authority_id=payload.servicing_authority_id,
        version=version,
        status="draft",
        cadence=payload.cadence,
        total_amount_cents=_cents(payload.total_amount),
        installment_count=payload.installment_count,
        first_due_date=schedule[0][0],
        next_due_date=schedule[0][0],
        schedule_snapshot=snapshot,
        schedule_sha256=schedule_sha256,
        supersedes_id=latest.id if latest else None,
        created_by_user_id=actor.id,
    )
    db.add(row)
    await db.flush()
    for sequence, (due, amount) in enumerate(schedule, 1):
        db.add(PaymentInstallment(plan_id=row.id, sequence=sequence, due_date=due, amount_cents=amount))
    await log_event(
        db,
        profile_id=profile.id,
        actor_id=actor.id,
        event_type="private_plan.created",
        entity_type="private_funding_payment_plan",
        entity_id=row.id,
        summary=f"Created private-funding schedule v{version}",
        metadata={"total_amount_cents": _cents(payload.total_amount), "installment_count": payload.installment_count},
    )
    return row


async def activate_private_plan(
    db: AsyncSession,
    *,
    plan_id: UUID,
    expected_record_version: int,
    actor: User,
) -> PrivateFundingPaymentPlan:
    plan_profile_id = (
        await db.execute(
            select(PrivateFundingPaymentPlan.application_profile_id).where(
                PrivateFundingPaymentPlan.id == plan_id
            )
        )
    ).scalar_one_or_none()
    if plan_profile_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment plan not found")
    await _lock_profile(db, plan_profile_id)
    plan = (
        await db.execute(
            select(PrivateFundingPaymentPlan).where(PrivateFundingPaymentPlan.id == plan_id).with_for_update()
        )
    ).scalar_one_or_none()
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment plan not found")
    if plan.record_version != expected_record_version:
        raise HTTPException(status.HTTP_409_CONFLICT, "Payment plan changed; reload before activating")
    if plan.status not in {"draft", "authorized", "paused"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "Payment plan cannot be activated from its current status")
    confirmation = await _current_confirmation(db, plan.application_profile_id)
    if not confirmation:
        raise HTTPException(status.HTTP_409_CONFLICT, "Actual funding confirmation is required")
    if plan.funding_confirmation_id != confirmation.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Actual funding confirmation changed; replace and reauthorize the schedule",
        )
    current_sheet = await db.get(ProductionTermSheet, plan.production_term_sheet_id)
    if (
        not current_sheet
        or current_sheet.status != "current"
        or current_sheet.version != plan.production_term_sheet_version
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "The payment schedule is not based on the exact current term sheet")
    if not await _private_agreement_is_current(db, plan=plan):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The executed stage-two production agreement changed or is unavailable",
        )
    qc_names = {"qualified commercial", "qualified commercial llc"}
    is_external = (
        plan.funding_party_kind.strip().lower() not in qc_names
        and plan.creditor_name.strip().lower() not in qc_names
    )
    authority = await db.get(PaymentServicingAuthority, plan.servicing_authority_id) if plan.servicing_authority_id else None
    if is_external and not _servicing_authority_is_effective(
        authority,
        profile_id=plan.application_profile_id,
        on_date=_firm_today(),
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "Active servicing authority is required for an external private funder")
    if is_external and (
        authority.creditor_name.strip().casefold() != plan.creditor_name.strip().casefold()
        or authority.payee_name.strip().casefold()
        != str(plan.payee_name or "").strip().casefold()
        or authority.settlement_destination_ref.strip()
        != str(plan.settlement_destination_ref or "").strip()
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Servicing authority creditor, payee, or settlement destination changed",
        )
    mandate = (
        await db.execute(
            select(AchMandate).where(
                AchMandate.private_plan_id == plan.id,
                AchMandate.status == "active",
                AchMandate.revoked_at.is_(None),
                AchMandate.ach_class == "CCD",
            ).order_by(AchMandate.version.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if (
        not _mandate_is_current(mandate)
        or mandate.authorized_amount_cents < plan.total_amount_cents
        or mandate.obligation_sha256 != plan.schedule_sha256
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "Valid business ACH mandate is required")
    source = await db.get(PaymentFundingSource, mandate.funding_source_id) if mandate else None
    if (
        source is None
        or source.application_profile_id != plan.application_profile_id
        or source.status != "verified"
        or source.revoked_at is not None
        or source.owner_type != "business"
        or str(source.ach_class).upper() != "CCD"
        or not _business_account_attested(source)
        or not _mandate_matches_source(mandate, source)
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The private schedule requires a current verified business payment account",
        )
    past_due = (
        await db.execute(
            select(PaymentInstallment.id)
            .where(
                PaymentInstallment.plan_id == plan.id,
                PaymentInstallment.status.in_({"scheduled", "action_required"}),
                PaymentInstallment.due_date <= _firm_today(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if past_due:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Private schedule contains a due or past-due unclaimed payment; create a future-dated replacement",
        )
    competing = (
        await db.execute(
            select(PrivateFundingPaymentPlan.id)
            .where(
                PrivateFundingPaymentPlan.application_profile_id == plan.application_profile_id,
                PrivateFundingPaymentPlan.status == "active",
                PrivateFundingPaymentPlan.id != plan.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if competing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Another private-funding schedule is already active")
    plan.funding_confirmation_id = confirmation.id
    plan.status = "active"
    plan.record_version += 1
    plan.activated_at = _now()
    plan.activated_by_user_id = actor.id
    plan.paused_at = None
    return plan


async def pause_private_plan(
    db: AsyncSession,
    *,
    plan_id: UUID,
    expected_record_version: int,
    actor: User,
    reason: str | None = None,
) -> PrivateFundingPaymentPlan:
    profile_id = (
        await db.execute(
            select(PrivateFundingPaymentPlan.application_profile_id).where(
                PrivateFundingPaymentPlan.id == plan_id
            )
        )
    ).scalar_one_or_none()
    if profile_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment plan not found")
    await _lock_profile(db, profile_id)
    plan = (
        await db.execute(
            select(PrivateFundingPaymentPlan)
            .where(PrivateFundingPaymentPlan.id == plan_id)
            .with_for_update()
        )
    ).scalar_one()
    if plan.record_version != expected_record_version:
        raise HTTPException(status.HTTP_409_CONFLICT, "Payment plan changed; reload before pausing")
    if plan.status == "paused":
        return plan
    if plan.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only an active payment plan can be paused")
    mandates = (
        await db.execute(
            select(AchMandate)
            .where(AchMandate.private_plan_id == plan.id, AchMandate.status == "active")
            .with_for_update()
        )
    ).scalars().all()
    await cancel_unclaimed_transfer_intents(
        db,
        mandate_ids=[mandate.id for mandate in mandates],
        reason="private_plan_paused",
    )
    plan.status = "paused"
    plan.paused_at = _now()
    plan.record_version += 1
    await log_event(
        db,
        profile_id=plan.application_profile_id,
        actor_id=actor.id,
        event_type="private_plan.paused",
        entity_type="private_funding_payment_plan",
        entity_id=plan.id,
        summary="Paused private-funding payment schedule",
        metadata={"reason": reason},
    )
    return plan


async def cancel_private_plan(
    db: AsyncSession,
    *,
    plan_id: UUID,
    expected_record_version: int,
    actor: User,
    reason: str | None = None,
) -> PrivateFundingPaymentPlan:
    profile_id = (
        await db.execute(
            select(PrivateFundingPaymentPlan.application_profile_id).where(
                PrivateFundingPaymentPlan.id == plan_id
            )
        )
    ).scalar_one_or_none()
    if profile_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment plan not found")
    await _lock_profile(db, profile_id)
    plan = (
        await db.execute(
            select(PrivateFundingPaymentPlan)
            .where(PrivateFundingPaymentPlan.id == plan_id)
            .with_for_update()
        )
    ).scalar_one()
    if plan.status == "cancelled":
        return plan
    if plan.record_version != expected_record_version:
        raise HTTPException(status.HTTP_409_CONFLICT, "Payment plan changed; reload before cancelling")
    if plan.status in {"completed", "superseded"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "Payment plan is already final")
    mandates = (
        await db.execute(
            select(AchMandate)
            .where(AchMandate.private_plan_id == plan.id, AchMandate.status == "active")
            .with_for_update()
        )
    ).scalars().all()
    await cancel_unclaimed_transfer_intents(
        db,
        mandate_ids=[mandate.id for mandate in mandates],
        reason="private_plan_cancelled",
    )
    now = _now()
    for mandate in mandates:
        mandate.status = "revoked"
        mandate.revoked_at = now
        mandate.revoked_by_user_id = actor.id
    installments = (
        await db.execute(
            select(PaymentInstallment)
            .where(
                PaymentInstallment.plan_id == plan.id,
                PaymentInstallment.status.in_({"scheduled", "action_required", "processing"}),
            )
            .with_for_update()
        )
    ).scalars().all()
    for installment in installments:
        in_flight = (
            await db.execute(
                select(PaymentTransfer.id)
                .where(
                    PaymentTransfer.installment_id == installment.id,
                    PaymentTransfer.status.in_({"submitting", "submitted", "pending", "posted", "settled"}),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if in_flight is None:
            installment.status = "cancelled"
    plan.status = "cancelled"
    plan.cancelled_at = now
    plan.next_due_date = None
    plan.record_version += 1
    await log_event(
        db,
        profile_id=plan.application_profile_id,
        actor_id=actor.id,
        event_type="private_plan.cancelled",
        entity_type="private_funding_payment_plan",
        entity_id=plan.id,
        summary="Permanently cancelled private-funding payment schedule",
        metadata={"reason": reason},
    )
    return plan


async def build_summary(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    permissions: PaymentPermissions,
) -> PaymentSummary:
    economics = economics_snapshot(profile)
    agreement_candidates = await agreement_document_candidates(db, profile)
    authorization_delivery = (
        await db.execute(
            select(ApplicationRoomDelivery)
            .where(
                ApplicationRoomDelivery.profile_id == profile.id,
                ApplicationRoomDelivery.action_kind == "payment_authorization_request",
            )
            .order_by(ApplicationRoomDelivery.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    allocation_row = await latest_allocation(db, profile.id)
    obligation = await current_obligation(db, profile.id)
    confirmation = await _current_confirmation(db, profile.id)
    sources = (
        await db.execute(
            select(PaymentFundingSource)
            .where(PaymentFundingSource.application_profile_id == profile.id)
            .order_by(PaymentFundingSource.verified_at.desc().nullslast())
        )
    ).scalars().all()
    funding_source = next((row for row in sources if row.status == "verified" and not row.revoked_at), None)
    mandates: list[AchMandate] = []
    transfers: list[PaymentTransfer] = []
    receipts: list[BankDirectFeeReceipt] = []
    readiness = PaymentReadiness()
    debit_notice: PaymentDebitNotice | None = None
    if obligation:
        mandates = (
            await db.execute(
                select(AchMandate).where(AchMandate.fee_obligation_id == obligation.id).order_by(AchMandate.version.desc())
            )
        ).scalars().all()
        transfers = (
            await db.execute(
                select(PaymentTransfer).where(PaymentTransfer.fee_obligation_id == obligation.id).order_by(PaymentTransfer.created_at.desc())
            )
        ).scalars().all()
        receipts = (
            await db.execute(
                select(BankDirectFeeReceipt).where(BankDirectFeeReceipt.obligation_id == obligation.id).order_by(BankDirectFeeReceipt.received_on.desc())
            )
        ).scalars().all()
        readiness, _, _ = await fee_release_readiness(db, obligation)
        from app.services import ach_fee_workflow

        debit_notice = await ach_fee_workflow.current_debit_notice(db, obligation.id)
        await ach_fee_workflow.sync_notice_delivery(db, debit_notice)
    plans = (
        await db.execute(
            select(PrivateFundingPaymentPlan)
            .where(PrivateFundingPaymentPlan.application_profile_id == profile.id)
            .order_by(PrivateFundingPaymentPlan.version.desc())
        )
    ).scalars().all()
    installments: list[PaymentInstallment] = []
    if plans:
        installments = (
            await db.execute(
                select(PaymentInstallment)
                .where(PaymentInstallment.plan_id == plans[0].id)
                .order_by(PaymentInstallment.sequence)
            )
        ).scalars().all()
    refunded = 0
    if transfers:
        refunded = int((await db.execute(
            select(func.coalesce(func.sum(PaymentRefund.amount_cents), 0)).where(
                PaymentRefund.transfer_id.in_([row.id for row in transfers]),
                PaymentRefund.status.in_(REFUND_COMPLETED_STATUSES),
            )
        )).scalar_one())
    ach_collected_gross = sum(row.amount_cents for row in transfers if row.status == COLLECTED_TRANSFER_STATUS)
    ach_collected_net = max(0, ach_collected_gross - refunded)
    processing = sum(row.amount_cents for row in transfers if row.status in PROCESSING_TRANSFER_STATUSES)
    bank_received = sum(
        row.amount_cents for row in receipts if row.receipt_type == "bank_direct"
    )
    external_received = sum(
        row.amount_cents for row in receipts if row.receipt_type == "external_manual"
    )
    gross = obligation.gross_fee_cents if obligation else 0
    deferred = obligation.deferred_cents if obligation else 0
    waived = obligation.waived_cents if obligation else 0
    net_collectible = max(0, gross - deferred - waived)
    collected = ach_collected_net + bank_received + external_received
    totals = PaymentSummaryTotals(
        bank_direct_expected=(obligation.bank_direct_cents if obligation else 0) / 100,
        bank_direct_received=bank_received / 100,
        external_received=external_received / 100,
        client_ach_target=(obligation.client_ach_cents if obligation else 0) / 100,
        scheduled=sum(item.amount_cents for item in installments if item.status == "scheduled") / 100,
        processing=processing / 100,
        collected=collected / 100,
        refunded=refunded / 100,
        waived=waived / 100,
        outstanding=max(0, net_collectible - collected) / 100,
    )

    lines: list[FeeObligationLine] = []
    obligation_response = None
    active_mandate = next((row for row in mandates if row.status == "active" and not row.revoked_at), None)
    displayed_mandate = active_mandate or (mandates[0] if mandates else None)
    displayed_mandate_current = await fee_mandate_is_current(
        db,
        mandate=displayed_mandate,
        obligation=obligation,
        source=funding_source,
        notice=debit_notice,
    )
    transfer = transfers[0] if transfers else None
    if obligation:
        lines = (
            await db.execute(
                select(FeeObligationLine)
                .where(FeeObligationLine.obligation_id == obligation.id)
                .order_by(FeeObligationLine.line_type)
            )
        ).scalars().all()
        governing_ids = {
            line.governing_agreement_document_id
            for line in lines
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
        line_responses: list[FeeObligationLineResponse] = []
        for line in sorted(lines, key=lambda item: 0 if item.line_type == "origination" else 1):
            governing_file = governing_files.get(line.governing_agreement_document_id)
            agreement_reference = (
                obligation.agreement_reference
                if line.line_type == "origination"
                else (
                    f"{governing_file.file_name} ({governing_file.id})"
                    if governing_file is not None
                    else "Consulting and Fee Schedule Addendum"
                )
            )
            line_responses.append(FeeObligationLineResponse(
                id=line.id,
                component=line.line_type,
                label="Origination fee" if line.line_type == "origination" else "Consulting fee",
                amount=line.amount_cents / 100,
                collection_amount=line.client_ach_cents / 100,
                agreement_reference=agreement_reference,
                governing_agreement_document_id=line.governing_agreement_document_id,
                governing_agreement_sha256=line.governing_agreement_sha256,
                agreement_component_scope=line.agreement_component_scope,
                earning_milestone=line.earning_milestone,
                agreement_verified=readiness.agreement_signed,
                earned=line.line_type == "origination" or bool(line.earned_confirmed_at),
            ))
        collected_for_obligation = ach_collected_net + bank_received + external_received
        obligation_status = obligation.status
        if readiness.ready_for_release and not transfer:
            obligation_status = "ready_for_release"
        obligation_response = FeeObligationResponse(
            id=obligation.id,
            version=obligation.record_version,
            status=obligation_status,
            amount=obligation.client_ach_cents / 100,
            authorized_amount=(active_mandate.authorized_amount_cents if active_mandate else 0) / 100,
            collected_amount=collected_for_obligation / 100,
            refunded_amount=refunded / 100,
            outstanding_amount=max(0, net_collectible - collected_for_obligation) / 100,
            lines=line_responses,
            agreement_ready=readiness.agreement_signed,
            prepared_at=obligation.created_at,
            authorization_sent_at=obligation.authorization_sent_at,
            released_at=transfer.created_at if transfer else None,
        )

    current_sheet = (
        await db.execute(
            select(ProductionTermSheet)
            .where(ProductionTermSheet.profile_id == profile.id, ProductionTermSheet.status == "current")
            .limit(1)
        )
    ).scalar_one_or_none()
    private_eligible = bool(current_sheet and _private_funder_type(current_sheet) in PRIVATE_FUNDER_TYPES)
    private_reason = None
    if not current_sheet:
        private_reason = "No current Production Term Sheet is available."
    elif not private_eligible:
        private_reason = "The current Production Term Sheet is not explicitly classified as private funding."

    private_plan_response = None
    authority_rows = (
        await db.execute(
            select(PaymentServicingAuthority)
            .where(PaymentServicingAuthority.application_profile_id == profile.id)
            .order_by(PaymentServicingAuthority.created_at.desc())
        )
    ).scalars().all()
    authority_responses = [
        ServicingAuthorityResponse(
            id=row.id,
            status=row.status,
            creditor_name=row.creditor_name,
            payee_name=row.payee_name,
            settlement_destination_ref=row.settlement_destination_ref,
            agreement_reference=row.agreement_reference,
            effective_from=row.effective_from,
            effective_to=row.effective_to,
        )
        for row in authority_rows
    ]
    if plans:
        plan = plans[0]
        plan_mandate = (
            await db.execute(
                select(AchMandate)
                .where(AchMandate.private_plan_id == plan.id)
                .order_by(AchMandate.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        authority = await db.get(PaymentServicingAuthority, plan.servicing_authority_id) if plan.servicing_authority_id else None
        plan_source = (
            await db.get(PaymentFundingSource, plan_mandate.funding_source_id)
            if plan_mandate
            else None
        )
        activation_blockers: list[str] = []
        private_agreement_ready = await _private_agreement_is_current(db, plan=plan)
        if not private_agreement_ready:
            activation_blockers.append(
                "Executed stage-two production agreement is missing or changed"
            )
        if not confirmation:
            activation_blockers.append("Actual funding confirmation is required")
        if not plan_mandate or not _mandate_is_current(plan_mandate):
            activation_blockers.append("Client business ACH authorization is required")
        elif plan_mandate.obligation_sha256 != plan.schedule_sha256:
            activation_blockers.append("Client authorization does not match this schedule")
        if (
            not plan_source
            or plan_source.status != "verified"
            or plan_source.revoked_at
            or plan_source.owner_type != "business"
            or str(plan_source.ach_class).upper() != "CCD"
            or not _business_account_attested(plan_source)
            or not plan_mandate
            or not _mandate_matches_source(plan_mandate, plan_source)
        ):
            activation_blockers.append("Verified business payment account is required")
        exact_current_sheet = bool(
            current_sheet
            and current_sheet.id == plan.production_term_sheet_id
            and current_sheet.version == plan.production_term_sheet_version
            and current_sheet.status == "current"
        )
        if not exact_current_sheet:
            activation_blockers.append(
                "Schedule must be replaced from the exact current Production Term Sheet"
            )
        qc_names = {"qualified commercial", "qualified commercial llc"}
        is_external = (
            plan.funding_party_kind.strip().casefold() not in qc_names
            and plan.creditor_name.strip().casefold() not in qc_names
        )
        authority_ready = bool(
            not is_external
            or (
                _servicing_authority_is_effective(
                    authority,
                    profile_id=plan.application_profile_id,
                    on_date=_firm_today(),
                )
                and authority.creditor_name.strip().casefold()
                == plan.creditor_name.strip().casefold()
                and authority.payee_name.strip().casefold()
                == str(plan.payee_name or "").strip().casefold()
                and authority.settlement_destination_ref.strip()
                == str(plan.settlement_destination_ref or "").strip()
            )
        )
        if not authority_ready:
            activation_blockers.append(
                "Active matching servicing authority is required for the external funder"
            )
        if any(
            installment.status in {"scheduled", "action_required"}
            and installment.due_date <= _firm_today()
            for installment in installments
        ):
            activation_blockers.append(
                "Schedule has a due or past-due unclaimed payment; create a future-dated replacement"
            )
        private_plan_response = PrivateFundingPlanResponse(
            id=plan.id,
            status=plan.status,
            creditor_name=plan.creditor_name,
            total_amount=plan.total_amount_cents / 100,
            installment_amount=(installments[0].amount_cents / 100) if installments else 0,
            cadence=plan.cadence,
            installment_count=plan.installment_count,
            next_due_at=plan.next_due_date,
            servicing_authority_ready=authority_ready,
            agreement_ready=private_agreement_ready,
            funding_confirmed=bool(plan.funding_confirmation_id or confirmation),
            mandate_status=("signed" if plan_mandate and plan_mandate.status == "active" else plan_mandate.status if plan_mandate else None),
            record_version=plan.record_version,
            production_term_sheet_id=plan.production_term_sheet_id,
            production_term_sheet_version=plan.production_term_sheet_version,
            production_package_id=plan.production_package_id,
            production_package_revision_id=plan.production_package_revision_id,
            agreement_sha256=plan.agreement_sha256,
            agreement_executed_at=plan.agreement_executed_at,
            schedule_sha256=plan.schedule_sha256,
            agreement_reference=plan.agreement_reference,
            payee_name=plan.payee_name,
            settlement_destination_ref=plan.settlement_destination_ref,
            funding_source_status=plan_source.status if plan_source else None,
            servicing_authority_id=plan.servicing_authority_id,
            servicing_authority_status=authority.status if authority else None,
            activation_blockers=activation_blockers,
            installments=installments,
        )

    transfer_response = None
    if transfer:
        transfer_refunded = int((await db.execute(
            select(func.coalesce(func.sum(PaymentRefund.amount_cents), 0)).where(
                PaymentRefund.transfer_id == transfer.id,
                PaymentRefund.status.in_(REFUND_RESERVED_STATUSES),
            )
        )).scalar_one())
        code = (transfer.provider_failure_code or "").upper()
        transfer_response = PaymentTransferResponse(
            id=transfer.id,
            amount=transfer.amount_cents / 100,
            status=transfer.status,
            provider_transfer_id=transfer.plaid_transfer_id,
            submitted_at=transfer.submitted_at,
            funds_available_at=transfer.funds_available_at,
            failed_at=transfer.returned_at if transfer.status in {"failed", "returned"} else None,
            return_code=transfer.provider_failure_code,
            return_reason=transfer.provider_failure_message,
            retry_eligible=transfer.status in {"failed", "returned", "action_required"} and code in RETRYABLE_RETURN_CODES and transfer.attempt_no < 3,
            resume_eligible=_same_intent_resume_eligible(transfer),
            retry_count=max(0, transfer.attempt_no - 1),
            refundable_amount=max(0, transfer.amount_cents - transfer_refunded) / 100 if transfer.status == "funds_available" else 0,
        )

    audit_rows = (
        await db.execute(
            select(PaymentAuditEvent)
            .where(PaymentAuditEvent.application_profile_id == profile.id)
            .order_by(PaymentAuditEvent.created_at.desc())
            .limit(100)
        )
    ).scalars().all()
    timeline = [PaymentTimelineItem(
        id=row.id,
        kind=row.event_type,
        title=row.summary,
        detail=None,
        amount=(float(row.metadata_json["amount_cents"]) / 100) if isinstance(row.metadata_json, dict) and row.metadata_json.get("amount_cents") is not None else None,
        tone="danger" if "failed" in row.event_type or "returned" in row.event_type else "success" if "collected" in row.event_type or "confirmed" in row.event_type else "info",
        actor_name=None,
        occurred_at=row.created_at,
    ) for row in audit_rows]

    display_name, _, _ = await _profile_identity(db, profile)
    from app.services import ach_fee_workflow, plaid_transfer

    agreement_state = await ach_fee_workflow.fee_agreement_state(db, profile)
    legal_gate = ach_fee_workflow.legal_approval_required()
    ach_enabled = bool(
        get_settings().payments_enabled and plaid_transfer.enabled() and not legal_gate
    )
    mandate_artifact = None
    if displayed_mandate and displayed_mandate.certificate_bucket_file_id:
        proof_file = await db.get(BucketFile, displayed_mandate.certificate_bucket_file_id)
        if proof_file and proof_file.deleted_at is None:
            try:
                proof_download_url = await ach_fee_workflow.verified_protected_download_url(
                    proof_file,
                    download_filename="QC-one-time-ACH-authorization.pdf",
                )
            except HTTPException:
                proof_download_url = None

            mandate_artifact = {
                "bucket_file_id": str(proof_file.id),
                "name": proof_file.file_name,
                "download_url": proof_download_url,
                "sha256": proof_file.content_hash,
                "retention_class": proof_file.retention_class,
                "protected_until": proof_file.protected_until,
                "legal_hold": proof_file.legal_hold,
            }
    return PaymentSummary(
        profile_id=profile.id,
        client_id=profile.client_id,
        loan_id=profile.loan_id,
        intake_id=profile.intake_id,
        display_name=display_name,
        approved_amount=float(economics.approved_amount) if economics.approved_amount is not None else None,
        accepted_amount=float(economics.accepted_amount) if economics.accepted_amount is not None else None,
        funded_amount=float(economics.funded_amount) if economics.funded_amount is not None else None,
        origination_fee_points=float(economics.origination_points) if economics.origination_points is not None else None,
        origination_fee=economics.origination_fee_cents / 100,
        consulting_fee=float(economics.consulting_fee or 0),
        gross_expected_fee=economics.gross_fee_cents / 100,
        allocation=allocation_response(allocation_row, current_gross_cents=economics.gross_fee_cents) if allocation_row else None,
        obligation=obligation_response,
        funding_confirmation=ActualFundingConfirmationResponse(
            id=confirmation.id,
            funded_at=confirmation.actual_funding_date,
            funded_amount=float(confirmation.actual_funded_amount),
            funding_party=confirmation.funding_party_name,
            transaction_reference=confirmation.funding_reference,
            source=confirmation.source,
            note=confirmation.note,
            confirmed_at=confirmation.confirmed_at,
        ) if confirmation else None,
        funding_source=PaymentFundingSourceResponse(
            id=funding_source.id,
            ownership_type=funding_source.owner_type,
            ach_class=funding_source.ach_class,
            institution_name=funding_source.institution_name,
            account_name=funding_source.account_name,
            account_mask=funding_source.account_mask,
            account_subtype=funding_source.account_subtype,
            status="connected" if funding_source.status == "verified" else funding_source.status,
            connected_at=funding_source.verified_at,
            business_account_attested=_business_account_attested(funding_source),
        ) if funding_source else None,
        mandate=AchMandateResponse(
            id=displayed_mandate.id,
            status="signed" if displayed_mandate_current else displayed_mandate.status,
            current=displayed_mandate_current,
            authorized_amount=displayed_mandate.authorized_amount_cents / 100,
            sec_code=displayed_mandate.ach_class,
            payer_name=displayed_mandate.payer_name,
            authorization_type=displayed_mandate.authorization_type,
            scheduled_debit_at=displayed_mandate.scheduled_debit_at,
            revocation_cutoff_at=displayed_mandate.revocation_cutoff_at,
            proof_copy_delivery_status=displayed_mandate.proof_copy_delivery_status,
            proof_copy_sent_at=displayed_mandate.proof_copy_sent_at,
            proof_copy_delivered_at=displayed_mandate.proof_copy_delivered_at,
            signed_at=displayed_mandate.signed_at,
            revoked_at=displayed_mandate.revoked_at,
            certificate_available=bool(displayed_mandate.certificate_s3_key),
            can_revoke=bool(
                displayed_mandate.status == "active"
                and displayed_mandate.revoked_at is None
                and not (transfer and (transfer.claimed_at or transfer.submitted_at))
            ),
            can_resend_proof=bool(displayed_mandate.certificate_s3_key),
            artifact=mandate_artifact,
        ) if displayed_mandate else None,
        authorization_request_delivery=({
            "status": authorization_delivery.status,
            "sent_at": (
                authorization_delivery.updated_at
                if authorization_delivery.status == "sent"
                else None
            ),
            "delivered_at": None,
            "failed_reason": (
                authorization_delivery.detail
                if authorization_delivery.status == "failed"
                else None
            ),
        } if authorization_delivery is not None else None),
        debit_notice=(
            PaymentDebitNoticeRead.model_validate(debit_notice)
            if debit_notice is not None
            else None
        ),
        transfer=transfer_response,
        fee_agreement=agreement_state,
        ach_authorization_enabled=ach_enabled,
        legal_approval_required=legal_gate,
        private_funding_eligible=private_eligible,
        private_funding_reason=private_reason,
        private_plan=private_plan_response,
        servicing_authorities=authority_responses,
        agreement_documents=agreement_candidates,
        timeline=timeline,
        totals=totals,
        readiness=readiness,
        permissions=permissions,
        server_now=_now(),
    )
