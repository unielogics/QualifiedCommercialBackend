"""Standard QC MCA policy, separate from the MCA refinance product.

Public answers are declarations, never verified financial evidence. The
published schedule and accepted, entity-scoped tax return control qualification.
Factor pricing describes this new product only; it does not constrain source
loans or turn a factor into an APR.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.services import debt_types
from app.services.financial_version_selection import (
    active_debt_schedule_rows,
    debt_schedule_completion,
)

PROGRAM_KEY = "merchant_cash_advance"
EVIDENCE_POLICY_KEY = "mca_funding_baseline"
POLICY_VERSION = "qc-mca-funding-2026-11-09-v1"
MIN_FACTOR = Decimal("1.22")
MAX_FACTOR = Decimal("1.38")
MAX_EXISTING_MCAS = 2


def current_date():
    return datetime.now(ZoneInfo("America/New_York")).date()


def required_tax_year() -> int:
    return current_date().year - 1


def required_statement_months() -> list[str]:
    today = current_date()
    current = today.year * 12 + today.month - 1
    return [f"{index // 12:04d}-{index % 12 + 1:02d}" for index in range(current - 6, current)]


def intake_documents() -> list[dict[str, Any]]:
    """Exactly the standard product's three collection lines, any industry."""
    return [
        {"name": "Business bank statements — last 6 months", "category": "Bank Statements",
         "description": "Six consecutive complete statement months for the business's accounts, with all pages. One combined PDF may cover multiple months.",
         "allow_multiple_files": True, "required": True},
        {"name": "Business debt schedule", "category": "Debts",
         "description": "A current complete business debt schedule, including each active MCA and whether its terms or remittance have been modified.",
         "allow_multiple_files": True, "required": True},
        {"name": "Business tax return — prior calendar year", "category": "Tax Returns",
         "description": f"The complete {required_tax_year()} business tax return. Review reported ordinary business income before interest, depreciation or amortization addbacks. An older or personal return does not substitute.",
         "allow_multiple_files": True, "required": True},
    ]


def declaration_metadata() -> dict[str, Any]:
    return {"policy_version": POLICY_VERSION, "program_key": PROGRAM_KEY,
            "maximum_existing_active_mcas": MAX_EXISTING_MCAS,
            "mca_count_basis": "existing_before_new_advance",
            "tax_income_basis": "reported_ordinary_business_income_before_addbacks",
            "required_tax_year": required_tax_year(),
            "factor_range": {"minimum": str(MIN_FACTOR), "maximum": str(MAX_FACTOR)},
            "verification_status": "pending_document_review"}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def assess_evidence(*, schedule_body: dict | None, tax_records: Sequence[Mapping],
                    bank_months: Sequence[str], business_entity: str | None,
                    bank_accounts: Mapping[str, Sequence[str]] | None = None) -> dict[str, Any]:
    """Tri-state screen over accepted evidence, never uploaded-file counts.

    Tax records must come from financial_cockpit.tax_years, which enforces
    business-source and reviewed identity association. Missing or conflicting
    ordinary income stays unknown even when EBITDA/other income is available.
    """
    needs, failures = [], []
    rows = active_debt_schedule_rows(schedule_body)
    complete = schedule_body is not None and debt_schedule_completion(schedule_body)["complete"]
    count, modified = None, None
    if not complete:
        needs.append("Save a completed business debt schedule.")
    else:
        types = [debt_types.normalize_debt_type(row.get("debt_type") or row.get("category")) for row in rows]
        if any(value not in debt_types.DEBT_TYPE_SLUGS or value == "other" for value in types):
            needs.append("Classify each active obligation to confirm the existing MCA count.")
        else:
            advances = [row for row, kind in zip(rows, types, strict=True) if debt_types.is_advance_type(kind)]
            count = len(advances)
            if count > MAX_EXISTING_MCAS:
                failures.append("More than 2 existing active MCAs before the new advance.")
            # The worksheet stores explicit yes/no enum answers; older debt
            # projections store booleans. Blank or other values remain unknown.
            flags = [{"yes": True, "no": False}.get(value, value) if isinstance(value, str) else value
                     for row in advances for value in [row.get("mca_modified")]]
            if any(value is True for value in flags):
                modified = True
                failures.append("An existing active MCA has been modified.")
            elif all(value is False for value in flags):
                modified = False
            else:
                needs.append("Confirm whether every existing active MCA has been modified.")

    year = required_tax_year()
    candidates = [row for row in tax_records if row.get("accepted") is True and str(row.get("year")) == str(year)
                  and row.get("tax_form") in {"1120S", "1065", "1120"}]
    # An identity-free file cannot establish whose business income was reported.
    values = [_number(row.get("ordinary_business_income")) for row in candidates] if business_entity else []
    income = values[0] if values and all(value is not None and value == values[0] for value in values) else None
    if income is None:
        needs.append(f"Review the {year} business return's reported ordinary business income before addbacks for this business.")
    elif income < 0:
        failures.append(f"Reported ordinary business income on the {year} business return is negative before addbacks.")
    expected_months = set(required_statement_months())
    missing_by_account = ({str(key): sorted(expected_months - set(months)) for key, months in bank_accounts.items()}
                          if bank_accounts else {})
    missing_months = sorted(expected_months - set(bank_months)
                            | {month for months in missing_by_account.values() for month in months})
    if bank_accounts is not None and not bank_accounts:
        needs.append("Identify the business accounts covered by the accepted bank statements.")
    if missing_months:
        needs.append("Accept the last 6 complete statement months: " + ", ".join(missing_months) + ".")
    return {**declaration_metadata(), "verification_status": "evidence_review",
            "active_mca_count": count, "has_modified_mca": modified,
            "prior_year_ordinary_business_income": income, "tax_year": year,
            "missing_statement_months": missing_months,
            "missing_statement_months_by_account": missing_by_account,
            "recommendation_status": "not_eligible" if failures else "needs_information" if needs else "recommended",
            "auto_underwriting_allowed": not failures and not needs,
            "reasons": failures + needs,
            "source_refs": {"tax_files": [str(row.get("file_id")) for row in candidates]}}
