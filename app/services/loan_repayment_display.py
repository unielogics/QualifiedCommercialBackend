"""Client-safe descriptions of canonical repayment calculations.

Never derive a second schedule in an exporter or trust a browser's summary.
Callers pass the shared engine's result for the immutable terms being shown.
"""
from __future__ import annotations

from typing import Any


def configured_structure_label(values: dict[str, Any], label: str) -> str:
    if not values.get("repayment_config"):
        return label
    kind = values.get("repayment_structure")
    events = values.get("balloon_events") or []
    if kind in {"interest_only", "revolving_interest_only"}:
        return f"{label} with principal balloons" if events else label
    if kind in {"fully_amortizing", "balloon", "interest_only_then_amortizing"}:
        prefix = "Interest only, then amortizing" if kind == "interest_only_then_amortizing" else "Amortizing"
        return f"{prefix} with balloon payments" if events or values.get("balloon_amount") else prefix
    return label


def repayment_lines(summary: dict[str, Any] | None) -> list[str]:
    if not summary or not summary.get("repayment_config"):
        return []
    config = summary["repayment_config"]
    lines: list[str] = []
    amortization = config.get("amortization_months")
    if amortization is not None:
        lines.append(f"Amortization: {int(amortization)} months after the interest-only period; contractual maturity: {int(summary['term_months'])} months.")
    if summary.get("scheduled_balloons"):
        behavior = "Recalculate later regular payments" if config.get("after_balloon") == "recast" else "Keep the regular payment (an earlier payoff is possible)"
        lines.append(f"After scheduled principal balloons: {behavior}.")
        lines.append("Percentages apply to remaining principal after the regular payment and any earlier balloons at that payment. Same-payment balloons apply in the listed order.")
    for event in summary.get("scheduled_balloons") or []:
        month = float(event.get("due_month") or event["month"])
        basis = f"{float(event['value']):g}% of remaining principal" if event["type"] == "percent_balance" else f"${float(event['value']):,.2f} principal"
        lines.append(f"Month {int(event['month'])}, payment {int(event['period'])} (loan month {month:g}): {basis} = ${float(event['amount']):,.2f}; principal remaining ${float(event['balance_after']):,.2f}.")
    maturity = float(summary.get("maturity_balloon") or 0)
    if maturity > 0:
        lines.append(f"Additional residual principal due at maturity: ${maturity:,.2f}.")
    if summary.get("first_year_actual_debt_service") is not None:
        lines.append(f"First-year scheduled cash payments: ${float(summary['first_year_actual_debt_service']):,.2f}.")
    if summary.get("peak_rolling_12_month_debt_service") is not None:
        lines.append(f"Coverage basis: highest scheduled cash due in any rolling 12 months, ${float(summary['peak_rolling_12_month_debt_service']):,.2f}, including scheduled balloons and maturity principal once.")
    return lines
