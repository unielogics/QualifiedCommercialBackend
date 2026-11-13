"""Immutable, shareable operating disclosure and conditional earnings plan PDF."""
from __future__ import annotations

import html
import io
import logging

from app.schemas.earnings_improvement import cost_amount

log = logging.getLogger(__name__)


def _text(value):
    return html.escape(str(value if value not in (None, "") else "Not provided"))


def _money(value, currency):
    if value in (None, ""):
        return "Not available"
    amount = float(value)
    code = str(currency or "").upper()
    return f"{'-' if amount < 0 else ''}${abs(amount):,.2f}" if code == "USD" else f"{html.escape(code or 'Currency unconfirmed')} {amount:,.2f}"


def _ratio(value):
    return "Not available" if value is None else f"{float(value):.2f}x"


def _unit_breakdown(row, currency):
    if row.get("cost_basis") != "per_unit":
        return ""
    return f"Quantity: {_text(row.get('current_units_per_period'))} {_text(row.get('unit_label'))}; unit cost: {_money(row.get('current_unit_cost'), currency)}; fixed cost: {_money(row.get('current_fixed_amount'), currency)} / {_text(row.get('cadence'))}. Recorded source total: {_money(row.get('current_amount'), currency)}. The derived total uses quantity times unit cost plus fixed cost; the source total is not added again."


def _business_units(summary, body, currency):
    from app.services.operating_unit_economics import evaluate

    value = summary.get("unit_economics") or evaluate(body)
    if value is None:
        return ""
    rows = "".join(f"<tr><td>{_text(row.get('label'))}<br><small>{_text(row.get('scope_note')) if row.get('scope_note') else _text(row.get('volume_behavior')).title()}</small></td>"
        f"<td class='num'>{_money(row.get('current_annual_amount'), currency)}</td>"
        f"<td class='num'>{_money(row.get('current_average_cost_per_unit'), currency)}</td>"
        f"<td class='num'>{_money(row.get('current_variable_cost_per_unit'), currency)}</td>"
        f"<td class='num'>{_money(row.get('proposed_annual_amount'), currency)}</td></tr>" for row in value.get("rows", []))
    one_time = "".join(f"<p>{_text(row.get('label'))}: one-time disclosure {_money(row.get('one_time_amount'), currency)}. {_text(row.get('scope_note'))}</p>"
        for row in value.get("rows", []) if not row.get("recurring", True))
    missing = "<ul>" + "".join(f"<li>{_text(path)}</li>" for path in value["missing"]) + "</ul>" if value.get("missing") else ""
    return (f"<h2>Annual business-unit economics</h2><p><b>Unit definition:</b> {_text(value.get('unit_label'))}. "
        f"<b>Units reporting period:</b> {_text(value.get('historical_units_period_start'))} to {_text(value.get('historical_units_period_end'))}. "
        f"<b>Historical units:</b> {_text(value.get('historical_units_annual'))}; <b>Disclosed annual revenue:</b> {_money(value.get('historical_revenue_annual'), currency)}; "
        f"<b>Revenue per business unit:</b> {_money(value.get('current_revenue_per_unit'), currency)}.</p>"
        "<table><thead><tr><th>Expense / behavior</th><th>Current annual cost</th><th>Average / business unit</th><th>Variable / business unit</th><th>Proposed annual cost</th></tr></thead><tbody>" + rows + "</tbody></table>" + one_time +
        f"<p><b>Proposed annual units:</b> {_text(value.get('proposed_units_annual'))}; <b>Proposed annual revenue:</b> {_money(value.get('proposed_revenue_annual'), currency)}; "
        f"<b>Added revenue:</b> {_money(value.get('revenue_change_annual'), currency)}; <b>Added variable costs:</b> {_money(value.get('variable_cost_change_annual'), currency)}; "
        f"<b>Net recurring impact before implementation costs:</b> {_money(value.get('net_annual_impact'), currency)}.</p>" + missing +
        "<p class='muted'>Business volume holds historical selling price and variable rates constant. Fixed expenses stay fixed; average cost per unit is descriptive. Local expense units are not scaled twice. These disclosures and proposals do not replace verified historical earnings or source NOI.</p>")


def _before_after(summary, currency):
    if not summary:
        return "<p class='muted'>Save a version to capture the financial evidence and reviewed projection. Historical figures remain unchanged.</p>"
    baseline = summary.get("baseline") or {}
    projections = summary.get("projection") or {}
    proposed, approved = projections.get("proposed") or {}, projections.get("approved") or {}
    metrics = [
        ("Annual earnings baseline", "earnings", "earnings", False),
        ("Annual operating income (NOI)", "noi", "noi", False),
        ("Coverage with current debt", "dscr", "dscr_current", True),
        ("Coverage with refinance", None, "dscr_with_refinance", True),
        ("First-year earnings", None, "first_year_earnings", False),
        ("First-year operating income (NOI)", None, "first_year_noi", False),
        ("Earnings still needed for 1.25x - current debt", None, "target_gap_current", False),
        ("Earnings still needed for 1.25x - refinance", None, "target_gap_refinance", False),
    ]
    rows = []
    for label, current_key, key, ratio in metrics:
        row_currency = baseline.get("noi_currency", currency) if key in {"noi", "first_year_noi"} else currency
        show = _ratio if ratio else lambda value, code=row_currency: _money(value, code)
        rows.append(f"<tr><td>{label}</td><td class='num'>{show(baseline.get(current_key))}</td><td class='num'>{show(proposed.get(key))}</td><td class='num'>{show(approved.get(key))}</td></tr>")
    noi = baseline.get("noi_governance") or {}
    noi_basis = ""
    if noi:
        noi_basis = (f"<p class='muted'>NOI basis: {_text(noi.get('label') or baseline.get('noi_label'))}; "
                     f"{_text(noi.get('period') or baseline.get('noi_period'))}; {_text(noi.get('currency') or baseline.get('noi_currency'))}. "
                     f"Status: {_text(noi.get('status'))}. {_text(noi.get('status_reason'))}</p>")
    if proposed.get("noi_status") or approved.get("noi_status"):
        noi_basis += (f"<p class='muted'>NOI illustration status - all proposals: {_text(proposed.get('noi_status')).replace('_', ' ')}; "
                      f"staff-reviewed adjustments: {_text(approved.get('noi_status')).replace('_', ' ')}. "
                      "Projected NOI can be shown from a comparable NOI baseline independently of EBITDA; it does not establish debt coverage.</p>")
    totals = summary.get("totals") or {}
    impacts = "".join(f"<tr><td>{label}</td><td class='num'>{_money((totals.get('proposed') or {}).get(key), currency)}</td><td class='num'>{_money((totals.get('approved') or {}).get(key), currency)}</td></tr>"
        for key, label in (("annual_impact", "Net annual improvement"), ("monthly_impact", "Monthly improvement"), ("first_year_impact", "First-year improvement"), ("one_time_cost", "One-time implementation cost")))
    warnings = list(dict.fromkeys([*(summary.get("missing") or []), *(summary.get("risk_flags") or []), *(summary.get("notes") or [])]))
    warning_html = "<h3>Missing confirmations and risks</h3><ul>" + "".join(f"<li>{_text(item)}</li>" for item in warnings) + "</ul>" if warnings else ""
    return f"<h2>Earnings and coverage - before and after</h2><p class='muted'>Selected baseline: {_text(baseline.get('period'))}; {_text(baseline.get('currency'))}; {_text(baseline.get('basis'))} basis. Proposed and staff-reviewed columns are conditional illustrations; financing proceeds and principal repayments do not create earnings.</p>" + \
        noi_basis + "<table><thead><tr><th>Measure</th><th>Current baseline</th><th>All proposals</th><th>Staff-reviewed adjustments</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>" + \
        "<h3>Adjustment totals</h3><table><thead><tr><th>Impact</th><th>All proposals</th><th>Staff-reviewed adjustments</th></tr></thead><tbody>" + impacts + "</tbody></table>" + warning_html


def _released_cash_flow(summary, body):
    if summary.get("cash_bridge"):
        return ""  # The captured net cash bridge below replaces the legacy gross relief.
    cash = summary.get("released_cash_flow") or {}
    rows = cash.get("allocations") or (body.get("released_cash_flow") or {}).get("allocations") or []
    if not cash.get("required") and not rows:
        return ""
    details = "".join(f"<tr><td>{_text(row.get('label'))}</td><td class='num'>{_money(row.get('amount_monthly'), 'USD')}</td><td>{_text(row.get('notes'))}</td></tr>" for row in rows)
    return ("<h2>Recurring released-cash-flow allocation</h2>"
        "<p>Uses of recurring debt-service relief from the saved coverage plan. These amounts do not increase historical earnings, EBITDA or NOI and are separate from one-time loan proceeds.</p>"
        f"<p>Monthly released cash flow: {_money(cash.get('amount_monthly'), 'USD')}; allocated: {_money(cash.get('allocated_monthly'), 'USD')}; remaining: {_money(cash.get('unallocated_monthly'), 'USD')}. Status: {_text(cash.get('status'))}.</p>"
        "<table><thead><tr><th>Monthly use</th><th>Monthly amount</th><th>Explanation</th></tr></thead><tbody>"
        + (details or "<tr><td colspan='3'>No monthly allocation entered.</td></tr>") + "</tbody></table>")


def _goal_tracking(summary, body):
    """Render only the immutable saved-version snapshot; never infer a goal."""
    goal, cash = summary.get("goal_progress") or {}, summary.get("cash_bridge") or {}
    if not goal:
        return ""
    currency = goal.get("currency")
    rows = "".join(f"<tr><td>{label}</td><td class='num'>{_money(goal.get(key), currency)}</td></tr>"
        for label, key in (("Current annual NOI", "current_noi"), ("Zero / break-even", "zero"),
            ("Proposed annual NOI", "proposed_noi"), ("Staff annual NOI goal", "target"),
            ("Remaining annual gap with proposals", "proposed_gap")))
    result = (f"<h2>Annual NOI goal - saved revision {_text(goal.get('goal_revision'))}</h2>"
        "<table><thead><tr><th>Measure</th><th>Annual amount</th></tr></thead><tbody>" + rows + "</tbody></table>"
        "<p class='muted'>Operating-plan NOI is shown before additional loan operating charges; those charges appear in the cash bridge. A source loss remains negative and missing figures stay unavailable. Loan payment relief does not improve NOI.</p>")
    if not cash:
        return result
    rows = "".join(f"<tr><td>{label}</td><td class='num'>{_money(cash.get(key), cash.get('currency'))}</td></tr>"
        for label, key in (("Current regular debt service", "before_debt_service_monthly"),
            ("Saved-plan regular debt service (includes financing charges)", "after_debt_service_monthly"),
            ("New recurring operating charges", "new_operating_charges_monthly"),
            ("Net loan cash change", "net_loan_cash_change_monthly"),
            ("Conditional operations change", "modeled_operations_monthly"),
            ("Combined incremental change", "combined_incremental_monthly"),
            ("Assigned loan cash relief", "allocated_monthly"), ("Still to assign", "unallocated_monthly")))
    result += ("<h2>Loan and operations cash bridge</h2><table><thead><tr><th>Measure</th><th>Monthly amount</th></tr></thead><tbody>"
        + rows + "</tbody></table><p class='muted'>Incremental illustration, not a cash-flow statement or bank balance. Financing charges included in debt service are not deducted twice. Operations timing and setup costs are separate; assignments do not create additional expenses or earnings.</p>")
    allocations = {row["id"]: row for row in (body.get("released_cash_flow") or {}).get("allocations", [])}
    expenses = {row["id"]: row for row in body.get("expenses", [])}
    links = []
    for link in cash.get("linked_allocations", []):
        row, expense = allocations.get(link["id"], {}), expenses.get(link.get("expense_row_id"), {})
        links.append(f"<tr><td>{_text(row.get('label'))}<br><small>{_text(link.get('purpose')).replace('_', ' ')}"
            + (f"; {_text(expense.get('label'))}" if expense else "") + "</small></td>"
            f"<td class='num'>{_money(link.get('amount_monthly'), cash.get('currency'))}</td>"
            f"<td>{_text(link.get('status'))}</td></tr>")
    if links:
        result += "<h3>Monthly cash assignments</h3><table><thead><tr><th>Use and linked item</th><th>Amount</th><th>Status</th></tr></thead><tbody>" + "".join(links) + "</tbody></table>"
    return result


def _expense_groups(summary, body, currency):
    groups = summary.get("expense_groups") or []
    if not groups:
        return ""
    details = []
    for group in groups:
        group_currency = group.get("currency") or currency
        rows = [row for row in body.get("expenses") or []
                if not row.get("excluded") and group.get("key") and row.get("source_group_key") == group.get("key")]
        amounts = (f"Source annual reference: {_money(group.get('baseline_annual_amount'), group_currency)}; "
                   f"disclosed annual detail: {_money(group.get('disclosed_annual_amount'), group_currency)}; "
                   f"difference: {_money(group.get('difference_annual'), group_currency)}.")
        items = "".join(f"<li>{_text(row.get('label') or row.get('category'))}: {_money(cost_amount(row), currency)} / "
                        f"{_text(row.get('cadence')).replace('_', ' ')}"
                        + (f"; {_text(row.get('payee'))}" if row.get("payee") else "") + "</li>" for row in rows)
        notes = " ".join(str(note) for note in group.get("notes") or [])
        details.append(f"<div class='expense-note'><h3>{_text(group.get('label') or group.get('category'))}</h3>"
            f"<p>{amounts} Status: {_text(group.get('status')).replace('_', ' ')}.</p>"
            + ("<ul>" + items + "</ul>" if items else "<p>No active expense detail is linked to this source reference.</p>")
            + (f"<p class='muted'>{_text(notes)}</p>" if notes else "") + "</div>")
    return ("<h2>Source expense groups and disclosed detail</h2>"
        "<p class='muted'>Source totals are references for reconciliation. Their detailed expense lines replace the reference in the disclosure; the source total is not added again. A difference does not change historical NOI or create a savings adjustment.</p>"
        + "".join(details))


def _marketing_assumptions(row, impact, currency):
    if row.get("kind") != "marketing_investment":
        return ""
    calculation = impact.get("calculation") or {}
    multiple = row.get("revenue_return_multiple")
    margin = row.get("contribution_margin_pct")
    margin_text = f"{float(margin):g}%" if margin is not None else "Not available"
    return (f"<p><b>Product or service:</b> {_text(row.get('product_name'))}. "
        f"<b>Assumed revenue per added marketing dollar:</b> {_ratio(multiple)}; "
        f"<b>Contribution margin:</b> {margin_text}.</p>"
        f"<p>Per {_text(row.get('cadence')).replace('_', ' ')} period: added spend {_money(calculation.get('incremental_spend'), currency)}; "
        f"modeled added revenue {_money(calculation.get('incremental_revenue'), currency)}; "
        f"contribution before added marketing spend {_money(calculation.get('incremental_contribution'), currency)}.</p>"
        "<p class='muted'>Net effect = added spend times (revenue return multiple times contribution margin / 100 - 1), before separately disclosed implementation costs. Added marketing spend is deducted once. These are assumptions, not guaranteed sales or historical income.</p>")


def _command_value(row, value, currency):
    return _text(value) if row.get("target") == "unit_quantity" else _money(value, currency)


def _preparer_handoff(summary, currency, body):
    """Only a captured version summary supplies this handoff, never live inputs."""
    value = summary.get("preparer_handoff")
    if not value:
        return ""
    historical = value.get("historical") or {}
    groups = {row["key"]: row for row in historical.get("source_groups", [])}
    splits = []
    for row in historical.get("itemization", []):
        group = groups.get(row["source_group_key"], {})
        splits.append(f"<tr><td>{_text(group.get('label') or row.get('expense_category'))}</td><td>{_text(row.get('year'))}</td>"
            f"<td class='num'>{_money(row.get('reference_annual_amount'), currency)}</td>"
            f"<td class='num'>{_money(row.get('children_annual_amount'), currency)}</td>"
            f"<td class='num'>{_money(row.get('remainder_annual_amount'), currency)}</td><td>{_text(row.get('status'))}; {_text(row.get('reference_authority')).replace('_', ' ')}</td></tr>")
    corrections = []
    for row in historical.get("corrections_for_preparer_review", []):
        group = groups.get(row["source_group_key"], {})
        corrections.append(f"<p><b>{_text(group.get('label') or 'Historical source variance')} - {_text(row.get('year'))}:</b> {_text(row.get('reason'))}. "
            f"Evidence: {_text(row.get('evidence_note') or 'Linked supporting documents')}. Status: unresolved preparer review.</p>")
    commands = []
    labels = {row["id"]: row.get("label") for row in body.get("expenses", [])}
    target_labels = {"total_cost": "Current-period total budget", "unit_cost": "Cost per local unit", "unit_quantity": "Local units per period",
        "unit_price": "Selling price per product/service unit", "annual_price_revenue": "Annual revenue at the proposed selling prices"}
    for row in (value.get("future") or {}).get("commands", []):
        command = row.get("command") or {}
        unit = "%" if command.get("mode") == "percent" else " per year" if row.get("target") == "annual_price_revenue" or command.get("allocation") == "proportional" else " in the stated item/unit period"
        amount = _text(command.get("value")) if command.get("mode") == "percent" else _command_value(row, command.get("value"), currency)
        commands.append(f"<p><b>{_text(row.get('expense_category') or 'Pricing').replace('_', ' ')}:</b> {_text(command.get('direction'))} {amount}{unit}; "
            f"target {_text(target_labels.get(row.get('target'), row.get('target')))}. Current {_command_value(row, row.get('current_value'), currency)}; proposed {_command_value(row, row.get('proposed_value'), currency)}; "
            f"annual recurring effect before implementation {_money(row.get('gross_annual_effect'), currency)}. Status: {_text(row.get('status'))}.</p>"
            + "<ul>" + "".join(f"<li>{_text(note)}</li>" for note in [*row.get("assumptions", []), *row.get("issues", [])]) + "</ul>"
            + ("<ul>" + "".join(f"<li>{_text(labels.get(item['expense_row_id']) or 'Expense item')}: annual budget {_money(item['current_annual_cost'], currency)} to {_money(item['proposed_annual_cost'], currency)}</li>" for item in row.get("allocations", [])) + "</ul>" if row.get("allocations") else ""))
    return ("<h2>Tax-preparer handoff - saved version</h2><h3>Historical records and unresolved corrections</h3>"
        "<p>Source income and expense records remain historical references. Itemization does not replace verified NOI, amend a return or establish deductibility. Tax form and line classification require the preparer's confirmation when not documented.</p>"
        + ("<table><thead><tr><th>Source group</th><th>Year</th><th>Reference annual</th><th>Detail annual</th><th>Remainder</th><th>Reconciliation</th></tr></thead><tbody>" + "".join(splits) + "</tbody></table>" if splits else "")
        + "".join(corrections) + "<h3>Future operating proposals - separate from historical tax records</h3>" + "".join(commands)
        + "<p class='muted'>These are conditional future plans, not filed deductions or approved tax adjustments. No return is filed or amended by this handoff.</p>")


def build_html(*, body: dict, financial_totals: dict | None = None) -> str:
    facts = financial_totals or {}
    summary = facts.get("improvement_plan") or {}
    sources = {str(row.get("file_id")): row.get("file_name") for row in facts.get("evidence_sources") or []}
    impacts = {str(row.get("id")): row for row in summary.get("adjustments") or []}
    currency = str(body.get("currency") or "Currency not supplied")
    expenses, actions = body.get("expenses") or [], body.get("adjustments") or []

    def evidence_names(row):
        return "; ".join(sources.get(str(value)) or "Linked source document (name unavailable)" for value in row.get("source_file_ids") or []) or "No linked supporting document"

    active_expenses = [row for row in expenses if not row.get("excluded")]
    expense_rows = "".join(
        f"<tr><td>{index}. {_text(row.get('label') or row.get('category'))}<br><small>{_text(row.get('category')).replace('_', ' ')}</small></td>"
        f"<td>{_text(row.get('payee'))}</td><td class='num'>{_money(cost_amount(row), currency)}</td>"
        f"<td>{_text(row.get('cadence')).replace('_', ' ')}</td><td>{'Yes' if row.get('essential') else 'No'}</td></tr>"
        for index, row in enumerate(active_expenses, 1))
    if not expense_rows:
        expense_rows = "<tr><td colspan='5'>" + ("The business confirmed no operating expenses to disclose." if body.get("no_operating_expenses_confirmed") else "No expense disclosure entered.") + "</td></tr>"
    # Long note/evidence cells can exceed a page. Flow them outside the table
    # so both PDF engines can paginate every paragraph without clipping a row.
    expense_notes = "".join(
        f"<div class='expense-note'><h3>{index}. {_text(row.get('label') or row.get('category'))} - notes and evidence</h3>"
        + (f"<p>{_unit_breakdown(row, currency)}</p>" if row.get("cost_basis") == "per_unit" else "")
        + (f"<p>{_text(row['notes'])}</p>" if row.get("notes") else "")
        + (f"<p class='muted'><b>Evidence:</b> {_text(evidence_names(row))}</p>" if row.get("source_file_ids") else "") + "</div>"
        for index, row in enumerate(active_expenses, 1) if row.get("notes") or row.get("source_file_ids") or row.get("cost_basis") == "per_unit")
    adjustment_html = []
    for index, row in enumerate((row for row in actions if not row.get("excluded")), 1):
        impact = impacts.get(str(row.get("id"))) or {}
        proposal = f"{_money(cost_amount(row), currency)} to {_money(cost_amount(row, 'proposed'), currency)} / {_text(row.get('cadence')).replace('_', ' ')}"
        drivers = [(key.replace("_", " "), row[key]) for key in (
            "product_name",
            "cost_basis", "unit_label", "current_units_per_period", "proposed_units_per_period", "current_fixed_amount", "proposed_fixed_amount",
            "units_per_period", "current_unit_price", "proposed_unit_price", "current_unit_cost", "proposed_unit_cost", "contribution_margin_pct",
            "processing_volume", "processing_transaction_count", "current_processing_rate_pct", "proposed_processing_rate_pct",
            "current_processing_fixed_fee", "proposed_processing_fixed_fee", "current_processing_per_transaction_fee", "proposed_processing_per_transaction_fee",
            "current_processing_rate_basis", "proposed_processing_rate_basis") if row.get(key) not in (None, "", "unknown")]
        if row.get("kind") == "unit_volume":
            proposal = f"{_text(row.get('unit_change_value'))}{'%' if row.get('unit_change_mode') == 'percent' else ' units'} change in annual business volume"
        if row.get("command"):
            command = row["command"]
            resolved = (impact.get("calculation") or {}).get("command") or {}
            proposal = (f"{_text(command.get('direction'))} {_text(command.get('value'))}{'%' if command.get('mode') == 'percent' else ''} "
                f"{_text(command.get('target')).replace('_', ' ')}; resolved current {_text(resolved.get('current_value'))} to {_text(resolved.get('proposed_value'))}. "
                + ("Annual dollars." if command.get("target") == "annual_price_revenue" or row.get("kind") == "expense_group_change" else "Amounts use the stated item/unit period."))
        driver_html = "<br>".join(f"{_text(key)}: {_text(value)}" for key, value in drivers)
        issues = "<br>".join(_text(value) for value in [*(impact.get("issues") or []), *(impact.get("risk_flags") or [])])
        placement = ""
        if row.get("expense_category") is not None or row.get("source_group_key"):
            owner = next((expense for expense in expenses if str(expense.get("id")) == str(row.get("expense_row_id"))), None)
            category = owner.get("category") if owner else row.get("expense_category")
            group_key = owner.get("source_group_key") if owner else row.get("source_group_key")
            group = next((item for item in summary.get("expense_groups", []) if item.get("key") == group_key), None)
            placement = (f"<p><b>Activity placement:</b> {_text(category).replace('_', ' ')}"
                + (f"; source expense group: {_text(group.get('label') if group else 'Selected group (current source reference unavailable)')}" if group_key else "")
                + ". Placement organizes the plan; it is not financial source evidence.</p>")
        adjustment_html.append(f"<section><h3>{index}. {_text(row.get('label'))} <small>{_text(impact.get('review_state') or row.get('status') or 'proposed').upper().replace('_', ' ')}</small></h3>"
            f"<p><b>Change:</b> {_text(row.get('kind')).replace('_', ' ')} - {proposal}</p>"
            f"<p>{_text(row.get('explanation'))}</p>" + placement + (f"<p>{driver_html}</p>" if driver_html else "") +
            _marketing_assumptions(row, impact, currency) +
            f"<p><b>Net annual impact:</b> {_money(impact.get('net_annual_impact'), currency)}; <b>Monthly:</b> {_money(impact['net_annual_impact'] / 12 if impact.get('net_annual_impact') is not None else None, currency)}; <b>First year:</b> {_money(impact.get('first_year_impact'), currency)}</p>"
            f"<p><b>Implementation cost:</b> {_money(row.get('implementation_cost'), currency)} / {_text(row.get('implementation_cost_cadence')).replace('_', ' ')}"
            f"<br><b>Effective:</b> {_text(row.get('effective_date'))} - <b>Responsible:</b> {_text(row.get('owner_name'))}</p>"
            f"<p class='muted'><b>Evidence:</b> {_text(evidence_names(row))}<br>{_text(row.get('source_note'))}</p>" +
            (f"<p class='muted'>{issues}</p>" if issues else "") + "</section>")
    excluded = sum(bool(row.get("excluded")) for row in [*expenses, *actions])
    return """<!doctype html><html><head><meta charset='utf-8'><style>
    @page {size:Letter; margin:32px;}
    body{font:11px Arial,sans-serif;color:#172033;line-height:1.4}h1{font-size:22px;margin:0 0 8px}h2{font-size:15px;margin:22px 0 8px}h3{font-size:12px;margin:10px 0 6px}
    p{margin:5px 0}small,.muted{font-size:9px;color:#64748b}.notice{padding:10px;background:#f1f5f9;border-left:3px solid #38549b;margin:12px 0}
    table{width:100%;border-collapse:collapse;table-layout:fixed}thead{display:table-header-group}th{font-size:9px;text-align:left;background:#f1f5f9}th,td{padding:6px;border-bottom:1px solid #dbe2ec;vertical-align:top;overflow-wrap:anywhere}td.num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}tr{break-inside:avoid}section{padding:10px 0;border-bottom:1px solid #dbe2ec;break-inside:avoid}section p,.expense-note p{overflow-wrap:anywhere}.expense-note{padding:6px 0}h3{break-after:avoid}
    </style></head><body>""" + f"<h1>Operating expenses and earnings plan</h1><p><b>{_text(body.get('business_name'))}</b></p>" + \
        f"<p>{_text(body.get('period_start'))} to {_text(body.get('period_end'))} | {_text(currency)} | {_text(body.get('basis'))} basis</p>" + \
        "<div class='notice'>Expense disclosures and proposed earnings improvements. These are separate from historical P&amp;L, NOI, EBITDA, current debt coverage and program qualification. Client submissions await staff review; improvements are conditional and require baseline reconciliation and supporting evidence.</div>" + \
        _before_after(summary, currency) + \
        _goal_tracking(summary, body) + \
        _business_units(summary, body, currency) + \
        _released_cash_flow(summary, body) + \
        _expense_groups(summary, body, currency) + \
        _preparer_handoff(summary, currency, body) + \
        f"<h2>Operating expense disclosure</h2><p class='muted'>Loan payments, transfers, income taxes, owner draws and noncash depreciation are disclosed separately from operating cash costs.</p><table><thead><tr><th>Expense</th><th>Payee</th><th>Current amount</th><th>Cadence</th><th>Essential</th></tr></thead><tbody>{expense_rows}</tbody></table>{expense_notes}" + \
        f"<h2>Earnings adjustments and explanation</h2>{''.join(adjustment_html) or '<p>No improvements proposed.</p>'}" + \
        f"<p class='muted'>{excluded} excluded or duplicate rows remain in the immutable version history.</p><h2>Plan notes</h2><p>{_text(body.get('notes'))}</p></body></html>"


def _render_pymupdf(document: str) -> bytes:
    import fitz

    buffer = io.BytesIO()
    writer = fitz.DocumentWriter(buffer)
    story = fitz.Story(html=document)
    media = fitz.paper_rect("letter")
    frame = media + (32, 32, -32, -32)
    more, count = 1, 0
    try:
        while more and count < 1000:
            count += 1
            device = writer.begin_page(media)
            more, _ = story.place(frame)
            story.draw(device)
            writer.end_page()
        if more:
            raise RuntimeError("Operating plan PDF exceeds the pagination limit")
    finally:
        writer.close()
    return buffer.getvalue()


def _with_footer(pdf: bytes) -> bytes:
    import fitz

    document = fitz.open(stream=pdf, filetype="pdf")
    for number, page in enumerate(document, 1):
        page.insert_text((32, page.rect.height - 17), f"Operating expenses and earnings plan - Page {number} of {len(document)}", fontsize=8, color=(0.39, 0.45, 0.55))
    return document.tobytes(garbage=3, deflate=True)


def render_pdf(*, body: dict, financial_totals: dict | None = None) -> bytes:
    document = build_html(body=body, financial_totals=financial_totals)
    try:
        from weasyprint import HTML

        pdf = HTML(string=document).write_pdf()
        if pdf:
            return _with_footer(pdf)
    except Exception:  # noqa: BLE001 - native PDF libraries may be unavailable locally
        log.info("Operating plan native PDF renderer unavailable; using PyMuPDF", exc_info=True)
    pdf = _render_pymupdf(document)
    if not pdf:
        raise RuntimeError("Operating plan PDF renderer returned no content")
    return _with_footer(pdf)
