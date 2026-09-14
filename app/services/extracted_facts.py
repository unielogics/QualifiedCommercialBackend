from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

# AI providers and legacy importers have used several names for the same
# operator-facing field. Keep this list deliberately explicit: fuzzy matching
# could incorrectly merge related-but-distinct underwriting facts.
_FIELD_ALIASES = {
    "business_activity_code": "naics_code",
    "business_entity_type": "entity_type",
    "business_name": "legal_entity_name",
    "company_legal_name": "legal_entity_name",
    "company_name": "legal_entity_name",
    "entity_name": "legal_entity_name",
    "entity_structure": "entity_type",
    "industry_code": "naics_code",
    "industry_name": "industry",
    "legal_business_name": "legal_entity_name",
    "loan_request_amount": "requested_amount",
    "requested_loan_amount": "requested_amount",
    "sub_industry": "subindustry",
}

# These facts can legitimately have several accepted values on one file. For
# example, a two-year tax package has two tax years and an application can have
# multiple principals. Collapse repeated copies of the same value, but never
# treat a different value as an alternative for a scalar profile field.
_MULTI_VALUE_FIELDS = frozenset(
    {
        "borrower_email",
        "business_email",
        "business_phone",
        "contact_email",
        "contact_phone",
        "email",
        "filing_year",
        "owner_name",
        "phone",
        "primary_owner_name",
        "principal_name",
        "return_type",
        "return_year",
        "tax_form",
        "tax_form_type",
        "tax_year",
    }
)


def canonical_field_key(field_key: str | None) -> str:
    """Return the stable operator-facing identity for an extracted field."""
    normalized = re.sub(r"[^a-z0-9]+", "_", str(field_key or "").casefold()).strip("_")
    return _FIELD_ALIASES.get(normalized, normalized)


def canonical_field_aliases(field_key: str) -> frozenset[str]:
    """Return every explicit storage key that resolves to one logical field."""
    canonical = canonical_field_key(field_key)
    return frozenset(
        {canonical}
        | {alias for alias, target in _FIELD_ALIASES.items() if target == canonical}
    )


def normalized_fact_value(fact: Any) -> str:
    """Return a stable comparison value for duplicate extracted facts."""
    normalized = str(getattr(fact, "normalized_value", "") or "").strip().casefold()
    if normalized:
        return " ".join(normalized.split())
    value = getattr(fact, "value", None)
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return " ".join(str(value or "").strip().casefold().split())


def fact_review_group_key(fact: Any) -> tuple[str, str | None]:
    """Return the review identity for a scalar field or multi-valued fact."""
    canonical = canonical_field_key(getattr(fact, "field_key", None))
    return (
        canonical,
        normalized_fact_value(fact) if canonical in _MULTI_VALUE_FIELDS else None,
    )


def accepted_review_group_keys(facts: Iterable[Any]) -> set[tuple[str, str | None]]:
    return {
        fact_review_group_key(fact)
        for fact in facts
        if getattr(fact, "status", None) == "accepted"
    }


def pending_review_group_keys(facts: Iterable[Any]) -> set[tuple[str, str | None]]:
    """Return actionable groups, excluding suggestions shadowed by acceptance."""
    rows = list(facts)
    accepted = accepted_review_group_keys(rows)
    return {
        fact_review_group_key(fact)
        for fact in rows
        if getattr(fact, "status", None) == "suggested"
        and fact_review_group_key(fact) not in accepted
    }


def facts_resolved_by_review(facts: Iterable[Any], selected: Any, action: str) -> list[Any]:
    """Return the pending rows resolved by one operator decision.

    Acceptance fills the logical field, so every other suggestion in the same
    canonical group is superseded. Rejection applies to every repeated copy of
    the same proposed value, while a genuinely different value remains for the
    operator to review next.
    """
    selected_group = fact_review_group_key(selected)
    selected_value = normalized_fact_value(selected)
    resolved: list[Any] = []
    for fact in facts:
        if getattr(fact, "status", None) != "suggested":
            continue
        if fact_review_group_key(fact) != selected_group:
            continue
        if action == "reject" and normalized_fact_value(fact) != selected_value:
            continue
        resolved.append(fact)
    return resolved
