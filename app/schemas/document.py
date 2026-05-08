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
    # Per-loan due date (alembic 0022). When set, overrides the
    # default `requested_on + due_offset_days` math; the cron uses
    # whatever non-null value lives here. NULL = computed default.
    due_date: date | None = None


class DocumentPatch(BaseModel):
    """Partial update — currently used by the loan detail's
    Workflow tab to set/clear `due_date`. Fields are independently
    optional; missing keys leave the column alone, explicit `null`
    clears the override (returns to computed default)."""

    due_date: date | None = None
    # Sentinel string the PATCH handler reads to distinguish
    # "leave alone" (key absent) from "clear" (key=null). pydantic
    # collapses both to None, so the handler uses model_fields_set.


class WorkflowDocRead(BaseModel):
    """One row of the loan's Workflow tab — the AI's collection
    plan for that document. Joins the Document with the per-loan-type
    checklist offset + the live scenario classifier so the operator
    sees exactly what the AI will do next.

    Surfaced by `GET /loans/{loan_id}/workflow`. Read-only; mutations
    go through PATCH /documents/{id}.
    """

    document_id: UUID
    name: str
    status: DocStatus
    checklist_key: str | None
    is_other: bool
    requested_on: date | None
    received_on: date | None
    # The actual due_date stored on the row (NULL when default).
    due_date: date | None
    # The default that would apply if `due_date` is cleared —
    # `requested_on + due_offset_days` (per-item if available, else
    # the loan-type's `first_reminder_days`).
    default_due_date: date | None
    # The date the cron will use today. Equals `due_date` if set,
    # otherwise `default_due_date`. NULL only when both are unknown
    # (e.g. requested_on missing on a malformed legacy row).
    effective_due_date: date | None
    # Positive = before due, negative = overdue, 0 = due today.
    # NULL when no effective due date.
    days_until_due: int | None
    # Active collection scenario or None when out of range
    # (4+ days before due, or doc not in REQUESTED status).
    scenario: str | None
    # The next scenario the doc will transition into (and how many
    # days from today). Helps the operator see what's coming.
    next_scenario: str | None = None
    next_scenario_in_days: int | None = None


class WorkflowRunResult(BaseModel):
    """Returned by POST /loans/{loan_id}/run-doc-reminders. Echoes
    the per-scenario count so the UI can show "1 just_late, 2
    heads_up sent" alongside any chat preview."""

    counts: dict[str, int]


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
