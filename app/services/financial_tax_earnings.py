"""Pure normalization of tax earnings operands, without accepting a source.

Only current-period numeric deductions are eligible. A known amortization
deduction can contribute to a labelled estimate while unknown depreciation
remains unknown. Accumulated balances and operating taxes are never addbacks.
"""

from __future__ import annotations

import math
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

_ENTITY_SUFFIXES = {
    "corp", "corporation", "inc", "incorporated", "llc", "llp", "lp", "ltd",
    "limited", "pllc",
}
_ENTITY_SUFFIX_PHRASES = (
    ("professional", "limited", "liability", "company"),
    ("limited", "liability", "company"),
    ("limited", "liability", "partnership"),
)
_INTEREST = ("interest", "interest_expense", "interest_deduction", "interest_paid")
_COMBINED_DA = (
    "depreciation_and_amortization", "depreciation_amortization",
    "depreciation_and_amortization_expense",
)
_DEPRECIATION = (
    "depreciation", "depreciation_expense", "depreciation_current",
    "current_year_depreciation", "depreciation_current_year",
)
_AMORTIZATION = (
    "amortization", "amortization_expense", "amortization_current",
    "current_year_amortization", "amortization_current_year",
    "amortization_within_other_deductions", "other_deductions_detail.amortization",
)
_ORDINARY_BUSINESS_INCOME = (
    "ordinary_business_income", "ordinary_business_income_loss",
    "ordinary_business_income_loss_current",
)
_FORM_KEYS = ("tax_form", "form", "form_type", "return_form", "return_type", "tax_form_type")


def _scalar(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) else value


def _number(value: Any) -> float | None:
    value = _scalar(value)
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip().replace(",", "").replace("$", "")
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1].strip()
    try:
        number = float(Decimal(raw))
        return number if math.isfinite(number) else None
    except (InvalidOperation, OverflowError, ValueError):
        return None


def _first(facts: dict[str, Any], paths: tuple[str, ...]) -> tuple[float | None, list[str]]:
    for path in paths:
        value: Any = facts
        for key in path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        number = _number(value)
        if number is not None:
            return number, [path]
    return None, []


def _entity_words(value: Any) -> tuple[str, ...]:
    raw = str(value or "").casefold().replace("&", " and ")
    # Punctuated legal abbreviations have the same suffix meaning as LLC/LLP.
    raw = re.sub(r"\bp\s*\.\s*l\s*\.\s*l\s*\.\s*c\.?\b", "pllc", raw)
    raw = re.sub(r"\bl\s*\.\s*l\s*\.\s*([cp])\.?\b", r"ll\1", raw)
    raw = re.sub(r"\bl\s*\.\s*p\.?\b", "lp", raw)
    words = re.findall(r"[a-z0-9]+", raw)
    while words:
        matched = next((phrase for phrase in _ENTITY_SUFFIX_PHRASES
                        if tuple(words[-len(phrase):]) == phrase), None)
        if matched:
            del words[-len(matched):]
        elif words[-1] in _ENTITY_SUFFIXES:
            words.pop()
        else:
            break
    return tuple(words)


def entities_match(expected: Any, observed: Any) -> bool:
    """Exact words after punctuation, conjunction and legal-suffix normalization."""
    expected_words = _entity_words(expected)
    return bool(expected_words and expected_words == _entity_words(observed))


def _tax_form(facts: dict[str, Any]) -> str | None:
    forms = " ".join(str(_scalar(facts.get(key)) or "") for key in _FORM_KEYS)
    # A personal return containing business attachments remains a personal
    # source. A caller must scope a separately extracted Schedule C itself.
    if re.search(r"\b1040(?:[\s-]*(?:NR[\s-]*EZ|NR|SR|X))?\b", forms, re.I):
        return "1040"
    if re.search(r"\bschedule\s*k[\s-]*1\b|\bk[\s-]*1\b", forms, re.I):
        return "schedule_k1"
    if re.search(r"\bschedule\s*c\b", forms, re.I):
        return "schedule_c"
    if re.search(r"\b1120[\s-]*s\b", forms, re.I):
        return "1120S"
    if re.search(r"\b1120\b", forms, re.I):
        return "1120"
    if re.search(r"\b1065\b", forms, re.I):
        return "1065"
    return None


def tax_source_is_business(facts: dict[str, Any]) -> bool:
    """Reject personal-return packets; entity matching remains the caller's job.

    Older extractions may omit the form number. Business-named or explicitly
    ordinary-business sources can remain candidates, without gaining a known
    currency or period merely from that legacy fallback.
    """
    form = _tax_form(facts)
    if form in {"1040", "schedule_c", "schedule_k1"}:
        return False
    if form in {"1065", "1120", "1120S"}:
        return True
    return bool(facts.get("entity_name") or facts.get("business_name")
                or any(facts.get(key) is not None for key in _ORDINARY_BUSINESS_INCOME))


def _iso_date(value: Any) -> str | None:
    try:
        return date.fromisoformat(str(_scalar(value))).isoformat()
    except (TypeError, ValueError):
        return None


def _period(facts: dict[str, Any]) -> tuple[str | None, str | None]:
    first, last = _iso_date(facts.get("period_start")), _iso_date(facts.get("period_end"))
    if first and last:
        return first, last
    # A bare tax_year never proves a full calendar period (short/fiscal
    # returns exist). Only an explicit calendar-year phrase supplies bounds.
    for key in ("tax_year", "fiscal_year", "return_year", "reporting_period"):
        value = str(_scalar(facts.get(key)) or "")
        if "calendar year" not in value.casefold():
            continue
        match = re.search(r"\b((?:19|20)\d{2})\b", value)
        if match:
            year = match.group(1)
            return first or f"{year}-01-01", last or f"{year}-12-31"
    return first, last


def normalize_tax_earnings(facts: dict[str, Any]) -> dict[str, Any]:
    """Return selected current-period operands with exact source fact paths.

    ``depreciation`` is the known combined D&A contribution used by legacy
    tax estimates. ``depreciation_complete`` distinguishes a full known D&A
    amount from a known component plus an unknown component.
    """
    interest, interest_paths = _first(facts, _INTEREST)
    combined, combined_paths = _first(facts, _COMBINED_DA)
    depreciation, depreciation_paths = _first(facts, _DEPRECIATION)
    amortization, amortization_paths = _first(facts, _AMORTIZATION)
    if combined is not None:
        known_da = combined
        selected_da_paths = combined_paths
        complete = True
    else:
        known_parts = [value for value in (depreciation, amortization) if value is not None]
        known_da = round(sum(known_parts), 2) if known_parts else None
        selected_da_paths = depreciation_paths + amortization_paths
        complete = depreciation is not None and amortization is not None
    form = _tax_form(facts)
    reported_currency = str(_scalar(facts.get("currency")) or "").strip().upper()
    currency = (reported_currency if re.fullmatch(r"[A-Z]{3}", reported_currency)
                and reported_currency != "XXX" else None)
    if not reported_currency and form in {"1065", "1120", "1120S"}:
        currency = "USD"
    basis = None
    for key in ("basis", "accounting_basis", "accounting_method"):
        raw = re.sub(r"\s+", " ", str(_scalar(facts.get(key)) or "").strip().casefold())
        if raw in {"cash", "cash basis", "cash method"}:
            basis = "cash"
            break
        if raw in {"accrual", "accrual basis", "accrual method"}:
            basis = "accrual"
            break
    first, last = _period(facts)
    return {
        "interest": interest,
        "depreciation": known_da,
        "depreciation_expense": depreciation,
        "amortization": amortization,
        "depreciation_complete": complete,
        "missing_da_components": [] if complete else [key for key, value in (
            ("depreciation", depreciation), ("amortization", amortization)) if value is None],
        "component_fact_paths": {
            "interest": interest_paths,
            "depreciation": selected_da_paths,
            "depreciation_expense": depreciation_paths,
            "amortization": amortization_paths,
        },
        "currency": currency,
        "basis": basis,
        "period_start": first,
        "period_end": last,
        "form": form,
        "business_tax_source": tax_source_is_business(facts),
    }
