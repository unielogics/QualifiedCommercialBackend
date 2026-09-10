"""What a lender on the roster can carry.

`Lender.products` used to be validated against `LoanType`, which is the six
real-estate loan types and nothing else — it is `Loan.type`, it drives the
rate-sheet SKUs and the DSCR/LTV validators, and it is generated into the
frontends. That was the right vocabulary when every counter-party was a
real-estate lender. It is the wrong one now that the roster also holds the
business-lending programmes and the service partners (a card-processing
partner most of all), because a processing partner is not a legal `Loan.type`
and must never become one.

So the roster gets its own list. It is a plain dict on purpose — not a
`StrEnum` in `app/enums.py`, which the TypeScript generator would emit — and
it is deliberately a superset of the six lists the firm already keeps
(`LoanType`, `LoanProgram`, `MAIN_STREET_PROGRAM_LABELS`, the Capital OS
product catalogue, `product_finder`, the marketing site), so nothing the desk
sells is unnameable here. The real-estate group IS `LoanType`, unchanged:
those are the only products that can match a `Loan.type`, and the loan
Connect-Lender dropdown keeps working exactly as it did.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.enums import LoanProgram, LoanType
from app.services.main_street_programs import MAIN_STREET_PROGRAM_LABELS

MERCHANT_PROCESSING = "merchant_processing"

REAL_ESTATE_PRODUCTS: tuple[str, ...] = tuple(t.value for t in LoanType)

BUSINESS_LENDING_PRODUCTS: tuple[str, ...] = (
    "sba",
    "sba_grocery",
    "sba_made_in_america",
    "term_loan_3_5_year",
    "term_loan_10_year",
    "term_loan_loc_hybrid",
    "jumbo_term_loan",
    "line_of_credit",
    "equipment_financing",
    "transportation_finance",
    "real_estate_backed",
    "jumbo_dscr",
    "reinsurance_backed",
    "mca_refinance",
)

SERVICE_PRODUCTS: tuple[str, ...] = (
    MERCHANT_PROCESSING,
    "transportation_factoring",
    "debt_consulting",
    "business_systems",
)

LENDER_PRODUCT_GROUPS: dict[str, tuple[str, ...]] = {
    "real_estate": REAL_ESTATE_PRODUCTS,
    "business_lending": BUSINESS_LENDING_PRODUCTS,
    "services": SERVICE_PRODUCTS,
}

GROUP_LABELS: dict[str, str] = {
    "real_estate": "Real estate",
    "business_lending": "Business lending",
    "services": "Services",
}

_EXTRA_LABELS: dict[str, str] = {
    "dscr": "DSCR Rental",
    "fix_and_flip": "Fix & Flip",
    "ground_up": "Ground-Up Construction",
    "bridge": "Bridge",
    "portfolio": "Portfolio",
    "cash_out_refi": "Cash-Out Refinance",
    "jumbo_dscr": "Jumbo DSCR",
    "reinsurance_backed": "Reinsurance-backed",
    "mca_refinance": "MCA Refinance",
    "business_systems": "Business Systems (POS)",
}

LENDER_PRODUCT_LABELS: dict[str, str] = {
    key: _EXTRA_LABELS.get(key) or MAIN_STREET_PROGRAM_LABELS.get(key) or key.replace("_", " ").title()
    for group in LENDER_PRODUCT_GROUPS.values()
    for key in group
}

LENDER_PRODUCT_KEYS: frozenset[str] = frozenset(LENDER_PRODUCT_LABELS)

# The public capital-partner application form offers ids the roster never
# had. Promotion used to pass them through unvalidated, and the roster then
# refused to load. These are the honest mappings; anything else is dropped.
CAPITAL_PARTNER_ALIASES: dict[str, str] = {
    "sba_7a": "sba",
    "sba_7(a)": "sba",
    "commercial": "real_estate_backed",
    "multifamily": "dscr",
}


def is_known_product(key: str) -> bool:
    return key in LENDER_PRODUCT_KEYS


def group_of(key: str) -> str | None:
    for group, keys in LENDER_PRODUCT_GROUPS.items():
        if key in keys:
            return group
    return None


def normalize_products(values: Iterable[object]) -> list[str]:
    """Strings, de-duplicated, order kept. Raises ValueError on a key the
    roster does not know, naming it, so a typo surfaces as a 422 rather than
    as a row nothing can match."""
    out: list[str] = []
    for value in values or []:
        key = value.value if hasattr(value, "value") else str(value)
        key = key.strip()
        if not key:
            continue
        if key not in LENDER_PRODUCT_KEYS:
            raise ValueError(f"Unknown lender product: {key!r}")
        if key not in out:
            out.append(key)
    return out


def has_lending_product(products: Iterable[str] | None) -> bool:
    """Whether any product can match a Loan.type — the real-estate group."""
    return any(p in REAL_ESTATE_PRODUCTS for p in (products or []))


def _self_check() -> None:
    # Every vocabulary the firm keeps is representable here.
    missing = [p.value for p in LoanProgram if p.value not in LENDER_PRODUCT_KEYS]
    missing += [k for k in MAIN_STREET_PROGRAM_LABELS if k not in LENDER_PRODUCT_KEYS]
    if missing:  # pragma: no cover - a build-time guard
        raise RuntimeError(f"lender_products is missing {missing}")


_self_check()
