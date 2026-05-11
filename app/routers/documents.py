from __future__ import annotations

import logging
from datetime import date
from uuid import UUID, uuid4

log = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.enums import DocStatus, Role
from app.models.activity import Activity
from app.models.document import Document
from app.models.loan import Loan
from app.schemas.document import (
    DocumentCustomCreate,
    DocumentPatch,
    DocumentRead,
    DocumentRequest,
    DocumentUploadComplete,
    DocumentUploadInit,
    DocumentUploadInitResponse,
)
from app.services import calendar_emitter
from app.services.activity_log import mark_loan_dirty
from app.services.ai.vector_store import log_event as vector_log

router = APIRouter(prefix="/documents", tags=["documents"])


def _scope_loan(user, loan: Loan) -> bool:
    if user.role == Role.CLIENT and user.client and loan.client_id == user.client.id:
        return True
    if user.role == Role.BROKER and user.broker and loan.broker_id == user.broker.id:
        return True
    return user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC}


@router.get("", response_model=list[DocumentRead])
async def list_documents(
    loan_id: UUID | None = None,
    client_id: UUID | None = None,
    user: CurrentUser = None,
    db: AsyncSession = Depends(get_db),
) -> list[DocumentRead]:
    """List documents. Filters: by loan (scoped to the loan's client),
    or by client (returns all docs across that client's loans — used by
    the operator-side Vault tab on the client profile page)."""
    stmt = select(Document).join(Loan, Loan.id == Document.loan_id)
    if loan_id is not None:
        stmt = stmt.where(Document.loan_id == loan_id)
    if client_id is not None:
        # Filter across all of a client's loans without round-tripping the
        # loan list first.
        stmt = stmt.where(Loan.client_id == client_id)
    if user.role == Role.CLIENT and user.client:
        stmt = stmt.where(Loan.client_id == user.client.id)
    elif user.role == Role.BROKER and user.broker:
        stmt = stmt.where(Loan.broker_id == user.broker.id)
    elif user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")
    rows = (await db.execute(stmt)).scalars().all()
    return [DocumentRead.model_validate(r) for r in rows]


@router.post("/request", response_model=DocumentRead, status_code=status.HTTP_201_CREATED)
async def request_document(
    payload: DocumentRequest, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DocumentRead:
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Clients cannot request docs")
    loan = await db.get(Loan, payload.loan_id)
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    doc = Document(
        loan_id=loan.id,
        name=payload.name,
        category=payload.category,
        status=DocStatus.REQUESTED,
        requested_on=date.today(),
    )
    db.add(doc)
    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=user.id,
            actor_label=user.role,
            kind="document.requested",
            summary=f"Requested: {payload.name}",
        )
    )
    await vector_log(
        db,
        loan_id=loan.id,
        deal_id=loan.deal_id,
        kind="document.requested",
        content=f"Requested document: {payload.name} ({payload.category or 'uncategorized'})",
    )
    await db.flush()
    await db.refresh(doc)
    # Emit a calendar reminder so the borrower / operator sees the
    # doc on their agenda. Default 7-day cadence; Phase 4 swaps the
    # constant for the per-loan-type checklist's first_reminder_days.
    await calendar_emitter.emit_for_document_request(db, doc)
    # Phase 6 — Living Loan File should reflect the new outstanding doc.
    await mark_loan_dirty(db, loan.id)
    await db.flush()
    return DocumentRead.model_validate(doc)


@router.post("/upload-init", response_model=DocumentUploadInitResponse)
async def upload_init(
    payload: DocumentUploadInit, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DocumentUploadInitResponse:
    """Returns a presigned S3 URL the client uses to PUT the file directly.

    Three categorization paths (mutually exclusive in payload):
      - `fulfill_document_id`: fill an existing REQUESTED row with
        the new file. Status stays REQUESTED here; flips to RECEIVED
        on `/upload-complete`.
      - `checklist_key`: create a fresh row linked to that
        checklist item (REQUESTED row didn't exist — legacy loan).
      - `is_other=True`: off-checklist upload; the vision scanner
        may auto-link if it recognizes a known type.

    Legacy callers that send only `category` still work — the row
    is created uncategorized (`checklist_key=None, is_other=False`)
    and operators can re-categorize later. The vision scanner skips
    these uncategorized legacy uploads.
    """
    settings = get_settings()
    loan = await db.get(Loan, payload.loan_id)
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")

    s3_key = f"loans/{loan.deal_id}/{uuid4()}-{payload.name}"

    doc: Document
    if payload.fulfill_document_id is not None:
        doc = await db.get(Document, payload.fulfill_document_id)
        if doc is None or doc.loan_id != loan.id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "fulfill_document_id doesn't belong to this loan",
            )
        if doc.status not in (DocStatus.REQUESTED, DocStatus.PENDING):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Document is already in status={doc.status} — cannot replace via upload-init.",
            )
        # Repoint to the new file. Status stays REQUESTED until the
        # client posts /upload-complete (we treat partial uploads as
        # not-yet-fulfilled).
        doc.s3_key = s3_key
        if payload.name and payload.name != doc.name:
            # Borrower's chosen filename — keep the checklist label
            # but stash the user-visible filename under the row.
            doc.name = payload.name
        if payload.category and not doc.category:
            doc.category = payload.category
    elif payload.checklist_key is not None:
        doc = Document(
            loan_id=loan.id,
            name=payload.name,
            category=payload.category,
            checklist_key=payload.checklist_key,
            is_other=False,
            s3_key=s3_key,
            status=DocStatus.PENDING,
        )
        db.add(doc)
    elif payload.is_other:
        doc = Document(
            loan_id=loan.id,
            name=payload.name,
            category=payload.category,
            checklist_key=None,
            is_other=True,
            s3_key=s3_key,
            status=DocStatus.PENDING,
        )
        db.add(doc)
    else:
        # Legacy path — keeps existing callers (e.g. mobile vault
        # before this update lands) working. Doc gets no checklist
        # link; vision scanner will skip it.
        doc = Document(
            loan_id=loan.id,
            name=payload.name,
            category=payload.category,
            s3_key=s3_key,
            status=DocStatus.PENDING,
        )
        db.add(doc)

    await db.flush()
    await db.refresh(doc)

    upload_url: str | None = None
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        import boto3

        s3 = boto3.client(
            "s3",
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_region,
        )
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.s3_bucket,
                "Key": s3_key,
                "ContentType": payload.content_type,
                "ServerSideEncryption": "AES256",
            },
            ExpiresIn=900,
        )

    return DocumentUploadInitResponse(document_id=doc.id, upload_url=upload_url, s3_key=s3_key)


@router.post("/upload-complete", response_model=DocumentRead)
async def upload_complete(
    payload: DocumentUploadComplete,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DocumentRead:
    """Posted by the client after the S3 PUT succeeds. Flips the doc
    to RECEIVED + queues a vision scan. The scheduler's
    `job_document_scan_drain` picks it up within ~60-120s, writes
    notes back, posts an AI reaction to the loan's chat thread.

    Idempotent — re-calling on an already-received doc is a no-op
    (won't re-queue the scan)."""
    doc = await db.get(Document, payload.document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")

    loan = await db.get(Loan, doc.loan_id)
    if loan is None or not _scope_loan(user, loan):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")

    already_received = doc.status in (DocStatus.RECEIVED, DocStatus.VERIFIED)
    doc.status = DocStatus.RECEIVED
    if doc.received_on is None:
        doc.received_on = date.today()
    # Only flag for scan if we haven't already (and we have an s3 key
    # to actually fetch). Skip uncategorized legacy uploads — the
    # scanner needs at least one of checklist_key / is_other to know
    # what it's checking against.
    if (
        not already_received
        and doc.s3_key
        and (doc.checklist_key is not None or doc.is_other)
    ):
        doc.scan_dirty = True
        doc.ai_scan_status = "queued"

    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=user.id,
            actor_label=user.role,
            kind="document.received",
            summary=f"Received: {doc.name}",
            payload={
                "doc_id": str(doc.id),
                "checklist_key": doc.checklist_key,
                "is_other": doc.is_other,
            },
        )
    )

    # Fire any checklist items anchored on `doc_received:<this name>`
    # — eager event-driven scheduler. e.g. uploading "Bank Statements
    # (2 mo)" might trigger an internal "Order appraisal" AITask
    # with due_offset_days=2. Idempotent: re-firing on a doc that's
    # already RECEIVED is harmless (the dedup keys gate dupes).
    if not already_received and doc.checklist_key:
        try:
            from app.services.checklist_scheduler import fire_anchor_dependents  # local import
            await fire_anchor_dependents(
                db,
                loan=loan,
                anchor_event=f"doc_received:{doc.checklist_key}",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "upload_complete: anchor scheduler failed loan=%s doc=%s",
                loan.deal_id, doc.id,
            )

    await mark_loan_dirty(db, loan.id)
    await db.flush()
    await db.refresh(doc)
    return DocumentRead.model_validate(doc)


# ── Conversational doc-collector: re-route an orphan upload ──────────
#
# Hit by the `confirm_document_routing` chat CTA. The borrower
# uploaded a file via the chat composer and the AI proposed a
# checklist slot ("Looks like Bank Statements — file under that?").
# Tapping confirm sends the doc here; the slot is set and any
# pre-existing REQUESTED row is merged into this doc (so anchors
# fire from a single source of truth).


class DocumentRouteRequest(BaseModel):
    checklist_key: str | None = None
    is_other: bool = False


@router.patch("/{document_id}", response_model=DocumentRead)
async def patch_document(
    document_id: UUID,
    payload: DocumentPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DocumentRead:
    """Partial update. Today only `due_date` is mutable through
    this — set to a date to override the AI's collection schedule
    for this single doc on this loan; pass null in the body to
    clear the override and return to the per-loan-type default.

    The cron honors `due_date` directly when set; the operator can
    accelerate ("collect by Monday") or delay without touching
    settings."""
    doc = await db.get(Document, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    loan = await db.get(Loan, doc.loan_id)
    if loan is None or not _scope_loan(user, loan):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    if user.role == Role.CLIENT:
        # Borrowers can't reschedule their own collection — that's
        # an operator workflow lever.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Borrowers cannot edit due dates",
        )

    sent = payload.model_fields_set
    if "due_date" in sent:
        doc.due_date = payload.due_date
        db.add(
            Activity(
                loan_id=loan.id,
                actor_id=user.id,
                actor_label=user.role,
                kind="document.due_date_changed",
                summary=(
                    f"Due date for {doc.name} → "
                    f"{doc.due_date.isoformat() if doc.due_date else 'default'}"
                ),
                payload={
                    "doc_id": str(doc.id),
                    "due_date": doc.due_date.isoformat() if doc.due_date else None,
                },
            )
        )
    if "status" in sent and payload.status is not None:
        # Operator/agent flips a doc in/out of the AI's collection
        # plan (alembic 0023 added DocStatus.SKIPPED). We only
        # accept the REQUESTED ↔ SKIPPED toggle here — other
        # statuses (RECEIVED, VERIFIED, FLAGGED) are managed by
        # the upload + scan flow and shouldn't be set manually.
        if payload.status not in (DocStatus.REQUESTED, DocStatus.SKIPPED):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Only REQUESTED ↔ SKIPPED transitions are allowed here.",
            )
        old = doc.status
        doc.status = payload.status
        db.add(
            Activity(
                loan_id=loan.id,
                actor_id=user.id,
                actor_label=user.role,
                kind="document.status_changed",
                summary=f"{doc.name}: {old} → {doc.status}",
                payload={
                    "doc_id": str(doc.id),
                    "from": str(old),
                    "to": str(doc.status),
                    "reason": (
                        "skipped_by_operator"
                        if payload.status == DocStatus.SKIPPED
                        else "unskipped_by_operator"
                    ),
                },
            )
        )
    if "name" in sent and payload.name is not None:
        new_name = payload.name.strip()
        if not new_name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "name cannot be empty")
        doc.name = new_name[:255]

    await db.flush()
    await db.refresh(doc)
    return DocumentRead.model_validate(doc)


@router.post("/{document_id}/route", response_model=DocumentRead)
async def route_document(
    document_id: UUID,
    payload: DocumentRouteRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DocumentRead:
    """Relink an orphan upload to a checklist slot.

    Used by the chat's `confirm_document_routing` CTA after the
    vision scan proposed a slot. If a REQUESTED row already exists
    on the same loan with the proposed `checklist_key`, the orphan's
    s3_key + scan results merge into that row and the orphan is
    deleted; otherwise we just rewrite this row's checklist_key /
    is_other and let it stand on its own.

    Idempotent — calling twice with the same key is a no-op the
    second time.
    """
    doc = await db.get(Document, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    loan = await db.get(Loan, doc.loan_id)
    if loan is None or not _scope_loan(user, loan):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")

    target_key = (payload.checklist_key or "").strip() or None

    if target_key is None:
        # Mark explicitly as off-checklist. Vault still shows it.
        doc.is_other = True
        doc.checklist_key = None
        await db.flush()
        await db.refresh(doc)
        return DocumentRead.model_validate(doc)

    # Look for an existing REQUESTED row on this loan with that key.
    target = (
        await db.execute(
            select(Document).where(
                Document.loan_id == loan.id,
                Document.checklist_key == target_key,
                Document.id != doc.id,
                Document.status == DocStatus.REQUESTED,
            )
        )
    ).scalars().first()

    if target is not None:
        # Merge: move the orphan's blob + scan onto the REQUESTED row,
        # then delete the orphan. Anchor scheduler fires from the
        # surviving (target) row to keep dedup correct.
        target.s3_key = doc.s3_key
        target.status = DocStatus.RECEIVED
        if target.received_on is None:
            target.received_on = date.today()
        target.scan_dirty = True
        target.ai_scan_status = "queued"
        if doc.ai_notes:
            target.ai_notes = doc.ai_notes
        if doc.ai_scan_confidence is not None:
            target.ai_scan_confidence = doc.ai_scan_confidence
        db.add(
            Activity(
                loan_id=loan.id,
                actor_id=user.id,
                actor_label=user.role,
                kind="document.routed",
                summary=f"Routed: {doc.name} → {target.name}",
                payload={
                    "orphan_doc_id": str(doc.id),
                    "target_doc_id": str(target.id),
                    "checklist_key": target_key,
                },
            )
        )
        await db.delete(doc)
        await db.flush()
        try:
            from app.services.checklist_scheduler import fire_anchor_dependents
            await fire_anchor_dependents(
                db,
                loan=loan,
                anchor_event=f"doc_received:{target_key}",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "route_document: anchor scheduler failed loan=%s key=%s",
                loan.deal_id, target_key,
            )
        await mark_loan_dirty(db, loan.id)
        await db.refresh(target)
        return DocumentRead.model_validate(target)

    # No pre-existing REQUESTED row — relink in place.
    doc.checklist_key = target_key
    doc.is_other = False
    if doc.status == DocStatus.PENDING:
        doc.status = DocStatus.RECEIVED
    if doc.received_on is None:
        doc.received_on = date.today()
    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=user.id,
            actor_label=user.role,
            kind="document.routed",
            summary=f"Routed: {doc.name} → {target_key}",
            payload={"doc_id": str(doc.id), "checklist_key": target_key},
        )
    )
    await db.flush()
    try:
        from app.services.checklist_scheduler import fire_anchor_dependents
        await fire_anchor_dependents(
            db,
            loan=loan,
            anchor_event=f"doc_received:{target_key}",
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "route_document: anchor scheduler failed loan=%s key=%s",
            loan.deal_id, target_key,
        )
    await mark_loan_dirty(db, loan.id)
    await db.refresh(doc)
    return DocumentRead.model_validate(doc)
