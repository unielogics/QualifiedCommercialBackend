"""Password-protected evidence replacement requests.

The unreadable upload remains immutable evidence.  A request creates a separate
client-room checklist item so the replacement has its own provenance and can be
fulfilled through the normal upload flow.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.services.client_room import room_url as canonical_room_url
from app.models.application_profile import ApplicationProfile, ApplicationRoomDelivery
from app.models.bucket import (
    BucketFile,
    BucketFileAnalysis,
    BucketRequestedDocument,
    BucketUploadLink,
)
from app.models.user import User
from app.services.email.user_mailer import send_as_user

REQUEST_KIND = "password_protected_replacement"
ACTION_KIND = "unlocked_copy_request"
INITIATION_SOURCE = "staff_unlocked_copy_request"
_REQUIREMENT_PREFIX = "unlocked_copy"

# Only values from the document classifier's published vocabulary are exposed
# to an unauthenticated room-token holder.  The database column is a free-form
# string, so passing it through directly would turn future/provider-specific
# values into an accidental public API (and could reveal internal labels).
_PUBLIC_ANALYSIS_CLASSIFICATIONS = frozenset(
    {
        "accounts_receivable_aging",
        "balance_sheet",
        "bank_statement",
        "business_license_or_permit",
        "collateral_debt_evidence",
        "commercial_lease",
        "current_p_and_l",
        "debt_schedule",
        "entity_or_vesting",
        "equipment_quote_or_invoice",
        "fleet_or_vehicle_schedule",
        "floorplan_mca_inventory",
        "franchise_agreement",
        "hoa",
        "identity",
        "insurance",
        "inventory_or_purchase_ledger",
        "lease_or_rent",
        "merchant_processing_offer",
        "merchant_processing_statement",
        "other",
        "payoff_or_mortgage_statement",
        "payroll_report",
        "personal_financial_statement",
        "purchase_contract",
        "real_estate_schedule",
        "tax_return",
        "transportation_authority",
        "unreadable",
    }
)
_PROCESSING_ANALYSIS_STATUSES = frozenset({"pending", "queued", "processing", "running"})
_PUBLIC_ANALYSIS_STATUSES = _PROCESSING_ANALYSIS_STATUSES | frozenset(
    {"completed", "failed", "skipped"}
)


@dataclass(frozen=True)
class UnlockedCopyRequestState:
    source_file_id: UUID
    source_content_hash: str | None
    requested_document_id: UUID
    request_status: str
    delivery_id: UUID | None
    delivery_status: str | None
    requested_at: datetime
    last_delivery_at: datetime | None
    replacement_review_state: str


@dataclass(frozen=True)
class UnlockedCopyRequestOutcome:
    requested_document: BucketRequestedDocument
    delivery: ApplicationRoomDelivery
    room_url: str
    deduplicated: bool
    created_request: bool
    source_file_id: UUID | None = None
    replacement_review_state: str | None = None


@dataclass(frozen=True)
class PublicFileAnalysisState:
    """Minimal, non-sensitive analysis lifecycle for client-room file rows."""

    analysis_status: str | None
    analysis_reason_code: str | None
    analysis_classification: str | None
    analysis_review_state: str


@dataclass(frozen=True)
class _ReplacementAttemptState:
    review_state: str
    trigger_file_id: UUID | None = None
    trigger_content_hash: str | None = None


@dataclass(frozen=True)
class _DeliveryDecision:
    delivery: ApplicationRoomDelivery | None
    should_create: bool
    idempotency_key: str | None
    attempt_number: int
    replacement_review_state: str | None = None
    replacement_trigger_file_id: UUID | None = None
    replacement_trigger_content_hash: str | None = None


class StaleUnlockedCopyRequest(RuntimeError):
    """A replacement still points at an obsolete source-file request."""


def is_password_protected_analysis(analysis: BucketFileAnalysis | None) -> bool:
    """True only when extraction proved that the file needs an open password."""

    return bool(
        analysis and analysis.status == "skipped" and analysis.skip_reason == "password_protected"
    )


def is_password_protected_file(file: BucketFile, analysis: BucketFileAnalysis | None) -> bool:
    """Include encrypted members recorded against a ZIP transport container."""

    analysis_is_current = bool(
        analysis
        and (not getattr(file, "content_hash", None) or analysis.content_hash == file.content_hash)
    )
    return (analysis_is_current and is_password_protected_analysis(analysis)) or bool(
        getattr(file, "extraction_reason", None) and "zip_entry_encrypted" in file.extraction_reason
    )


def _source_content_hash(file: BucketFile, analysis: BucketFileAnalysis | None) -> str | None:
    return getattr(file, "content_hash", None) or (analysis.content_hash if analysis else None)


def current_request_state(
    file: BucketFile,
    analysis: BucketFileAnalysis | None,
    state: UnlockedCopyRequestState | None,
) -> UnlockedCopyRequestState | None:
    if state is None:
        return None
    # Replacement attempts (and extracted ZIP children) inherit the original
    # source file's request. Their own hash must not invalidate that parent
    # request; hash invalidation only applies when the original object itself
    # was replaced in place.
    if state.source_file_id != file.id:
        return state
    fingerprint = _source_content_hash(file, analysis)
    if state.source_content_hash and fingerprint and state.source_content_hash != fingerprint:
        return None
    return state


def public_analysis_state(
    file: BucketFile,
    analysis: BucketFileAnalysis | None,
) -> PublicFileAnalysisState:
    """Normalize one current analysis without exposing model/error detail.

    Receipt is deliberately narrower than ``status == "completed"``.  A
    completed analysis classified as unreadable (or with an unknown/missing
    classification) still needs another copy; it must not make an unlocked-copy
    checklist item look satisfied merely because the upload pipeline stopped.
    """

    if (
        analysis is not None
        and getattr(file, "content_hash", None)
        and getattr(analysis, "content_hash", None) != file.content_hash
    ):
        analysis = None
    password_protected = is_password_protected_file(file, analysis)
    raw_status = str(getattr(analysis, "status", "") or "")
    analysis_status = raw_status if raw_status in _PUBLIC_ANALYSIS_STATUSES else None
    raw_classification = str(getattr(analysis, "classification", "") or "")
    analysis_classification = (
        raw_classification if raw_classification in _PUBLIC_ANALYSIS_CLASSIFICATIONS else None
    )

    if password_protected:
        # A ZIP parent can prove that a member is encrypted even when there is
        # no per-parent analysis row.  Project one coherent public terminal
        # state so the lock and retry affordance never disagree.
        return PublicFileAnalysisState(
            analysis_status="skipped",
            analysis_reason_code="password_protected",
            analysis_classification="unreadable",
            analysis_review_state="needs_another_copy",
        )
    if analysis is None or analysis_status in _PROCESSING_ANALYSIS_STATUSES:
        return PublicFileAnalysisState(
            analysis_status=analysis_status,
            analysis_reason_code="analysis_pending",
            analysis_classification=analysis_classification,
            analysis_review_state="checking",
        )
    if analysis_status == "failed":
        return PublicFileAnalysisState(
            analysis_status="failed",
            analysis_reason_code="analysis_failed",
            analysis_classification=analysis_classification,
            # A provider/runtime failure says nothing about the readability of
            # the client's bytes. Keep the replacement under review so staff
            # can retry analysis without asking the client for another file.
            analysis_review_state="checking",
        )
    if analysis_status == "skipped":
        reason = (
            "archive_container"
            if getattr(analysis, "skip_reason", None) == "zip_parent_archive"
            else "unreadable"
        )
        return PublicFileAnalysisState(
            analysis_status="skipped",
            analysis_reason_code=reason,
            analysis_classification=analysis_classification or "unreadable",
            analysis_review_state="needs_another_copy",
        )
    if analysis_status == "completed" and analysis_classification not in {
        None,
        "unreadable",
    }:
        return PublicFileAnalysisState(
            analysis_status="completed",
            analysis_reason_code=None,
            analysis_classification=analysis_classification,
            analysis_review_state="received",
        )
    if analysis_status == "completed":
        return PublicFileAnalysisState(
            analysis_status="completed",
            analysis_reason_code="unreadable",
            analysis_classification=analysis_classification,
            analysis_review_state="needs_another_copy",
        )
    # Unknown persisted statuses remain in checking rather than becoming a
    # false success.  Their raw values are intentionally not public.
    return PublicFileAnalysisState(
        analysis_status=None,
        analysis_reason_code="analysis_pending",
        analysis_classification=analysis_classification,
        analysis_review_state="checking",
    )


def _request_key(file: BucketFile, analysis: BucketFileAnalysis | None) -> str:
    # A content suffix prevents a direct S3/object replacement under a legacy
    # file row from inheriting an earlier request.
    fingerprint = _source_content_hash(file, analysis) or "unfingerprinted"
    return f"{_REQUIREMENT_PREFIX}:{file.id.hex}:{fingerprint[:16]}"


def _delivery_key(*parts: object) -> str:
    material = ":".join(str(part) for part in parts)
    return f"unlocked:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _room_url(link: BucketUploadLink, requested_document_id: UUID) -> str:
    return f"{canonical_room_url(link.token)}?tab=todo&request={requested_document_id}"


def _source_file_id(document: BucketRequestedDocument) -> UUID | None:
    source = getattr(document, "requirement_source", None)
    if not isinstance(source, dict) or source.get("kind") != REQUEST_KIND:
        return None
    try:
        return UUID(str(source.get("source_file_id")))
    except (TypeError, ValueError):
        return None


def _document_created_key(document: BucketRequestedDocument) -> tuple[float, str]:
    created_at = getattr(document, "created_at", None)
    return (
        created_at.timestamp() if created_at is not None else 0,
        str(document.id),
    )


def _delivery_created_key(delivery: ApplicationRoomDelivery) -> tuple[float, str]:
    created_at = getattr(delivery, "created_at", None)
    return (
        created_at.timestamp() if created_at is not None else 0,
        str(delivery.id),
    )


def _canonical_document(
    candidates: list[BucketRequestedDocument],
    delivered_document_ids: set[UUID],
) -> BucketRequestedDocument:
    """Choose the task whose durable email/manual delivery clients already know."""

    delivered = [
        document for document in candidates if document.id in delivered_document_ids
    ]
    # The source-row lock prevents new duplicates. For legacy duplicates, the
    # first task is the stable URL embedded in the first idempotent delivery.
    return min(delivered or candidates, key=_document_created_key)


async def _delivered_document_ids(
    db: AsyncSession,
    document_ids: set[UUID],
) -> set[UUID]:
    if not document_ids:
        return set()
    deliveries = list(
        (
            await db.execute(
                select(ApplicationRoomDelivery).where(
                    ApplicationRoomDelivery.requested_document_id.in_(document_ids),
                    ApplicationRoomDelivery.action_kind == ACTION_KIND,
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        delivery.requested_document_id
        for delivery in deliveries
        if delivery.requested_document_id in document_ids
    }


async def _canonical_request_document(
    db: AsyncSession,
    *,
    bucket_id: UUID,
    requirement_key: str,
    for_update: bool,
) -> BucketRequestedDocument | None:
    statement = (
        select(BucketRequestedDocument)
        .where(
            BucketRequestedDocument.bucket_id == bucket_id,
            BucketRequestedDocument.requirement_key == requirement_key,
            BucketRequestedDocument.status != "not_applicable",
        )
        .order_by(
            BucketRequestedDocument.created_at.asc(),
            BucketRequestedDocument.id.asc(),
        )
    )
    if for_update:
        statement = statement.with_for_update()
    candidates = list((await db.execute(statement)).scalars().all())
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    delivered_ids = await _delivered_document_ids(
        db, {document.id for document in candidates}
    )
    return _canonical_document(candidates, delivered_ids)


def public_request_metadata(
    document: BucketRequestedDocument,
    uploaded_files: list[object],
) -> dict[str, object | None]:
    """Return only the replacement-request metadata safe for client rooms."""

    source_file_id = _source_file_id(document)
    if source_file_id is None:
        return {
            "request_kind": None,
            "source_file_id": None,
            "replacement_review_state": None,
        }
    direct_attempts = [
        file
        for file in uploaded_files
        if getattr(file, "requested_document_id", None) == document.id
    ]
    direct_ids = {getattr(file, "id", None) for file in direct_attempts}
    attempts_by_id: dict[object, object] = {}
    for file in uploaded_files:
        file_id = getattr(file, "id", None)
        if file_id is None or file_id == source_file_id:
            continue
        canonical_request_id = getattr(
            getattr(file, "unlocked_copy_request", None),
            "requested_document_id",
            None,
        )
        if (
            getattr(file, "requested_document_id", None) == document.id
            or canonical_request_id == document.id
            or getattr(file, "parent_zip_file_id", None) in direct_ids
        ):
            attempts_by_id.setdefault(file_id, file)
    attempts = list(attempts_by_id.values())
    review_state = _aggregate_replacement_review_state(
        [getattr(file, "analysis_review_state", "checking") for file in attempts]
    )
    return {
        "request_kind": "unlocked_copy",
        "source_file_id": source_file_id,
        "replacement_review_state": review_state,
    }


async def current_public_request_documents(
    db: AsyncSession,
    documents: list[BucketRequestedDocument],
) -> list[BucketRequestedDocument]:
    """Hide obsolete unlocked-copy tasks from every client-facing projection.

    The source evidence may be linked from another bucket, so room-visible
    files are not an authoritative source lookup. Resolve every source in one
    batched query, require the current fingerprint and a current lock, then
        retain one stable matching task for each source. This prevents an old
    h1 task from surviving an in-place h2 replacement, removes duplicates, and
    hides a task when its source was deleted or became readable.
    """

    source_ids = {
        source_file_id
        for document in documents
        if (source_file_id := _source_file_id(document)) is not None
    }
    if not source_ids:
        return documents
    source_files = list(
        (
            await db.execute(
                select(BucketFile).where(
                    BucketFile.id.in_(source_ids),
                    BucketFile.status == "uploaded",
                    BucketFile.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    source_by_id = {file.id: file for file in source_files}
    analyses = (
        list(
            (
                await db.execute(
                    select(BucketFileAnalysis)
                    .where(BucketFileAnalysis.bucket_file_id.in_(source_by_id))
                    .order_by(
                        BucketFileAnalysis.bucket_file_id,
                        BucketFileAnalysis.analysis_version.desc(),
                        BucketFileAnalysis.created_at.desc(),
                        BucketFileAnalysis.id.desc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        if source_by_id
        else []
    )
    latest_analysis: dict[UUID, BucketFileAnalysis] = {}
    for analysis in analyses:
        source_file = source_by_id.get(analysis.bucket_file_id)
        if (
            source_file is not None
            and source_file.content_hash
            and analysis.content_hash != source_file.content_hash
        ):
            continue
        latest_analysis.setdefault(analysis.bucket_file_id, analysis)

    matching_by_source: dict[UUID, list[BucketRequestedDocument]] = {}
    for document in documents:
        source_file_id = _source_file_id(document)
        if source_file_id is None:
            continue
        if getattr(document, "status", None) == "not_applicable":
            continue
        source_file = source_by_id.get(source_file_id)
        analysis = latest_analysis.get(source_file_id)
        source = (
            document.requirement_source if isinstance(document.requirement_source, dict) else {}
        )
        requested_hash = str(source.get("source_content_hash") or "")
        current_hash = _source_content_hash(source_file, analysis) if source_file else None
        if (
            source_file is None
            or not requested_hash
            or not current_hash
            or requested_hash != current_hash
            or not is_password_protected_file(source_file, analysis)
        ):
            continue
        matching_by_source.setdefault(source_file_id, []).append(document)

    matching_ids = {
        document.id
        for candidates in matching_by_source.values()
        for document in candidates
    }
    delivered_ids = await _delivered_document_ids(db, matching_ids)
    current_ids = {
        _canonical_document(candidates, delivered_ids).id
        for candidates in matching_by_source.values()
    }
    return [
        document
        for document in documents
        if _source_file_id(document) is None or document.id in current_ids
    ]


def _aggregate_replacement_review_state(states: list[str]) -> str:
    """Collapse every retry/ZIP member into the reusable request lifecycle."""

    normalized = {str(state or "checking") for state in states}
    # A ZIP parent can be terminal while one extracted child is readable, and
    # allow_multiple_files permits a later good retry. Any readable attempt
    # fulfills the request; otherwise active analysis wins over failures.
    if "received" in normalized:
        return "received"
    if "checking" in normalized:
        return "checking"
    if normalized:
        return "needs_another_copy"
    return "requested"


async def request_states_for_files(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    file_ids: set[UUID],
    file_fingerprints: dict[UUID, str | None] | None = None,
) -> dict[UUID, UnlockedCopyRequestState]:
    """Return the latest durable replacement request for each source file."""

    return await request_states_for_bucket(
        db,
        bucket_id=profile.primary_bucket_id,
        file_ids=file_ids,
        file_fingerprints=file_fingerprints,
    )


async def request_states_for_bucket(
    db: AsyncSession,
    *,
    bucket_id: UUID | None,
    file_ids: set[UUID],
    file_fingerprints: dict[UUID, str | None] | None = None,
) -> dict[UUID, UnlockedCopyRequestState]:
    if not bucket_id or not file_ids:
        return {}
    documents = list(
        (
            await db.execute(
                select(BucketRequestedDocument)
                .where(
                    BucketRequestedDocument.bucket_id == bucket_id,
                    BucketRequestedDocument.requirement_key.like(f"{_REQUIREMENT_PREFIX}:%"),
                    BucketRequestedDocument.status != "not_applicable",
                )
                .order_by(
                    BucketRequestedDocument.created_at.desc(),
                    BucketRequestedDocument.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    if not documents:
        return {}

    document_by_id = {
        document.id: document for document in documents if _source_file_id(document) is not None
    }
    document_ids = list(document_by_id)
    direct_attempt_ids = select(BucketFile.id).where(
        BucketFile.requested_document_id.in_(document_ids)
    )
    attempts = list(
        (
            await db.execute(
                select(BucketFile).where(
                    BucketFile.bucket_id == bucket_id,
                    BucketFile.status == "uploaded",
                    or_(
                        BucketFile.requested_document_id.in_(document_ids),
                        (
                            BucketFile.parent_zip_file_id.in_(direct_attempt_ids)
                            & BucketFile.deleted_at.is_(None)
                        ),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    direct_document_by_file = {
        file.id: file.requested_document_id
        for file in attempts
        if file.requested_document_id in document_by_id
    }
    document_id_by_attempt: dict[UUID, UUID] = dict(direct_document_by_file)
    for file in attempts:
        parent_document_id = direct_document_by_file.get(file.parent_zip_file_id)
        if parent_document_id is not None:
            document_id_by_attempt[file.id] = parent_document_id

    relevant_document_ids: set[UUID] = set()
    for document in documents:
        if _source_file_id(document) in file_ids:
            relevant_document_ids.add(document.id)
    relevant_document_ids.update(
        document_id
        for attempt_id, document_id in document_id_by_attempt.items()
        if attempt_id in file_ids
    )
    if not relevant_document_ids:
        return {}

    def _group_key(
        document: BucketRequestedDocument,
    ) -> tuple[UUID | None, str | None]:
        source = (
            document.requirement_source
            if isinstance(document.requirement_source, dict)
            else {}
        )
        return (
            _source_file_id(document),
            str(source.get("source_content_hash") or "") or None,
        )

    # If an upload points to one of two legacy duplicate tasks, load the whole
    # same-source/fingerprint group. The canonical task and every replacement
    # attempt must share one lifecycle even when the upload originally named a
    # duplicate row that is now hidden from the public room.
    relevant_group_keys = {
        _group_key(document)
        for document in documents
        if document.id in relevant_document_ids
    }
    relevant_document_ids.update(
        document.id
        for document in documents
        if _group_key(document) in relevant_group_keys
    )

    relevant_documents = {
        document.id: document
        for document in documents
        if document.id in relevant_document_ids
    }
    deliveries = list(
        (
            await db.execute(
                select(ApplicationRoomDelivery)
                .where(
                    ApplicationRoomDelivery.requested_document_id.in_(relevant_document_ids),
                    ApplicationRoomDelivery.action_kind == ACTION_KIND,
                )
                .order_by(
                    ApplicationRoomDelivery.requested_document_id,
                    ApplicationRoomDelivery.created_at.desc(),
                    ApplicationRoomDelivery.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    latest_delivery: dict[UUID, ApplicationRoomDelivery] = {}
    for delivery in deliveries:
        if delivery.requested_document_id:
            latest_delivery.setdefault(delivery.requested_document_id, delivery)

    document_groups: dict[
        tuple[UUID | None, str | None], list[BucketRequestedDocument]
    ] = {}
    for document in relevant_documents.values():
        document_groups.setdefault(_group_key(document), []).append(document)
    ordered_documents: list[BucketRequestedDocument] = []
    canonical_document_id_by_document_id: dict[UUID, UUID] = {}
    delivered_document_ids = set(latest_delivery)

    def _group_order(
        candidates: list[BucketRequestedDocument],
    ) -> tuple[bool, tuple[float, str]]:
        source_file_id = _source_file_id(candidates[0])
        source = (
            candidates[0].requirement_source
            if isinstance(candidates[0].requirement_source, dict)
            else {}
        )
        requested_hash = str(source.get("source_content_hash") or "") or None
        matches_current_file = bool(
            file_fingerprints
            and source_file_id is not None
            and requested_hash is not None
            and file_fingerprints.get(source_file_id) == requested_hash
        )
        return (
            matches_current_file,
            max(_document_created_key(document) for document in candidates),
        )

    for candidates in sorted(
        document_groups.values(),
        key=_group_order,
        reverse=True,
    ):
        canonical = _canonical_document(candidates, delivered_document_ids)
        ordered_documents.append(canonical)
        canonical_document_id_by_document_id.update(
            {document.id: canonical.id for document in candidates}
        )

    latest_delivery_by_canonical_document_id: dict[
        UUID, ApplicationRoomDelivery
    ] = {}
    for physical_document_id, delivery in latest_delivery.items():
        canonical_document_id = canonical_document_id_by_document_id.get(
            physical_document_id, physical_document_id
        )
        current = latest_delivery_by_canonical_document_id.get(canonical_document_id)
        if current is None or _delivery_created_key(delivery) > _delivery_created_key(
            current
        ):
            latest_delivery_by_canonical_document_id[canonical_document_id] = delivery

    relevant_attempts = [
        file
        for file in attempts
        if document_id_by_attempt.get(file.id) in relevant_document_ids
        and getattr(file, "deleted_at", None) is None
    ]
    document_id_by_attempt = {
        attempt_id: canonical_document_id_by_document_id.get(document_id, document_id)
        for attempt_id, document_id in document_id_by_attempt.items()
    }
    attempt_ids = {file.id for file in relevant_attempts}
    latest_analysis: dict[UUID, BucketFileAnalysis] = {}
    if attempt_ids:
        analyses = list(
            (
                await db.execute(
                    select(BucketFileAnalysis)
                    .where(BucketFileAnalysis.bucket_file_id.in_(attempt_ids))
                    .order_by(
                        BucketFileAnalysis.bucket_file_id,
                        BucketFileAnalysis.analysis_version.desc(),
                        BucketFileAnalysis.created_at.desc(),
                        BucketFileAnalysis.id.desc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        attempt_by_id = {file.id: file for file in relevant_attempts}
        for analysis in analyses:
            file = attempt_by_id.get(analysis.bucket_file_id)
            if (
                file is not None
                and file.content_hash
                and analysis.content_hash != file.content_hash
            ):
                continue
            latest_analysis.setdefault(analysis.bucket_file_id, analysis)

    review_states_by_document: dict[UUID, list[str]] = {
        document.id: [] for document in ordered_documents
    }
    for file in relevant_attempts:
        document_id = document_id_by_attempt[file.id]
        review_states_by_document[document_id].append(
            public_analysis_state(file, latest_analysis.get(file.id)).analysis_review_state
        )

    states: dict[UUID, UnlockedCopyRequestState] = {}
    for document in ordered_documents:
        document_id = document.id
        source_file_id = _source_file_id(document)
        if source_file_id is None:
            continue
        delivery = latest_delivery_by_canonical_document_id.get(document.id)
        source = (
            document.requirement_source if isinstance(document.requirement_source, dict) else {}
        )
        state = UnlockedCopyRequestState(
            source_file_id=source_file_id,
            source_content_hash=str(source.get("source_content_hash") or "") or None,
            requested_document_id=document.id,
            request_status=document.status,
            delivery_id=delivery.id if delivery else None,
            delivery_status=delivery.status if delivery else None,
            requested_at=document.created_at,
            last_delivery_at=delivery.created_at if delivery else None,
            replacement_review_state=_aggregate_replacement_review_state(
                review_states_by_document[document_id]
            ),
        )
        related_ids = {source_file_id}
        related_ids.update(
            attempt_id
            for attempt_id, attempt_document_id in document_id_by_attempt.items()
            if attempt_document_id == document_id
        )
        for related_id in related_ids & file_ids:
            states.setdefault(related_id, state)
    return states


async def password_protection_for_files(
    db: AsyncSession,
    *,
    bucket_id: UUID,
    files: list[BucketFile],
) -> tuple[
    set[UUID],
    dict[UUID, UnlockedCopyRequestState],
    dict[UUID, PublicFileAnalysisState],
]:
    """Build client-safe lock and replacement state for uploaded-file reads."""

    file_ids = {file.id for file in files}
    files_by_id = {file.id: file for file in files}
    latest: dict[UUID, BucketFileAnalysis] = {}
    if file_ids:
        analyses = list(
            (
                await db.execute(
                    select(BucketFileAnalysis)
                    .where(BucketFileAnalysis.bucket_file_id.in_(file_ids))
                    .order_by(
                        BucketFileAnalysis.bucket_file_id,
                        BucketFileAnalysis.analysis_version.desc(),
                        BucketFileAnalysis.created_at.desc(),
                        BucketFileAnalysis.id.desc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        for analysis in analyses:
            current_file = files_by_id.get(analysis.bucket_file_id)
            if (
                current_file
                and current_file.content_hash
                and analysis.content_hash != current_file.content_hash
            ):
                continue
            latest.setdefault(analysis.bucket_file_id, analysis)
    protected = {file.id for file in files if is_password_protected_file(file, latest.get(file.id))}
    analysis_states = {file.id: public_analysis_state(file, latest.get(file.id)) for file in files}
    requests = await request_states_for_bucket(
        db,
        bucket_id=bucket_id,
        file_ids=file_ids,
        file_fingerprints={
            file.id: _source_content_hash(file, latest.get(file.id)) for file in files
        },
    )
    requests = {
        file_id: current
        for file_id, state in requests.items()
        if (current := current_request_state(files_by_id[file_id], latest.get(file_id), state))
        is not None
    }
    return protected, requests, analysis_states


async def _existing_request_document(
    db: AsyncSession,
    *,
    bucket_id: UUID,
    file: BucketFile,
    analysis: BucketFileAnalysis | None,
    for_update: bool = False,
) -> BucketRequestedDocument | None:
    """Resolve the reusable parent task before considering a new one.

    A replacement upload can itself be locked.  Because that upload points at
    the original requested-document row, reusing the parent keeps one client
    task and prevents an ever-growing chain of "unlocked copy of unlocked
    copy" requests.
    """

    requested_document_id = getattr(file, "requested_document_id", None)
    if requested_document_id is not None:
        statement = select(BucketRequestedDocument).where(
            BucketRequestedDocument.id == requested_document_id,
            BucketRequestedDocument.bucket_id == bucket_id,
        )
        # Read lineage before taking locks. The canonical source row is the
        # serialization lock; taking D1 first would invert A -> document order
        # used by direct-source requests and can deadlock concurrent A/B clicks.
        parent = (await db.execute(statement)).scalar_one_or_none()
        if parent is not None and _source_file_id(parent) is not None:
            return await _current_parent_request_document(
                db,
                bucket_id=bucket_id,
                parent=parent,
                for_update=for_update,
            )

    parent_zip_file_id = getattr(file, "parent_zip_file_id", None)
    if requested_document_id is None and parent_zip_file_id is not None:
        # Extracted ZIP members inherit the archive's upload target through
        # parent_zip_file_id rather than carrying requested_document_id.
        statement = (
            select(BucketRequestedDocument)
            .join(
                BucketFile,
                BucketFile.requested_document_id == BucketRequestedDocument.id,
            )
            .where(
                BucketFile.id == parent_zip_file_id,
                BucketFile.bucket_id == bucket_id,
                BucketRequestedDocument.bucket_id == bucket_id,
            )
        )
        parent = (await db.execute(statement)).scalar_one_or_none()
        if parent is not None and _source_file_id(parent) is not None:
            return await _current_parent_request_document(
                db,
                bucket_id=bucket_id,
                parent=parent,
                for_update=for_update,
            )

    return await _canonical_request_document(
        db,
        bucket_id=bucket_id,
        requirement_key=_request_key(file, analysis),
        for_update=for_update,
    )


async def _current_parent_request_document(
    db: AsyncSession,
    *,
    bucket_id: UUID,
    parent: BucketRequestedDocument,
    for_update: bool,
) -> BucketRequestedDocument:
    """Resolve an attached replacement to the source's current visible task."""

    source_file_id = _source_file_id(parent)
    if source_file_id is None:  # pragma: no cover - caller invariant
        raise StaleUnlockedCopyRequest("The original unlocked-copy request is invalid.")
    source_statement = select(BucketFile).where(
        BucketFile.id == source_file_id,
        BucketFile.status == "uploaded",
        BucketFile.deleted_at.is_(None),
    )
    if for_update:
        source_statement = source_statement.with_for_update()
    source_file = (await db.execute(source_statement)).scalar_one_or_none()
    if source_file is None:
        raise StaleUnlockedCopyRequest(
            "The original locked file is no longer active. Refresh the evidence list before requesting another copy."
        )

    analysis_statement = (
        select(BucketFileAnalysis)
        .where(BucketFileAnalysis.bucket_file_id == source_file_id)
        .order_by(
            BucketFileAnalysis.analysis_version.desc(),
            BucketFileAnalysis.created_at.desc(),
            BucketFileAnalysis.id.desc(),
        )
    )
    if source_file.content_hash:
        analysis_statement = analysis_statement.where(
            BucketFileAnalysis.content_hash == source_file.content_hash
        )
    source_analysis = (await db.execute(analysis_statement.limit(1))).scalar_one_or_none()
    current_hash = _source_content_hash(source_file, source_analysis)
    if not current_hash or not is_password_protected_file(source_file, source_analysis):
        raise StaleUnlockedCopyRequest(
            "The original file is no longer confirmed as password-protected. Refresh the evidence list before requesting another copy."
        )

    current = await _canonical_request_document(
        db,
        bucket_id=bucket_id,
        requirement_key=_request_key(source_file, source_analysis),
        for_update=for_update,
    )
    current_source = (
        current.requirement_source
        if current is not None and isinstance(current.requirement_source, dict)
        else {}
    )
    if (
        current is not None
        and _source_file_id(current) == source_file_id
        and str(current_source.get("source_content_hash") or "") == current_hash
    ):
        return current
    raise StaleUnlockedCopyRequest(
        "This replacement belongs to an obsolete request. Request an unlocked copy from the current locked source file."
    )


async def require_current_unlocked_copy_upload_target(
    db: AsyncSession,
    document: BucketRequestedDocument,
    *,
    for_update: bool = False,
) -> None:
    """Reject a stale room task at both upload-init and upload-complete."""

    if _source_file_id(document) is None:
        return
    current = await _current_parent_request_document(
        db,
        bucket_id=document.bucket_id,
        parent=document,
        for_update=for_update,
    )
    if current.id != document.id:
        raise StaleUnlockedCopyRequest(
            "This upload request was replaced by a newer task. Refresh the application room and upload to the current request."
        )


async def _latest_request_delivery(
    db: AsyncSession, requested: BucketRequestedDocument
) -> ApplicationRoomDelivery | None:
    requirement_key = getattr(requested, "requirement_key", None)
    sibling_document_ids = select(BucketRequestedDocument.id).where(
        BucketRequestedDocument.id == requested.id
        if not requirement_key
        else (
            (BucketRequestedDocument.bucket_id == requested.bucket_id)
            & (BucketRequestedDocument.requirement_key == requirement_key)
            & (BucketRequestedDocument.status != "not_applicable")
        )
    )
    return (
        await db.execute(
            select(ApplicationRoomDelivery)
            .where(
                ApplicationRoomDelivery.requested_document_id.in_(sibling_document_ids),
                ApplicationRoomDelivery.action_kind == ACTION_KIND,
            )
            .order_by(
                ApplicationRoomDelivery.created_at.desc(),
                ApplicationRoomDelivery.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _replacement_attempt_state(
    db: AsyncSession,
    *,
    bucket_id: UUID,
    requested: BucketRequestedDocument,
) -> _ReplacementAttemptState:
    requirement_key = getattr(requested, "requirement_key", None)
    sibling_document_ids = select(BucketRequestedDocument.id).where(
        BucketRequestedDocument.id == requested.id
        if not requirement_key
        else (
            (BucketRequestedDocument.bucket_id == bucket_id)
            & (BucketRequestedDocument.requirement_key == requirement_key)
            & (BucketRequestedDocument.status != "not_applicable")
        )
    )
    direct_attempt_ids = select(BucketFile.id).where(
        BucketFile.requested_document_id.in_(sibling_document_ids)
    )
    attempts = list(
        (
            await db.execute(
                select(BucketFile)
                .where(
                    BucketFile.bucket_id == bucket_id,
                    BucketFile.status == "uploaded",
                    BucketFile.deleted_at.is_(None),
                    or_(
                        BucketFile.requested_document_id.in_(sibling_document_ids),
                        BucketFile.parent_zip_file_id.in_(direct_attempt_ids),
                    ),
                )
                .order_by(BucketFile.created_at.desc(), BucketFile.id.desc())
            )
        )
        .scalars()
        .all()
    )
    if not attempts:
        return _ReplacementAttemptState("requested")

    attempt_by_id = {file.id: file for file in attempts}
    analyses = list(
        (
            await db.execute(
                select(BucketFileAnalysis)
                .where(BucketFileAnalysis.bucket_file_id.in_(attempt_by_id))
                .order_by(
                    BucketFileAnalysis.bucket_file_id,
                    BucketFileAnalysis.analysis_version.desc(),
                    BucketFileAnalysis.created_at.desc(),
                    BucketFileAnalysis.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    latest_analysis: dict[UUID, BucketFileAnalysis] = {}
    for analysis in analyses:
        file = attempt_by_id.get(analysis.bucket_file_id)
        if file is not None and file.content_hash and analysis.content_hash != file.content_hash:
            continue
        latest_analysis.setdefault(analysis.bucket_file_id, analysis)
    review_state = _aggregate_replacement_review_state(
        [
            public_analysis_state(file, latest_analysis.get(file.id)).analysis_review_state
            for file in attempts
        ]
    )
    trigger = attempts[0]
    return _ReplacementAttemptState(
        review_state,
        trigger_file_id=trigger.id,
        trigger_content_hash=getattr(trigger, "content_hash", None),
    )


def _provider_trigger(
    delivery: ApplicationRoomDelivery | None,
) -> tuple[str | None, str | None]:
    provider = (
        getattr(delivery, "provider_result", None)
        if delivery is not None and isinstance(getattr(delivery, "provider_result", None), dict)
        else {}
    )
    return (
        str(provider.get("replacement_trigger_file_id") or "") or None,
        str(provider.get("replacement_trigger_content_hash") or "") or None,
    )


async def _delivery_decision(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    requested: BucketRequestedDocument,
    source_file_id: UUID,
    source_content_hash: str | None,
    send_email: bool,
    retry_failed: bool,
) -> _DeliveryDecision:
    latest = await _latest_request_delivery(db, requested)
    replacement_state: _ReplacementAttemptState | None = None
    trigger_file_id: UUID | None = None
    trigger_content_hash: str | None = None

    if latest is None:
        idempotency_key = _delivery_key(
            profile.id, source_file_id, source_content_hash, ACTION_KIND
        )
        attempt_number = 1
    elif not send_email or not retry_failed:
        return _DeliveryDecision(latest, False, None, latest.attempt_number)
    elif latest.status in {"failed", "created"}:
        replacement_state = await _replacement_attempt_state(
            db,
            bucket_id=profile.primary_bucket_id,
            requested=requested,
        )
        if replacement_state.review_state in {"checking", "received"}:
            return _DeliveryDecision(
                latest,
                False,
                None,
                latest.attempt_number,
                replacement_review_state=replacement_state.review_state,
            )
        idempotency_key = _delivery_key(
            "retry",
            latest.idempotency_key or latest.id,
            latest.attempt_number + 1,
        )
        attempt_number = latest.attempt_number + 1
        raw_trigger_id, trigger_content_hash = _provider_trigger(latest)
        try:
            trigger_file_id = UUID(raw_trigger_id) if raw_trigger_id else None
        except ValueError:
            trigger_file_id = None
        if replacement_state.review_state == "needs_another_copy":
            trigger_file_id = replacement_state.trigger_file_id
            trigger_content_hash = replacement_state.trigger_content_hash
    else:
        replacement_state = await _replacement_attempt_state(
            db,
            bucket_id=profile.primary_bucket_id,
            requested=requested,
        )
        if (
            replacement_state.review_state != "needs_another_copy"
            or replacement_state.trigger_file_id is None
        ):
            return _DeliveryDecision(
                latest,
                False,
                None,
                latest.attempt_number,
                replacement_review_state=replacement_state.review_state,
            )
        trigger_file_id = replacement_state.trigger_file_id
        trigger_content_hash = replacement_state.trigger_content_hash
        latest_trigger_id, latest_trigger_hash = _provider_trigger(latest)
        if latest_trigger_id == str(trigger_file_id) and latest_trigger_hash == (
            trigger_content_hash or None
        ):
            return _DeliveryDecision(
                latest,
                False,
                None,
                latest.attempt_number,
                replacement_review_state=replacement_state.review_state,
                replacement_trigger_file_id=trigger_file_id,
                replacement_trigger_content_hash=trigger_content_hash,
            )
        idempotency_key = _delivery_key(
            "replacement_retry",
            requested.id,
            trigger_file_id,
            trigger_content_hash,
        )
        attempt_number = latest.attempt_number + 1

    duplicate = (
        await db.execute(
            select(ApplicationRoomDelivery).where(
                ApplicationRoomDelivery.idempotency_key == idempotency_key
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        return _DeliveryDecision(
            duplicate,
            False,
            idempotency_key,
            duplicate.attempt_number,
            replacement_review_state=(
                replacement_state.review_state if replacement_state else None
            ),
            replacement_trigger_file_id=trigger_file_id,
            replacement_trigger_content_hash=trigger_content_hash,
        )
    return _DeliveryDecision(
        latest,
        True,
        idempotency_key,
        attempt_number,
        replacement_review_state=(replacement_state.review_state if replacement_state else None),
        replacement_trigger_file_id=trigger_file_id,
        replacement_trigger_content_hash=trigger_content_hash,
    )


async def request_unlocked_copy(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    file: BucketFile,
    analysis: BucketFileAnalysis | None,
    link: BucketUploadLink,
    recipient: str | None,
    user: User,
    send_email: bool = True,
    delivery_note: str | None = None,
    retry_failed: bool = False,
    before_email_delivery: Callable[[], Awaitable[None]] | None = None,
) -> UnlockedCopyRequestOutcome:
    """Create and deliver one idempotent request for an unlocked replacement."""

    requested = await _existing_request_document(
        db,
        bucket_id=profile.primary_bucket_id,
        file=file,
        analysis=analysis,
        for_update=True,
    )
    created_request = requested is None
    if requested is None:
        requirement_key = _request_key(file, analysis)
        source_file_id = file.id
        source_content_hash = _source_content_hash(file, analysis)
        display_name = " ".join(file.file_name.split()) or "document.pdf"
        requested = BucketRequestedDocument(
            bucket_id=profile.primary_bucket_id,
            name=f"Unlocked copy of {display_name}"[:180],
            category="Replacement Documents",
            description=(
                "Please upload a copy that opens without a password. Save or export the PDF "
                "without encryption before uploading it. Do not send the password by email."
            ),
            required=True,
            # A replacement can itself still be locked or corrupt. Keep this
            # task reusable so the borrower can correct it without staff
            # having to manufacture another checklist item.
            allow_multiple_files=True,
            status="requested",
            is_custom=True,
            requirement_key=requirement_key,
            requirement_source={
                "kind": REQUEST_KIND,
                "source_file_id": str(source_file_id),
                "source_content_hash": source_content_hash,
                "source_file_name": display_name[:255],
            },
        )
        db.add(requested)
        await db.flush()
    else:
        raw_source = getattr(requested, "requirement_source", None)
        source = raw_source if isinstance(raw_source, dict) else {}
        source_file_id = _source_file_id(requested) or file.id
        source_content_hash = str(source.get("source_content_hash") or "") or _source_content_hash(
            file, analysis
        )
        display_name = (
            " ".join(str(source.get("source_file_name") or "").split())
            or " ".join(file.file_name.split())
            or "document.pdf"
        )

    room_url = _room_url(link, requested.id)
    decision = await _delivery_decision(
        db,
        profile=profile,
        requested=requested,
        source_file_id=source_file_id,
        source_content_hash=source_content_hash,
        send_email=send_email,
        retry_failed=retry_failed,
    )
    if not decision.should_create:
        if decision.delivery is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("Unlocked-copy delivery decision has no durable row")
        return UnlockedCopyRequestOutcome(
            requested_document=requested,
            delivery=decision.delivery,
            room_url=room_url,
            deduplicated=True,
            created_request=created_request,
            source_file_id=source_file_id,
            replacement_review_state=decision.replacement_review_state,
        )

    attempt_number = decision.attempt_number
    idempotency_key = decision.idempotency_key
    if idempotency_key is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("Unlocked-copy delivery decision has no idempotency key")

    display_name = display_name or "your PDF"
    will_send = bool(send_email and recipient)
    if will_send and before_email_delivery is not None:
        # This hook runs from the final delivery decision, not from a separate
        # preflight query. Training-mode confirmation therefore cannot be
        # skipped if replacement analysis changes while the request is open.
        await before_email_delivery()
    channel = "email" if will_send else "none"
    delivery_status = "sending" if will_send else "created"
    detail = (
        "Email delivery in progress"
        if will_send
        else delivery_note or "Created without sending; no client email is available"
    )
    delivery = ApplicationRoomDelivery(
        profile_id=profile.id,
        bucket_id=profile.primary_bucket_id,
        requested_document_id=requested.id,
        action_kind=ACTION_KIND,
        channel=channel,
        recipient_email=recipient if will_send else None,
        status=delivery_status,
        detail=detail,
        provider_result={
            "accepted": False,
            "message_id": None,
            "source_file_id": str(source_file_id),
            "source_content_hash": source_content_hash,
            "request_kind": REQUEST_KIND,
            "replacement_trigger_file_id": (
                str(decision.replacement_trigger_file_id)
                if decision.replacement_trigger_file_id
                else None
            ),
            "replacement_trigger_content_hash": (decision.replacement_trigger_content_hash),
        },
        created_by_user_id=user.id,
        initiation_source=INITIATION_SOURCE,
        idempotency_key=idempotency_key,
        attempt_number=attempt_number,
    )
    db.add(delivery)
    await db.flush()

    if will_send:
        # Make the idempotency marker durable before crossing the email
        # boundary.  A worker/process crash can now leave a visible manual-link
        # request in "sending", but it cannot cause the next click to emit a
        # duplicate message.  This commit also releases the source-file lock so
        # concurrent clicks can observe and reuse this delivery row.
        await db.commit()
        body = (
            f"We could not open {display_name} because it requires a password.\n\n"
            "Please save or export a new copy with password protection removed, then upload "
            f"that unlocked copy in your secure application room:\n{room_url}\n\n"
            "Do not email or text the document password. For security, your application-room "
            "PIN is not included in this email."
        )
        result = await send_as_user(
            db,
            user.id,
            to_emails=[recipient],
            subject=f"Action needed: upload an unlocked copy of {display_name}"[:998],
            body_text=body,
        )
        delivery.status = "sent" if result.ok else "failed"
        delivery.detail = result.detail
        delivery.provider_result = {
            "accepted": result.ok,
            "message_id": result.message_id,
            "source_file_id": str(source_file_id),
            "source_content_hash": source_content_hash,
            "request_kind": REQUEST_KIND,
            "replacement_trigger_file_id": (
                str(decision.replacement_trigger_file_id)
                if decision.replacement_trigger_file_id
                else None
            ),
            "replacement_trigger_content_hash": (decision.replacement_trigger_content_hash),
        }
    return UnlockedCopyRequestOutcome(
        requested_document=requested,
        delivery=delivery,
        room_url=room_url,
        deduplicated=False,
        created_request=created_request,
        source_file_id=source_file_id,
        replacement_review_state=decision.replacement_review_state,
    )
