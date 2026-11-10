"""Investor-safe cost disclosure shared by reports and marketplace projections."""
from __future__ import annotations

import re
from copy import deepcopy
from html import escape
from typing import Any

from app.services.investor_report import safe_text

COST_LABELS = {"closing_cost": "Closing cost", "escrow_reserve": "Escrow / reserve", "referral": "Referral fee",
    "origination": "Origination / points", "consulting": "Consulting fee", "servicing": "Servicing fee",
    "management": "Management fee", "other": "Other cost"}
CALCULATION_FIELDS = ("gross_loan_amount", "investor_funding_amount", "minimum_investor_contribution", "funding_mode",
    "other_funding_required", "other_funding_status",
    "borrower_withheld_fees", "borrower_paid_separately", "escrow_reserves", "net_client_cash", "investor_gross_interest",
    "investor_gross_receipts", "investor_fees", "investor_initial_outlay", "investor_net_income", "investor_net_roi_pct",
    "investor_gross_roi_pct", "status", "recurring_borrower_annual", "coverage_with_costs", "allocation_reconciliation",
    "platform_fee_revenue", "management_revenue", "closing_costs_by_payer", "financing_fee_coverage")


def cost_label(row):
    label = safe_text(row.get("label")) or COST_LABELS.get(row.get("category"), "Other cost")
    recipient = str(row.get("recipient") or "").strip()
    if recipient and re.search(re.escape(recipient), label, re.I):
        return COST_LABELS.get(row.get("category"), "Other cost")
    return label


def cost_formula(row, currency="USD"):
    value = row.get("value")
    if value is None:
        return "Amount / rate not entered"
    basis = {"loan_amount": "gross loan", "investor_funding": "investor capital", "outstanding_principal": "opening outstanding principal", "interest": "scheduled interest"}.get(row.get("basis"), "unconfirmed base")
    amount = f"{float(value):g}% of {basis}" if row.get("method") == "percent" else money(value, currency)
    timing = {"closing": "at closing", "monthly": "per month", "annual": "per year"}.get(row.get("timing"), "timing unconfirmed")
    return f"{amount} - {timing}"


def _record(value):
    return value if isinstance(value, dict) else {}


def public_budget(report: Any) -> dict[str, Any]:
    """Amounts are exposed; recipient identities, notes and source IDs are not."""
    report = _record(report)
    recorded = _record(report.get("recorded"))
    current = _record(report.get("current"))
    basis = report.get("basis") or ("recorded_terms" if recorded else "staff_proposal")
    chosen = recorded if basis == "recorded_terms" else {} if basis == "unrecorded_terms" else current
    raw_calc = _record(report.get("calculation"))
    result = {"basis": basis,
        "stale": bool(report.get("stale")), "revision": chosen.get("revision"), "current_revision": current.get("revision"),
        "currency": chosen.get("currency"), "funding_mode": chosen.get("funding_mode") or "single_investor",
        "costs_confirmed": bool(chosen.get("costs_confirmed")), "rows": [],
        "calculation": {key: deepcopy(raw_calc[key]) for key in CALCULATION_FIELDS if key in raw_calc}}
    if isinstance(report.get("current_allocation_reconciliation"), dict):
        result["current_allocation_reconciliation"] = deepcopy(report["current_allocation_reconciliation"])
    if isinstance(report.get("current_financing_fee_coverage"), dict):
        result["current_financing_fee_coverage"] = deepcopy(report["current_financing_fee_coverage"])
    sources = {str(row.get("id")): row for row in chosen.get("rows") or [] if isinstance(row, dict)}
    calculations = {str(row.get("id")): row for row in raw_calc.get("rows") or [] if isinstance(row, dict)}
    for key, row in sources.items():
        if row.get("enabled") is False:
            continue
        computed = calculations.get(key) or {}
        result["rows"].append({"label": cost_label(row),
            **{field: deepcopy(row.get(field)) for field in ("category", "payer", "method", "value", "basis", "timing", "settlement", "reserve_release", "recipient_role")},
            **{field: deepcopy(computed.get(field)) for field in ("total_amount", "closing_amount", "recurring_total", "status")}})
    # Engine issue text may contain a private custom cost label; public review
    # remains useful without copying any free-form operator prose.
    result["issues"] = ["One or more cost inputs or closing allocations require staff review."] if raw_calc.get("issues") else []
    result["note"] = "Forecast charges and reserves only. Escrow is not fee income. Actual timing, losses, taxes and unrecorded costs may change returns."
    return result


def frozen_budget(snapshot: Any, revision: Any, terms: dict[str, Any], calculation: Any = None) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict) or revision is None:
        return None
    from app.services.deal_budget import calculate

    return public_budget({"recorded": snapshot, "current": {}, "basis": "recorded_terms",
        "calculation": deepcopy(calculation) if isinstance(calculation, dict) and calculation else calculate(snapshot, terms)})


def money(value, currency="USD"):
    if value is None:
        return "Unknown"
    return f"{float(value):,.2f} {currency or 'currency unconfirmed'}"


def render_budget_html(disclosure: dict[str, Any] | None) -> str:
    if disclosure is None:
        return '<section class="deal-budget"><h2>Closing costs and investor charges</h2><p>No cost budget was recorded with this terms version. Fees, reserves, net client proceeds and net investor return remain unconfirmed.</p></section>'
    calc = _record(disclosure.get("calculation"))
    currency = disclosure.get("currency") or "USD"
    rows = []
    for item in disclosure.get("rows") or []:
        treatment = "Reserve, not fee income" if item.get("category") == "escrow_reserve" else str(item.get("settlement") or "unconfirmed").replace("_", " ")
        rows.append(f'<tr><td>{escape(item["label"])}<br><small>{escape(cost_formula(item, currency))}</small></td><td>{escape(str(item.get("payer") or "Unknown").title())}</td><td>{escape(str(item.get("timing") or "Unknown").title())}</td><td>{escape(money(item.get("closing_amount"), currency))}</td><td>{escape(money(item.get("recurring_total"), currency))}</td><td>{escape(treatment)}</td></tr>')
    if not rows:
        rows.append('<tr><td colspan="6">No charges entered.' + (" Staff confirmed no listed costs." if disclosure.get("costs_confirmed") else " Cost review is incomplete; blank values do not mean zero.") + '</td></tr>')
    net_roi = calc.get("investor_net_roi_pct")
    summary = [("Gross loan", money(calc.get("gross_loan_amount"), currency)), ("Investor capital", money(calc.get("investor_funding_amount"), currency)),
        ("Net loan proceeds at funding", money(calc.get("net_client_cash"), currency)), ("Borrower closing cash paid separately", money(calc.get("borrower_paid_separately"), currency)),
        ("Other funding required (unconfirmed)", money(calc.get("other_funding_required"), currency)), ("Investor charges over the term", money(calc.get("investor_fees"), currency)),
        ("Illustrative net investor return", f"{float(net_roi):.2f}% over the full term" if net_roi is not None else "Unknown - complete the cost review")]
    summary_html = "".join(f'<span><b>{escape(label)}:</b> {escape(value)}</span>' for label, value in summary)
    return f'<section class="deal-budget"><h2>Closing costs and investor charges - recorded revision {disclosure.get("revision", "unknown")}</h2><table><thead><tr><th>Item</th><th>Payer</th><th>Timing</th><th>At closing</th><th>Over term</th><th>Treatment</th></tr></thead><tbody>{"".join(rows)}</tbody></table><div class="budget-totals">{summary_html}</div><p>{escape(safe_text(disclosure.get("note")))} Costs paid separately are not added to loan principal. Withheld amounts are counted once. Proceeds shown at funding precede the disclosed use-of-funds allocations and separately paid borrower cash.</p></section>'


BUDGET_CSS = """.deal-budget { border: 1px solid #dce3ec; border-radius: 9px; padding: 10px 12px; margin: 12px 0; } .deal-budget h2 { break-after: avoid; } .deal-budget table { border-collapse: collapse; width: 100%; font-size: 8px; } .deal-budget thead { display: table-header-group; } .deal-budget tr { break-inside: avoid; } .deal-budget td, .deal-budget th { padding: 5px; border-bottom: 1px solid #dce3ec; text-align: left; overflow-wrap: anywhere; } .deal-budget p { color: #52637a; font-size: 8px; } .budget-totals { margin-top: 8px; display: grid; grid-template-columns: 1fr 1fr; gap: 5px 12px; font-size: 8px; }"""
