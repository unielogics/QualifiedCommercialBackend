"""Canonical Field Desk lead classification and legacy compatibility.

The Field Desk historically implied that every prospect was an auto dealer.
Keep the old inputs readable, but normalize every new write to the small,
stable keys in this module so Marketing, AI Intake, and Portfolio agree on
the same classification.
"""

from __future__ import annotations

from typing import Final, Literal, cast

LeadType = Literal["dealer", "main_street", "real_estate"]
FundingIntent = Literal[
    "general_capital",
    "working_capital",
    "equipment",
    "business_acquisition",
    "real_estate",
    "debt_refinance",
    "mca_refinance",
    "other",
]

DEFAULT_LEAD_TYPE: Final[LeadType] = "dealer"
LEAD_TYPES: Final[tuple[LeadType, ...]] = ("dealer", "main_street", "real_estate")
FUNDING_INTENTS: Final[tuple[FundingIntent, ...]] = (
    "general_capital",
    "working_capital",
    "equipment",
    "business_acquisition",
    "real_estate",
    "debt_refinance",
    "mca_refinance",
    "other",
)

LEAD_TYPE_LABELS: Final[dict[LeadType, str]] = {
    "dealer": "Car dealership",
    "main_street": "Main Street business",
    "real_estate": "Real estate",
}

_LEAD_TYPE_ALIASES: Final[dict[str, LeadType]] = {
    "dealer": "dealer",
    "dealership": "dealer",
    "auto_dealer": "dealer",
    "car_dealer": "dealer",
    "dealer_gatekeeper_v1": "dealer",
    "main_street": "main_street",
    "mainstreet": "main_street",
    "business": "main_street",
    "operating_business": "main_street",
    "main_street_v1": "main_street",
    "mca_refi_v1": "main_street",
    "mca": "main_street",
    "mca_refinance": "main_street",
    "real_estate": "real_estate",
    "realestate": "real_estate",
    "property": "real_estate",
    "real_estate_dscr_v1": "real_estate",
    "funding_review": "real_estate",
    "commercial_foreclosure_bailout_v1": "real_estate",
    "foreclosure_rescue": "real_estate",
}

_FUNDING_INTENT_ALIASES: Final[dict[str, FundingIntent]] = {
    **{key: key for key in FUNDING_INTENTS},  # type: ignore[dict-item]
    "general": "general_capital",
    "capital": "general_capital",
    "floorplan": "general_capital",
    "floor_plan": "general_capital",
    "acquisition": "business_acquisition",
    "purchase_business": "business_acquisition",
    "refinance": "debt_refinance",
    "refinance_debt": "debt_refinance",
    "debt_consolidation": "debt_refinance",
    "mca_refi": "mca_refinance",
    "merchant_cash_advance_refinance": "mca_refinance",
    "property": "real_estate",
    "purchase": "real_estate",
    "cash_out": "real_estate",
    "construction": "real_estate",
}

_INTAKE_VARIANTS: Final[dict[LeadType, str]] = {
    "dealer": "dealer_gatekeeper_v1",
    "main_street": "main_street_v1",
    "real_estate": "real_estate_dscr_v1",
}


def _slug(value: object) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def normalize_lead_type(
    value: object | None,
    *,
    default: LeadType | None = DEFAULT_LEAD_TYPE,
) -> LeadType:
    """Return a canonical lead type or raise for an explicit unknown value."""

    if value is None or not str(value).strip():
        if default is None:
            raise ValueError("lead_type is required")
        return default
    normalized = _LEAD_TYPE_ALIASES.get(_slug(value))
    if normalized is None:
        raise ValueError(f"lead_type must be one of {', '.join(LEAD_TYPES)}")
    return normalized


def normalize_funding_intent(
    value: object | None,
    *,
    allow_none: bool = True,
) -> FundingIntent | None:
    """Normalize both the new intent enum and legacy funding-purpose keys."""

    if value is None or not str(value).strip():
        if allow_none:
            return None
        raise ValueError("funding_intent is required")
    normalized = _FUNDING_INTENT_ALIASES.get(_slug(value))
    if normalized is None:
        raise ValueError(f"funding_intent must be one of {', '.join(FUNDING_INTENTS)}")
    return normalized


def intake_variant_for(
    lead_type: object | None,
    funding_intent: object | None = None,
) -> str:
    """Choose the durable AI Intake variant for a Field Desk classification."""

    normalized_type = normalize_lead_type(lead_type)
    normalized_intent = normalize_funding_intent(funding_intent)
    if normalized_type == "main_street" and normalized_intent == "mca_refinance":
        return "mca_refi_v1"
    return _INTAKE_VARIANTS[normalized_type]


def lead_type_for_intake_variant(value: object | None) -> LeadType:
    """Map existing intake variants back to a Field Desk type."""

    return normalize_lead_type(value)


def legacy_funding_purpose(value: object | None) -> str | None:
    """Project a canonical intent into the pre-upgrade Portfolio vocabulary."""

    intent = normalize_funding_intent(value)
    if intent is None:
        return None
    return {
        "general_capital": "other",
        "working_capital": "working_capital",
        "equipment": "equipment",
        "business_acquisition": "other",
        "real_estate": "real_estate",
        "debt_refinance": "refinance",
        "mca_refinance": "refinance",
        "other": "other",
    }[intent]


def main_street_intent(value: object | None) -> str:
    """Translate canonical intent into the established Main Street taxonomy."""

    intent = normalize_funding_intent(value)
    return {
        "working_capital": "working_capital",
        "equipment": "equipment",
        "debt_refinance": "refinance_debt",
        "general_capital": "not_sure",
        "business_acquisition": "not_sure",
        "real_estate": "property",
        "mca_refinance": "refinance_debt",
        "other": "not_sure",
        None: "not_sure",
    }[intent]


def application_vertical_for(lead_type: object | None) -> LeadType:
    """ApplicationProfile uses the same three canonical vertical keys."""

    return cast(LeadType, normalize_lead_type(lead_type))
