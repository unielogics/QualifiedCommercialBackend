from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
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
    DocumentRead,
    DocumentRequest,
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
    stmt = select(Document)
    if loan_id is not None:
        stmt = stmt.where(Document.loan_id == loan_id)
    if client_id is not None:
        # Join on Loan.client_id so a single query covers all of the
        # client's loans without round-tripping the loan list first.
        stmt = stmt.join(Loan, Loan.id == Document.loan_id).where(Loan.client_id == client_id)
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

    When AWS keys are not configured (dev mode), returns the s3_key but a None
    upload_url so the frontend can show a 'configure S3' message.
    """
    settings = get_settings()
    loan = await db.get(Loan, payload.loan_id)
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")

    s3_key = f"loans/{loan.deal_id}/{uuid4()}-{payload.name}"
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
