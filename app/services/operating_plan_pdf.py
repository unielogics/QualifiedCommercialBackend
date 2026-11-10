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
    return f"{'-' if amount < 0 else ''}${abs(amount):,.2f}" if currency == "USD" else f"{html.escape(currency)} {amount:,.2f}"


def _ratio(value):
    return "Not available" if value is None else f"{float(value):.2f}x"


def _unit_breakdown(row, currency):
    if row.get("cost_basis") != "per_unit":
        return ""
    return f"Quantity: {_text(row.get('current_units_per_period'))} {_text(row.get('unit_label'))}; unit cost: {_money(row.get('current_unit_cost'), currency)}; fixed cost: {_money(row.get('current_fixed_amount'), currency)} / {_text(row.get('cadence'))}. Recorded source total: {_money(row.get('current_amount'), currency)}. The derived total uses quantity times unit cost plus fixed cost; the source total is not added again."


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
        ("Earnings still needed for 1.25x - current debt", None, "target_gap_current", False),
        ("Earnings still needed for 1.25x - refinance", None, "target_gap_refinance", False),
    ]
    rows = []
    for label, current_key, key, ratio in metrics:
        show = _ratio if ratio else lambda value: _money(value, currency)
        rows.append(f"<tr><td>{label}</td><td class='num'>{show(baseline.get(current_key))}</td><td class='num'>{show(proposed.get(key))}</td><td class='num'>{show(approved.get(key))}</td></tr>")
    totals = summary.get("totals") or {}
    impacts = "".join(f"<tr><td>{label}</td><td class='num'>{_money((totals.get('proposed') or {}).get(key), currency)}</td><td class='num'>{_money((totals.get('approved') or {}).get(key), currency)}</td></tr>"
        for key, label in (("annual_impact", "Net annual improvement"), ("monthly_impact", "Monthly improvement"), ("first_year_impact", "First-year improvement"), ("one_time_cost", "One-time implementation cost")))
    warnings = list(dict.fromkeys([*(summary.get("missing") or []), *(summary.get("risk_flags") or []), *(summary.get("notes") or [])]))
    warning_html = "<h3>Missing confirmations and risks</h3><ul>" + "".join(f"<li>{_text(item)}</li>" for item in warnings) + "</ul>" if warnings else ""
    return f"<h2>Earnings and coverage - before and after</h2><p class='muted'>Selected baseline: {_text(baseline.get('period'))}; {_text(baseline.get('currency'))}; {_text(baseline.get('basis'))} basis. Proposed and staff-reviewed columns are conditional illustrations; financing proceeds and principal repayments do not create earnings.</p>" + \
        "<table><thead><tr><th>Measure</th><th>Current baseline</th><th>All proposals</th><th>Staff-reviewed adjustments</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>" + \
        "<h3>Adjustment totals</h3><table><thead><tr><th>Impact</th><th>All proposals</th><th>Staff-reviewed adjustments</th></tr></thead><tbody>" + impacts + "</tbody></table>" + warning_html


def _released_cash_flow(summary, body):
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
            "cost_basis", "unit_label", "current_units_per_period", "proposed_units_per_period", "current_fixed_amount", "proposed_fixed_amount",
            "units_per_period", "current_unit_price", "proposed_unit_price", "current_unit_cost", "proposed_unit_cost", "contribution_margin_pct",
            "processing_volume", "processing_transaction_count", "current_processing_rate_pct", "proposed_processing_rate_pct",
            "current_processing_fixed_fee", "proposed_processing_fixed_fee", "current_processing_per_transaction_fee", "proposed_processing_per_transaction_fee",
            "current_processing_rate_basis", "proposed_processing_rate_basis") if row.get(key) not in (None, "", "unknown")]
        driver_html = "<br>".join(f"{_text(key)}: {_text(value)}" for key, value in drivers)
        issues = "<br>".join(_text(value) for value in [*(impact.get("issues") or []), *(impact.get("risk_flags") or [])])
        adjustment_html.append(f"<section><h3>{index}. {_text(row.get('label'))} <small>{_text(impact.get('review_state') or row.get('status') or 'proposed').upper().replace('_', ' ')}</small></h3>"
            f"<p><b>Change:</b> {_text(row.get('kind')).replace('_', ' ')} - {proposal}</p>"
            f"<p>{_text(row.get('explanation'))}</p>" + (f"<p>{driver_html}</p>" if driver_html else "") +
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
        _released_cash_flow(summary, body) + \
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
