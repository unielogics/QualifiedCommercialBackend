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

# Business questions live in Step 4. Keeping their presentation metadata next
# to the routing fields prevents the UI from drifting from the rule engine.
BUSINESS_QUESTION_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "key": "tax_and_legal",
        "title": "Tax, judgments, and civil actions",
        "questions": (
            ("open_tax_liens", "Does the business or any required owner have an open tax lien?"),
            ("tax_liability_over_10000", "Is any outstanding tax liability above $10,000?"),
            ("tax_payment_plan_current", "Is the disclosed tax balance on a current payment plan?"),
            ("open_judgments", "Is there an open judgment against the business or a required owner?"),
            ("open_civil_actions_as_defendant", "Is the business or a required owner currently a defendant in a civil action?"),
            ("civil_action_financial_institution_within_10_years", "Within 10 years, has a financial institution brought a civil action against the business or a required owner?"),
            ("judgment_over_2000_within_12_months", "Was a judgment above $2,000 filed within the past 12 months?"),
            ("judgment_over_50000_within_7_years", "Was a judgment of $50,000 or more filed within the past 7 years?"),
            ("aggregate_liens_judgments_over_25000_within_7_years", "Did aggregate tax liens and judgments reach $25,000 or more within the past 7 years?"),
            ("term_obligations_released_or_on_plan", "Are the disclosed liens or judgments released or on a current payment plan?"),
        ),
    },
    {
        "key": "business_activity",
        "title": "Restricted business activity",
        "questions": tuple((key, label) for key, label in {
            "speculative_real_estate_flipping": "Is the primary business speculative real-estate flipping?",
            "gambling_or_bail_bonds": "Does the business operate gambling or bail-bond services?",
            "lending_investment_crypto_mlm": "Is the business a lender, investment, crypto, or multi-level-marketing operation?",
            "nonprofit_or_government": "Is the applicant a nonprofit or government entity?",
            "marijuana_or_firearms": "Is the business marijuana- or firearm-related?",
            "prurient_business": "Is the business adult-oriented or prurient in nature?",
            "auto_or_title_asset_sales": "Does the business sell auto/title assets as its primary activity?",
        }.items()),
    },
)


def applicable_business_questions(
    *, naics_code: str | None, routing_result: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Return Step 4 questions that can still affect a candidate route.

    Tax/legal questions remain relevant to either direct route. Restricted
    activity flags are suppressed only when the canonical NAICS already blocks
    both direct routes, because asking a client to reconfirm a classification
    that cannot alter the result adds friction without underwriting value.
    Conditional follow-ups are described to the client and enforced by the API.
    """

    programs = (routing_result or {}).get("programs") or []
    both_hard_naics_blocked = bool(programs) and all(
        any(".naics_" in str(rule.get("rule_id") or "") for rule in program.get("matched_rules") or [])
        for program in programs
    )
    result: list[dict[str, Any]] = []
    for group in BUSINESS_QUESTION_GROUPS:
        if group["key"] == "business_activity" and both_hard_naics_blocked:
            continue
        questions: list[dict[str, Any]] = []
        for key, label in group["questions"]:
            item: dict[str, Any] = {"key": key, "label": label}
            if key == "tax_payment_plan_current":
                item["show_when"] = {"tax_liability_over_10000": True}
            elif key == "term_obligations_released_or_on_plan":
                item["show_when_any"] = {
                    "judgment_over_50000_within_7_years": True,
                    "aggregate_liens_judgments_over_25000_within_7_years": True,
                }
            questions.append(item)
        result.append({"key": group["key"], "title": group["title"], "questions": questions})
    return result


def business_answer_blockers(
    groups: list[dict[str, Any]], answers: dict[str, Any] | None
) -> list[str]:
    """Return only the currently applicable unanswered Step 4 questions."""

    values = answers or {}
    blockers: list[str] = []
    for group in groups:
        for question in group.get("questions") or []:
            show_when = question.get("show_when") or {}
            show_when_any = question.get("show_when_any") or {}
            if show_when and any(
                values.get(key) is not expected for key, expected in show_when.items()
            ):
                continue
            if show_when_any and not any(
                values.get(key) is expected for key, expected in show_when_any.items()
            ):
                continue
            key = str(question.get("key") or "")
            if key and not isinstance(values.get(key), bool):
                blockers.append(
                    str(question.get("label") or key.replace("_", " ").title())
                )
    return blockers


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
