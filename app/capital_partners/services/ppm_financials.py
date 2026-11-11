"""Deterministic disclosure sections from the saved, shareable financial view."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.services.investor_report import safe_text

from .document_catalog import safe_document_summaries


def _record(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _money(value, currency):
    number = _number(value)
    if number is None:
        return "Not established"
    code = str(currency or "").upper()
    label = f"{number:,.2f}"
    return f"{code} {label}" if re.fullmatch(r"[A-Z]{3}", code) and code != "XXX" else f"{label} (currency not confirmed)"


def _ratio(value):
    number = _number(value)
    return f"{number:.2f}x" if number is not None else "Not established"


def _label(value):
    return safe_text(str(value or "")).replace("_", " ").strip()


def _fact(label, value, note=None):
    return {"label": label, "value": value, **({"note": safe_text(note)} if note else {})}


def _allocations(value):
    if isinstance(value, dict):
        value = [{"category": key, "amount": amount} for key, amount in value.items() if key != "other_label"]
    return [[_label(row.get("label") or row.get("category") or "Other use"), row.get("amount")]
            for row in value if isinstance(row, dict) and _number(row.get("amount")) is not None] if isinstance(value, list) else []


def financial_sections(financials, listing_fields, budget, currency=None):
    """Never substitute tax income, deposits or projections for canonical NOI."""
    financials, listing_fields, budget = map(_record, (financials, listing_fields, budget))
    earnings = _record(financials.get("operating_earnings"))
    noi = _record(earnings.get("noi"))
    historical = _record(noi.get("historical"))
    scenario = _record(noi.get("scenario"))
    annual_noi = _number(historical.get("annualized_value"))
    selected_noi = annual_noi if annual_noi is not None else _number(historical.get("value"))
    noi_currency = historical.get("currency")
    label = "Saved selected NOI - annualized" if annual_noi is not None else "Saved selected NOI"
    details = [_label(historical.get(key)) for key in ("label", "kind", "period", "basis") if historical.get(key)]
    if historical.get("provisional") or historical.get("tax_derived_estimate") or noi.get("review_needed"):
        details.append("Estimate or review pending; this does not establish a documented P&L baseline.")
    facts = [
        _fact("Offering / business", safe_text(listing_fields.get("name")) or "Not supplied"),
        _fact(label, _money(selected_noi, noi_currency), "; ".join(details) if selected_noi is not None else safe_text(noi.get("reason") or "A selected, supported operating-income source is not established.")),
        _fact("Debt-service coverage - current selected basis", _ratio(financials.get("dscr")), "Saved coverage basis: " + (_label(financials.get("dscr_basis")) or "Not established")),
        _fact("Debt-service coverage - saved plan illustration", _ratio(financials.get("dscr_after_plan"))),
        _fact("Recorded annual debt service", _money(financials.get("annual_debt_service"), financials.get("debt_service_currency") or financials.get("denominator_currency")), "No currency conversion is inferred from the offering currency."),
    ]
    if scenario.get("plan") is not None:
        facts.append(_fact("NOI with saved operating changes - illustration", _money(scenario["plan"], noi_currency), "Proposed operating actions are distinct from historical earnings. Refinancing changes debt service, not core operating income."))
    if financials.get("tax_income") is not None:
        facts.append(_fact("Tax-return income - separate measure", _money(financials["tax_income"], None), f"Tax year {_label(financials.get('tax_year')) or 'not confirmed'}. This figure is not substituted for NOI."))
    sections = [{"key": "financial_evidence", "title": "Business and financial evidence", "paragraphs": [
        "Historical evidence, selected earnings estimates and saved-plan illustrations are separate. Bank deposits are cash activity and do not establish operating income."], "facts": facts}]
    terms = _record(financials.get("proposed_terms"))
    calculation = _record(budget.get("calculation"))
    reconciliation = _record(budget.get("current_allocation_reconciliation")) or _record(calculation.get("allocation_reconciliation"))
    uses = _allocations(terms.get("proposal_use_of_funds"))
    if uses:
        scope = reconciliation.get("scope")
        scopes = {"proposed_loan": "Declared proposed-loan allocations", "proposed_reference": "Prior-proposal uses - reference only",
                  "proposed_gross_budget": "Gross-facility uses - net allocation requires review", "requested_reference": "Requested-budget uses - reference only"}
        sections.append({"key": "proposed_allocations", "title": "Saved financing use of proceeds", "paragraphs": [
            scopes.get(scope, "Saved proposal uses; allocation scope requires confirmation."),
            "These uses remain subject to closing review. Declared purposes are not proof that an expense or investment occurred."],
            "facts": [_fact("Allocation review", _label(reconciliation.get("status")) or "Not confirmed", reconciliation.get("condition"))],
            "table": {"columns": ["Purpose", "Recorded amount"], "rows": [[label, _money(amount, currency)] for label, amount in uses]}})
    requested = _record(financials.get("use_of_funds"))
    request_uses = _allocations(requested.get("category_totals"))
    if request_uses or requested.get("requested_amount") is not None:
        sections.append({"key": "requested_allocations", "title": "Client request - separate budget reference", "paragraphs": [
            "The client request is distinct from proposed loan principal, net borrower proceeds and investor capital. Requested uses do not establish closing allocations for a different proposed amount."],
            "facts": [_fact("Client requested amount", _money(requested.get("requested_amount"), currency)),
                      _fact("Unallocated requested budget", _money(requested.get("unallocated_amount"), currency))],
            "table": {"columns": ["Requested purpose", "Declared amount"], "rows": [[label, _money(amount, currency)] for label, amount in request_uses]}})
    summaries = safe_document_summaries(financials.get("document_summaries"))
    if summaries["bank_activity"]:
        sections.append({"key": "bank_aggregates", "title": "Aggregate bank activity", "paragraphs": [
            "Period totals only. Transaction details, account identifiers and counterparties are excluded. These are cash-activity aggregates, not earned-revenue or NOI determinations."], "facts": [],
            "table": {"columns": ["Month", "Deposits", "Withdrawals", "Net cash activity"],
                      "rows": [[row["month"], *[_money(row.get(key), row.get("currency")) for key in ("deposits", "withdrawals", "net")]] for row in summaries["bank_activity"]]}})
    if summaries["tax_years"]:
        sections.append({"key": "tax_aggregates", "title": "Year-to-year business tax aggregates", "paragraphs": [
            "Tax-return income and deductions are reported on their tax basis. They are separate from a reviewed operating-income baseline; source tax returns remain access-controlled."], "facts": [],
            "table": {"columns": ["Year", "Gross receipts", "Tax income", "Total deductions"],
                      "rows": [[str(row["year"]), *[_money(row.get(key), row.get("currency")) for key in ("gross_receipts", "income", "total_deductions")]] for row in summaries["tax_years"]]}})
    return sections
