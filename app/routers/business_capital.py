"""QC-branded educational marketing and contact-only intake.

No counterparty identities, source records, numeric terms or policy approvals
are published by this projection. Private catalog approval gates remain intact.
"""
from __future__ import annotations

# ruff: noqa: B008
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.routers.arf import ReferralCreate, save_contact_referral

public_router = APIRouter(prefix="/public/business-capital", tags=["business-capital"])

# Mapping is server-private. Public requests and responses use descriptive slugs.
PRODUCT_KEYS = {
    "business-line-of-credit": "arf_bankroll",
    "interest-only-line-of-credit": "arf_io_bankroll",
    "flexible-payment-line-of-credit": "arf_elevate_bankroll",
    "interest-only-business-loan": "arf_io_flex_pay",
}
INDUSTRY_KEYS = {"plumbing-business": "plumbing", "electrical-contractor": "electrical", "hvac-business": "hvac"}

CATALOG = {
    "products": [
        {
            "slug": "business-line-of-credit", "title": "Business Revolving Line of Credit",
            "description": "Explore a line-of-credit structure for recurring operating needs. Your review considers access to funds, repayment schedules, and how the financing fits your business cash flow.",
            "features": ["Recurring operating needs", "Draw and repayment structure review", "Business cash-flow review"],
        },
        {
            "slug": "interest-only-line-of-credit", "title": "Interest-Only Business Line of Credit",
            "description": "Review a line-of-credit structure with an interest-only feature. Understand when principal is due, how costs accumulate, and what changes when the interest-only period ends.",
            "features": ["Interest-only feature review", "Principal repayment planning", "Total financing cost comparison"],
        },
        {
            "slug": "flexible-payment-line-of-credit", "title": "Flexible-Payment Business Line of Credit",
            "description": "Explore whether a line-of-credit structure can fit changing operating expenses. The review considers payment timing, draw conditions, and the business's ability to repay.",
            "features": ["Payment schedule review", "Seasonal cash-flow planning", "Draw conditions and repayment terms"],
        },
        {
            "slug": "interest-only-business-loan", "title": "Interest-Only Business Loan",
            "description": "Review a business-loan structure with an interest-only feature. Compare the initial payment period, later principal obligations, and total cost against your intended use of funds.",
            "features": ["Initial payment period review", "Later principal obligations", "Use-of-funds and cost comparison"],
        },
    ],
    "industries": [
        {"slug": "plumbing-business", "title": "Plumbing Business Financing", "uses": ["Materials and inventory", "Payroll and project timing", "Vehicles and operating expenses"]},
        {"slug": "electrical-contractor", "title": "Electrical Contractor Financing", "uses": ["Materials and equipment", "Payroll between project milestones", "Contract and operating expenses"]},
        {"slug": "hvac-business", "title": "HVAC Business Financing", "uses": ["Equipment and replacement inventory", "Seasonal working capital", "Payroll and service operations"]},
    ],
}


class ContactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: UUID
    full_name: str = Field(min_length=2, max_length=160)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=32)
    business_name: str | None = Field(default=None, max_length=180)
    product_slug: Literal["business-line-of-credit", "interest-only-line-of-credit", "flexible-payment-line-of-credit", "interest-only-business-loan"] | None = None
    industry_slug: Literal["plumbing-business", "electrical-contractor", "hvac-business"] | None = None
    source: str | None = Field(default=None, max_length=160)
    campaign: str | None = Field(default=None, max_length=160)
    partner: str | None = Field(default=None, max_length=160)
    consent: Literal[True]
    website: str = Field(default="", max_length=0)


@public_router.get("/catalog")
async def catalog():
    return CATALOG


@public_router.post("/referrals", status_code=status.HTTP_201_CREATED)
async def referral(payload: ContactRequest, db: AsyncSession = Depends(get_db)):
    values = payload.model_dump(exclude={"product_slug", "industry_slug"})
    values["program_key"] = PRODUCT_KEYS.get(payload.product_slug)
    values["industry_key"] = INDUSTRY_KEYS.get(payload.industry_slug)
    return await save_contact_referral(ReferralCreate(**values), db)
