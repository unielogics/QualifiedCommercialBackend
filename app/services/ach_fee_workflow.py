"""Compliance workflow for one-time business ACH success-fee collection.

The ordinary payments service owns ledger math and provider intents.  This
module owns the documents around that movement: the deal-specific success-fee
agreement, the exact one-time CCD authorization proof, the separate advance
debit notice, and their retention/delivery evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, date, datetime, time, timedelta
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.models.application_profile import ApplicationProfile
from app.models.bucket import BucketDocumentSignature, BucketFile, BucketRequestedDocument
from app.models.message_send import MessageSend
from app.models.payments import (
    AchMandate,
    FeeObligation,
    FeeObligationLine,
    PaymentDebitNotice,
    PaymentFundingSource,
    PaymentTransfer,
)
from app.models.stored_signature import StoredSignature
from app.models.user import User
from app.schemas.payments import FeeObligationCreate
from app.services import ach_authorization, stored_signatures
from app.services import payment_authorization as private_storage
from app.services.messaging import outbox

FIRM_TIMEZONE = ZoneInfo("America/New_York")
SUCCESS_FEE_SIGNATURE_KIND = "success_fee_agreement"
CONSULTING_ADDENDUM_SIGNATURE_KIND = "contract_consulting_addendum"
SUCCESS_FEE_DOCUMENT_VERSION = "success-fee-2026-10-06-v1"
NOTICE_BUSINESS_DAYS = 2
RETENTION_CLASSES = {
    "ach_authorization_proof",
    "ach_debit_notice",
    "payment_agreement",
}
log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def two_year_deadline(anchor: datetime) -> datetime:
    """Two calendar years after ``anchor`` (Feb. 29 becomes Feb. 28)."""

    anchor = _utc(anchor)
    try:
        return anchor.replace(year=anchor.year + 2)
    except ValueError:
        return anchor.replace(year=anchor.year + 2, day=28)


def extend_retention(current: datetime | None, *, anchor: datetime) -> datetime:
    """Return a retention deadline that can only move later."""

    candidate = two_year_deadline(anchor)
    if current is None:
        return candidate
    return max(_utc(current), candidate)


def protect_bucket_file(
    file: BucketFile,
    *,
    retention_class: str,
    anchor: datetime,
    entity_type: str,
    entity_id: UUID,
    immutable_ref: str,
) -> None:
    if retention_class not in RETENTION_CLASSES:
        raise ValueError(f"Unknown ACH retention class: {retention_class}")
    file.retention_class = retention_class
    file.protected_until = extend_retention(file.protected_until, anchor=anchor)
    file.source_entity_type = entity_type
    file.source_entity_id = entity_id
    file.source_immutable_ref = immutable_ref[:160]


def _is_banking_day(day: date) -> bool:
    from app.services import payments as pay

    return day.weekday() < 5 and day not in (
        pay.banking_holidays(day.year - 1)
        | pay.banking_holidays(day.year)
        | pay.banking_holidays(day.year + 1)
    )


def add_business_days(value: datetime, days: int) -> datetime:
    """Advance by full US banking days while preserving local wall time."""

    local = _utc(value).astimezone(FIRM_TIMEZONE)
    remaining = max(0, days)
    while remaining:
        local += timedelta(days=1)
        if _is_banking_day(local.date()):
            remaining -= 1
    return local.astimezone(UTC)


def _previous_banking_day(day: date) -> date:
    candidate = day - timedelta(days=1)
    while not _is_banking_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def debit_window(scheduled_day: date) -> tuple[datetime, datetime, datetime, datetime]:
    if not _is_banking_day(scheduled_day):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Scheduled debit date must be a US banking day",
        )
    start_local = datetime.combine(scheduled_day, time.min, tzinfo=FIRM_TIMEZONE)
    end_local = datetime.combine(scheduled_day, time.max, tzinfo=FIRM_TIMEZONE)
    scheduled_local = datetime.combine(scheduled_day, time(hour=12), tzinfo=FIRM_TIMEZONE)
    cutoff_local = datetime.combine(
        _previous_banking_day(scheduled_day), time(hour=17), tzinfo=FIRM_TIMEZONE
    )
    return (
        scheduled_local.astimezone(UTC),
        start_local.astimezone(UTC),
        end_local.astimezone(UTC),
        cutoff_local.astimezone(UTC),
    )


def legal_approval_required() -> bool:
    """Counsel approval gates production Plaid only; sandbox stays usable."""

    from app.services import plaid_transfer

    try:
        return plaid_transfer.environment() == "production" and not bool(
            get_settings().payments_ach_legal_approved
        )
    except Exception:  # invalid provider configuration must never bypass counsel
        return True


def require_legal_approval() -> None:
    if legal_approval_required():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Production ACH is awaiting counsel approval",
        )


def require_fee_workflow_enabled() -> None:
    """Gate creation of new fee-agreement and ACH collection authority."""

    if not get_settings().payments_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ACH payments are not enabled",
        )
    require_legal_approval()


def require_authorization_terms_binding(
    *,
    submitted_sha256: str,
    submitted_scheduled_debit_at: datetime,
    exact_text: str,
    notice: PaymentDebitNotice,
) -> str:
    """Fail closed when the signed screen is not the current server snapshot."""

    digest = hashlib.sha256(exact_text.encode("utf-8")).hexdigest()
    if (
        submitted_sha256.strip().lower() != digest
        or _utc(submitted_scheduled_debit_at) != _utc(notice.scheduled_debit_at)
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Authorization terms or scheduled debit changed; refresh before signing",
        )
    return digest


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _agreement_text(snapshot: dict[str, Any]) -> str:
    lines = snapshot["fee_lines"]
    line_text = "\n".join(
        f"- {row['label']}: earned {row['earning_milestone']}; total {_money(row['amount_cents'])}; "
        f"one-time ACH portion {_money(row['client_ach_cents'])}; governing agreement "
        f"{row['governing_agreement']}."
        for row in lines
    )
    qc = snapshot["qc_countersignature"]
    return f"""
QUALIFIED COMMERCIAL LLC — DEAL-SPECIFIC SUCCESS FEE AGREEMENT

Agreement ID: {snapshot['agreement_id']}
Business: {snapshot['business_name']}
Client contact: {snapshot['client_name']}
Application file: {snapshot['profile_id']}

1. Exact deal economics
Accepted financing amount: {snapshot['accepted_amount_display']}
Origination fee percentage: {snapshot['origination_points_display']}
Fixed consulting fee: {_money(snapshot['consulting_fee_cents'])}

2. Earned success-fee components
{line_text}

Only the components listed above are covered. A fee is not earned merely by an
application, estimate, indication, approval, or unsigned term sheet. The
origination component is earned only upon actual funding. Any consulting
component is earned only upon the expressly confirmed milestone stated above.

3. Collection allocation
Gross covered fee: {_money(snapshot['gross_fee_cents'])}
Exact one-time business ACH amount: {_money(snapshot['client_ach_cents'])}
Bank-direct amount: {_money(snapshot['bank_direct_cents'])}
External/manual amount: {_money(snapshot['external_cents'])}
Deferred amount: {_money(snapshot['deferred_cents'])}
Waived amount: {_money(snapshot['waived_cents'])}

This agreement does not itself authorize a bank debit. Any ACH collection
requires a later, separate, exact one-time CCD authorization from an eligible
business account, an advance debit notice stating the date and amount, and a
separate release by authorized Qualified Commercial staff. It creates no
recurring or variable debit authority.

4. Relationship to other agreements and charges
This deal-specific agreement supplements the Master Commercial Finance
Consulting & Professional Services Agreement. It records the exact success-fee
economics for this financing and controls only if those exact economics conflict
with a more general description. Qualified Commercial's fee is separate from
all lender, broker, closing, legal, appraisal, filing, and other third-party
charges. Signing final loan documents alone does not earn the origination
component; actual financing funding is required.

5. Commercial account, payment security, and exclusions
The client represents that this is a commercial transaction and that the person
signing for the client is authorized to bind the business. If the client later
chooses ACH, the account must be a business account eligible for CCD entries.
Bank credentials are entered through Plaid; Qualified Commercial does not
receive or store the client's online-banking username or password. This
agreement creates no authority for recurring or variable charges, hourly fees,
retainers, late fees, unspecified future amounts, or percentage-of-revenue
debits. Revoking a later unsubmitted ACH authorization stops that payment method
only; it does not cancel a fee that has otherwise been earned and remains due.

6. Electronic records; governing law
The parties consent to electronic records and signatures. The executed PDF,
its SHA-256 digest, timestamps, signer identity, IP address, and user agent may
be retained as evidence. The client may request a copy at any time.
This agreement follows the New Jersey governing-law and jurisdiction provisions
of the Master Agreement.

Qualified Commercial LLC countersignature
[[QC_SIGNATURE]]
Name: {qc['typed_name']}
Title: {qc['title'] or 'Authorized Signatory'}
Email: {qc['email'] or 'support@qualifiedcommercial.com'}
Stored signature ID: {qc['stored_signature_id']}
Stored signature SHA-256: {qc['signature_sha256']}
Adopted at: {qc['adopted_at']}

Client acceptance follows through the secure application-room E-SIGN ceremony.
""".strip()


async def _current_signed_consulting_addendum(
    db: AsyncSession, profile: ApplicationProfile
) -> tuple[BucketFile, BucketDocumentSignature, BucketRequestedDocument]:
    """Return the newest signed, hash-verified consulting addendum."""

    if profile.primary_bucket_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Application file has no document bucket")
    rows = (
        await db.execute(
            select(BucketDocumentSignature, BucketRequestedDocument)
            .join(
                BucketRequestedDocument,
                BucketRequestedDocument.id
                == BucketDocumentSignature.requested_document_id,
            )
            .where(
                BucketRequestedDocument.bucket_id == profile.primary_bucket_id,
                BucketRequestedDocument.signature_kind
                == CONSULTING_ADDENDUM_SIGNATURE_KIND,
                BucketDocumentSignature.signed_at.is_not(None),
                BucketDocumentSignature.esign_consent.is_(True),
            )
            .order_by(BucketDocumentSignature.signed_at.desc())
        )
    ).all()
    for signature, requested in rows:
        file = (
            await db.get(BucketFile, signature.result_file_id)
            if signature.result_file_id
            else None
        )
        if (
            file is not None
            and file.bucket_id == profile.primary_bucket_id
            and file.deleted_at is None
            and len(str(file.content_hash or "")) == 64
        ):
            return file, signature, requested
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        "Sign the Consulting and Fee Schedule Addendum before including a consulting fee",
    )


async def consulting_addendum_from_snapshot(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    snapshot: dict[str, Any],
) -> BucketFile | None:
    """Resolve the exact signed addendum stored in a prepared agreement."""

    value = snapshot.get("consulting_addendum")
    if not isinstance(value, dict) or profile.primary_bucket_id is None:
        return None
    try:
        file_id = UUID(str(value.get("bucket_file_id")))
        signature_id = UUID(str(value.get("signature_id")))
    except (TypeError, ValueError):
        return None
    file = await db.get(BucketFile, file_id)
    signature = await db.get(BucketDocumentSignature, signature_id)
    requested = (
        await db.get(BucketRequestedDocument, signature.requested_document_id)
        if signature is not None
        else None
    )
    return (
        file
        if file is not None
        and file.bucket_id == profile.primary_bucket_id
        and file.deleted_at is None
        and str(file.content_hash or "").lower()
        == str(value.get("sha256") or "").lower()
        and signature is not None
        and signature.result_file_id == file.id
        and signature.signed_at is not None
        and signature.esign_consent
        and requested is not None
        and requested.signature_kind == CONSULTING_ADDENDUM_SIGNATURE_KIND
        else None
    )


async def _agreement_snapshot(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    agreement_id: UUID,
    include_origination_fee: bool,
    include_consulting_fee: bool,
    consulting_milestone_confirmed: bool,
    expected_allocation_version: int | None,
) -> tuple[dict[str, Any], StoredSignature, bytes]:
    from app.services import payments as pay

    allocation = await pay.latest_allocation(db, profile.id, for_update=True)
    if allocation is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Save a balanced fee allocation before preparing the agreement",
        )
    if expected_allocation_version and allocation.version != expected_allocation_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Fee allocation changed; reload before preparing the agreement",
        )
    economics = pay.economics_snapshot(profile)
    full_origination = economics.origination_fee_cents
    full_consulting = pay._cents(economics.consulting_fee)
    selected, origination_ach, consulting_ach = pay._select_obligation_allocation(
        allocation={key: int(value or 0) for key, value in allocation.allocation.items()},
        full_origination_cents=full_origination,
        full_consulting_cents=full_consulting,
        include_origination=include_origination_fee,
        include_consulting=include_consulting_fee,
        origination_client_ach_cents=allocation.origination_client_ach_cents,
        consulting_client_ach_cents=allocation.consulting_client_ach_cents,
    )
    if selected["client_ach_cents"] <= 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Allocate an amount to client ACH before preparing this agreement",
        )
    if include_consulting_fee and not consulting_milestone_confirmed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Confirm the consulting-fee earning milestone before including it",
        )
    consulting_addendum: dict[str, Any] | None = None
    if include_consulting_fee:
        addendum_file, addendum_signature, addendum_request = (
            await _current_signed_consulting_addendum(db, profile)
        )
        consulting_addendum = {
            "bucket_file_id": str(addendum_file.id),
            "requested_document_id": str(addendum_request.id),
            "signature_id": str(addendum_signature.id),
            "sha256": str(addendum_file.content_hash).lower(),
            "signed_at": addendum_signature.signed_at.isoformat(),
            "signature_kind": CONSULTING_ADDENDUM_SIGNATURE_KIND,
        }
    qc_signature = await stored_signatures.current_qc_signature(db)
    qc_png = stored_signatures.signature_png(qc_signature)
    if qc_signature is None or qc_png is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A current verified Qualified Commercial stored signature is required",
        )
    business_name, client_name, _ = await pay._profile_identity(db, profile)
    settings = get_settings()
    fee_lines: list[dict[str, Any]] = []
    if include_origination_fee and full_origination:
        fee_lines.append({
            "component": "origination",
            "label": "Origination success fee",
            "amount_cents": full_origination,
            "client_ach_cents": origination_ach,
            "earning_milestone": "actual financing funding",
            "governing_agreement": (
                f"Deal-Specific Success Fee Agreement {agreement_id}"
            ),
        })
    if include_consulting_fee and full_consulting:
        fee_lines.append({
            "component": "consulting",
            "label": "Consulting fee",
            "amount_cents": full_consulting,
            "client_ach_cents": consulting_ach,
            "earning_milestone": "the deal-specific consulting milestone confirmed by staff",
            "governing_agreement": (
                "Consulting and Fee Schedule Addendum "
                f"{consulting_addendum['bucket_file_id']}"
            ),
        })
    snapshot = {
        "workflow": SUCCESS_FEE_SIGNATURE_KIND,
        "document_version": SUCCESS_FEE_DOCUMENT_VERSION,
        "agreement_id": str(agreement_id),
        "profile_id": str(profile.id),
        "business_name": business_name or "Application business",
        "client_name": client_name or "Client",
        "accepted_amount": str(economics.accepted_amount or "0"),
        "accepted_amount_display": (
            f"${economics.accepted_amount:,.2f}"
            if economics.accepted_amount is not None
            else "Not recorded"
        ),
        "origination_points": str(economics.origination_points or "0"),
        "origination_points_display": f"{economics.origination_points or 0}%",
        "origination_fee_cents": full_origination if include_origination_fee else 0,
        "consulting_fee_cents": full_consulting if include_consulting_fee else 0,
        "gross_fee_cents": (
            (full_origination if include_origination_fee else 0)
            + (full_consulting if include_consulting_fee else 0)
        ),
        **selected,
        "origination_client_ach_cents": origination_ach,
        "consulting_client_ach_cents": consulting_ach,
        "include_origination_fee": include_origination_fee,
        "include_consulting_fee": include_consulting_fee,
        "consulting_milestone_confirmed": consulting_milestone_confirmed,
        "consulting_addendum": consulting_addendum,
        "allocation_id": str(allocation.id),
        "allocation_version": allocation.version,
        "allocation_sha256": allocation.allocation_sha256,
        "fee_lines": fee_lines,
        "qc_countersignature": {
            "stored_signature_id": str(qc_signature.id),
            "typed_name": (
                settings.payments_counter_signatory_name.strip()
                or qc_signature.typed_name
            ),
            "title": (
                settings.payments_counter_signatory_title.strip()
                or qc_signature.title
            ),
            "email": settings.payments_counter_signatory_email.strip() or None,
            "signature_sha256": qc_signature.signature_sha256,
            "adopted_at": qc_signature.adopted_at.isoformat(),
        },
    }
    return snapshot, qc_signature, qc_png


async def prepare_success_fee_agreement(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    actor: User,
    include_origination_fee: bool,
    include_consulting_fee: bool,
    consulting_milestone_confirmed: bool,
    expected_allocation_version: int | None,
    idempotency_key: str,
) -> BucketRequestedDocument:
    if profile.primary_bucket_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Application file has no document bucket")
    from app.services import payments as pay

    await pay._lock_profile(db, profile.id)
    replay = (
        await db.execute(
            select(BucketRequestedDocument)
            .where(
                BucketRequestedDocument.bucket_id == profile.primary_bucket_id,
                BucketRequestedDocument.signature_kind == SUCCESS_FEE_SIGNATURE_KIND,
            )
            .order_by(BucketRequestedDocument.created_at.desc())
        )
    ).scalars().all()
    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    for row in replay:
        source = row.requirement_source or {}
        if source.get("idempotency_sha256") == key_hash:
            return row

    agreement_id = uuid4()
    snapshot, _, _ = await _agreement_snapshot(
        db,
        profile=profile,
        agreement_id=agreement_id,
        include_origination_fee=include_origination_fee,
        include_consulting_fee=include_consulting_fee,
        consulting_milestone_confirmed=consulting_milestone_confirmed,
        expected_allocation_version=expected_allocation_version,
    )
    document_text = _agreement_text(snapshot)
    document_sha = hashlib.sha256(document_text.encode("utf-8")).hexdigest()
    for previous in replay:
        if previous.status != "uploaded":
            previous.status = "superseded"
            previous.required = False
    row = BucketRequestedDocument(
        id=agreement_id,
        bucket_id=profile.primary_bucket_id,
        name="Deal-Specific Success Fee Agreement",
        category="compliance",
        description="Review and electronically sign the exact success-fee agreement for this deal.",
        required=True,
        is_custom=True,
        requires_signature=True,
        signature_kind=SUCCESS_FEE_SIGNATURE_KIND,
        signature_document_text=document_text,
        requirement_key=f"payment_success_fee_agreement:{profile.id}",
        requirement_source={
            "workflow": SUCCESS_FEE_SIGNATURE_KIND,
            "idempotency_sha256": key_hash,
            "document_sha256": document_sha,
            "prepared_by_user_id": str(actor.id),
            "prepared_at": _now().isoformat(),
            "snapshot": snapshot,
        },
    )
    db.add(row)
    await pay.log_event(
        db,
        profile_id=profile.id,
        actor_id=actor.id,
        event_type="success_fee_agreement.prepared",
        entity_type="bucket_requested_document",
        entity_id=row.id,
        summary="Prepared deal-specific Success Fee Agreement",
        metadata={
            "document_sha256": document_sha,
            "client_ach_cents": snapshot["client_ach_cents"],
            "allocation_version": snapshot["allocation_version"],
        },
    )
    await db.flush()
    return row


async def prepared_agreement_is_current(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    requested: BucketRequestedDocument,
) -> bool:
    from app.services import payments as pay

    source = requested.requirement_source or {}
    snapshot = source.get("snapshot") if isinstance(source, dict) else None
    if not isinstance(snapshot, dict) or requested.status not in {"requested", "uploaded"}:
        return False
    if (
        str(snapshot.get("profile_id") or "") != str(profile.id)
        or str(snapshot.get("agreement_id") or "") != str(requested.id)
    ):
        return False
    try:
        rendered_text = _agreement_text(snapshot)
    except (KeyError, TypeError, ValueError):
        return False
    if rendered_text != (requested.signature_document_text or ""):
        return False
    if source.get("document_sha256") != hashlib.sha256(
        (requested.signature_document_text or "").encode("utf-8")
    ).hexdigest():
        return False
    current = pay.economics_snapshot(profile)
    if str(current.accepted_amount or "0") != str(snapshot.get("accepted_amount") or "0"):
        return False
    if str(current.origination_points or "0") != str(snapshot.get("origination_points") or "0"):
        return False
    allocation = await pay.latest_allocation(db, profile.id)
    obligation_id = source.get("obligation_id")
    if not obligation_id:
        return bool(
            allocation
            and allocation.version == snapshot.get("allocation_version")
            and allocation.allocation_sha256 == snapshot.get("allocation_sha256")
        )
    try:
        obligation = await db.get(FeeObligation, UUID(str(obligation_id)))
    except ValueError:
        return False
    signature = (
        await db.execute(
            select(BucketDocumentSignature)
            .where(BucketDocumentSignature.requested_document_id == requested.id)
            .order_by(BucketDocumentSignature.signed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    result_file = (
        await db.get(BucketFile, signature.result_file_id)
        if signature and signature.result_file_id
        else None
    )
    expected_allocation = {
        key: int(snapshot.get(key) or 0)
        for key in (
            "client_ach_cents",
            "bank_direct_cents",
            "external_cents",
            "deferred_cents",
            "waived_cents",
        )
    }
    lines = (
        await db.execute(
            select(FeeObligationLine).where(
                FeeObligationLine.obligation_id == obligation.id
            )
        )
    ).scalars().all() if obligation else []
    line_agreements_current = bool(
        obligation
        and await pay.fee_lines_have_current_governing_agreements(
            db, obligation, lines=lines
        )
    )
    return bool(
        obligation
        and obligation.application_profile_id == profile.id
        and obligation.superseded_at is None
        and obligation.status not in {"cancelled", "superseded"}
        and result_file
        and result_file.deleted_at is None
        and result_file.content_hash
        and result_file.content_hash == source.get("signed_pdf_sha256")
        and obligation.agreement_document_id == result_file.id
        and obligation.agreement_sha256 == result_file.content_hash
        and allocation
        and allocation.obligation_id == obligation.id
        and allocation.gross_fee_cents == int(snapshot.get("gross_fee_cents") or 0)
        and {
            key: int((allocation.allocation or {}).get(key, 0) or 0)
            for key in expected_allocation
        }
        == expected_allocation
        and allocation.origination_client_ach_cents
        == int(snapshot.get("origination_client_ach_cents") or 0)
        and allocation.consulting_client_ach_cents
        == int(snapshot.get("consulting_client_ach_cents") or 0)
        and line_agreements_current
    )


async def _success_fee_requested_document(
    db: AsyncSession, profile: ApplicationProfile
) -> BucketRequestedDocument | None:
    if profile.primary_bucket_id is None:
        return None
    return (
        await db.execute(
            select(BucketRequestedDocument)
            .where(
                BucketRequestedDocument.bucket_id == profile.primary_bucket_id,
                BucketRequestedDocument.signature_kind == SUCCESS_FEE_SIGNATURE_KIND,
                BucketRequestedDocument.status != "superseded",
            )
            .order_by(BucketRequestedDocument.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _success_fee_sign_route(profile: ApplicationProfile) -> str:
    return (
        "/dealer-os/public/room/{token}/sign"
        if profile.dealer_id is not None
        else "/application-profiles/public/room/{token}/sign"
    )


async def fee_agreement_state(db: AsyncSession, profile: ApplicationProfile) -> dict[str, Any] | None:
    requested = await _success_fee_requested_document(db, profile)
    if requested is None:
        return None
    signature = (
        await db.execute(
            select(BucketDocumentSignature)
            .where(BucketDocumentSignature.requested_document_id == requested.id)
            .order_by(BucketDocumentSignature.signed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    file = await db.get(BucketFile, signature.result_file_id) if signature and signature.result_file_id else None
    source = requested.requirement_source or {}
    signed = bool(signature and file and file.deleted_at is None)
    artifact_url = None
    if signed and file:
        try:
            artifact_url = await verified_protected_download_url(
                file,
                download_filename="QC-Deal-Specific-Success-Fee-Agreement.pdf",
            )
        except HTTPException:
            # Keep the Payments screen usable while withholding an artifact
            # whose bytes cannot be proven to match the signed record.
            artifact_url = None
    return {
        "id": str(requested.id),
        "requested_document_id": str(requested.id),
        "status": "signed" if signed else "awaiting_signature",
        "signed": signed,
        "agreement_reference": f"Deal-Specific Success Fee Agreement {requested.id}",
        "template_version": SUCCESS_FEE_DOCUMENT_VERSION,
        "sign_url": None,
        "sign_route": _success_fee_sign_route(profile),
        "document_version": signature.document_version if signature else SUCCESS_FEE_DOCUMENT_VERSION,
        "document_sha256": source.get("document_sha256"),
        "signed_pdf_sha256": file.content_hash if file else None,
        "certificate_file_id": str(file.id) if file else None,
        "signed_at": signature.signed_at if signature else None,
        "typed_name": signature.typed_name if signature else None,
        "prepared_at": source.get("prepared_at"),
        "signature_request_delivery_status": source.get(
            "signature_request_delivery_status"
        ),
        "signature_request_delivery_id": source.get("signature_request_delivery_id"),
        "proof_email_status": source.get("signed_copy_delivery_status"),
        "artifact": ({
            "bucket_file_id": str(file.id),
            "name": file.file_name,
            "download_url": artifact_url,
            "download_route": f"/buckets/admin/{file.bucket_id}/files/{file.id}/url",
            "sha256": file.content_hash,
            "retention_class": file.retention_class,
            "protected_until": file.protected_until,
            "legal_hold": file.legal_hold,
        } if file else None),
        "current": await prepared_agreement_is_current(
            db, profile=profile, requested=requested
        ),
    }


async def qc_signature_for_prepared_agreement(
    db: AsyncSession, requested: BucketRequestedDocument
) -> tuple[StoredSignature, bytes]:
    source = requested.requirement_source or {}
    snapshot = source.get("snapshot") if isinstance(source, dict) else None
    qc = snapshot.get("qc_countersignature") if isinstance(snapshot, dict) else None
    try:
        signature_id = UUID(str(qc.get("stored_signature_id")))
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Prepared agreement has no bound Qualified Commercial signature",
        ) from exc
    signature = await db.get(StoredSignature, signature_id)
    raw = stored_signatures.signature_png(signature)
    current = await stored_signatures.current_qc_signature(db)
    if (
        signature is None
        or raw is None
        or signature.subject_type != "qc"
        or signature.revoked_at is not None
        or current is None
        or current.id != signature.id
        or signature.signature_sha256 != qc.get("signature_sha256")
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The Qualified Commercial signature bound to this agreement is unavailable",
        )
    return signature, raw


async def finalize_success_fee_agreement(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    requested: BucketRequestedDocument,
    result_file: BucketFile,
) -> FeeObligation:
    """Turn the exact signed PDF into the governing fee obligation."""

    require_fee_workflow_enabled()
    from app.services import payments as pay

    source = dict(requested.requirement_source or {})
    if source.get("obligation_id"):
        try:
            existing = await db.get(FeeObligation, UUID(str(source["obligation_id"])))
        except ValueError:
            existing = None
        if existing is not None:
            return existing
    snapshot = source.get("snapshot")
    if not isinstance(snapshot, dict):
        raise HTTPException(status.HTTP_409_CONFLICT, "Agreement snapshot is unavailable")
    if not result_file.content_hash:
        raise HTTPException(status.HTTP_409_CONFLICT, "Signed agreement PDF is not hash verified")
    try:
        actor_id = UUID(str(source["prepared_by_user_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Agreement preparer is unavailable") from exc
    actor = await db.get(User, actor_id)
    if actor is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Agreement preparer is unavailable")
    payload = FeeObligationCreate(
        agreement_document_id=result_file.id,
        include_origination_fee=bool(snapshot.get("include_origination_fee")),
        include_consulting_fee=bool(snapshot.get("include_consulting_fee")),
        consulting_milestone_confirmed=bool(snapshot.get("consulting_milestone_confirmed")),
        client_ach_cents=int(snapshot.get("client_ach_cents") or 0),
        origination_client_ach_cents=int(snapshot.get("origination_client_ach_cents") or 0),
        consulting_client_ach_cents=int(snapshot.get("consulting_client_ach_cents") or 0),
        bank_direct_cents=int(snapshot.get("bank_direct_cents") or 0),
        external_cents=int(snapshot.get("external_cents") or 0),
        deferred_cents=int(snapshot.get("deferred_cents") or 0),
        waived_cents=int(snapshot.get("waived_cents") or 0),
        reason="Executed deal-specific Success Fee Agreement",
    )
    obligation = await pay.create_fee_obligation(
        db, profile=profile, payload=payload, actor=actor
    )
    lines = (
        await db.execute(
            select(FeeObligationLine).where(FeeObligationLine.obligation_id == obligation.id)
        )
    ).scalars().all()
    consulting_addendum = (
        await consulting_addendum_from_snapshot(db, profile=profile, snapshot=snapshot)
        if bool(snapshot.get("include_consulting_fee"))
        else None
    )
    if bool(snapshot.get("include_consulting_fee")) and consulting_addendum is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The signed Consulting and Fee Schedule Addendum changed or is unavailable",
        )
    milestone_by_component = {
        row["component"]: row.get("earning_milestone")
        for row in snapshot.get("fee_lines", [])
        if isinstance(row, dict) and row.get("component")
    }
    for line in lines:
        governing_file = (
            consulting_addendum if line.line_type == "consulting" else result_file
        )
        if governing_file is None or not governing_file.content_hash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"The governing agreement for the {line.line_type} fee is unavailable",
            )
        line.governing_agreement_document_id = governing_file.id
        line.governing_agreement_sha256 = governing_file.content_hash
        line.agreement_component_scope = line.line_type
        line.earning_milestone = milestone_by_component.get(line.line_type)
    protect_bucket_file(
        result_file,
        retention_class="payment_agreement",
        anchor=result_file.created_at or _now(),
        entity_type="fee_obligation",
        entity_id=obligation.id,
        immutable_ref=result_file.content_hash,
    )
    if consulting_addendum is not None and consulting_addendum.content_hash:
        protect_bucket_file(
            consulting_addendum,
            retention_class="payment_agreement",
            anchor=consulting_addendum.created_at or _now(),
            entity_type="fee_obligation",
            entity_id=obligation.id,
            immutable_ref=consulting_addendum.content_hash,
        )
    source["obligation_id"] = str(obligation.id)
    source["signed_pdf_sha256"] = result_file.content_hash
    requested.requirement_source = source
    await db.flush()
    return obligation


def _success_fee_agreement_copy_draft(
    *,
    requested: BucketRequestedDocument,
    recipient_email: str,
    signer_name: str,
    pdf: bytes,
) -> outbox.Draft:
    return outbox.Draft(
        to=recipient_email,
        subject="Signed: Deal-Specific Success Fee Agreement",
        body_text=(
            f"Hello {signer_name},\n\nAttached is the exact Deal-Specific Success Fee "
            "Agreement you signed electronically. Keep this executed copy for your records. "
            "This agreement is separate from any later ACH authorization and debit notice.\n\n"
            "Qualified Commercial LLC"
        ),
        attachments=[("QC-Deal-Specific-Success-Fee-Agreement.pdf", pdf, "application/pdf")],
        headers={
            "Message-ID": (
                f"<success-fee-agreement-{requested.id}@qualifiedcommercial.com>"
            )
        },
    )


async def ensure_success_fee_agreement_copy_queued(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    requested: BucketRequestedDocument,
    result_file: BucketFile,
    recipient_email: str | None,
    signer_name: str,
) -> MessageSend | None:
    source = dict(requested.requirement_source or {})
    existing_id = source.get("signed_copy_message_send_id")
    if existing_id:
        try:
            existing = await db.get(MessageSend, UUID(str(existing_id)))
        except ValueError:
            existing = None
        if existing is not None:
            return existing
    pdf = await _get_private_bytes(
        result_file.s3_key,
        version_id=result_file.s3_version_id,
        expected_sha256=result_file.content_hash,
    )
    if not pdf or not recipient_email:
        return None
    row = await outbox.record(
        db,
        channel="email",
        status="queued",
        draft=_success_fee_agreement_copy_draft(
            requested=requested,
            recipient_email=recipient_email,
            signer_name=signer_name,
            pdf=pdf,
        ),
        context="success_fee_agreement",
        template_key=f"success_fee_signed_{requested.id}",
        subject=outbox.Subject(
            profile_id=profile.id,
            client_id=profile.client_id,
            loan_id=profile.loan_id,
            intake_id=profile.intake_id,
        ),
    )
    if row is None:
        return None
    source["signed_copy_message_send_id"] = str(row.id)
    source["signed_copy_delivery_status"] = row.status
    requested.requirement_source = source
    await db.flush()
    return row


async def deliver_success_fee_agreement_copy(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    requested: BucketRequestedDocument,
    result_file: BucketFile,
    recipient_email: str | None,
    signer_name: str,
) -> bool:
    source = dict(requested.requirement_source or {})
    try:
        row_id = UUID(str(source.get("signed_copy_message_send_id")))
    except (TypeError, ValueError):
        return False
    row = await db.get(MessageSend, row_id)
    pdf = await _get_private_bytes(
        result_file.s3_key,
        version_id=result_file.s3_version_id,
        expected_sha256=result_file.content_hash,
    )
    if row is None or not pdf or not recipient_email:
        return False
    outcome = await outbox.deliver_email(
        db,
        _success_fee_agreement_copy_draft(
            requested=requested,
            recipient_email=recipient_email,
            signer_name=signer_name,
            pdf=pdf,
        ),
        context="success_fee_agreement",
        template_key=f"success_fee_signed_{requested.id}",
        subject=outbox.Subject(
            profile_id=profile.id,
            client_id=profile.client_id,
            loan_id=profile.loan_id,
            intake_id=profile.intake_id,
        ),
        recorded_row=row,
        durable_handoff=True,
    )
    source["signed_copy_delivery_status"] = row.status
    requested.requirement_source = source
    await db.flush()
    return outcome.ok


async def prepare_debit_notice(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    obligation: FeeObligation,
    actor: User,
    scheduled_debit_date: date,
    submission_window: str,
    idempotency_key: str,
) -> PaymentDebitNotice:
    if submission_window != "business_day_et":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "submission_window must be business_day_et",
        )
    scheduled, window_start, window_end, cutoff = debit_window(scheduled_debit_date)
    if scheduled < add_business_days(_now(), NOTICE_BUSINESS_DAYS):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Scheduled debit date must allow two full business days of advance notice",
        )
    from app.services import payments as pay

    snapshot_blockers = await pay.fee_obligation_snapshot_blockers(db, obligation)
    if snapshot_blockers:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This fee request is no longer current: " + "; ".join(snapshot_blockers),
        )

    _, client_name, client_email = await pay._profile_identity(db, profile)
    if not client_email or "@" not in client_email:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The application room needs a valid client email",
        )
    durable_key = (
        f"fee-notice:{obligation.id}:"
        f"{hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()[:32]}"
    )
    replay = (
        await db.execute(
            select(PaymentDebitNotice).where(
                PaymentDebitNotice.idempotency_key == durable_key
            )
        )
    ).scalar_one_or_none()
    if replay is not None:
        if replay.fee_obligation_id != obligation.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "Idempotency key was reused")
        return replay
    current = (
        await db.execute(
            select(PaymentDebitNotice)
            .where(
                PaymentDebitNotice.fee_obligation_id == obligation.id,
                PaymentDebitNotice.superseded_at.is_(None),
                PaymentDebitNotice.revoked_at.is_(None),
                PaymentDebitNotice.status.notin_(("cancelled", "consumed")),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if current and current.mandate_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Revoke the current ACH authorization before changing its debit date",
        )
    now = _now()
    if current:
        current.status = "superseded"
        current.superseded_at = now
        current.superseded_by_user_id = actor.id
    base_snapshot = {
        "notice_type": "one_time_fee",
        "profile_id": str(profile.id),
        "fee_obligation_id": str(obligation.id),
        "amount_cents": obligation.client_ach_cents,
        "currency": obligation.currency,
        "recipient_name": client_name,
        "recipient_email": client_email,
        "scheduled_debit_at": scheduled.isoformat(),
        "debit_window_start_at": window_start.isoformat(),
        "debit_window_end_at": window_end.isoformat(),
        "notice_business_days": NOTICE_BUSINESS_DAYS,
        "revocation_cutoff_at": cutoff.isoformat(),
        "submission_window": submission_window,
    }
    row = PaymentDebitNotice(
        application_profile_id=profile.id,
        fee_obligation_id=obligation.id,
        status="draft",
        delivery_status=None,
        notice_type="one_time_fee",
        amount_cents=obligation.client_ach_cents,
        currency=obligation.currency,
        recipient_name=client_name,
        recipient_email=client_email,
        scheduled_debit_at=scheduled,
        debit_window_start_at=window_start,
        debit_window_end_at=window_end,
        notice_business_days=NOTICE_BUSINESS_DAYS,
        revocation_cutoff_at=cutoff,
        notice_snapshot=base_snapshot,
        notice_sha256=_canonical_hash(base_snapshot),
        idempotency_key=durable_key,
        created_by_user_id=actor.id,
    )
    db.add(row)
    await db.flush()
    return row


async def current_debit_notice(
    db: AsyncSession, obligation_id: UUID
) -> PaymentDebitNotice | None:
    return (
        await db.execute(
            select(PaymentDebitNotice)
            .where(
                PaymentDebitNotice.fee_obligation_id == obligation_id,
                PaymentDebitNotice.superseded_at.is_(None),
                PaymentDebitNotice.revoked_at.is_(None),
            )
            .order_by(PaymentDebitNotice.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _successful_debit_notice_message(
    db: AsyncSession, notice: PaymentDebitNotice
) -> MessageSend | None:
    """Return canonical evidence that this exact notice reached provider handoff."""

    return (
        await db.execute(
            select(MessageSend)
            .where(
                MessageSend.context == "ach_debit_notice",
                MessageSend.template_key.like(f"ach_debit_notice_{notice.id}%"),
                MessageSend.status.in_(("sent", "delivered")),
            )
            .order_by(MessageSend.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def sync_notice_delivery(
    db: AsyncSession, notice: PaymentDebitNotice | None
) -> MessageSend | None:
    if notice is None:
        return None
    message = (
        await db.get(MessageSend, notice.message_send_id)
        if notice.message_send_id
        else None
    )
    if message is None or message.status not in {"sent", "delivered"}:
        successful = await _successful_debit_notice_message(db, notice)
        if successful is not None:
            message = successful
            notice.message_send_id = successful.id
    if message is None:
        return None
    notice.delivery_status = message.status
    notice.provider = message.provider or notice.provider
    notice.provider_message_id = message.provider_message_id or notice.provider_message_id
    notice.rfc_message_id = message.rfc_message_id or notice.rfc_message_id
    if message.delivered_at:
        notice.delivered_at = message.delivered_at
    if message.status in {"bounced", "complained"}:
        notice.bounced_at = message.failed_at or _now()
    elif message.status in {"failed", "blocked"}:
        notice.failed_at = message.failed_at or _now()
    notice.delivery_evidence_snapshot = {
        **(notice.delivery_evidence_snapshot or {}),
        "message_send_id": str(message.id),
        "status": message.status,
        "provider": message.provider,
        "provider_message_id": message.provider_message_id,
        "rfc_message_id": message.rfc_message_id,
        "delivered_at": message.delivered_at.isoformat() if message.delivered_at else None,
        "failed_at": message.failed_at.isoformat() if message.failed_at else None,
    }
    return message


def notice_release_blockers(
    notice: PaymentDebitNotice | None,
    message: MessageSend | None,
    *,
    at: datetime | None = None,
) -> list[str]:
    now = _utc(at or _now())
    if notice is None:
        return ["A separate advance debit notice is required"]
    status_value = (message.status if message else notice.delivery_status or "").lower()
    if notice.provider_accepted_at is None or status_value not in {"sent", "delivered"}:
        if status_value in {"failed", "blocked", "bounced", "complained"}:
            return ["Advance debit notice delivery failed or bounced; resend it"]
        return ["Advance debit notice has not been accepted for delivery"]
    blockers: list[str] = []
    deadline = add_business_days(notice.provider_accepted_at, notice.notice_business_days or 2)
    if now < deadline:
        blockers.append("Two business days have not elapsed since the advance debit notice was accepted")
    if notice.debit_window_start_at and now < _utc(notice.debit_window_start_at):
        blockers.append("The scheduled debit submission window has not opened")
    if notice.debit_window_end_at and now > _utc(notice.debit_window_end_at):
        blockers.append("The scheduled debit submission window has passed")
    return blockers


async def _get_private_bytes(
    key: str | None,
    *,
    version_id: str | None = None,
    expected_sha256: str | None = None,
) -> bytes | None:
    if not key:
        return None

    def _read() -> bytes | None:
        try:
            params = {"Bucket": get_settings().s3_bucket, "Key": key}
            if version_id:
                params["VersionId"] = version_id
            response = private_storage._private_s3_client().get_object(**params)
            value = response["Body"].read()
            if expected_sha256 and not hashlib.sha256(value).hexdigest() == expected_sha256:
                log.error("protected ACH artifact hash mismatch key=%s", key)
                return None
            return value
        except Exception as exc:  # noqa: BLE001
            log.warning("protected ACH artifact read failed key=%s: %s", key, exc)
            return None

    return await asyncio.to_thread(_read)


async def verified_protected_download_url(
    file: BucketFile,
    *,
    download_filename: str | None,
    ttl_seconds: int = 300,
) -> str:
    """Verify exact protected bytes, then sign that immutable S3 version."""

    if file.retention_class not in RETENTION_CLASSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This document is not classified as protected ACH evidence",
        )
    if file.status != "uploaded" or not file.content_hash:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The protected document is not available with verified integrity",
        )
    verified = await _get_private_bytes(
        file.s3_key,
        version_id=file.s3_version_id,
        expected_sha256=file.content_hash,
    )
    if verified is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The protected document failed its integrity check",
        )
    url = private_storage.presign_private_s3_object(
        file.s3_key,
        ttl_seconds=ttl_seconds,
        download_filename=download_filename,
        version_id=file.s3_version_id,
    )
    if not url:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The protected document is temporarily unavailable",
        )
    return url


def _content_addressed_key(key: str, digest: str) -> str:
    """Return an S3 key whose identity is bound to the exact PDF bytes."""

    path = PurePosixPath(key)
    suffix = path.suffix or ".pdf"
    stem = path.name[: -len(suffix)] if path.suffix else path.name
    return str(path.with_name(f"{stem}-{digest}{suffix}"))


async def _reserve_protected_file(
    *,
    profile: ApplicationProfile,
    key: str,
    filename: str,
    digest: str,
    size_bytes: int,
    retention_class: str,
    anchor: datetime,
    entity_type: str,
    entity_id: UUID,
    uploaded_by_name: str | None,
    uploaded_by_email: str | None,
) -> UUID:
    """Persist the compliance ledger row before touching object storage.

    This deliberately uses its own short transaction.  A process crash after
    the reservation leaves a visible ``pending_upload`` record which the same
    idempotent call can resume; it cannot leave an untracked S3 proof object.
    """

    if profile.primary_bucket_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Application file has no document bucket")

    async def _existing_id(session: AsyncSession) -> UUID | None:
        row = (
            await session.execute(
                select(BucketFile).where(
                    BucketFile.source_entity_type == entity_type,
                    BucketFile.source_entity_id == entity_id,
                    BucketFile.retention_class == retention_class,
                    BucketFile.source_immutable_ref == digest,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.protected_until = extend_retention(row.protected_until, anchor=anchor)
        await session.commit()
        return row.id

    async with SessionLocal() as durable_db:
        existing_id = await _existing_id(durable_db)
        if existing_id is not None:
            return existing_id
        row = BucketFile(
            bucket_id=profile.primary_bucket_id,
            file_name=filename[:255],
            s3_key=key,
            content_type="application/pdf",
            size_bytes=size_bytes,
            uploaded_by_name=uploaded_by_name,
            uploaded_by_email=uploaded_by_email,
            source_kind="generated",
            source_detail=retention_class,
            status="pending_upload",
            content_hash=digest,
        )
        protect_bucket_file(
            row,
            retention_class=retention_class,
            anchor=anchor,
            entity_type=entity_type,
            entity_id=entity_id,
            immutable_ref=digest,
        )
        durable_db.add(row)
        try:
            await durable_db.commit()
            return row.id
        except IntegrityError:
            # A concurrent signer may have reserved the exact same immutable
            # artifact.  Resolve that row instead of producing a second object.
            await durable_db.rollback()
            existing_id = await _existing_id(durable_db)
            if existing_id is None:
                raise
            return existing_id


async def _finalize_protected_file(
    file_id: UUID,
    *,
    digest: str,
    version_id: str | None,
    status_value: str,
) -> None:
    async with SessionLocal() as durable_db:
        row = (
            await durable_db.execute(
                select(BucketFile).where(BucketFile.id == file_id).with_for_update()
            )
        ).scalar_one()
        row.status = status_value
        if status_value == "uploaded":
            row.content_hash = digest
            row.s3_version_id = version_id
        await durable_db.commit()


async def _store_protected_pdf(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    key: str,
    filename: str,
    pdf: bytes,
    retention_class: str,
    anchor: datetime,
    entity_type: str,
    entity_id: UUID,
    uploaded_by_name: str | None,
    uploaded_by_email: str | None,
) -> BucketFile:
    if profile.primary_bucket_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Application file has no document bucket")
    digest = hashlib.sha256(pdf).hexdigest()
    immutable_key = _content_addressed_key(key, digest)
    file_id = await _reserve_protected_file(
        profile=profile,
        key=immutable_key,
        filename=filename,
        digest=digest,
        size_bytes=len(pdf),
        retention_class=retention_class,
        anchor=anchor,
        entity_type=entity_type,
        entity_id=entity_id,
        uploaded_by_name=uploaded_by_name,
        uploaded_by_email=uploaded_by_email,
    )
    try:
        storage = await asyncio.to_thread(
            private_storage.put_private_s3_object,
            key=immutable_key,
            body=pdf,
            content_type="application/pdf",
            prevent_overwrite=True,
        )
        stored = await _get_private_bytes(
            immutable_key,
            version_id=storage.get("version_id"),
            expected_sha256=digest,
        )
        if stored is None:
            raise RuntimeError("Stored ACH proof failed its integrity verification")
    except Exception:
        await _finalize_protected_file(
            file_id,
            digest=digest,
            version_id=None,
            status_value="integrity_failed",
        )
        raise
    await _finalize_protected_file(
        file_id,
        digest=digest,
        version_id=storage.get("version_id"),
        status_value="uploaded",
    )
    # The durable reservation transaction committed independently; load its
    # authoritative row into the caller's unit of work for normal FK binding.
    row = await db.get(BucketFile, file_id, populate_existing=True)
    if row is None:
        raise RuntimeError("Protected ACH artifact ledger row is unavailable")
    return row


def _mandate_proof_draft(mandate: AchMandate, pdf: bytes) -> outbox.Draft:
    is_schedule = mandate.private_plan_id is not None
    return outbox.Draft(
        to=mandate.payer_email or "",
        subject=(
            "Your fixed private-funding ACH schedule authorization"
            if is_schedule
            else "Your one-time ACH authorization"
        ),
        body_text=(
            f"Hello {mandate.payer_name},\n\nAttached is the exact "
            f"{'fixed private-funding payment schedule' if is_schedule else 'one-time business ACH debit'} "
            "authorization you signed. Keep this copy for your records. You may revoke future "
            "unsubmitted payments from the secure application room under the terms shown in the proof.\n\n"
            "Qualified Commercial LLC"
        ),
        attachments=[(
            "QC-private-funding-ACH-schedule-authorization.pdf"
            if is_schedule
            else "QC-one-time-ACH-authorization.pdf",
            pdf,
            "application/pdf",
        )],
    )


async def _successful_mandate_proof_message(
    db: AsyncSession, mandate: AchMandate
) -> MessageSend | None:
    return (
        await db.execute(
            select(MessageSend)
            .where(
                MessageSend.context == "ach_mandate_proof",
                MessageSend.template_key.like(f"ach_mandate_proof_{mandate.id}%"),
                MessageSend.status.in_(("sent", "delivered")),
            )
            .order_by(MessageSend.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def ensure_mandate_proof_queued(
    db: AsyncSession,
    *,
    mandate: AchMandate,
    profile: ApplicationProfile,
    pdf: bytes | None = None,
    force_new: bool = False,
) -> MessageSend | None:
    proof_file = (
        await db.get(BucketFile, mandate.certificate_bucket_file_id)
        if mandate.certificate_bucket_file_id
        else None
    )
    pdf = pdf if pdf is not None else await _get_private_bytes(
        proof_file.s3_key if proof_file else mandate.certificate_s3_key,
        version_id=proof_file.s3_version_id if proof_file else None,
        expected_sha256=(
            proof_file.content_hash if proof_file else mandate.certificate_sha256
        ),
    )
    if not pdf or not mandate.payer_email:
        if await _successful_mandate_proof_message(db, mandate) is None:
            mandate.proof_copy_delivery_status = "failed"
            mandate.proof_copy_last_error = "Authorization proof PDF or payer email is unavailable"
        return None
    current = (
        await db.get(MessageSend, mandate.proof_copy_message_send_id)
        if mandate.proof_copy_message_send_id
        else None
    )
    if current is not None and not force_new:
        return current
    template_key = (
        f"ach_mandate_proof_{mandate.id}_r_{uuid4().hex[:6]}"
        if force_new
        else f"ach_mandate_proof_{mandate.id}"
    )
    draft = _mandate_proof_draft(mandate, pdf)
    draft.headers["Message-ID"] = f"<{template_key}@qualifiedcommercial.com>"
    row = await outbox.record(
        db,
        channel="email",
        status="queued",
        draft=draft,
        context="ach_mandate_proof",
        template_key=template_key,
        subject=outbox.Subject(
            profile_id=profile.id,
            client_id=profile.client_id,
            loan_id=profile.loan_id,
            intake_id=profile.intake_id,
        ),
    )
    if row is not None and await _successful_mandate_proof_message(db, mandate) is None:
        mandate.proof_copy_message_send_id = row.id
        mandate.proof_copy_delivery_status = row.status
        mandate.proof_copy_last_error = None
    if proof_file:
        proof_file.protected_until = extend_retention(
            proof_file.protected_until, anchor=_now()
        )
    await db.flush()
    return row


async def deliver_mandate_proof(
    db: AsyncSession,
    *,
    mandate: AchMandate,
    profile: ApplicationProfile,
    pdf: bytes | None = None,
    recorded_row: MessageSend | None = None,
) -> bool:
    proof_file = (
        await db.get(BucketFile, mandate.certificate_bucket_file_id)
        if mandate.certificate_bucket_file_id
        else None
    )
    pdf = pdf if pdf is not None else await _get_private_bytes(
        proof_file.s3_key if proof_file else mandate.certificate_s3_key,
        version_id=proof_file.s3_version_id if proof_file else None,
        expected_sha256=(
            proof_file.content_hash if proof_file else mandate.certificate_sha256
        ),
    )
    row = recorded_row
    if row is None and mandate.proof_copy_message_send_id:
        row = await db.get(MessageSend, mandate.proof_copy_message_send_id)
    if row is None or not pdf or not mandate.payer_email:
        return False
    prior_success = await _successful_mandate_proof_message(db, mandate)
    outcome = await outbox.deliver_email(
        db,
        _mandate_proof_draft(mandate, pdf),
        context="ach_mandate_proof",
        template_key=row.template_key,
        subject=outbox.Subject(
            profile_id=profile.id,
            client_id=profile.client_id,
            loan_id=profile.loan_id,
            intake_id=profile.intake_id,
        ),
        recorded_row=row,
        durable_handoff=True,
    )
    if prior_success is None:
        mandate.proof_copy_message_send_id = row.id
        mandate.proof_copy_delivery_status = row.status
        if outcome.ok:
            mandate.proof_copy_sent_at = _now()
            mandate.proof_copy_delivered_at = row.delivered_at
            mandate.proof_copy_last_error = None
        else:
            mandate.proof_copy_last_error = outcome.detail[:1000]
    if proof_file:
        proof_file.protected_until = extend_retention(
            proof_file.protected_until, anchor=_now()
        )
    await db.flush()
    return outcome.ok


async def sync_mandate_proof_delivery(
    db: AsyncSession, mandate: AchMandate | None
) -> MessageSend | None:
    """Refresh the durable customer-copy evidence from the audited outbox row."""

    if mandate is None:
        return None
    message = (
        await db.get(MessageSend, mandate.proof_copy_message_send_id)
        if mandate.proof_copy_message_send_id
        else None
    )
    if message is None or message.status not in {"sent", "delivered"}:
        successful = await _successful_mandate_proof_message(db, mandate)
        if successful is not None:
            message = successful
            mandate.proof_copy_message_send_id = successful.id
    if message is None:
        mandate.proof_copy_delivery_status = "unavailable"
        mandate.proof_copy_last_error = "Authorization proof delivery record is unavailable"
        return None
    mandate.proof_copy_delivery_status = message.status
    mandate.proof_copy_delivered_at = message.delivered_at
    if message.status in {"failed", "blocked", "bounced", "complained"}:
        mandate.proof_copy_last_error = (
            message.detail or "Authorization proof email was not delivered"
        )[:1000]
    elif message.status in {"sent", "delivered"}:
        mandate.proof_copy_last_error = None
    await db.flush()
    return message


def _debit_notice_draft(
    *,
    notice: PaymentDebitNotice,
    mandate: AchMandate,
    pdf: bytes,
) -> outbox.Draft:
    return outbox.Draft(
        to=notice.recipient_email,
        subject=f"Advance notice: {_money(notice.amount_cents)} ACH debit",
        body_text=(
            f"Hello {notice.recipient_name or mandate.payer_name},\n\n"
            f"Attached is advance notice of the exact one-time {_money(notice.amount_cents)} "
            f"business ACH debit scheduled for {notice.scheduled_debit_at.astimezone(FIRM_TIMEZONE).date().isoformat()} "
            f"from the account ending {notice.account_mask or '----'}. This is separate from your signed "
            "authorization proof. The attachment states the submission window and revocation cutoff. "
            "You may revoke an unsubmitted debit in the secure application room or by emailing "
            "support@qualifiedcommercial.com before that cutoff.\n\n"
            "Qualified Commercial LLC"
        ),
        attachments=[("QC-advance-ACH-debit-notice.pdf", pdf, "application/pdf")],
    )


async def ensure_debit_notice_queued(
    db: AsyncSession,
    *,
    notice: PaymentDebitNotice,
    mandate: AchMandate,
    profile: ApplicationProfile,
    force_new: bool = False,
) -> MessageSend | None:
    notice_file = (
        await db.get(BucketFile, notice.notice_bucket_file_id)
        if notice.notice_bucket_file_id
        else None
    )
    pdf = await _get_private_bytes(
        notice_file.s3_key if notice_file else None,
        version_id=notice_file.s3_version_id if notice_file else None,
        expected_sha256=notice_file.content_hash if notice_file else None,
    )
    if not pdf:
        if await _successful_debit_notice_message(db, notice) is None:
            notice.status = "delivery_failed"
            notice.delivery_status = "failed"
            notice.failed_at = _now()
            notice.last_error = "The exact protected debit-notice PDF is unavailable"
        return None
    current = await db.get(MessageSend, notice.message_send_id) if notice.message_send_id else None
    if current is not None and not force_new:
        return current
    template_key = (
        f"ach_debit_notice_{notice.id}_r_{uuid4().hex[:7]}"
        if force_new
        else f"ach_debit_notice_{notice.id}"
    )
    draft = _debit_notice_draft(notice=notice, mandate=mandate, pdf=pdf)
    draft.headers["Message-ID"] = f"<{template_key}@qualifiedcommercial.com>"
    row = await outbox.record(
        db,
        channel="email",
        status="queued",
        draft=draft,
        context="ach_debit_notice",
        template_key=template_key,
        subject=outbox.Subject(
            profile_id=profile.id,
            client_id=profile.client_id,
            loan_id=profile.loan_id,
            intake_id=profile.intake_id,
        ),
    )
    if row is None:
        return None
    prior_success = await _successful_debit_notice_message(db, notice)
    prior_evidence = dict(notice.delivery_evidence_snapshot or {})
    attempts = list(prior_evidence.get("attempts") or [])
    if notice.message_send_id:
        attempts.append({
            "message_send_id": str(notice.message_send_id),
            "status": notice.delivery_status,
            "provider_message_id": notice.provider_message_id,
            "provider_accepted_at": (
                notice.provider_accepted_at.isoformat()
                if notice.provider_accepted_at
                else None
            ),
        })
    if prior_success is None:
        notice.message_send_id = row.id
        notice.status = "queued"
        notice.delivery_status = "queued"
        notice.provider = None
        notice.provider_message_id = None
        notice.rfc_message_id = row.rfc_message_id
        notice.sent_at = None
        notice.provider_accepted_at = None
        notice.delivered_at = None
        notice.bounced_at = None
        notice.failed_at = None
        notice.last_error = None
    notice.delivery_evidence_snapshot = {"attempts": attempts}
    await db.flush()
    return row


async def bind_and_queue_debit_notice(
    db: AsyncSession,
    *,
    notice: PaymentDebitNotice,
    mandate: AchMandate,
    profile: ApplicationProfile,
    funding_source: PaymentFundingSource,
    authorization_text: str,
) -> PaymentDebitNotice:
    earliest = add_business_days(_now(), notice.notice_business_days or NOTICE_BUSINESS_DAYS)
    if notice.scheduled_debit_at < earliest:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The scheduled debit is too soon to provide two business days' advance notice; ask QC to reschedule it",
        )
    notice.mandate_id = mandate.id
    notice.account_mask = funding_source.account_mask
    notice.authorization_text_sha256 = hashlib.sha256(
        authorization_text.encode("utf-8")
    ).hexdigest()
    snapshot = {
        **(notice.notice_snapshot or {}),
        "mandate_id": str(mandate.id),
        "authorization_text_sha256": notice.authorization_text_sha256,
        "agreement_document_id": str(mandate.agreement_document_id),
        "agreement_sha256": mandate.agreement_sha256,
        "account_mask": funding_source.account_mask,
        "ach_class": "CCD",
        "authorization_type": mandate.authorization_type,
    }
    notice.notice_snapshot = snapshot
    notice.notice_sha256 = _canonical_hash(snapshot)
    pdf = await asyncio.to_thread(
        ach_authorization.render_debit_notice_pdf,
        notice=notice,
        mandate=mandate,
        funding_source=funding_source,
        authorization_text=authorization_text,
    )
    key = f"payments/ach-notices/{profile.id}/{notice.id}/advance-notice.pdf"
    file = await _store_protected_pdf(
        db,
        profile=profile,
        key=key,
        filename="QC Advance Notice of One-Time ACH Debit.pdf",
        pdf=pdf,
        retention_class="ach_debit_notice",
        anchor=max(_now(), notice.scheduled_debit_at),
        entity_type="payment_debit_notice",
        entity_id=notice.id,
        uploaded_by_name=notice.recipient_name,
        uploaded_by_email=notice.recipient_email,
    )
    notice.notice_bucket_file_id = file.id
    await ensure_debit_notice_queued(
        db,
        notice=notice,
        mandate=mandate,
        profile=profile,
    )
    await db.flush()
    return notice


async def deliver_debit_notice(
    db: AsyncSession,
    *,
    notice: PaymentDebitNotice,
    mandate: AchMandate,
    profile: ApplicationProfile,
    recorded_row: MessageSend | None = None,
) -> bool:
    notice_file = (
        await db.get(BucketFile, notice.notice_bucket_file_id)
        if notice.notice_bucket_file_id
        else None
    )
    pdf = await _get_private_bytes(
        notice_file.s3_key if notice_file else None,
        version_id=notice_file.s3_version_id if notice_file else None,
        expected_sha256=notice_file.content_hash if notice_file else None,
    )
    row = recorded_row
    if row is None and notice.message_send_id:
        row = await db.get(MessageSend, notice.message_send_id)
    if row is None or not pdf:
        return False
    prior_success = await _successful_debit_notice_message(db, notice)
    outcome = await outbox.deliver_email(
        db,
        _debit_notice_draft(notice=notice, mandate=mandate, pdf=pdf),
        context="ach_debit_notice",
        template_key=row.template_key,
        subject=outbox.Subject(
            profile_id=profile.id,
            client_id=profile.client_id,
            loan_id=profile.loan_id,
            intake_id=profile.intake_id,
        ),
        recorded_row=row,
        durable_handoff=True,
    )
    evidence = dict(notice.delivery_evidence_snapshot or {})
    attempts = list(evidence.get("attempts") or [])
    attempts.append({
        "message_send_id": str(row.id),
        "status": row.status,
        "provider": row.provider,
        "provider_message_id": row.provider_message_id,
        "rfc_message_id": row.rfc_message_id,
        "accepted_at": _now().isoformat() if outcome.ok else None,
        "error": None if outcome.ok else outcome.detail[:1000],
    })
    notice.delivery_evidence_snapshot = {**evidence, "attempts": attempts}
    if prior_success is None:
        notice.message_send_id = row.id
        notice.delivery_status = row.status
        notice.provider = row.provider
        notice.provider_message_id = row.provider_message_id
        notice.rfc_message_id = row.rfc_message_id
        notice.sent_at = _now() if outcome.ok else None
        notice.provider_accepted_at = _now() if outcome.ok else None
        notice.failed_at = None if outcome.ok else _now()
        notice.last_error = None if outcome.ok else outcome.detail[:1000]
        notice.status = "sent" if outcome.ok else "delivery_failed"
    if notice_file:
        notice_file.protected_until = extend_retention(
            notice_file.protected_until, anchor=max(_now(), notice.scheduled_debit_at)
        )
    await db.flush()
    return outcome.ok


async def store_mandate_proof(
    db: AsyncSession,
    *,
    mandate: AchMandate,
    profile: ApplicationProfile,
    pdf: bytes,
) -> BucketFile:
    key = f"payments/ach-mandates/{profile.id}/{mandate.id}/certificate.pdf"
    file = await _store_protected_pdf(
        db,
        profile=profile,
        key=key,
        filename="QC One-Time ACH Authorization Proof.pdf",
        pdf=pdf,
        retention_class="ach_authorization_proof",
        anchor=mandate.signed_at,
        entity_type="ach_mandate",
        entity_id=mandate.id,
        uploaded_by_name=mandate.payer_name,
        uploaded_by_email=mandate.payer_email,
    )
    mandate.certificate_s3_key = file.s3_key
    mandate.certificate_sha256 = file.content_hash
    mandate.certificate_bucket_file_id = file.id
    mandate.retention_until = extend_retention(mandate.retention_until, anchor=mandate.signed_at)
    await db.flush()
    return file


async def extend_mandate_retention(
    db: AsyncSession, mandate: AchMandate, *, anchor: datetime
) -> None:
    # Lock every row whose deadline can move.  ``max(current, candidate)`` is
    # monotonic only when concurrent updates cannot overwrite one another.
    locked_mandate = (
        await db.execute(
            select(AchMandate).where(AchMandate.id == mandate.id).with_for_update()
        )
    ).scalar_one()
    locked_mandate.retention_until = extend_retention(
        locked_mandate.retention_until, anchor=anchor
    )
    ids = {
        value
        for value in (
            locked_mandate.certificate_bucket_file_id,
            locked_mandate.agreement_document_id,
        )
        if value
    }
    if locked_mandate.fee_obligation_id:
        governing_ids = (
            await db.execute(
                select(FeeObligationLine.governing_agreement_document_id).where(
                    FeeObligationLine.obligation_id
                    == locked_mandate.fee_obligation_id,
                    FeeObligationLine.governing_agreement_document_id.is_not(None),
                )
            )
        ).scalars().all()
        ids.update(value for value in governing_ids if value)
    notice = (
        await db.execute(
            select(PaymentDebitNotice)
            .where(PaymentDebitNotice.mandate_id == mandate.id)
            .order_by(PaymentDebitNotice.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if notice and notice.notice_bucket_file_id:
        ids.add(notice.notice_bucket_file_id)
    if ids:
        files = (
            await db.execute(
                select(BucketFile).where(BucketFile.id.in_(ids)).with_for_update()
            )
        ).scalars().all()
        for file in files:
            file.protected_until = extend_retention(file.protected_until, anchor=anchor)
    await db.flush()


def _revocation_confirmation_draft(mandate: AchMandate) -> outbox.Draft:
    return outbox.Draft(
        to=mandate.payer_email or "",
        subject="Your one-time ACH authorization was revoked",
        body_text=(
            f"Hello {mandate.payer_name},\n\nYour exact one-time business ACH "
            f"authorization {mandate.id} was revoked before submission. No new debit "
            "may be originated under that authorization. Revocation does not recall a "
            "debit already submitted and does not cancel an underlying fee that was "
            "otherwise earned under the signed agreement. Keep your authorization proof "
            "and this confirmation for your records.\n\nQualified Commercial LLC"
        ),
        headers={
            "Message-ID": (
                f"<ach-mandate-revoked-{mandate.id}@qualifiedcommercial.com>"
            )
        },
    )


async def ensure_revocation_confirmation_queued(
    db: AsyncSession, mandate: AchMandate
) -> MessageSend | None:
    """Persist the notification claim in the same transaction as revocation."""

    template_key = f"ach_mandate_revoked_{mandate.id}"
    existing = (
        await db.execute(
            select(MessageSend)
            .where(
                MessageSend.context == "ach_mandate_revocation",
                MessageSend.template_key == template_key,
            )
            .order_by(MessageSend.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None or not mandate.payer_email:
        return existing
    return await outbox.record(
        db,
        channel="email",
        status="queued",
        draft=_revocation_confirmation_draft(mandate),
        context="ach_mandate_revocation",
        template_key=template_key,
        subject=outbox.Subject(profile_id=mandate.application_profile_id),
    )


async def deliver_revocation_confirmation(
    db: AsyncSession, mandate: AchMandate
) -> MessageSend | None:
    """Deliver only after the mandate and queued ledger row were committed."""

    row = await ensure_revocation_confirmation_queued(db, mandate)
    if row is None or row.status in {"sent", "delivered"}:
        return row
    outcome = await outbox.deliver_email(
        db,
        _revocation_confirmation_draft(mandate),
        context="ach_mandate_revocation",
        template_key=f"ach_mandate_revoked_{mandate.id}",
        subject=outbox.Subject(profile_id=mandate.application_profile_id),
        recorded_row=row,
        durable_handoff=True,
    )
    return outcome.row


async def revoke_mandate(
    db: AsyncSession,
    *,
    mandate: AchMandate,
    actor_user_id: UUID | None,
    reason: str | None,
) -> AchMandate:
    from app.services import payments as pay

    if mandate.revoked_at is not None or mandate.status == "revoked":
        await ensure_revocation_confirmation_queued(db, mandate)
        await extend_mandate_retention(db, mandate, anchor=mandate.revoked_at or _now())
        return mandate
    transfers = (
        await db.execute(
            select(PaymentTransfer).where(PaymentTransfer.mandate_id == mandate.id).with_for_update()
        )
    ).scalars().all()
    if any(
        row.plaid_transfer_id
        or row.submitted_at
        or row.claimed_at
        or row.status in {"submitting", "submitted", "pending", "posted", "settled", "funds_available"}
        for row in transfers
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This debit was already submitted and can no longer be revoked here",
        )
    await pay.cancel_unclaimed_transfer_intents(
        db, mandate_ids=[mandate.id], reason="one_time_authorization_revoked"
    )
    now = _now()
    mandate.status = "revoked"
    mandate.revoked_at = now
    mandate.revoked_by_user_id = actor_user_id
    mandate.terminated_at = now
    mandate.termination_reason = (reason or "Authorization revoked")[:240]
    notice = (
        await db.execute(
            select(PaymentDebitNotice).where(PaymentDebitNotice.mandate_id == mandate.id)
        )
    ).scalar_one_or_none()
    if notice and notice.status != "consumed":
        notice.status = "revoked"
        notice.revoked_at = now
        notice.revoked_by_user_id = actor_user_id
    await extend_mandate_retention(db, mandate, anchor=now)
    confirmation = await ensure_revocation_confirmation_queued(db, mandate)
    await pay.log_event(
        db,
        profile_id=mandate.application_profile_id,
        actor_id=actor_user_id,
        event_type="ach_mandate.revoked",
        entity_type="ach_mandate",
        entity_id=mandate.id,
        summary="Revoked one-time ACH authorization before submission",
        metadata={
            "reason": mandate.termination_reason,
            "confirmation_message_send_id": str(confirmation.id) if confirmation else None,
            "confirmation_delivery_status": confirmation.status if confirmation else "unavailable",
        },
    )
    return mandate


async def read_protected_pdf(key: str | None) -> bytes | None:
    return await _get_private_bytes(key)

