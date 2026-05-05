"""Module 7 — Hard lending limits and broker pricing defaults.

Validators in services/lender_matrix.py reference these. Don't hard-code
the numbers anywhere else.
"""

from __future__ import annotations

# DSCR loans
DSCR_MAX_LTV_PURCHASE: float = 0.80
DSCR_MAX_LTV_CASH_OUT: float = 0.75
DSCR_MIN_FICO: int = 680
DSCR_MIN_RATIO_STANDARD: float = 1.00
DSCR_MIN_RATIO_PREFERRED: float = 1.20

# Fix & Flip / Bridge
FF_MAX_LTC: float = 0.85
FF_MAX_ARV_LTV: float = 0.70
FF_TERM_MIN_MONTHS: int = 12
FF_TERM_MAX_MONTHS: int = 18

# Pricing (Module 8)
DEFAULT_BROKER_ORIGINATION_PCT: float = 0.0150  # 1.50%
RATE_DROP_PER_DISCOUNT_POINT: float = 0.0025    # 25 bps per 1.00% point
DEFAULT_MAX_BUY_DOWN_POINTS: float = 3.0

# HUD-1 fixed fees (Module 10)
HUD_LENDER_UNDERWRITING_FEE: int = 1295
HUD_PROCESSING_FEE: int = 795
HUD_APPRAISAL_FEE_SFR: int = 650
HUD_APPRAISAL_FEE_2_4_UNITS: int = 850
HUD_INSPECTION_FEE_FLIPS: int = 250
HUD_RECORDING_FEES: int = 250
HUD_TITLE_INSURANCE_RATE: float = 0.005  # 0.5% of loan amount

# Soft pull
SOFT_PULL_VALIDITY_DAYS: int = 90

# Broker tiers (lifetime points = funded $)
BROKER_TIER_THRESHOLDS = {
    "bronze": 0,
    "silver": 5_000_000,
    "gold": 15_000_000,
    "platinum": 35_000_000,
}
