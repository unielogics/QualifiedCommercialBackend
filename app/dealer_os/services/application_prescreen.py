"""Deterministic, self-reported eligibility checkpoint between Steps 1 and 2."""

from __future__ import annotations

from typing import Any

RULES_VERSION = "quidity_step1_v1"
REQUIRED_OWNER_FIELDS = (
    "citizen_or_lpr",
    "credit_660_or_higher",
    "bankruptcy_timing",
    "foreclosure_within_3_years",
    "felony_timing",
)
BANKRUPTCY_VALUES = {"none", "within_3_years", "4_to_7_years", "more_than_7_years"}
FELONY_VALUES = {"none", "within_10_years", "more_than_10_years"}


def owner_answer_complete(answer: dict[str, Any] | None) -> bool:
    data = answer or {}
    return bool(
        isinstance(data.get("citizen_or_lpr"), bool)
        and isinstance(data.get("credit_660_or_higher"), bool)
        and data.get("bankruptcy_timing") in BANKRUPTCY_VALUES
        and isinstance(data.get("foreclosure_within_3_years"), bool)
        and data.get("felony_timing") in FELONY_VALUES
    )


def screen_application(
    *,
    requested_amount: float,
    refinance_debt: bool,
    required_owner_ids: list[str],
    owner_answers: dict[str, dict[str, Any]],
    verified_credit_by_owner: dict[str, int | None] | None = None,
) -> dict[str, Any]:
    """Evaluate only the two direct-application Quidity programs.

    Reasons are deliberately borrower-safe. The original requested amount is
    echoed and never changed; amount fit is a route, not a mutation.
    """

    amount = max(float(requested_amount or 0), 0.0)
    verified = verified_credit_by_owner or {}
    ez_blocks: list[str] = []
    micro_blocks: list[str] = []

    if amount < 15_000:
        ez_blocks.append("The requested amount is below the direct program minimum.")
        micro_blocks.append("The requested amount is below the $15,000 program minimum.")
    elif amount < 25_000:
        ez_blocks.append("EZ Term starts at $25,000.")
    elif amount > 50_000:
        micro_blocks.append("MicroCap is limited to $50,000.")
    if amount > 500_000:
        ez_blocks.append("EZ Term is limited to $500,000; a funding call is recommended.")

    if refinance_debt:
        micro_blocks.append("MicroCap is for working capital only and cannot refinance debt.")

    for owner_id in required_owner_ids:
        answer = owner_answers.get(owner_id) or {}
        if not owner_answer_complete(answer):
            continue
        if not answer["citizen_or_lpr"]:
            reason = "A 20%+ owner must be a U.S. citizen or legal permanent resident."
            ez_blocks.append(reason)
            micro_blocks.append(reason)

        verified_score = verified.get(owner_id)
        credit_passes = verified_score >= 660 if verified_score is not None else answer["credit_660_or_higher"]
        if not credit_passes:
            reason = "A 20%+ owner does not meet the preliminary 660 credit threshold."
            ez_blocks.append(reason)
            micro_blocks.append(reason)

        bankruptcy = answer["bankruptcy_timing"]
        if bankruptcy == "within_3_years":
            ez_blocks.append("A bankruptcy within 3 years is outside EZ Term guidelines.")
            micro_blocks.append("A bankruptcy within 3 years is outside MicroCap guidelines.")
        elif bankruptcy == "4_to_7_years":
            ez_blocks.append("A bankruptcy within 7 years is outside EZ Term guidelines.")

        if answer["foreclosure_within_3_years"]:
            micro_blocks.append("A foreclosure within 3 years is outside MicroCap guidelines.")

        felony = answer["felony_timing"]
        if felony != "none":
            micro_blocks.append("A felony history is outside MicroCap guidelines.")
        if felony == "within_10_years":
            ez_blocks.append("A felony conviction within 10 years is outside EZ Term guidelines.")

    def unique(values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    ez_blocks = unique(ez_blocks)
    micro_blocks = unique(micro_blocks)
    programs = [
        {
            "program_key": "term_loan_3_5_year",
            "name": "EZ Term Loan",
            "eligible": not ez_blocks,
            "blocked_by": ez_blocks,
            "action": "start_application" if not ez_blocks else "review_alternative",
        },
        {
            "program_key": "term_loan_10_year",
            "name": "MicroCap Working Capital",
            "eligible": not micro_blocks,
            "blocked_by": micro_blocks,
            "action": "start_application" if not micro_blocks else "review_alternative",
        },
    ]
    eligible = [item["program_key"] for item in programs if item["eligible"]]
    if eligible:
        headline = "Preliminary direct-program fit found"
        next_action = "Continue to verification"
    elif amount > 500_000:
        headline = "Direct programs do not fit this request"
        next_action = "Book a funding call for a larger facility"
    else:
        headline = "No direct-program fit based on current self-report"
        next_action = "Continue to verification or review another funding path"

    return {
        "rules_version": RULES_VERSION,
        "verification": "Self-reported and unverified",
        "client_requested_amount": amount,
        "refinance_debt": refinance_debt,
        "programs": programs,
        "eligible_program_keys": eligible,
        "booking_recommended": not eligible,
        "headline": headline,
        "next_action": next_action,
    }

