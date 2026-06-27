"""Rate sheet — operator-facing SKU list with super-admin CRUD."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser, GatedUser, require_role
from app.enums import LoanType, Role
from app.models.rate_sheet import RateSheetEntry

router = APIRouter(prefix="/rates", tags=["rates"])


class RateSKU(BaseModel):
    id: str
    label: str
    loan_type: LoanType
    rate: float          # e.g. 7.500 (% as a number)
    points: float
    term: str            # e.g. "30 yr", "12 mo"
    credit_tier: str = "Base"
    min_fico: int
    max_fico: int | None = None
    min_loan_amount: float = 0
    max_loan_amount: float | None = None
    max_ltv: float       # 0..1
    delta_bps: int       # change vs yesterday


class RateSKUCreate(BaseModel):
    id: str = Field(min_length=2, max_length=64)
    label: str = Field(min_length=2, max_length=160)
    loan_type: LoanType
    rate: float = Field(gt=0)
    points: float = Field(ge=0)
    term: str = Field(default="", max_length=32)
    credit_tier: str = Field(default="Base", min_length=1, max_length=80)
    min_fico: int = Field(ge=300, le=850)
    max_fico: int | None = Field(default=None, ge=300, le=850)
    min_loan_amount: float = Field(default=0, ge=0)
    max_loan_amount: float | None = Field(default=None, gt=0)
    max_ltv: float = Field(gt=0, le=1)
    delta_bps: int = 0


class RateSKUUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=2, max_length=160)
    loan_type: LoanType | None = None
    rate: float | None = Field(default=None, gt=0)
    points: float | None = Field(default=None, ge=0)
    term: str | None = Field(default=None, max_length=32)
    credit_tier: str | None = Field(default=None, min_length=1, max_length=80)
    min_fico: int | None = Field(default=None, ge=300, le=850)
    max_fico: int | None = Field(default=None, ge=300, le=850)
    min_loan_amount: float | None = Field(default=None, ge=0)
    max_loan_amount: float | None = Field(default=None, gt=0)
    max_ltv: float | None = Field(default=None, gt=0, le=1)
    delta_bps: int | None = None


# Seed defaults kept for credit-summary matching and first-run DB backfill.
_RATES: list[RateSKU] = [
    RateSKU(id="R-DSCR-30Y-75",  label="DSCR 30Y · 75 LTV",  loan_type=LoanType.DSCR,         rate=7.500, points=1.5,  term="30 yr", min_fico=680, max_ltv=0.75, delta_bps=-8),
    RateSKU(id="R-DSCR-30Y-80",  label="DSCR 30Y · 80 LTV",  loan_type=LoanType.DSCR,         rate=7.625, points=1.25, term="30 yr", min_fico=700, max_ltv=0.80, delta_bps=-5),
    RateSKU(id="R-DSCR-CO-70",   label="DSCR Cash-Out · 70", loan_type=LoanType.CASH_OUT_REFI,rate=8.000, points=1.0,  term="30 yr", min_fico=720, max_ltv=0.70, delta_bps=+10),
    RateSKU(id="R-FF-12-85",     label="Fix & Flip 12mo · 85 LTC", loan_type=LoanType.FIX_AND_FLIP, rate=9.875, points=2.0, term="12 mo", min_fico=660, max_ltv=0.85, delta_bps=0),
    RateSKU(id="R-GU-18-80",     label="Ground Up 18mo · 80 LTC",  loan_type=LoanType.GROUND_UP,    rate=10.250, points=2.5, term="18 mo", min_fico=680, max_ltv=0.80, delta_bps=+15),
    RateSKU(id="R-BRIDGE-24-70", label="Bridge 24mo · 70 LTV",     loan_type=LoanType.BRIDGE,       rate=8.625, points=2.0, term="24 mo", min_fico=680, max_ltv=0.70, delta_bps=-3),
    RateSKU(id="R-PORT-30Y-65",  label="Portfolio 30Y · 65 LTV",    loan_type=LoanType.PORTFOLIO,   rate=7.250, points=1.0, term="30 yr", min_fico=720, max_ltv=0.65, delta_bps=-12),
    RateSKU(id="R-DSCR-30Y-PRE", label="DSCR 30Y · Preferred",      loan_type=LoanType.DSCR,        rate=7.250, points=2.0, term="30 yr", min_fico=720, max_ltv=0.70, delta_bps=-10),
]


def _rate_to_db(rate: float) -> float:
    """Store as decimal in DB while API/UI use percent values."""
    return rate / 100 if rate > 1 else rate


def _rate_from_db(rate: object) -> float:
    value = float(rate or 0)
    return value * 100 if value <= 1 else value


def _term_to_months(term: str | None) -> int | None:
    raw = (term or "").strip().lower()
    if not raw:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", raw)
    if not match:
        return None
    value = float(match.group(1))
    if "yr" in raw or "year" in raw:
        return int(round(value * 12))
    return int(round(value))


def _term_from_months(months: int | None) -> str:
    if not months:
        return ""
    if months % 12 == 0:
        years = months // 12
        return f"{years} yr"
    return f"{months} mo"


def _row_to_read(row: RateSheetEntry) -> RateSKU:
    return RateSKU(
        id=row.sku,
        label=row.label,
        loan_type=LoanType(row.loan_type),
        rate=_rate_from_db(row.base_rate),
        points=float(row.points or 0),
        term=_term_from_months(row.term_months),
        credit_tier=row.credit_tier or "Base",
        min_fico=int(row.min_fico or 680),
        max_fico=int(row.max_fico) if row.max_fico is not None else None,
        min_loan_amount=float(row.min_loan_amount or 0),
        max_loan_amount=float(row.max_loan_amount) if row.max_loan_amount is not None else None,
        max_ltv=float(row.max_ltv or 0),
        delta_bps=int(row.delta_bps or 0),
    )


def _seed_row(rate: RateSKU) -> RateSheetEntry:
    return RateSheetEntry(
        sku=rate.id,
        loan_type=rate.loan_type,
        label=rate.label,
        base_rate=_rate_to_db(rate.rate),
        points=rate.points,
        credit_tier=rate.credit_tier,
        min_fico=rate.min_fico,
        max_fico=rate.max_fico,
        min_loan_amount=rate.min_loan_amount,
        max_loan_amount=rate.max_loan_amount,
        delta_bps=rate.delta_bps,
        max_ltv=rate.max_ltv,
        term_months=_term_to_months(rate.term),
    )


def _validate_bands(
    *,
    min_fico: int | None,
    max_fico: int | None,
    min_loan_amount: float | None,
    max_loan_amount: float | None,
) -> None:
    if max_fico is not None and min_fico is not None and max_fico < min_fico:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Max FICO must be greater than or equal to Min FICO")
    if (
        max_loan_amount is not None
        and min_loan_amount is not None
        and max_loan_amount < min_loan_amount
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Max loan amount must be greater than or equal to Min loan amount")


async def _ensure_rates_seeded(db: AsyncSession) -> None:
    rows = (await db.execute(select(RateSheetEntry))).scalars().all()
    if rows and any(str(row.sku).startswith("R-") for row in rows):
        return

    # The old seed used short demo SKUs (FF-90, DSCR-80, ...). There was no
    # admin editor before this endpoint, so replace that demo set with the
    # current operating rate sheet the UI has been showing from code.
    if rows:
        await db.execute(delete(RateSheetEntry))
    for rate in _RATES:
        db.add(_seed_row(rate))
    await db.flush()


@router.get("", response_model=list[RateSKU])
async def list_rates(
    _: GatedUser,
    db: AsyncSession = Depends(get_db),
) -> list[RateSKU]:
    """Authenticated. CLIENT role gated behind a valid soft credit pull —
    see deps.require_valid_credit_pull. Internal roles (broker, loan_exec,
    super_admin) bypass."""
    await _ensure_rates_seeded(db)
    rows = (
        await db.execute(
            select(RateSheetEntry).order_by(
                RateSheetEntry.loan_type.asc(),
                RateSheetEntry.min_loan_amount.asc(),
                RateSheetEntry.min_fico.asc(),
                RateSheetEntry.sku.asc(),
            )
        )
    ).scalars().all()
    return [_row_to_read(row) for row in rows]


@router.post(
    "",
    response_model=RateSKU,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def create_rate(
    payload: RateSKUCreate,
    _: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RateSKU:
    sku = payload.id.strip().upper()
    existing = (
        await db.execute(select(RateSheetEntry).where(RateSheetEntry.sku == sku))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Rate SKU already exists")
    _validate_bands(
        min_fico=payload.min_fico,
        max_fico=payload.max_fico,
        min_loan_amount=payload.min_loan_amount,
        max_loan_amount=payload.max_loan_amount,
    )
    row = RateSheetEntry(
        sku=sku,
        loan_type=payload.loan_type,
        label=payload.label.strip(),
        base_rate=_rate_to_db(payload.rate),
        points=payload.points,
        credit_tier=payload.credit_tier.strip(),
        min_fico=payload.min_fico,
        max_fico=payload.max_fico,
        min_loan_amount=payload.min_loan_amount,
        max_loan_amount=payload.max_loan_amount,
        delta_bps=payload.delta_bps,
        max_ltv=payload.max_ltv,
        term_months=_term_to_months(payload.term),
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return _row_to_read(row)


@router.patch(
    "/{sku}",
    response_model=RateSKU,
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def update_rate(
    sku: str,
    payload: RateSKUUpdate,
    _: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RateSKU:
    row = (
        await db.execute(select(RateSheetEntry).where(RateSheetEntry.sku == sku.upper()))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rate SKU not found")
    data = payload.model_dump(exclude_unset=True)
    next_min_fico = data.get("min_fico", row.min_fico)
    next_max_fico = data.get("max_fico", row.max_fico)
    next_min_amount = data.get("min_loan_amount", row.min_loan_amount)
    next_max_amount = data.get("max_loan_amount", row.max_loan_amount)
    _validate_bands(
        min_fico=next_min_fico,
        max_fico=next_max_fico,
        min_loan_amount=float(next_min_amount or 0),
        max_loan_amount=float(next_max_amount) if next_max_amount is not None else None,
    )
    if "label" in data and data["label"] is not None:
        row.label = data["label"].strip()
    if "loan_type" in data and data["loan_type"] is not None:
        row.loan_type = data["loan_type"]
    if "rate" in data and data["rate"] is not None:
        row.base_rate = _rate_to_db(data["rate"])
    if "points" in data and data["points"] is not None:
        row.points = data["points"]
    if "term" in data and data["term"] is not None:
        row.term_months = _term_to_months(data["term"])
    if "credit_tier" in data and data["credit_tier"] is not None:
        row.credit_tier = data["credit_tier"].strip()
    if "min_fico" in data and data["min_fico"] is not None:
        row.min_fico = data["min_fico"]
    if "max_fico" in data:
        row.max_fico = data["max_fico"]
    if "min_loan_amount" in data and data["min_loan_amount"] is not None:
        row.min_loan_amount = data["min_loan_amount"]
    if "max_loan_amount" in data:
        row.max_loan_amount = data["max_loan_amount"]
    if "max_ltv" in data and data["max_ltv"] is not None:
        row.max_ltv = data["max_ltv"]
    if "delta_bps" in data and data["delta_bps"] is not None:
        row.delta_bps = data["delta_bps"]
    await db.flush()
    await db.refresh(row)
    return _row_to_read(row)


@router.delete(
    "/{sku}",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def delete_rate(
    sku: str,
    _: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    row = (
        await db.execute(select(RateSheetEntry).where(RateSheetEntry.sku == sku.upper()))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rate SKU not found")
    await db.delete(row)
    await db.flush()
    return {"status": "deleted"}
