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


def render_debt_schedule_pdf(
    *,
    business_name: str,
    debts: list[tuple[str, float, float]],
    total_balance: float,
    total_monthly: float,
) -> bytes:
    from weasyprint import HTML

    rows = "".join(
        f"<tr><td>{html.escape(lender)}</td><td>${balance:,.2f}</td><td>${monthly:,.2f}</td></tr>"
        for lender, balance, monthly in debts
    )
    body = f"""
    <html>
      <head><style>{_STYLE}</style></head>
      <body>
        <h1>Business Debt Schedule</h1>
        <div class="muted">{html.escape(business_name)}</div>
        <table>
          <tr><th>Lender</th><th>Current balance</th><th>Monthly payment</th></tr>
          {rows}
          <tr class="totals"><td>Total</td><td>${total_balance:,.2f}</td><td>${total_monthly:,.2f}</td></tr>
        </table>
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
