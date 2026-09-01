"""Versioned, lender-neutral routing for Qualified Commercial applications.

This module is the single deterministic source used by Product Finder and the
formal application. It deliberately returns exact rule identifiers and safe
client-facing explanations while keeping lender/vendor identity out of API
responses, PDFs, and UI copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

RULES_VERSION = "qc_direct_programs_v2"
SBA_POLICY_VERSION = "sba_sop_50_10_8_notice_5000_876441"
SBA_POLICY_EFFECTIVE_DATE = "2026-03-01"

TERM_PROGRAM_KEY = "term_loan_3_5_year"
WORKING_CAPITAL_PROGRAM_KEY = "term_loan_10_year"

TERM_DISPLAY_NAME = "3-5 Year Business Term Loan"
WORKING_CAPITAL_DISPLAY_NAME = "10-Year Working Capital"


@dataclass(frozen=True)
class RuleMatch:
    rule_id: str
    matched_value: str
    explanation: str
    policy_version: str = RULES_VERSION
    policy_effective_date: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "matched_value": self.matched_value,
            "explanation": self.explanation,
            "policy_version": self.policy_version,
            "policy_effective_date": self.policy_effective_date,
        }


def _number(facts: dict[str, Any], key: str) -> float | None:
    try:
        value = facts.get(key)
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _bool(facts: dict[str, Any], key: str) -> bool | None:
    value = facts.get(key)
    return value if isinstance(value, bool) else None


def canonical_naics(facts: dict[str, Any]) -> tuple[str | None, str]:
    status = str(facts.get("taxonomy_status") or "official").lower()
    if status in {"pending", "unclassified"}:
        return None, status
    digits = "".join(ch for ch in str(facts.get("naics_code") or "") if ch.isdigit())
    return (digits[:6] or None), status


def _prefix(code: str, values: Iterable[str]) -> str | None:
    return next((value for value in values if code.startswith(value)), None)


def naics_exclusions(code: str | None, state: str | None) -> dict[str, list[RuleMatch]]:
    matches = {TERM_PROGRAM_KEY: [], WORKING_CAPITAL_PROGRAM_KEY: []}
    if not code:
        return matches

    state_code = (state or "").strip().upper()
    sector = code[:2]
    if sector in {"48", "49"}:
        explanation = "Transportation and warehousing businesses are outside this program's guidelines."
        for program in matches:
            matches[program].append(RuleMatch(f"{program}.naics_sector_48_49", sector, explanation))

    working_exact = {
        "441110": "New car dealers are outside the 10-year working-capital guidelines.",
        "441120": "Used car dealers are outside the 10-year working-capital guidelines.",
        "441210": "Recreational-vehicle dealers are outside the 10-year working-capital guidelines.",
        "441222": "Boat dealers are outside the 10-year working-capital guidelines.",
    }
    if code in working_exact:
        matches[WORKING_CAPITAL_PROGRAM_KEY].append(
            RuleMatch(f"{WORKING_CAPITAL_PROGRAM_KEY}.naics_{code}", code, working_exact[code])
        )
    if code.startswith("7225"):
        matches[WORKING_CAPITAL_PROGRAM_KEY].append(
            RuleMatch(
                f"{WORKING_CAPITAL_PROGRAM_KEY}.naics_7225",
                "7225",
                "Restaurants and other food-service businesses are outside the 10-year working-capital guidelines.",
            )
        )

    term_prefixes = {
        "4411": "Automobile dealers are outside the 3-5 year term guidelines.",
        "4412": "Other motor-vehicle and boat dealers are outside the 3-5 year term guidelines.",
        "4413": "Automotive parts, accessories, and tire retailers are outside the 3-5 year term guidelines.",
        "4452": "Specialty food retailers are outside the 3-5 year term guidelines.",
        "7223": "Special food services are outside the 3-5 year term guidelines.",
        "6215": "Medical and diagnostic laboratories are outside the 3-5 year term guidelines.",
        "6216": "Home health care services are outside the 3-5 year term guidelines.",
    }
    term_match = _prefix(code, term_prefixes)
    if term_match:
        matches[TERM_PROGRAM_KEY].append(
            RuleMatch(f"{TERM_PROGRAM_KEY}.naics_{term_match}", term_match, term_prefixes[term_match])
        )

    homebuilding = {"236115", "236116", "236117"}
    if code in homebuilding and state_code in {"FL", "AZ", "CO"}:
        matches[TERM_PROGRAM_KEY].append(
            RuleMatch(
                f"{TERM_PROGRAM_KEY}.naics_homebuilding_{state_code.lower()}",
                f"{code}:{state_code}",
                "New-home construction in this state is outside the 3-5 year term guidelines.",
            )
        )
    return matches


_RESTRICTED_FLAGS: dict[str, str] = {
    "speculative_real_estate_flipping": "Speculative real-estate flipping is outside the direct-program guidelines.",
    "gambling_or_bail_bonds": "Gambling and bail-bond operations are outside the direct-program guidelines.",
    "lending_investment_crypto_mlm": "Lending, investment, crypto, and multi-level-marketing operations require another funding path.",
    "nonprofit_or_government": "Nonprofit and government entities are outside the direct-program guidelines.",
    "marijuana_or_firearms": "Marijuana- and firearm-related businesses are outside the direct-program guidelines.",
    "prurient_business": "This business activity is outside the direct-program guidelines.",
    "auto_or_title_asset_sales": "Auto- or title-asset sales are outside the direct-program guidelines.",
}


def _program_row(
    key: str,
    name: str,
    maximum: float,
    blocks: list[RuleMatch],
    unresolved: list[str],
    strengths: list[str],
) -> dict[str, Any]:
    unique_matches = {match.rule_id: match for match in blocks}
    unique_open = list(dict.fromkeys(unresolved))
    status = "blocked" if unique_matches else "potential" if unique_open else "recommended"
    reasons = [match.explanation for match in unique_matches.values()]
    return {
        "program_key": key,
        "name": name,
        "status": status,
        "eligible": not unique_matches,
        "decision_type": "deterministic",
        "borrower_safe_reasons": reasons,
        "blocked_by": reasons,
        "matched_rules": [match.as_dict() for match in unique_matches.values()],
        "unresolved": unique_open,
        "strengths": list(dict.fromkeys(strengths)),
        "estimated_max_amount": maximum,
        "action": "start_application" if not unique_matches else "review_alternative",
        "verification": "Self-reported and unverified",
    }


def evaluate_direct_programs(facts: dict[str, Any]) -> dict[str, Any]:
    """Evaluate both direct programs independently from one fact dictionary."""

    amount = _number(facts, "requested_amount") or 0.0
    term_blocks: list[RuleMatch] = []
    working_blocks: list[RuleMatch] = []
    term_open: list[str] = []
    working_open: list[str] = []
    term_strengths: list[str] = []
    working_strengths: list[str] = []

    def block(target: list[RuleMatch], rule_id: str, value: Any, explanation: str) -> None:
        target.append(RuleMatch(rule_id, str(value), explanation))

    if amount < 25_000 or amount > 500_000:
        block(term_blocks, f"{TERM_PROGRAM_KEY}.amount", amount, "The requested amount is outside the $25,000-$500,000 range.")
    if amount < 15_000 or amount > 50_000:
        block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.amount", amount, "The requested amount is outside the $15,000-$50,000 range.")
    if facts.get("debt_refinance") is True or facts.get("refinance_debt") is True:
        block(
            working_blocks,
            f"{WORKING_CAPITAL_PROGRAM_KEY}.use_working_capital_only",
            "debt_refinance",
            "The 10-year program is for working capital only and cannot refinance debt.",
        )

    years = _number(facts, "years_in_business")
    if years is None:
        term_open.append("Confirm at least 2 years in business.")
        working_open.append("Confirm at least 2 years in business.")
    elif years < 2:
        block(term_blocks, f"{TERM_PROGRAM_KEY}.time_in_business", years, "At least 2 years in business are required.")
        block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.time_in_business", years, "At least 2 years in business are required.")
    else:
        term_strengths.append("Time in business meets the stated minimum.")
        working_strengths.append("Time in business meets the stated minimum.")

    revenue = _number(facts, "annual_revenue")
    if revenue is None:
        term_open.append("Confirm at least $50,000 in annual revenue.")
    elif revenue < 50_000:
        block(term_blocks, f"{TERM_PROGRAM_KEY}.annual_revenue", revenue, "Annual revenue is below the $50,000 minimum.")

    owner_count = _number(facts, "owner_count")
    if owner_count is None:
        working_open.append("Confirm the number of individual owners.")
    elif owner_count > 5:
        block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.owner_count", owner_count, "The 10-year program supports no more than five owners.")

    owners = facts.get("owners") if isinstance(facts.get("owners"), list) else [facts]
    for index, raw in enumerate(owners):
        owner = raw if isinstance(raw, dict) else {}
        owner_ref = str(owner.get("owner_id") or owner.get("id") or f"owner_{index + 1}")
        residency = str(owner.get("residency_status") or "").lower()
        if not residency and isinstance(owner.get("citizen_or_lpr"), bool):
            residency = "citizen_or_lpr" if owner["citizen_or_lpr"] else "other"
        if residency in {"citizen_or_lpr", "eligible"}:
            residency = "citizen"
        if not residency:
            term_open.append(f"Confirm residency status for {owner_ref}.")
            working_open.append(f"Confirm residency status for {owner_ref}.")
        elif residency not in {"citizen", "legal_permanent_resident"}:
            block(term_blocks, f"{TERM_PROGRAM_KEY}.residency", owner_ref, "A required owner does not meet the citizenship or permanent-residency requirement.")
            block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.residency", owner_ref, "A required owner does not meet the U.S.-citizen requirement.")
        elif residency == "legal_permanent_resident":
            block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.citizen_only", owner_ref, "The 10-year program requires the required owner to be a U.S. citizen.")

        verified_credit = owner.get("verified_credit_660_or_higher")
        credit_ok = verified_credit if isinstance(verified_credit, bool) else owner.get("credit_660_or_higher")
        if not isinstance(credit_ok, bool):
            credit_ok = owner.get("primary_owner_credit_660_or_higher")
        if not isinstance(credit_ok, bool):
            term_open.append(f"Confirm the 660 credit threshold for {owner_ref}.")
            working_open.append(f"Confirm the 660 credit threshold for {owner_ref}.")
        elif not credit_ok:
            block(term_blocks, f"{TERM_PROGRAM_KEY}.credit_660", owner_ref, "A required owner does not meet the preliminary 660 credit threshold.")
            block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.credit_660", owner_ref, "A required owner does not meet the preliminary 660 credit threshold.")

        bankruptcy = str(owner.get("bankruptcy_timing") or "")
        if bankruptcy == "within_3_years":
            block(term_blocks, f"{TERM_PROGRAM_KEY}.bankruptcy_7y", owner_ref, "A bankruptcy within 7 years is outside the 3-5 year term guidelines.")
            block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.bankruptcy_3y", owner_ref, "A bankruptcy within 3 years is outside the 10-year working-capital guidelines.")
        elif bankruptcy == "4_to_7_years":
            block(term_blocks, f"{TERM_PROGRAM_KEY}.bankruptcy_7y", owner_ref, "A bankruptcy within 7 years is outside the 3-5 year term guidelines.")
        elif not bankruptcy:
            term_open.append(f"Confirm bankruptcy history for {owner_ref}.")
            working_open.append(f"Confirm bankruptcy history for {owner_ref}.")

        if owner.get("foreclosure_within_3_years") is True:
            block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.foreclosure_3y", owner_ref, "A foreclosure within 3 years is outside the 10-year working-capital guidelines.")
        felony = str(owner.get("felony_timing") or "")
        if felony == "within_10_years":
            block(term_blocks, f"{TERM_PROGRAM_KEY}.felony_10y", owner_ref, "A felony conviction within 10 years is outside the 3-5 year term guidelines.")
        if felony in {"within_10_years", "more_than_10_years"}:
            block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.felony_any", owner_ref, "The disclosed felony history is outside the 10-year working-capital guidelines.")
        if owner.get("misdemeanor_within_5_years") is True or owner.get("misdemeanor_5y") is True:
            block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.misdemeanor_5y", owner_ref, "The disclosed recent background history is outside the 10-year working-capital guidelines.")
        if owner.get("misdemeanor_involving_minor") is True:
            block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.misdemeanor_minor", owner_ref, "The disclosed background history is outside the 10-year working-capital guidelines.")
        if owner.get("arrest_within_6_months") is True:
            block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.arrest_6m", owner_ref, "A recent arrest is outside the 10-year working-capital guidelines.")
        if owner.get("financial_related_crime") is True:
            block(term_blocks, f"{TERM_PROGRAM_KEY}.financial_crime", owner_ref, "The disclosed financial-related crime is outside the 3-5 year term guidelines.")
        if owner.get("ofac_match") is True:
            block(term_blocks, f"{TERM_PROGRAM_KEY}.ofac", owner_ref, "The disclosed sanctions issue requires a different review path.")
            block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.ofac", owner_ref, "The disclosed sanctions issue requires a different review path.")
        if owner.get("active_legal_charges") is True:
            block(term_blocks, f"{TERM_PROGRAM_KEY}.active_legal", owner_ref, "Active legal charges require a different review path.")
            block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.active_legal", owner_ref, "Active criminal charges require a different review path.")

    mca_count = _number(facts, "mca_count")
    if mca_count is None:
        term_open.append("Confirm outstanding MCA exposure.")
        working_open.append("Confirm outstanding MCA or SBA exposure.")
    else:
        if mca_count > 1:
            block(term_blocks, f"{TERM_PROGRAM_KEY}.mca_count", mca_count, "The 3-5 year term program permits no more than one outstanding MCA.")
        if mca_count > 2:
            block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.mca_count", mca_count, "The 10-year program permits no more than two outstanding MCA or SBA balances.")
        elif mca_count > 0:
            youngest = _number(facts, "youngest_mca_days")
            if youngest is None:
                working_open.append("Confirm that every MCA or SBA balance was funded more than 90 days ago.")
            elif youngest <= 90:
                block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.mca_age", youngest, "Every outstanding MCA or SBA balance must be older than 90 days.")

    positive = _number(facts, "positive_month_end_count")
    if positive is None:
        term_open.append("Confirm three positive month-end balances from verified bank evidence.")
        working_open.append("Confirm three positive month-end balances from verified bank evidence.")
    elif positive < 3:
        block(term_blocks, f"{TERM_PROGRAM_KEY}.positive_months", positive, "Three positive month-end balances are required.")
        block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.positive_months", positive, "Three positive month-end balances are required.")

    official = _bool(facts, "official_bank_statements")
    if official is None:
        term_open.append("Confirm six current months through Plaid Assets or bank-produced PDF statements.")
        working_open.append("Confirm six current months through Plaid Assets or bank-produced PDF statements.")
    elif official is False:
        term_open.append("Six current verified bank months are still required; use Plaid Assets or bank-produced PDF statements. CSVs and screenshots are supplemental.")
        working_open.append("Six current verified bank months are still required; use Plaid Assets or bank-produced PDF statements. CSVs and screenshots are supplemental.")

    nsfs = _number(facts, "nsf_count")
    if nsfs is None:
        working_open.append("Confirm NSF activity from the statement review.")
    elif nsfs > 2:
        block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.nsf_count", nsfs, "NSF activity exceeds the 10-year program limit.")
    negative_days = _number(facts, "negative_balance_days")
    if negative_days is None:
        working_open.append("Confirm negative-balance days from the statement review.")
    elif negative_days > 5:
        block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.negative_days", negative_days, "Negative-balance days exceed the 10-year program limit.")
    ucc_count = _number(facts, "active_ucc_count")
    if ucc_count is None:
        working_open.append("Confirm active UCC filings.")
    elif ucc_count > 4:
        block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.ucc_count", ucc_count, "Active UCC filings exceed the 10-year program limit.")

    cash_flow = _number(facts, "annual_cash_flow_available_for_debt")
    monthly_debt = _number(facts, "monthly_debt_payments")
    verified_dscr = _number(facts, "verified_dscr")
    dscr = verified_dscr
    if dscr is None and cash_flow is not None and monthly_debt is not None and monthly_debt > 0:
        dscr = cash_flow / (monthly_debt * 12)
    if dscr is None:
        working_open.append("Calculate business DSCR from cash flow and monthly debt payments.")
    elif dscr < 1.10:
        block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.dscr_1_10", round(dscr, 3), "Calculated business DSCR is below 1.10x.")
    else:
        working_strengths.append("Calculated business DSCR meets the 1.10x minimum.")

    # Legal and tax facts stay structured. Historical snapshots used one
    # combined flag; it remains readable, while new intake captures the exact
    # condition so one program's threshold is never applied to the other.
    legacy_open_obligation = facts.get("open_tax_liens_or_judgments") is True
    if facts.get("open_tax_liens") is True or legacy_open_obligation:
        block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.open_tax_lien", True, "Open tax liens are outside the 10-year working-capital guidelines.")
    if facts.get("open_judgments") is True or legacy_open_obligation:
        block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.open_judgment", True, "Open judgments are outside the 10-year working-capital guidelines.")
    if facts.get("open_civil_actions_as_defendant") is True:
        block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.open_civil_action", True, "An open civil action naming the applicant as defendant is outside the 10-year working-capital guidelines.")
    if facts.get("civil_action_financial_institution_within_10_years") is True:
        block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.financial_institution_civil_action", True, "A disclosed financial-institution civil action is outside the 10-year working-capital guidelines.")
    if facts.get("tax_liability_over_10000") is True:
        payment_plan = _bool(facts, "tax_payment_plan_current")
        if payment_plan is None:
            working_open.append("Confirm that tax liabilities above $10,000 are on a current payment plan.")
        elif not payment_plan:
            block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.tax_payment_plan", True, "Tax liabilities above $10,000 must be on a current payment plan.")

    if facts.get("judgment_over_2000_within_12_months") is True:
        block(term_blocks, f"{TERM_PROGRAM_KEY}.recent_judgment_2000", True, "A judgment above $2,000 filed within the past 12 months is outside the 3-5 year term guidelines.")
    large_term_obligation = (
        facts.get("judgment_over_50000_within_7_years") is True
        or facts.get("aggregate_liens_judgments_over_25000_within_7_years") is True
    )
    if large_term_obligation:
        resolved_or_plan = _bool(facts, "term_obligations_released_or_on_plan")
        if resolved_or_plan is None:
            term_open.append("Confirm that the disclosed lien or judgment is released or on a current payment plan.")
        elif not resolved_or_plan:
            block(term_blocks, f"{TERM_PROGRAM_KEY}.lien_judgment_plan", True, "The disclosed lien or judgment must be released or on a current payment plan for the 3-5 year term path.")

    for flag, explanation in _RESTRICTED_FLAGS.items():
        if facts.get(flag) is True:
            block(term_blocks, f"{TERM_PROGRAM_KEY}.restricted.{flag}", flag, explanation)
            block(working_blocks, f"{WORKING_CAPITAL_PROGRAM_KEY}.restricted.{flag}", flag, explanation)

    if facts.get("sba_ineligible") is True:
        working_blocks.append(
            RuleMatch(
                f"{WORKING_CAPITAL_PROGRAM_KEY}.sba_policy",
                SBA_POLICY_VERSION,
                "The disclosed business activity requires review under the current SBA eligibility policy.",
                policy_version=SBA_POLICY_VERSION,
                policy_effective_date=SBA_POLICY_EFFECTIVE_DATE,
            )
        )

    naics, taxonomy_status = canonical_naics(facts)
    if taxonomy_status in {"pending", "unclassified"} or not naics:
        term_open.append("Classification review required before an industry decision can be final.")
        working_open.append("Classification review required before an industry decision can be final.")
    else:
        matched = naics_exclusions(naics, facts.get("state"))
        term_blocks.extend(matched[TERM_PROGRAM_KEY])
        working_blocks.extend(matched[WORKING_CAPITAL_PROGRAM_KEY])

    annualized_bank_sales = _number(facts, "annualized_bank_sales")
    working_cap = 50_000.0
    if annualized_bank_sales is None:
        working_open.append("Calculate annualized sales from six current verified bank months to confirm the 10-year amount cap.")
    else:
        working_cap = min(working_cap, round(max(annualized_bank_sales, 0.0) * 0.50, 2))
        if working_cap < 15_000:
            block(
                working_blocks,
                f"{WORKING_CAPITAL_PROGRAM_KEY}.annualized_sales_cap",
                working_cap,
                "The 50% annualized-sales cap does not support the program's $15,000 minimum.",
            )
        elif amount > working_cap:
            working_open.append(
                f"The requested amount exceeds the 50% annualized-sales cap; the current evidence supports up to ${working_cap:,.0f} on this path."
            )

    term = _program_row(TERM_PROGRAM_KEY, TERM_DISPLAY_NAME, 500_000, term_blocks, term_open, term_strengths)
    working = _program_row(
        WORKING_CAPITAL_PROGRAM_KEY,
        WORKING_CAPITAL_DISPLAY_NAME,
        working_cap,
        working_blocks,
        working_open,
        working_strengths,
    )
    programs = [term, working]
    eligible = [row["program_key"] for row in programs if row["eligible"]]
    viable_max = max((row["estimated_max_amount"] for row in programs if row["status"] != "blocked"), default=0)
    recommended_amount = min(amount, viable_max) if amount and viable_max else viable_max or None
    return {
        "rules_version": RULES_VERSION,
        "sba_policy": {
            "version": SBA_POLICY_VERSION,
            "effective_date": SBA_POLICY_EFFECTIVE_DATE,
        },
        "verification": "Self-reported and unverified",
        "client_requested_amount": amount,
        "recommended_amount": recommended_amount,
        "amount_adjustment_required": bool(amount and viable_max and amount > viable_max),
        "canonical_naics_code": naics,
        "classification_status": taxonomy_status,
        "programs": programs,
        "evaluated_programs": programs,
        "eligible_program_keys": eligible,
        "recommended": [row for row in programs if row["status"] == "recommended"],
        "potential": [row for row in programs if row["status"] == "potential"],
        "blocked": [row for row in programs if row["status"] == "blocked"],
        "booking_recommended": not eligible,
        "headline": "Preliminary direct-program fit found" if eligible else "No direct-program fit based on current facts",
        "next_action": "Continue to verification" if eligible else "Review another funding path",
        "calculated_metrics": {
            "dscr": round(dscr, 3) if dscr is not None else None,
            "dscr_source": "verified" if verified_dscr is not None else "calculated" if dscr is not None else "unavailable",
            "monthly_debt_payments": monthly_debt,
            "annual_cash_flow_available_for_debt": cash_flow,
            "annualized_bank_sales": annualized_bank_sales,
            "working_capital_amount_cap": working_cap if annualized_bank_sales is not None else None,
            "working_capital_amount_cap_source": "six_current_bank_statements" if annualized_bank_sales is not None else "unavailable",
        },
    }
