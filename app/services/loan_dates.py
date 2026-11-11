"""Shared provisional funding and maturity dates for plans and loan versions."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

DATE_NOTE = "Estimated dates may change with funding timing and final closing terms."
PLAN_DATE_FIELDS = ("new_loan_expected_funding_date", "new_loan_maturity_date", "new_loan_maturity_mode")


def as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(ZoneInfo("America/New_York")).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def add_months(value: date, months: int) -> date:
    year, month = value.year + (value.month - 1 + months) // 12, (value.month - 1 + months) % 12 + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def add_business_days(value: date, days: int) -> date:
    remaining = max(0, int(days))
    while remaining:
        value += timedelta(days=1)
        if value.weekday() < 5:
            remaining -= 1
    return value


def loan_dates(expected_funding_date: Any, term_months: Any, maturity_date: Any = None,
               maturity_date_mode: str | None = None) -> dict[str, Any]:
    funding, maturity = as_date(expected_funding_date), as_date(maturity_date)
    term = int(term_months) if term_months is not None and term_months != "" else None
    calculated = add_months(funding, term) if funding and term and 1 <= term <= 600 else None
    mode = maturity_date_mode or ("manual" if maturity and maturity != calculated else "automatic")
    if mode not in {"automatic", "manual"}:
        raise ValueError("Choose automatic or manual maturity dates")
    if term is not None and not 1 <= term <= 600:
        raise ValueError("Loan term must be between 1 and 600 months")
    if mode == "automatic":
        maturity = add_months(funding, term) if funding and term else None
    if mode == "manual" and maturity is None:
        raise ValueError("Enter the manual maturity date or return to automatic calculation")
    if funding and maturity and maturity <= funding:
        raise ValueError("Maturity must be after the estimated funding date")
    return {"expected_funding_date": funding.isoformat() if funding else None,
            "maturity_date": maturity.isoformat() if maturity else None,
            "maturity_date_mode": mode, "maturity_is_estimate": True, "date_note": DATE_NOTE,
            "dates_recorded": bool(funding or maturity)}


def plan_date_fields(raw: dict[str, Any]) -> dict[str, Any]:
    dates = loan_dates(raw.get(PLAN_DATE_FIELDS[0]), raw.get("new_loan_term_months"),
                       raw.get(PLAN_DATE_FIELDS[1]), raw.get(PLAN_DATE_FIELDS[2]))
    return {PLAN_DATE_FIELDS[0]: dates["expected_funding_date"],
            PLAN_DATE_FIELDS[1]: dates["maturity_date"], PLAN_DATE_FIELDS[2]: dates["maturity_date_mode"]}


def preserve_plan_dates(record: dict[str, Any], current: Any, provided: set[str]) -> dict[str, Any]:
    """Old full-write clients retain dates they do not yet understand."""
    current = current if isinstance(current, dict) else {}
    result = dict(record)
    for field in PLAN_DATE_FIELDS:
        if field not in provided and field in current:
            result[field] = current[field]
    result.update(plan_date_fields(result))
    return result


def dates_from_plan(plan: Any) -> dict[str, Any]:
    plan = plan if isinstance(plan, dict) else {}
    return loan_dates(plan.get(PLAN_DATE_FIELDS[0]), plan.get("new_loan_term_months"),
                      plan.get(PLAN_DATE_FIELDS[1]), plan.get(PLAN_DATE_FIELDS[2]))


def client_terms_dates(row: Any) -> dict[str, Any]:
    stored = (getattr(row, "deal_budget_calculation", None) or {}).get("loan_dates")
    if isinstance(stored, dict):
        return loan_dates(stored.get("expected_funding_date"), row.term_months,
                          stored.get("maturity_date"), stored.get("maturity_date_mode"))
    # Historical estimates are anchored to that version, never today's date.
    anchor = getattr(row, "issued_at", None) or getattr(row, "created_at", None)
    funding = add_business_days(as_date(anchor), getattr(row, "closing_estimate_days", 0) or 0) if anchor else None
    return {**loan_dates(funding, row.term_months), "dates_recorded": False}


def firm_today() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def listing_dates(listing: Any) -> dict[str, Any]:
    snapshot = getattr(listing, "source_snapshot", None) or {}
    meta = snapshot.get("loan_dates") or (snapshot.get("financials") or {}).get("proposed_terms") or {}
    # A backfilled version estimate must not overwrite a legacy listing's
    # deliberately saved maturity. Infer its mode from the saved date instead.
    mode = meta.get("maturity_date_mode") if meta.get("expected_funding_date") and meta.get("dates_recorded") is not False else None
    try:
        return loan_dates(meta.get("expected_funding_date"), getattr(listing, "term_months", None),
                          getattr(listing, "maturity_date", None), mode)
    except (ValueError, TypeError, OverflowError):
        return {**loan_dates(None, None, getattr(listing, "maturity_date", None)),
                "date_note": "Review the estimated funding and maturity dates before publishing."}
