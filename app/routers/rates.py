"""Rate sheet — exposes the operator-facing SKU list.

Today the rate sheet is a curated set of synthetic SKUs. When market data
ingestion lands (Pricing agent), this endpoint will read from a pricing
table populated by the daily rate-sheet pull.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.deps import GatedUser
from app.enums import LoanType

router = APIRouter(prefix="/rates", tags=["rates"])


class RateSKU(BaseModel):
    id: str
    label: str
    loan_type: LoanType
    rate: float          # e.g. 7.500 (% as a number)
    points: float
    term: str            # e.g. "30 yr", "12 mo"
    min_fico: int
    max_ltv: float       # 0..1
    delta_bps: int       # change vs yesterday


# Synthetic rate sheet — replace with DB-backed table when pricing service ships.
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


@router.get("", response_model=list[RateSKU])
async def list_rates(_: GatedUser) -> list[RateSKU]:
    """Authenticated. CLIENT role gated behind a valid soft credit pull —
    see deps.require_valid_credit_pull. Internal roles (broker, loan_exec,
    super_admin) bypass."""
    return _RATES
