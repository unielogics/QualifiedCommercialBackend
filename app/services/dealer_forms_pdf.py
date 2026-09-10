"""Renders the on-screen Personal Financial Statement (PFS) and Debt Schedule
forms into a PDF that satisfies the corresponding BucketRequestedDocument
exactly like a real client upload would — a fallback for borrowers who don't
have or don't understand these documents. Structured input is rendered once
into the PDF by the caller and then discarded; see dealer_ai_intake.py's
submit endpoints for the data-minimization rationale.

Same WeasyPrint HTML.write_pdf() pattern as document_signature.py's
render_signature_certificate_pdf, kept in a separate module because this is
financial form content, not a signed-document certificate.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

FORM_DISCLAIMER = (
    "This form is provided for underwriting processing convenience only. It does not "
    "constitute financial, investment, tax, or legal advice, and completing it is voluntary. "
    "Information you enter here is used solely to evaluate this financing request."
)

_STYLE = """
  body { font-family: Inter, Arial, sans-serif; color: #111827; margin: 44px; }
  h1 { font-size: 20px; margin-bottom: 2px; }
  h2 { font-size: 14px; margin-top: 22px; color: #374151; }
  .muted { color: #6b7280; font-size: 12px; }
  table { width: 100%; border-collapse: collapse; margin-top: 10px; }
  th, td { border: 1px solid #d1d5db; padding: 7px 10px; font-size: 12px; text-align: left; }
  th { background: #f3f4f6; }
  .totals td { font-weight: 700; }
  .disclaimer { margin-top: 26px; font-size: 10px; color: #6b7280; border-top: 1px solid #d1d5db; padding-top: 10px; }
"""


def render_pfs_pdf(
    *,
    owner_full_name: str,
    statement_date: str,
    assets: list[tuple[str, float]],
    liabilities: list[tuple[str, float]],
    total_assets: float,
    total_liabilities: float,
    net_worth: float,
) -> bytes:
    from weasyprint import HTML

    asset_rows = "".join(
        f"<tr><td>{html.escape(label)}</td><td>${amount:,.2f}</td></tr>" for label, amount in assets
    )
    liability_rows = "".join(
        f"<tr><td>{html.escape(label)}</td><td>${amount:,.2f}</td></tr>" for label, amount in liabilities
    ) or "<tr><td colspan='2'>None reported</td></tr>"
    body = f"""
    <html>
      <head><style>{_STYLE}</style></head>
      <body>
        <h1>Personal Financial Statement</h1>
        <div class="muted">{html.escape(owner_full_name)} — as of {html.escape(statement_date)}</div>
        <h2>Assets</h2>
        <table>
          <tr><th>Category</th><th>Amount</th></tr>
          {asset_rows}
          <tr class="totals"><td>Total assets</td><td>${total_assets:,.2f}</td></tr>
        </table>
        <h2>Liabilities</h2>
        <table>
          <tr><th>Category</th><th>Amount</th></tr>
          {liability_rows}
          <tr class="totals"><td>Total liabilities</td><td>${total_liabilities:,.2f}</td></tr>
        </table>
        <h2>Net worth</h2>
        <table>
          <tr class="totals"><td>Total assets minus total liabilities</td><td>${net_worth:,.2f}</td></tr>
        </table>
        <div class="disclaimer">
          {html.escape(FORM_DISCLAIMER)} Submitted electronically {datetime.now(UTC).isoformat()}.
          No Social Security Number was collected on this form.
        </div>
      </body>
    </html>
    """
    pdf = HTML(string=body).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned no PDF bytes")
    return pdf


def build_pfs_413_html(*, body: dict, statement_date: str) -> str:
    """The Form 413 statement, laid out the way a lender expects to read it.

    Assets and liabilities side by side with net worth beneath, then income and
    contingent liabilities, then whichever supporting schedules have rows. Empty
    schedules are omitted rather than printed as headings with nothing under
    them — a partner reading this should not have to scan past seven blank
    tables to find the two that were filled in.

    Separate from the render so the layout can be tested without WeasyPrint's
    native Pango libraries, which are present in the container and absent from a
    dev checkout — the same split `dealer_os/services/report_pdf.py` uses.
    """
    from app.services import pfs_schema

    totals = pfs_schema.totals(body)
    applicant = body.get("applicant") or {}
    assets = body.get("assets") or {}
    liabilities = body.get("liabilities") or {}

    def _money(value) -> str:
        return f"${float(value or 0):,.2f}"

    def _summary(rows, values) -> str:
        return "".join(
            f"<tr><td>{html.escape(row.label)}</td>"
            f"<td class='num'>{_money(values.get(row.key))}</td></tr>"
            for row in rows
        )

    def _schedule(key: str) -> str:
        rows = (body.get("schedules") or {}).get(key) or []
        if not rows:
            return ""
        spec = pfs_schema.SCHEDULES_BY_KEY[key]
        head = "".join(f"<th>{html.escape(column)}</th>" for column in spec.columns)
        cells = "".join(
            "<tr>"
            + "".join(
                f"<td>{html.escape(str(row.get(column, '') or ''))}</td>" for column in spec.columns
            )
            + "</tr>"
            for row in rows
            if isinstance(row, dict)
        )
        return f"<h2>{html.escape(spec.label)}</h2><table><tr>{head}</tr>{cells}</table>"

    schedules = "".join(_schedule(spec.key) for spec in pfs_schema.SCHEDULES)
    name = applicant.get("name") or ""

    doc = f"""
    <html>
      <head><style>{_STYLE}
        .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
        .cols {{ display: flex; gap: 18px; }}
        .cols > div {{ flex: 1; }}
      </style></head>
      <body>
        <h1>Personal Financial Statement</h1>
        <div class="muted">
          {html.escape(name)} — as of {html.escape(statement_date)}<br />
          {html.escape(applicant.get("business_name") or "")}
        </div>
        <div class="cols">
          <div>
            <h2>Assets</h2>
            <table>
              <tr><th>Category</th><th class="num">Amount</th></tr>
              {_summary(pfs_schema.ASSET_ROWS, assets)}
              <tr class="totals"><td>Total assets</td>
                <td class="num">{_money(totals["total_assets"])}</td></tr>
            </table>
          </div>
          <div>
            <h2>Liabilities</h2>
            <table>
              <tr><th>Category</th><th class="num">Amount</th></tr>
              {_summary(pfs_schema.LIABILITY_ROWS, liabilities)}
              <tr class="totals"><td>Total liabilities</td>
                <td class="num">{_money(totals["total_liabilities"])}</td></tr>
            </table>
          </div>
        </div>
        <table>
          <tr class="totals"><td>Net worth</td>
            <td class="num">{_money(totals["net_worth"])}</td></tr>
        </table>
        <div class="cols">
          <div>
            <h2>Source of income (annual)</h2>
            <table>
              {_summary(pfs_schema.INCOME_ROWS, body.get("income") or {})}
              <tr class="totals"><td>Total</td>
                <td class="num">{_money(totals["total_income"])}</td></tr>
            </table>
          </div>
          <div>
            <h2>Contingent liabilities</h2>
            <table>
              {_summary(pfs_schema.CONTINGENT_ROWS, body.get("contingent") or {})}
              <tr class="totals"><td>Total</td>
                <td class="num">{_money(totals["total_contingent"])}</td></tr>
            </table>
          </div>
        </div>
        {schedules}
        <div class="disclaimer">
          {html.escape(FORM_DISCLAIMER)} Submitted electronically
          {datetime.now(UTC).isoformat()}.
          No Social Security Number was collected on this form; where a partner requires one it
          is provided separately.
        </div>
      </body>
    </html>
    """
    return doc


def render_pfs_413_pdf(*, body: dict, statement_date: str) -> bytes:
    """The Form 413 statement as PDF bytes.

    Kept alongside `render_pfs_pdf` rather than replacing it, so flows still on
    the old eight-row form keep working while they are migrated.
    """
    from weasyprint import HTML

    pdf = HTML(string=build_pfs_413_html(body=body, statement_date=statement_date)).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned no PDF bytes")
    return pdf


def _statement_money(value: Any) -> str:
    amount = float(value or 0)
    return f"(${abs(amount):,.2f})" if amount < 0 else f"${amount:,.2f}"


def _statement_section_html(section: Any, values: dict[str, Any], subtotal: Any) -> str:
    """One schema section as a table: its lines, then its subtotal. A contra
    line is labelled as the schema labels it ("Less: …") and shown as typed."""
    from app.services.pfs_schema import _amount

    lines = "".join(
        f"<tr><td>{html.escape(row.label)}"
        f"{' <span class=chip>add-back</span>' if row.addback else ''}</td>"
        f"<td class='num'>{_statement_money(_amount(values.get(row.key)))}</td></tr>"
        for row in section.rows
    )
    return (
        f"<h2>{html.escape(section.label)}</h2><table>{lines}"
        f"<tr class='totals'><td>{html.escape(section.subtotal_label)}</td>"
        f"<td class='num'>{_statement_money(subtotal)}</td></tr></table>"
    )


_STATEMENT_STYLE = """
  .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .chip { font-size: 9px; color: #6b7280; border: 1px solid #d1d5db; border-radius: 8px; padding: 0 5px; margin-left: 4px; }
  .memo td { background: #f9fafb; }
  .warn td { color: #b91c1c; font-weight: 700; }
  .notes { margin-top: 14px; font-size: 12px; white-space: pre-wrap; }
"""


def _statement_header_html(schema: Any, header: dict[str, Any]) -> str:
    parts = []
    for field in schema.header:
        value = str(header.get(field.key) or "").strip()
        if value and field.key != "business_name":
            parts.append(f"{html.escape(field.label)}: {html.escape(value)}")
    return " &middot; ".join(parts)


def _statement_notes_html(body: dict) -> str:
    notes = str(body.get("notes") or "").strip()
    return f"<div class='notes'><strong>Notes.</strong> {html.escape(notes)}</div>" if notes else ""


def build_p_and_l_html(*, body: dict) -> str:
    """The profit and loss statement as a lender reads it: revenue, operating
    expenses, the line below, then the EBITDA memo built from the add-back
    flags. Split from the render so the layout can be tested without
    WeasyPrint's native libraries, as `build_pfs_413_html` is."""
    from app.services import business_statement_schema as bss

    schema = bss.SCHEMA_FOR["p_and_l"]
    totals = schema.totals(body)
    header = body.get("header") or {}
    sections = body.get("sections") or {}
    by_key = {section.key: section for section in schema.sections}
    period = bss.period_label("p_and_l", body) or "period not stated"

    revenue = _statement_section_html(
        by_key["revenue"], sections.get("revenue") or {}, totals["gross_profit"]
    )
    opex = _statement_section_html(
        by_key["operating_expenses"],
        sections.get("operating_expenses") or {},
        totals["total_operating_expenses"],
    )
    other_description = str((sections.get("operating_expenses") or {}).get("other_description") or "").strip()
    below = _statement_section_html(
        by_key["below_the_line"], sections.get("below_the_line") or {}, totals["net_income"]
    )
    addbacks = [row for section in schema.sections for row in section.rows if row.addback]
    memo_rows = "".join(
        f"<tr class='memo'><td>Add: {html.escape(row.label.lower())}</td>"
        f"<td class='num'>{_statement_money(bss._amount((sections.get(section.key) or {}).get(row.key)))}</td></tr>"
        for section in schema.sections
        for row in section.rows
        if row in addbacks
    )
    doc = f"""
    <html>
      <head><style>{_STYLE}{_STATEMENT_STYLE}</style></head>
      <body>
        <h1>Profit and Loss Statement</h1>
        <div class="muted">
          {html.escape(str(header.get("business_name") or ""))} &mdash; {html.escape(period)}<br />
          {_statement_header_html(schema, header)}
        </div>
        {revenue}
        {opex}
        {f"<div class='muted'>Other expenses: {html.escape(other_description)}</div>" if other_description else ""}
        <table>
          <tr class="totals"><td>Operating income</td>
            <td class="num">{_statement_money(totals["operating_income"])}</td></tr>
        </table>
        {below}
        <h2>EBITDA (memo)</h2>
        <table>
          <tr class="memo"><td>Net income</td>
            <td class="num">{_statement_money(totals["net_income"])}</td></tr>
          {memo_rows}
          <tr class="totals"><td>EBITDA</td>
            <td class="num">{_statement_money(totals["ebitda"])}</td></tr>
          <tr class="memo"><td>Owner compensation (add-back candidate, not added)</td>
            <td class="num">{_statement_money(totals["owner_compensation"])}</td></tr>
        </table>
        {_statement_notes_html(body)}
        <div class="disclaimer">
          {html.escape(FORM_DISCLAIMER)} Submitted electronically {datetime.now(UTC).isoformat()}.
        </div>
      </body>
    </html>
    """
    return doc


def render_p_and_l_pdf(*, body: dict) -> bytes:
    from weasyprint import HTML

    pdf = HTML(string=build_p_and_l_html(body=body)).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned no PDF bytes")
    return pdf


def build_balance_sheet_html(*, body: dict) -> str:
    """The balance sheet with the identity line and any unreconciled
    difference printed, so a reader sees what the borrower saw."""
    from app.services import business_statement_schema as bss

    schema = bss.SCHEMA_FOR["balance_sheet"]
    totals = schema.totals(body)
    header = body.get("header") or {}
    sections = body.get("sections") or {}
    by_key = {section.key: section for section in schema.sections}
    as_of = bss.period_label("balance_sheet", body) or "date not stated"

    def part(key: str) -> str:
        return _statement_section_html(
            by_key[key], sections.get(key) or {}, totals[by_key[key].subtotal_key]
        )

    equity_note = (
        "<div class='muted'>Equity implied from assets less liabilities; the section was left blank.</div>"
        if totals["equity_implied"]
        else ""
    )
    imbalance = (
        ""
        if totals["balances"]
        else f"<tr class='warn'><td>Unreconciled difference</td>"
        f"<td class='num'>{_statement_money(totals['imbalance'])}</td></tr>"
    )
    doc = f"""
    <html>
      <head><style>{_STYLE}{_STATEMENT_STYLE}</style></head>
      <body>
        <h1>Balance Sheet</h1>
        <div class="muted">
          {html.escape(str(header.get("business_name") or ""))} &mdash; {html.escape(as_of)}<br />
          {_statement_header_html(schema, header)}
        </div>
        {part("current_assets")}
        {part("fixed_assets")}
        {part("other_assets")}
        <table>
          <tr class="totals"><td>Total assets</td>
            <td class="num">{_statement_money(totals["total_assets"])}</td></tr>
        </table>
        {part("current_liabilities")}
        {part("long_term_liabilities")}
        <table>
          <tr class="totals"><td>Total liabilities</td>
            <td class="num">{_statement_money(totals["total_liabilities"])}</td></tr>
        </table>
        {part("equity")}
        {equity_note}
        <h2>Total liabilities and equity</h2>
        <table>
          <tr class="totals"><td>Total liabilities and equity</td>
            <td class="num">{_statement_money(totals["total_liabilities_and_equity"])}</td></tr>
          <tr><td>Total assets</td>
            <td class="num">{_statement_money(totals["total_assets"])}</td></tr>
          {imbalance}
          <tr class="memo"><td>Working capital</td>
            <td class="num">{_statement_money(totals["working_capital"])}</td></tr>
          <tr class="memo"><td>Current ratio</td>
            <td class="num">{html.escape(str(totals["current_ratio"]) if totals["current_ratio"] is not None else "&mdash;")}</td></tr>
        </table>
        {_statement_notes_html(body)}
        <div class="disclaimer">
          {html.escape(FORM_DISCLAIMER)} Submitted electronically {datetime.now(UTC).isoformat()}.
        </div>
      </body>
    </html>
    """
    return doc


def render_balance_sheet_pdf(*, body: dict) -> bytes:
    from weasyprint import HTML

    pdf = HTML(string=build_balance_sheet_html(body=body)).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned no PDF bytes")
    return pdf


# The full schedule needs eleven columns, which does not fit a portrait page at
# a readable size. Landscape, and the notes hang under their own obligation as a
# spanning row so a long one wraps instead of squeezing every other column.
_SCHEDULE_STYLE = """
  @page { size: Letter landscape; margin: 34px; }
  body { font-family: Inter, Arial, sans-serif; color: #111827; margin: 0; }
  h1 { font-size: 19px; margin: 0 0 2px; }
  .muted { color: #6b7280; font-size: 12px; }
  table { width: 100%; border-collapse: collapse; margin-top: 12px; }
  th, td { border: 1px solid #d1d5db; padding: 5px 7px; font-size: 9.5px; text-align: left; }
  th { background: #f3f4f6; font-size: 9px; text-transform: uppercase; letter-spacing: .03em; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  tr.note td { background: #fafafa; color: #4b5563; font-style: italic; border-top: 0; }
  .totals td { font-weight: 700; background: #f9fafb; }
  .disclaimer { margin-top: 20px; font-size: 9px; color: #6b7280; border-top: 1px solid #d1d5db; padding-top: 8px; }
"""

_SCHEDULE_COLUMNS = (
    "Lender",
    "Type",
    "Original",
    "Balance",
    "Rate",
    "Monthly",
    "Originated",
    "Matures",
    "Secured",
    "Status",
    "Collateral",
)


def _cell(value: Any) -> str:
    """A cell, or an em dash. A blank looks like a rendering fault on a printed
    schedule; a dash reads as "not stated", which is what it means."""
    text = str(value if value is not None else "").strip()
    return html.escape(text) if text else "&mdash;"


def _money_cell(value: Any) -> str:
    if value in (None, "", 0):
        return "&mdash;"
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return html.escape(str(value))


def _schedule_row_html(row: dict[str, Any]) -> str:
    rate = row.get("rate")
    cells = [
        f"<td>{_cell(row.get('lender'))}</td>",
        f"<td>{_cell(row.get('debt_type'))}</td>",
        f"<td class='num'>{_money_cell(row.get('original_amount'))}</td>",
        f"<td class='num'>{_money_cell(row.get('balance'))}</td>",
        f"<td class='num'>{(f'{float(rate):g}%' if rate not in (None, '') else '&mdash;')}</td>",
        f"<td class='num'>{_money_cell(row.get('monthly_payment'))}</td>",
        f"<td>{_cell(row.get('originated_on'))}</td>",
        f"<td>{_cell(row.get('maturity_on'))}</td>",
        f"<td>{_cell((row.get('secured') or '').title() or None)}</td>",
        f"<td>{_cell((row.get('payment_status') or '').title() or None)}</td>",
        f"<td>{_cell(row.get('collateral'))}</td>",
    ]
    out = f"<tr>{''.join(cells)}</tr>"
    note = str(row.get("notes") or "").strip()
    if note:
        out += (
            f"<tr class='note'><td colspan='{len(_SCHEDULE_COLUMNS)}'>"
            f"Note: {html.escape(note)}</td></tr>"
        )
    return out


def render_debt_schedule_pdf(
    *,
    business_name: str,
    total_balance: float,
    total_monthly: float,
    debts: list[tuple[str, float, float]] | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> bytes:
    """The schedule as a lender reads it.

    Two shapes in, because two kinds of caller produce them. `rows` carries the
    full record — type, original amount, rate, both dates, secured, status,
    collateral and any note — and is what the borrower's form and the desk's
    editor both send. `debts` is the older three-tuple, still used where a
    caller genuinely only holds lender, balance and payment; it renders the
    narrow table rather than eleven columns of em dashes.
    """
    from weasyprint import HTML

    if rows is not None:
        header = "".join(f"<th>{column}</th>" for column in _SCHEDULE_COLUMNS)
        body_rows = "".join(_schedule_row_html(row) for row in rows)
        totals = (
            "<tr class='totals'>"
            "<td colspan='3'>Total</td>"
            f"<td class='num'>${total_balance:,.0f}</td>"
            "<td></td>"
            f"<td class='num'>${total_monthly:,.0f}</td>"
            f"<td colspan='{len(_SCHEDULE_COLUMNS) - 6}'></td>"
            "</tr>"
        )
        count = len(rows)
        style = _SCHEDULE_STYLE
        table = f"<table><tr>{header}</tr>{body_rows}{totals}</table>"
        summary = (
            f"{count} obligation{'' if count == 1 else 's'} &middot; "
            f"${total_monthly:,.0f} a month &middot; ${total_balance:,.0f} outstanding"
        )
    else:
        style = _STYLE
        narrow = "".join(
            f"<tr><td>{html.escape(lender)}</td><td>${balance:,.2f}</td><td>${monthly:,.2f}</td></tr>"
            for lender, balance, monthly in (debts or [])
        )
        table = (
            "<table><tr><th>Lender</th><th>Current balance</th><th>Monthly payment</th></tr>"
            f"{narrow}"
            f"<tr class='totals'><td>Total</td><td>${total_balance:,.2f}</td>"
            f"<td>${total_monthly:,.2f}</td></tr></table>"
        )
        summary = ""

    body = f"""
    <html>
      <head><style>{style}</style></head>
      <body>
        <h1>Business Debt Schedule</h1>
        <div class="muted">{html.escape(business_name)}{f" &middot; {summary}" if summary else ""}</div>
        {table}
        <div class="disclaimer">
          {html.escape(FORM_DISCLAIMER)} Submitted electronically {datetime.now(UTC).isoformat()}.
        </div>
      </body>
    </html>
    """
    pdf = HTML(string=body).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned no PDF bytes")
    return pdf
