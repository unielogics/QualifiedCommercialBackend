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
from datetime import UTC, date, datetime
from typing import Any

from app.services import debt_types
from app.services.loan_progress import (
    PAID_DOWN_HINT,
    finite_amount,
    format_paid_down_pct,
    loan_progress_fields,
)

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


# The 413 is a document a lender reads, not a spreadsheet they audit. The old
# style boxed every cell in a 1px grid, which on a form where most lines are
# zero produced a page of borders with figures hiding inside them. Rules run
# horizontally only, hairline, and the eye follows the numbers instead.
_PFS_STYLE = """
  @page {
    size: Letter;
    margin: 46px 52px 54px;
    @bottom-right {
      content: counter(page) " of " counter(pages);
      font-family: Inter, Arial, sans-serif;
      font-size: 8.5px;
      color: #9aa4b2;
    }
  }
  body { font-family: Inter, Arial, sans-serif; color: #14202e; margin: 0; font-size: 11px; line-height: 1.45; }

  .masthead { border-bottom: 2px solid #14202e; padding-bottom: 10px; margin-bottom: 4px; }
  .firm { font-size: 8.5px; letter-spacing: .16em; text-transform: uppercase; color: #6b7787; }
  h1 { font-size: 21px; font-weight: 650; margin: 3px 0 0; letter-spacing: -.01em; }

  /* Who and when, as labelled facts rather than a run-on line. A blank value
     is dropped by the builder, so "as of not stated" never prints. */
  .meta { display: flex; gap: 28px; margin: 12px 0 20px; }
  .meta div { }
  .meta dt { font-size: 8.5px; letter-spacing: .1em; text-transform: uppercase; color: #8b95a3; margin: 0; }
  .meta dd { font-size: 11.5px; margin: 2px 0 0; font-weight: 550; }

  h2 {
    font-size: 9px; font-weight: 650; letter-spacing: .12em; text-transform: uppercase;
    color: #46536380; color: #465363; margin: 20px 0 0; padding-bottom: 5px;
    border-bottom: 1px solid #14202e;
  }

  table { width: 100%; border-collapse: collapse; margin: 0; }
  /* No verticals, no outer box: one hairline under each line, and nothing else. */
  td, th { border: 0; border-bottom: 1px solid #edf0f4; padding: 6px 2px; font-size: 11px; text-align: left; }
  th { font-size: 8.5px; letter-spacing: .08em; text-transform: uppercase; color: #8b95a3; font-weight: 600; }
  .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  /* A form is mostly zeros until it is filled. Dimming them lets the figures
     that were actually entered carry the page. */
  .zero { color: #b3bcc8; }
  tr.totals td { border-bottom: 0; border-top: 1.5px solid #14202e; font-weight: 650; padding-top: 7px; }

  .cols { display: flex; gap: 30px; }
  .cols > div { flex: 1; }

  /* The one figure a reader is looking for. Tinted band, still no border. */
  .networth {
    display: flex; justify-content: space-between; align-items: baseline;
    background: #f2f5fa; padding: 13px 16px; margin: 22px 0 4px; border-radius: 3px;
  }
  .networth .k { font-size: 9px; letter-spacing: .12em; text-transform: uppercase; color: #465363; font-weight: 650; }
  .networth .v { font-size: 19px; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: -.015em; }
  .networth .v.negative { color: #b42318; }

  .schedules { margin-top: 4px; }
  .disclaimer {
    margin-top: 28px; font-size: 8.5px; line-height: 1.55; color: #8b95a3;
    border-top: 1px solid #edf0f4; padding-top: 10px;
  }
"""


def build_pfs_413_html(*, body: dict, statement_date: str, financial_totals: dict | None = None) -> str:
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
    if financial_totals is not None:
        totals.update({key: value for key, value in financial_totals.items() if key in totals})
    applicant = body.get("applicant") or {}
    assets = body.get("assets") or {}
    liabilities = body.get("liabilities") or {}

    def _money(value) -> str:
        if value is None and financial_totals is not None:
            return "&mdash;"
        return f"${float(pfs_schema._amount(value)):,.2f}"

    def _amount_cell(value) -> str:
        """A figure, dimmed when it is zero.

        Most lines on a 413 are zero until someone fills them in. Printing all
        of them at the same weight buries the four that were answered.
        """
        if value in (None, "") and financial_totals is not None:
            return "<td class='num zero'>&mdash;</td>"
        number = float(pfs_schema._amount(value))
        klass = "num zero" if number == 0 else "num"
        return f"<td class='{klass}'>{_money(number)}</td>"

    def _summary(rows, values) -> str:
        return "".join(
            f"<tr><td>{html.escape(row.label)}</td>{_amount_cell(values.get(row.key))}</tr>"
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
                f"<td>{html.escape(str(row.get(field_key, row.get(column, '')) or ''))}</td>" for field_key, column in spec.fields
            )
            + "</tr>"
            for row in rows
            if isinstance(row, dict)
        )
        return f"<h2>{html.escape(spec.label)}</h2><table><tr>{head}</tr>{cells}</table>"

    schedules = "".join(_schedule(spec.key) for spec in pfs_schema.SCHEDULES)

    # Labelled facts, and only the ones we hold. The old header ran these
    # together, so an unfilled date printed as "— as of not stated".
    meta_pairs = [
        ("Applicant", applicant.get("name")),
        ("Business", applicant.get("business_name")),
        ("As of", None if statement_date in (None, "", "not stated") else statement_date),
    ]
    meta = "".join(
        f"<div><dt>{label}</dt><dd>{html.escape(str(value))}</dd></div>"
        for label, value in meta_pairs
        if str(value or "").strip()
    )

    net_worth = float(totals["net_worth"] or 0)
    net_class = "v negative" if net_worth < 0 else "v"

    doc = f"""
    <html>
      <head><style>{_PFS_STYLE}</style></head>
      <body>
        <div class="masthead">
          <div class="firm">Qualified Commercial</div>
          <h1>Personal Financial Statement</h1>
        </div>
        <dl class="meta">{meta}</dl>

        <div class="cols">
          <div>
            <h2>Assets</h2>
            <table>
              {_summary(pfs_schema.ASSET_ROWS, assets)}
              <tr class="totals"><td>Total assets</td>
                <td class="num">{_money(totals["total_assets"])}</td></tr>
            </table>
          </div>
          <div>
            <h2>Liabilities</h2>
            <table>
              {_summary(pfs_schema.LIABILITY_ROWS, liabilities)}
              <tr class="totals"><td>Total liabilities</td>
                <td class="num">{_money(totals["total_liabilities"])}</td></tr>
            </table>
          </div>
        </div>

        <div class="networth">
          <span class="k">Net worth</span>
          <span class="{net_class}">{_money(totals['net_worth'])}</span>
        </div>

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

        <div class="schedules">{schedules}</div>

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


def render_pfs_413_pdf(*, body: dict, statement_date: str, financial_totals: dict | None = None) -> bytes:
    """The Form 413 statement as PDF bytes.

    Kept alongside `render_pfs_pdf` rather than replacing it, so flows still on
    the old eight-row form keep working while they are migrated.
    """
    from weasyprint import HTML

    pdf = HTML(string=build_pfs_413_html(body=body, statement_date=statement_date, financial_totals=financial_totals)).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned no PDF bytes")
    return pdf


def _statement_money(value: Any) -> str:
    if value is None:
        return "&mdash;"
    amount = float(value or 0)
    return f"(${abs(amount):,.2f})" if amount < 0 else f"${amount:,.2f}"


def _statement_section_html(section: Any, values: dict[str, Any], subtotal: Any, *, preserve_unknowns: bool = False) -> str:
    """One schema section as a table: its lines, then its subtotal. A contra
    line is labelled as the schema labels it ("Less: …") and shown as typed."""
    from app.services.pfs_schema import _amount

    lines = "".join(
        f"<tr><td>{html.escape(row.label)}"
        f"{' <span class=chip>add-back</span>' if row.addback else ''}</td>"
        f"<td class='num'>{_statement_money(None if preserve_unknowns and values.get(row.key) in (None, '') else _amount(values.get(row.key)))}</td></tr>"
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


def build_p_and_l_html(*, body: dict, financial_totals: dict | None = None) -> str:
    """The profit and loss statement as a lender reads it: revenue, operating
    expenses, the line below, then the EBITDA memo built from the add-back
    flags. Split from the render so the layout can be tested without
    WeasyPrint's native libraries, as `build_pfs_413_html` is."""
    from app.services import business_statement_schema as bss

    schema = bss.SCHEMA_FOR["p_and_l"]
    totals = schema.totals(body)
    if financial_totals is not None:
        totals.update({key: value for key, value in financial_totals.items() if key in totals})
    header = body.get("header") or {}
    sections = body.get("sections") or {}
    by_key = {section.key: section for section in schema.sections}
    period = bss.period_label("p_and_l", body) or "period not stated"

    revenue = _statement_section_html(
        by_key["revenue"], sections.get("revenue") or {}, totals["gross_profit"], preserve_unknowns=financial_totals is not None
    )
    opex = _statement_section_html(
        by_key["operating_expenses"],
        sections.get("operating_expenses") or {},
        totals["total_operating_expenses"],
        preserve_unknowns=financial_totals is not None,
    )
    other_description = str((sections.get("operating_expenses") or {}).get("other_description") or "").strip()
    below = _statement_section_html(
        by_key["below_the_line"], sections.get("below_the_line") or {}, totals["net_income"], preserve_unknowns=financial_totals is not None
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


def render_p_and_l_pdf(*, body: dict, financial_totals: dict | None = None) -> bytes:
    from weasyprint import HTML

    pdf = HTML(string=build_p_and_l_html(body=body, financial_totals=financial_totals)).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned no PDF bytes")
    return pdf


def build_balance_sheet_html(*, body: dict, financial_totals: dict | None = None) -> str:
    """The balance sheet with the identity line and any unreconciled
    difference printed, so a reader sees what the borrower saw."""
    from app.services import business_statement_schema as bss

    schema = bss.SCHEMA_FOR["balance_sheet"]
    totals = schema.totals(body)
    if financial_totals is not None:
        totals.update({key: value for key, value in financial_totals.items() if key in totals})
        totals["equity_implied"] = False
    header = body.get("header") or {}
    sections = body.get("sections") or {}
    by_key = {section.key: section for section in schema.sections}
    as_of = bss.period_label("balance_sheet", body) or "date not stated"

    def part(key: str) -> str:
        return _statement_section_html(
            by_key[key], sections.get(key) or {}, totals[by_key[key].subtotal_key], preserve_unknowns=financial_totals is not None
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


def render_balance_sheet_pdf(*, body: dict, financial_totals: dict | None = None) -> bytes:
    from weasyprint import HTML

    pdf = HTML(string=build_balance_sheet_html(body=body, financial_totals=financial_totals)).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned no PDF bytes")
    return pdf


def build_real_estate_schedule_html(*, body: dict, financial_totals: dict | None = None) -> str:
    """Property and lien identities stay separate; unknown values stay visible."""
    from app.services import real_estate_schema as res

    totals = res.totals(body)
    if financial_totals is not None:
        totals.update({key: financial_totals[key] for key in totals if key in financial_totals})

    def value(spec, raw):
        if raw in (None, "", "unknown"):
            return "<span class='unknown'>Not stated</span>"
        if spec.get("input") == "money":
            return html.escape(f"${float(raw):,.2f}")
        if spec.get("input") in {"rate", "percent"}:
            return html.escape(f"{float(raw):g}%")
        return html.escape(str(raw).replace("_", " ") if spec.get("input") == "select" else str(raw))

    def fields(specs, row):
        pairs = list(specs)
        rendered = []
        for index in range(0, len(pairs), 2):
            cells = "".join(f"<th>{html.escape(spec['label'])}</th><td>{value(spec, row.get(spec['key']))}</td>" for spec in pairs[index:index + 2])
            if len(pairs[index:index + 2]) == 1:
                cells += "<th></th><td></td>"
            rendered.append("<tr>" + cells + "</tr>")
        return "<table class='fields'><tbody>" + "".join(rendered) + "</tbody></table>"

    properties, mortgages = res.properties(body), res.mortgages(body)
    sections = []
    for ordinal, property_row in enumerate(properties, 1):
        own = [row for row in mortgages if row.get("property_row_id") == property_row["id"]]
        heading = html.escape(str(property_row.get("address") or "Address not stated"))
        content = f"<section class='property'><h2>Property {ordinal}: {heading}</h2>" + fields(res.PROPERTY_FIELDS, property_row)
        content += "<h3>Mortgages / liens</h3>"
        if not own:
            content += "<p>Reported free and clear.</p>" if property_row.get("mortgage_status") == "free_clear" else "<p class='unknown'>Mortgage details not stated.</p>"
        for number, mortgage in enumerate(own, 1):
            content += f"<h4>Lien {number}: {html.escape(str(mortgage.get('lender') or 'Lender not stated'))}</h4>"
            content += fields([spec for spec in res.MORTGAGE_FIELDS if spec["key"] != "property_row_id"], mortgage)
        sections.append(content + "</section>")
    unlinked = [row for row in mortgages if not row.get("property_row_id")]
    if unlinked:
        sections.append("<section class='property'><h2>Mortgages awaiting a property association</h2>" + "".join(fields([spec for spec in res.MORTGAGE_FIELDS if spec["key"] != "property_row_id"], row) for row in unlinked) + "</section>")
    summary = "".join(f"<tr><th>{html.escape(label)}</th><td>{value({'input': 'number' if key.endswith('_count') else 'money'}, totals.get(key))}</td></tr>" for key, label in res.TOTAL_LABELS.items())
    declaration = "<p class='declaration'>The owner declares that no real estate is owned.</p>" if body.get("no_real_estate") is True else "<p class='unknown'>Property details have not been provided.</p>" if not properties else ""
    notes = f"<h3>Notes</h3><p class='notes'>{html.escape(str(body['notes']))}</p>" if body.get("notes") else ""
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
      @page {{ size: Letter; margin: 32pt; @bottom-center {{ content: 'Page ' counter(page) ' of ' counter(pages); font-size: 8pt; color: #64748b; }} }}
      body {{ font-family: Arial, sans-serif; font-size: 10pt; color: #172033; }}
      h1 {{ font-size: 20pt; margin: 0 0 8pt; }} h2 {{ font-size: 14pt; margin: 0 0 10pt; overflow-wrap: anywhere; }}
      h3 {{ font-size: 11pt; margin: 16pt 0 6pt; }} h4 {{ font-size: 10pt; margin: 10pt 0 4pt; }}
      table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }} th, td {{ padding: 5pt; border-bottom: .5pt solid #dce2e9; text-align: left; vertical-align: top; overflow-wrap: anywhere; }}
      .fields th {{ width: 23%; background: #f3f5f8; font-size: 8pt; font-weight: normal; }} .fields td {{ width: 27%; font-size: 9pt; }}
      tr {{ break-inside: avoid; }} .property {{ break-before: page; }} .unknown, .context {{ color: #64748b; }}
      .context {{ font-size: 9pt; line-height: 1.45; }} .notes {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
    </style></head><body><h1>Real Estate Schedule</h1>
    <p><strong>Owner / entity:</strong> {html.escape(str(body.get('owner_name') or 'Not stated'))}<br><strong>As of:</strong> {html.escape(str(body.get('as_of') or 'Not stated'))}</p>
    {declaration}<table>{summary}</table>
    <p class='context'>Values and equity describe the whole properties; ownership percentages are shown separately. Cash flow uses principal and interest, excluding escrow only when its amount is established. Missing values remain unknown.</p>
    {notes}{''.join(sections)}<p class='context'>{html.escape(FORM_DISCLAIMER)}</p></body></html>"""


def render_real_estate_schedule_pdf(*, body: dict, financial_totals: dict | None = None) -> bytes:
    from weasyprint import HTML

    pdf = HTML(string=build_real_estate_schedule_html(body=body, financial_totals=financial_totals)).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned no PDF bytes")
    return pdf


# The full schedule includes native contract terms and does not fit a portrait page at
# a readable size. Landscape, and the notes hang under their own obligation as a
# spanning row so a long one wraps instead of squeezing every other column.
_SCHEDULE_STYLE = """
  @page { size: Letter landscape; margin: 34px; }
  body { font-family: Inter, Arial, sans-serif; color: #111827; margin: 0; }
  h1 { font-size: 19px; margin: 0 0 2px; }
  .muted { color: #6b7280; font-size: 12px; }
  table { width: 100%; border-collapse: collapse; margin-top: 12px; }
  th, td { border: 1px solid #d1d5db; padding: 3px 5px; font-size: 9.5px; text-align: left; }
  th { background: #f3f4f6; font-size: 9px; text-transform: uppercase; letter-spacing: .03em; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  tr.note td { background: #fafafa; color: #4b5563; font-style: italic; border-top: 0; }
  .totals td { font-weight: 700; background: #f9fafb; }
  .disclaimer { margin-top: 8px; font-size: 9px; color: #6b7280; border-top: 1px solid #d1d5db; padding-top: 6px; break-inside: avoid; page-break-inside: avoid; }
"""

_SCHEDULE_COLUMNS = (
    "#",
    "Lender",
    "Type",
    "Original",
    "Balance",
    "% paid down",
    "Rate",
    "Monthly",
    "Origination date",
    "Matures",
    "Secured",
    "Status",
    "Collateral",
    "Factor (×)",
    "Payment amount",
    "Frequency",
)


def _cell(value: Any) -> str:
    """A cell, or an em dash. A blank looks like a rendering fault on a printed
    schedule; a dash reads as "not stated", which is what it means."""
    text = str(value if value is not None else "").strip()
    return html.escape(text) if text else "&mdash;"


def _money_cell(value: Any) -> str:
    if value in (None, ""):
        return "&mdash;"
    amount = finite_amount(value)
    return f"${amount:,.2f}" if amount is not None else html.escape(str(value))


def _date_cell(value: Any) -> str:
    """A date the way a US lender reads one, mm/dd/yyyy. The form stores ISO;
    anything else is shown as given rather than dropped."""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%m/%d/%Y")
    text = str(value if value is not None else "").strip()
    if not text:
        return "&mdash;"
    try:
        return date.fromisoformat(text[:10]).strftime("%m/%d/%Y")
    except ValueError:
        return html.escape(text)


def _row_note_html(row: dict[str, Any]) -> str:
    """The spanning line under an obligation: the advance question's answer
    when the row is an advance, then any note. Nothing when there is neither."""
    parts: list[str] = []
    if debt_types.is_advance_type(row.get("debt_type")):
        modified = row.get("mca_modified")
        if modified is None:
            answer = "not stated"
        elif modified:
            answer = "Yes"
            detail = str(row.get("mca_modification_note") or "").strip()
            if detail:
                answer += f" &mdash; {html.escape(detail)}"
        else:
            answer = "No"
        parts.append(f"Modified by the funder for lower sales or revenue: {answer}")
    note = str(row.get("notes") or "").strip()
    if note:
        parts.append(f"Note: {html.escape(note)}")
    if not parts:
        return ""
    return f"<tr class='note'><td colspan='{len(_SCHEDULE_COLUMNS)}'>{' &middot; '.join(parts)}</td></tr>"


def _schedule_row_html(row: dict[str, Any], number: int | None = None) -> str:
    from app.services.debt_terms import normalize_terms

    row = normalize_terms(row)
    progress = loan_progress_fields(row)
    rate = row.get("rate")
    factor = row.get("factor_rate")
    cells = [
        # "Debt #1" on the owner's list, and the number the completeness
        # message names an obligation by.
        f"<td class='num'>{number if number is not None else '&mdash;'}</td>",
        f"<td>{_cell(row.get('lender'))}</td>",
        f"<td>{_cell(debt_types.debt_type_label(row.get('debt_type')))}</td>",
        f"<td class='num'>{_money_cell(row.get('original_amount'))}</td>",
        f"<td class='num'>{_money_cell(row.get('balance'))}</td>",
        f"<td class='num'>{_cell(format_paid_down_pct(progress['paid_down_pct']))}</td>",
        f"<td class='num'>{(f'{float(rate):g}%' if rate not in (None, '') else '&mdash;')}</td>",
        f"<td class='num'>{_money_cell(row.get('monthly_payment'))}{' <small>calculated</small>' if row.get('monthly_payment_source') == 'native_terms' else ''}</td>",
        f"<td>{_date_cell(progress['originated_on'])}</td>",
        f"<td>{_date_cell(row.get('maturity_on'))}</td>",
        f"<td>{_cell((row.get('secured') or '').title() or None)}</td>",
        f"<td>{_cell((row.get('payment_status') or '').title() or None)}</td>",
        f"<td>{_cell(row.get('collateral'))}</td>",
        f"<td class='num'>{(f'{float(factor):.3f}×' if factor is not None else '&mdash;')}</td>",
        f"<td class='num'>{_money_cell(row.get('payment_amount'))}</td>",
        f"<td>{_cell((row.get('payment_frequency') or '').title() or None)}</td>",
    ]
    return f"<tr>{''.join(cells)}</tr>" + _row_note_html(row)


def build_debt_schedule_html(
    *,
    business_name: str,
    total_balance: float | None,
    total_monthly: float | None,
    debts: list[tuple[str, float, float]] | None = None,
    rows: list[dict[str, Any]] | None = None,
    no_debt: bool = False,
) -> str:
    """The schedule as a lender reads it, as HTML; `render_debt_schedule_pdf`
    prints it. Split out so the layout is testable where WeasyPrint is not
    installed (the host venv), the way `build_pfs_413_html` is.

    Two shapes in, because two kinds of caller produce them. `rows` carries the
    full record — type, original amount, rate, both dates, secured, status,
    collateral, the advance question and any note — and is what the borrower's
    form, the on-screen modal and the desk's editor all send. `debts` is the
    older three-tuple, still used by the advance-terms form, which genuinely
    only holds funder, payback and payment; it renders the narrow table rather
    than eleven columns of em dashes.
    """
    if rows is not None:
        header = "".join(f"<th>{column}</th>" for column in _SCHEDULE_COLUMNS)
        body_rows = "".join(_schedule_row_html(row, number) for number, row in enumerate(rows, start=1))
        totals = (
            "<tr class='totals'>"
            "<td colspan='4'>Total</td>"
            f"<td class='num'>{_statement_money(total_balance)}</td>"
            "<td colspan='2'></td>"
            f"<td class='num'>{_statement_money(total_monthly)}</td>"
            f"<td colspan='{len(_SCHEDULE_COLUMNS) - 8}'></td>"
            "</tr>"
        )
        count = len(rows)
        style = _SCHEDULE_STYLE
        table = f"<table><tr>{header}</tr>{body_rows}{totals}</table>"
        summary = (
            f"{count} obligation{'' if count == 1 else 's'} &middot; "
            f"{_statement_money(total_monthly)} a month &middot; {_statement_money(total_balance)} outstanding"
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
            f"<tr class='totals'><td>Total</td><td>{_statement_money(total_balance)}</td>"
            f"<td>{_statement_money(total_monthly)}</td></tr></table>"
        )
        summary = ""

    body = f"""
    <html>
      <head><style>{style}</style></head>
      <body>
        <h1>Business Debt Schedule</h1>
        <div class="muted">{html.escape(business_name)}{f" &middot; {summary}" if summary else ""}</div>
        {'<p>The business declares that it has no outstanding business debt.</p>' if no_debt else ''}
        {table}
        <div class="disclaimer">
          {f'<span>{html.escape(PAID_DOWN_HINT)}</span><br>' if rows is not None else ''}
          {html.escape(FORM_DISCLAIMER)} Submitted electronically {datetime.now(UTC).isoformat()}.
        </div>
      </body>
    </html>
    """
    return body


def render_debt_schedule_pdf(
    *,
    business_name: str,
    total_balance: float | None,
    total_monthly: float | None,
    debts: list[tuple[str, float, float]] | None = None,
    rows: list[dict[str, Any]] | None = None,
    no_debt: bool = False,
) -> bytes:
    """`build_debt_schedule_html`, printed."""
    from weasyprint import HTML

    pdf = HTML(
        string=build_debt_schedule_html(
            business_name=business_name,
            total_balance=total_balance,
            total_monthly=total_monthly,
            debts=debts,
            rows=rows,
            no_debt=no_debt,
        )
    ).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned no PDF bytes")
    return pdf
