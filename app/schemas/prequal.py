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
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class PrequalRequestCreate(BaseModel):
    target_property_address: str = Field(min_length=3, max_length=500)
    purchase_price: float = Field(gt=0)
    requested_loan_amount: float = Field(gt=0)
    loan_type: Literal["dscr", "bridge"]
    expected_closing_date: date | None = None
    borrower_notes: str | None = Field(default=None, max_length=2000)


class PrequalRequestStartCreate(PrequalRequestCreate):
    """Same payload as PrequalRequestCreate. Distinguished only by which
    endpoint the borrower hits — the no-loan-yet variant uses this and
    the backend spawns a Loan stub before creating the request."""


class PrequalRequestApprove(BaseModel):
    approved_purchase_price: float = Field(gt=0)
    approved_loan_amount: float = Field(gt=0)
    admin_notes: str | None = Field(default=None, max_length=2000)


class PrequalRequestReject(BaseModel):
    # Reason is mandatory on reject — the borrower will see this verbatim.
    admin_notes: str = Field(min_length=3, max_length=2000)


class PrequalRequestRead(ORMModel):
    id: UUID
    loan_id: UUID
    requester_id: UUID
    target_property_address: str
    purchase_price: float
    requested_loan_amount: float
    approved_purchase_price: float | None
    approved_loan_amount: float | None
    loan_type: str
    expected_closing_date: date | None
    borrower_notes: str | None
    admin_notes: str | None
    status: str
    # Computed on read by the router (presigned GET URL, 24h TTL).
    # Always None for pending/rejected requests; populated for approved.
    pdf_url: str | None = None
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime
