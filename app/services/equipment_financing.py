"""Canonical preliminary screen for QC Equipment Financing.

This module intentionally contains no lender pricing.  It gives the legacy
dealer and Main Street screens the same bounded, explainable result while the
versioned funding-program engine remains the authoritative selection source.
Unknown facts are requests for information, never failed eligibility tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PROGRAM_KEY = "equipment_financing"
PUBLIC_SLUG = "equipment-financing"
MINIMUM_AMOUNT = 25_000.0
MAXIMUM_AMOUNT = 2_000_000.0
MINIMUM_TERM_MONTHS = 24
MAXIMUM_TERM_MONTHS = 84
MINIMUM_BUSINESS_AGE_YEARS = 1.0
MINIMUM_ANNUAL_REVENUE = 100_000.0
MINIMUM_CREDIT_SCORE = 640.0

EQUIPMENT_DETAIL_FIELDS = (
    "description",
    "age_years",
    "condition",
    "remaining_useful_life_years",
    "seller_name",
    "seller_contact",
    "seller_type",
    "purchase_price",
    "delivery_timing",
    "intended_business_use",
)

_EQUIPMENT_INTENTS = {
    "equipment",
    "equipment_finance",
    "equipment_financing",
    "machinery",
    "vehicle_financing",
}


def normalize_funding_intent(value: object | None) -> str | None:
    """Normalize a public attribution value without guessing a different need."""

    if value is None:
        return None
    normalized = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized in _EQUIPMENT_INTENTS:
        return "equipment"
    return normalized or None


def is_equipment_funding_intent(value: object | None) -> bool:
    return normalize_funding_intent(value) in _EQUIPMENT_INTENTS


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number


def assess_equipment_fit(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Return a tri-state, borrower-safe preliminary equipment screen.

    A known failed threshold produces ``not_eligible``.  If every known fact
    passes but at least one required fact is absent, the result is
    ``needs_information``.  Only a complete passing set is ``recommended``.
    This mirrors the versioned rule engine and prevents blank values from being
    silently coerced to zero.
    """

    intent = facts.get("equipment_financing_intent")
    age = _number(facts.get("business_age_years"))
    revenue = _number(facts.get("annual_revenue"))
    credit = _number(facts.get("credit_score"))
    cash_flow = _number(facts.get("cash_flow"))
    amount = _number(facts.get("requested_amount"))

    missing: list[str] = []
    failed: list[str] = []

    if intent is None:
        missing.append("Confirm that the request is for equipment financing")
    elif intent is not True:
        failed.append("The stated funding need is not equipment financing")

    checks = (
        (
            age,
            MINIMUM_BUSINESS_AGE_YEARS,
            "Time in business",
            "time in business below the program minimum (at least one year)",
        ),
        (revenue, MINIMUM_ANNUAL_REVENUE, "Annual revenue", "At least $100,000 in annual revenue"),
        (credit, MINIMUM_CREDIT_SCORE, "Estimated credit score", "Estimated credit score of 640 or higher"),
    )
    for actual, minimum, missing_label, failure_label in checks:
        if actual is None:
            missing.append(missing_label)
        elif actual < minimum:
            failed.append(failure_label)

    if cash_flow is None:
        missing.append("Positive cash flow")
    elif cash_flow <= 0:
        failed.append("Positive cash flow is required")

    amount_outside_published_range = False
    if amount is None:
        missing.append("Requested amount")
        request_classification = "needs_information"
    elif amount < MINIMUM_AMOUNT or amount > MAXIMUM_AMOUNT:
        amount_outside_published_range = True
        missing.append(
            "Custom review for a request outside the published $25,000 to $2,000,000 range"
        )
        request_classification = "custom_review"
    else:
        request_classification = "published_program"

    information_missing = [
        item for item in missing if not item.startswith("Custom review for a request")
    ]
    if failed:
        status = "not_eligible"
    elif information_missing:
        status = "needs_information"
        request_classification = "needs_information"
    elif amount_outside_published_range:
        # The published product is bounded, but QC may still have another
        # structure.  Keep this out of ordinary eligibility without presenting
        # a file-level rejection.
        status = "custom_review"
    else:
        status = "recommended"

    return {
        "eligible": status == "recommended",
        "recommendation_status": status,
        "blocked_by": failed,
        "needs": missing,
        "request_classification": request_classification,
        "published_amount_min": MINIMUM_AMOUNT,
        "published_amount_max": MAXIMUM_AMOUNT,
        "term_min_months": MINIMUM_TERM_MONTHS,
        "term_max_months": MAXIMUM_TERM_MONTHS,
    }
