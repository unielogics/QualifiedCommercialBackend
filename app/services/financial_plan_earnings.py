"""Hypothetical earnings beside a refinance plan, separate from its DSCR.

Financing changes debt service and cash retained, not EBITDA. Only explicit
operating hypotheses on a supported annual P&L can produce Adjusted EBITDA;
they remain unverified assumptions and never replace the factual baseline.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from app.services.financial_metric_reporting import validated_annual_pair

OPERATING_ADJUSTMENTS = frozenset({"owner_salary", "one_time_expense", "rent_to_owner"})


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _difference(before, after):
    return round(after - before, 2) if before is not None and after is not None else None


def scenario_ebitda(ebitda, plan, evaluation, dscr, service):
    headline = deepcopy((ebitda or {}).get("headline") or {})
    kind = headline.get("kind")
    supported = kind in {"computed_ebitda", "reported_ebitda", "tax_estimate"}
    before = _number(headline.get("value")) if supported else None
    annual = _number(headline.get("annualized_value")) if supported else None
    status = "projected_unchanged" if before is not None else "tax_estimate_only" if kind == "tax_estimate" else "unavailable"
    notes = ["Refinancing alone does not change EBITDA: interest, principal payments and financing proceeds are outside EBITDA. Debt-service savings and bank cash-flow changes are shown separately.",
             *(headline.get("notes") or [])]
    # Never borrow another period's eligible annual figure while displaying a
    # different headline as the scenario's current factual baseline.
    annual_pair = validated_annual_pair({"ebitda": {"candidates": [headline]}, "dscr": dscr, "debt": {"service": service}})
    adjusted_base = _number(annual_pair.get("ebitda_annual")) if annual_pair else None
    currency = str(headline.get("currency") or "").strip().upper() or None
    if adjusted_base is not None and currency != "USD":
        adjusted_base = None
        notes.append("Scenario addbacks are denominated in USD; they are not applied to a source EBITDA in another currency. No currency conversion is assumed.")
    if adjusted_base is None:
        notes.append("Adjusted EBITDA requires an accepted business return, or an accepted, reconciled annual P&L with a known reporting period and accounting basis, for the same business in a comparable USD denomination.")

    included, excluded, used_ids = [], [], set()
    for item in plan.get("add_backs") or []:
        monthly = _number(item.get("amount_monthly"))
        adjustment = {key: item.get(key) for key in ("id", "kind", "label", "note", "source")}
        adjustment.update(amount_monthly=monthly, amount_annual=round(monthly * 12, 2) if monthly is not None else None)
        reason = None
        if item.get("kind") in {"interest_refinanced", "depreciation"}:
            reason = "Interest and depreciation/amortization are already excluded from EBITDA; adding them again would double count."
        elif item.get("kind") not in OPERATING_ADJUSTMENTS:
            reason = "A generic DSCR addback is not an identified operating adjustment to EBITDA."
        elif monthly is None or monthly <= 0:
            reason = "A positive, finite operating adjustment amount is required."
        elif not str(item.get("note") or "").strip() or not str(item.get("source") or "").strip():
            reason = "Describe the operating assumption and its source before including it in hypothetical Adjusted EBITDA."
        elif item.get("id") and item["id"] in used_ids:
            reason = "This adjustment identity is already included; it is not counted twice."
        elif adjusted_base is None:
            reason = "No comparable, source-supported annual EBITDA baseline is available."
        if reason:
            excluded.append({**adjustment, "reason": reason, "status": "not_applied"})
        else:
            if item.get("id"):
                used_ids.add(item["id"])
            included.append({**adjustment, "status": "hypothetical_unverified"})

    adjustment_total = round(sum(item["amount_annual"] for item in included), 2)
    operating_cost = _number(evaluation.get("annual_new_operating_expenses", 0))
    adjusted_after = round(adjusted_base + adjustment_total - operating_cost, 2) if adjusted_base is not None and operating_cost is not None else None
    annual_after = round(annual - operating_cost, 2) if annual is not None and operating_cost is not None and (currency == "USD" or operating_cost == 0) else None
    factor = _number(headline.get("annualization_factor"))
    if factor is None and headline.get("months_covered") == 12:
        factor = 1.0
    after = round(before - operating_cost / factor, 2) if before is not None and operating_cost is not None and factor and (currency == "USD" or operating_cost == 0) else before if operating_cost == 0 else None
    adjusted_status = "unavailable" if adjusted_base is None else "hypothetical_adjusted" if included else "unchanged"
    if included:
        notes.append("Operating addbacks are staff hypotheses, not verified facts or lender-accepted adjustments. They change hypothetical Adjusted EBITDA only.")
    if operating_cost is None or operating_cost:
        notes.append("New recurring budget rows classified as operating expenses reduce hypothetical earnings once. Financing charges, capitalized costs and reserves do not change source EBITDA.")
    if (plan.get("overrides") or {}).get("income_annual") is not None:
        notes.append("The generic DSCR income override does not replace source EBITDA or create an EBITDA adjustment.")
    if plan.get("exclude"):
        notes.append("Excluding transfers or other bank payees changes the bank cash-flow proxy only, not EBITDA.")
    service_override = (plan.get("overrides") or {}).get("debt_service_monthly") is not None
    if service_override:
        notes.append("Projected savings include a hypothetical DSCR debt-service override; the published current debt service is unchanged.")
    replacement_missing = bool(evaluation.get("retired_count") and (evaluation.get("new_payment_monthly") is None or
        "new_regular_payment_monthly" in evaluation and evaluation.get("new_regular_payment_monthly") is None))
    if replacement_missing:
        notes.append("Debt-service savings are shown before adding a replacement payment; replacement loan terms have not been provided.")
    before_service = _number(service.get("monthly"))
    # Operating savings follow recurring payments; the DSCR denominator may
    # separately include the maturity/balloon stress window.
    after_service = _number(evaluation.get("debt_service_after_regular_monthly")
                            if "debt_service_after_regular_monthly" in evaluation else evaluation.get("debt_service_after_monthly")) if before_service is not None else None
    net_before, net_after = _number(evaluation.get("net_before_avg")), _number(evaluation.get("net_after_avg"))
    reported, computed = _number(headline.get("reported_value")), _number(headline.get("computed_value"))
    reconciliation_conflict = reported is not None and computed is not None and abs(reported - computed) > 0.01
    bank_basis = next((candidate for candidate in dscr.get("candidates", []) if candidate.get("key") == "bank_cash_flow"), {})
    return {"before": before, "after": after, "change": _difference(before, after),
            "value_basis": "period_ebitda", "annual_before": annual, "annual_after": annual_after,
            "annual_change": _difference(annual, annual_after), "status": "projected_operating_costs" if operating_cost else status,
            "annual_new_operating_expenses": operating_cost,
            "label": "EBITDA", "after_label": "After refinance EBITDA", "source_kind": kind,
            "period": headline.get("period"), "months_covered": headline.get("months_covered"),
            "annualization_factor": headline.get("annualization_factor"), "currency": currency,
            "basis": headline.get("basis"), "provisional": bool(headline.get("provisional", True) or reconciliation_conflict),
            "source_refs": headline.get("source_refs") or [], "baseline": headline if supported else None,
            "tax_estimate": headline if kind == "tax_estimate" else None,
            "adjusted_annual": {"before": adjusted_base, "after": adjusted_after,
                                "change": round(adjustment_total - operating_cost, 2) if adjusted_base is not None and operating_cost is not None else None,
                                "status": adjusted_status, "verified": False,
                                "label": "Hypothetical Adjusted EBITDA (annual)", "currency": "USD" if adjusted_base is not None else None,
                                "adjustments": included, "excluded_adjustments": excluded},
            "debt_service_savings_monthly": _difference(after_service, before_service),
            "debt_service_savings_currency": "USD",
            "debt_service_savings_status": "before_replacement_payment" if replacement_missing else "hypothetical_override" if service_override else "projected",
            "replacement_payment_missing": replacement_missing,
            "debt_service_override_applied": service_override,
            "cash_flow_change_monthly": _difference(net_before, net_after),
            "cash_flow_currency": bank_basis.get("numerator_currency"),
            "cash_flow_currency_status": bank_basis.get("currency_status", "unknown"),
            "notes": notes}
