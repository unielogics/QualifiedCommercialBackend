"""Pydantic schemas for the pre-qualification letter approval workflow.

  PrequalRequestCreate         — borrower submits against existing loan
  PrequalRequestStartCreate    — borrower submits AND we spawn a Loan stub
  PrequalRequestApprove        — admin authorizes + can override numbers
  PrequalRequestReject         — admin rejects with mandatory reason
  PrequalRequestRead           — what the API returns; pdf_url is a fresh
                                 24h presigned GET on every read so URLs
                                 never go stale in the borrower's UI
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


# ── Fix & Flip Scope-of-Work line ─────────────────────────────────────
#
# Each row is a free-form category + brief description + total $. The
# UI guides the borrower / admin through adding rows; the sum is what
# we validate against ARV-based caps. Stored as JSONB on the prequal
# row (see app/models/prequal_request.py.sow_items).
class SowLineItem(BaseModel):
    category: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    total_usd: float = Field(ge=0)


class PrequalRequestCreate(BaseModel):
    target_property_address: str = Field(min_length=3, max_length=500)
    # For F&F this is the BRV (Before Repair Value — what the borrower
    # is paying for the as-is property). For DSCR it's the purchase
    # price (or property value on refi). For Bridge it's the as-is
    # property value. The model field stays `purchase_price` for
    # back-compat across all four products.
    purchase_price: float = Field(gt=0)
    requested_loan_amount: float = Field(gt=0)
    # 4 loan types — DSCR Purchase, DSCR Refinance, Fix & Flip, Bridge.
    # Each has a distinct LTV cap on approve. See router LTV_CAPS.
    loan_type: Literal["dscr_purchase", "dscr_refi", "fix_flip", "bridge"]
    expected_closing_date: date | None = None
    borrower_notes: str | None = Field(default=None, max_length=2000)
    # LLC / entity name. None = "TBD" (borrower hasn't formed the LLC
    # yet); letter falls back to the borrower's individual legal name
    # in that case.
    borrower_entity: str | None = Field(default=None, max_length=500)
    # F&F (and Ground-Up later) collect the after-repair value + scope
    # of work alongside the BRV. Optional on submit so the schema
    # stays compatible with non-rehab products. Validated server-side
    # when loan_type='fix_flip'.
    arv_estimate: float | None = Field(default=None, ge=0)
    sow_items: list[SowLineItem] | None = None


class PrequalRequestStartCreate(PrequalRequestCreate):
    """Same payload as PrequalRequestCreate. Distinguished only by which
    endpoint the borrower hits — the no-loan-yet variant uses this and
    the backend spawns a Loan stub before creating the request."""


class PrequalRequestApprove(BaseModel):
    """Underwriter authorization. The calculator snapshot in
    `approved_scenario` is whatever the operator dialled in inside the
    review panel — its shape varies by loan_type, so JSONB. The PDF and
    the loan-on-conversion both read from it.

    Underwriter notes are saved on the request (visible to borrower in
    the app) but NEVER rendered on the PDF letter."""
    approved_purchase_price: float = Field(gt=0)
    approved_loan_amount: float = Field(gt=0)
    admin_notes: str | None = Field(default=None, max_length=2000)
    approved_scenario: dict[str, Any] | None = None
    # Optional override on the letter's expiration window. None → service
    # uses DEFAULT_LETTER_VALIDITY_DAYS (90 days).
    expiration_days: int | None = Field(default=None, ge=1, le=365)
    # Admin can edit the LLC / entity name — borrower may have submitted
    # "TBD" and now has it, or the original name needs correcting.
    borrower_entity: str | None = Field(default=None, max_length=500)
    # F&F-specific overrides (alembic 0014). Approving admin can edit
    # ARV and the SOW line items independently of what the borrower
    # submitted; the project-viability check (BRV + total_construction
    # vs ARV cap) re-runs server-side against these values.
    approved_arv: float | None = Field(default=None, ge=0)
    approved_sow_items: list[SowLineItem] | None = None
    approved_total_construction: float | None = Field(default=None, ge=0)


class PrequalRequestReject(BaseModel):
    # Reason is mandatory on reject — the borrower will see this verbatim.
    admin_notes: str = Field(min_length=3, max_length=2000)


class PrequalSellerOutcome(BaseModel):
    """Borrower (or super-admin on borrower's behalf) reports back what
    the seller said. Accept creates a Loan + quote_number; decline closes
    the request without a loan."""
    note: str | None = Field(default=None, max_length=1000)


class PrequalRequestRead(ORMModel):
    id: UUID
    # NULLABLE — only set once the borrower marks seller-accepted and we
    # spawn the Loan.
    loan_id: UUID | None
    requester_id: UUID
    target_property_address: str
    purchase_price: float
    requested_loan_amount: float
    # F&F / Ground-Up specifics. Always present in responses (null
    # when not relevant for the product).
    arv_estimate: float | None = None
    sow_items: list[dict[str, Any]] | None = None
    total_construction: float | None = None
    approved_arv: float | None = None
    approved_total_construction: float | None = None
    approved_purchase_price: float | None
    approved_loan_amount: float | None
    approved_scenario: dict[str, Any] | None
    loan_type: str
    expected_closing_date: date | None
    borrower_notes: str | None
    admin_notes: str | None
    borrower_entity: str | None
    status: str
    quote_number: str | None
    # Computed on read by the router (presigned GET URL, 24h TTL).
    # Always None for pending/rejected requests; populated for approved.
    pdf_url: str | None = None
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # alembic 0037 — revision chain. parent_prequal_request_id points
    # to the predecessor in the chain (NULL on originals); superseded_by_id
    # is the next link forward (NULL on the chain head). version_num is
    # 1 for originals, 2/3/... for each successive Updated Version.
    parent_prequal_request_id: UUID | None = None
    superseded_by_id: UUID | None = None
    source_analysis_run_id: UUID | None = None
    version_num: int = 1
