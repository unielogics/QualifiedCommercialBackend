"""Auditable Step 1.5 screening backed by the shared QC routing engine."""

from __future__ import annotations

from typing import Any

from .lender_neutral_routing import RULES_VERSION, evaluate_direct_programs

REQUIRED_OWNER_FIELDS = (
    "residency_status",
    "credit_660_or_higher",
    "bankruptcy_timing",
    "foreclosure_within_3_years",
    "felony_timing",
    "misdemeanor_within_5_years",
    "misdemeanor_involving_minor",
    "arrest_within_6_months",
    "financial_related_crime",
    "active_legal_charges",
    "ofac_match",
)
LEGACY_OWNER_FIELDS = ("citizen_or_lpr",)
ALLOWED_OWNER_FIELDS = frozenset(REQUIRED_OWNER_FIELDS + LEGACY_OWNER_FIELDS)
REQUIRED_FILE_FIELDS = (
    "refinance_debt",
    "open_tax_liens",
    "tax_liability_over_10000",
    "open_judgments",
    "open_civil_actions_as_defendant",
    "civil_action_financial_institution_within_10_years",
    "judgment_over_2000_within_12_months",
    "judgment_over_50000_within_7_years",
    "aggregate_liens_judgments_over_25000_within_7_years",
    "speculative_real_estate_flipping",
    "gambling_or_bail_bonds",
    "lending_investment_crypto_mlm",
    "nonprofit_or_government",
    "marijuana_or_firearms",
    "prurient_business",
    "auto_or_title_asset_sales",
)
CONDITIONAL_FILE_FIELDS = (
    "tax_payment_plan_current",
    "term_obligations_released_or_on_plan",
)
LEGACY_FILE_FIELDS = ("open_tax_liens_or_judgments",)
ALLOWED_FILE_FIELDS = frozenset(
    REQUIRED_FILE_FIELDS + CONDITIONAL_FILE_FIELDS + LEGACY_FILE_FIELDS
)
BANKRUPTCY_VALUES = {"none", "within_3_years", "4_to_7_years", "more_than_7_years"}
FELONY_VALUES = {"none", "within_10_years", "more_than_10_years"}
RESIDENCY_VALUES = {"citizen", "legal_permanent_resident", "other"}


def _residency(data: dict[str, Any]) -> str | None:
    value = str(data.get("residency_status") or "").strip().lower()
    if value in RESIDENCY_VALUES:
        return value
    legacy = data.get("citizen_or_lpr")
    if isinstance(legacy, bool):
        # Historical snapshots did not distinguish these two classes. They
        # remain readable, but new answers must use the explicit enum.
        return "citizen" if legacy else "other"
    return None


def owner_answer_complete(answer: dict[str, Any] | None) -> bool:
    data = answer or {}
    return bool(
        _residency(data) in RESIDENCY_VALUES
        and isinstance(data.get("credit_660_or_higher"), bool)
        and data.get("bankruptcy_timing") in BANKRUPTCY_VALUES
        and isinstance(data.get("foreclosure_within_3_years"), bool)
        and data.get("felony_timing") in FELONY_VALUES
        and isinstance(data.get("misdemeanor_within_5_years"), bool)
        and isinstance(data.get("misdemeanor_involving_minor"), bool)
        and isinstance(data.get("arrest_within_6_months"), bool)
        and isinstance(data.get("financial_related_crime"), bool)
        and isinstance(data.get("active_legal_charges"), bool)
        and isinstance(data.get("ofac_match"), bool)
    )


def file_answers_complete(answer: dict[str, Any] | None) -> bool:
    data = answer or {}
    if not all(isinstance(data.get(key), bool) for key in REQUIRED_FILE_FIELDS):
        return False
    if data.get("tax_liability_over_10000") is True and not isinstance(
        data.get("tax_payment_plan_current"), bool
    ):
        return False
    large_term_obligation = (
        data.get("judgment_over_50000_within_7_years") is True
        or data.get("aggregate_liens_judgments_over_25000_within_7_years") is True
    )
    if large_term_obligation and not isinstance(
        data.get("term_obligations_released_or_on_plan"), bool
    ):
        return False
    return True


def screen_application(
    *,
    requested_amount: float,
    refinance_debt: bool,
    required_owner_ids: list[str],
    owner_answers: dict[str, dict[str, Any]],
    verified_credit_by_owner: dict[str, int | None] | None = None,
    file_answers: dict[str, Any] | None = None,
    application_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the same direct-program rules used by Product Finder.

    The original self-report and any later verified credit fact remain
    separate. A verified tier can replace the current credit gate without
    mutating or deleting what the owner originally disclosed.
    """

    verified = verified_credit_by_owner or {}
    owners: list[dict[str, Any]] = []
    for owner_id in required_owner_ids:
        disclosed = dict(owner_answers.get(owner_id) or {})
        disclosed["owner_id"] = owner_id
        disclosed["residency_status"] = _residency(disclosed)
        score = verified.get(owner_id)
        if score is not None:
            disclosed["verified_credit_660_or_higher"] = score >= 660
        owners.append(disclosed)

    facts = dict(application_facts or {})
    facts.update(file_answers or {})
    facts.update(
        {
            "requested_amount": max(float(requested_amount or 0), 0.0),
            "refinance_debt": bool(refinance_debt),
            "owner_count": facts.get("owner_count", len(required_owner_ids)),
            "owners": owners,
        }
    )
    return evaluate_direct_programs(facts)
