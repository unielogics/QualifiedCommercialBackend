from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel

from app.enums import DocStatus
from app.schemas.common import ORMModel


class DocumentRead(ORMModel):
    id: UUID
    loan_id: UUID
    name: str
    category: str | None
    s3_key: str | None
    status: DocStatus
    requested_on: date | None
    received_on: date | None
    verified_at: datetime | None
    verified_by: str | None
    # AI scan + checklist linkage (alembic 0017)
    checklist_key: str | None = None
    is_other: bool = False
    ai_notes: str | None = None
    ai_scan_status: str = "unscanned"
    ai_scan_confidence: Decimal | None = None


class DocumentRequest(BaseModel):
    loan_id: UUID
    name: str
    category: str | None = None
    due_in_days: int = 7


class DocumentUploadInit(BaseModel):
    loan_id: UUID
    name: str
    content_type: str = "application/pdf"
    # Legacy free-form vault tab — "experience" / "active_asset".
    category: str | None = None

    # ── Categorization (Phase B) ─────────────────────────────────
    # Picks one of three paths in upload-init:
    #   fulfill_document_id set → fill the existing REQUESTED row
    #   checklist_key set       → create a new doc linked to that
    #                              checklist item (no requested row
    #                              existed, e.g. legacy loan)
    #   is_other = True         → off-checklist upload; vision scan
    #                              may auto-link if the model
    #                              recognizes a known type.
    fulfill_document_id: UUID | None = None
    checklist_key: str | None = None
    is_other: bool = False


class DocumentUploadInitResponse(BaseModel):
    document_id: UUID
    upload_url: str | None  # presigned S3 PUT URL; None when S3 not configured
    s3_key: str


class DocumentUploadComplete(BaseModel):
    """Posted from the client after the S3 PUT succeeds. Flips the
    doc to RECEIVED + queues a vision scan."""

    document_id: UUID


# ── Required documents (drives upload modal's checklist picker) ──

class RequiredDocumentRead(BaseModel):
    """One row of the loan's outstanding doc requirements. Joins the
    loan's `LoanTypeChecklist` against existing Document rows so the
    UI knows which slots are filled, in flight, or empty.

    Sentinel "Other" entry has checklist_key=None, is_other=True.
    """

    checklist_key: str | None
    label: str
    required: bool = False
    auto_request: bool = False
    is_other: bool = False
    current_document_id: UUID | None = None
    current_status: DocStatus | None = None
    received_on: date | None = None
    verified_at: datetime | None = None
    days_since_requested: int | None = None
