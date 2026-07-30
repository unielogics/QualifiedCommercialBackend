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
from datetime import datetime, timezone

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
          {html.escape(FORM_DISCLAIMER)} Submitted electronically {datetime.now(timezone.utc).isoformat()}.
          No Social Security Number was collected on this form.
        </div>
      </body>
    </html>
    """
    pdf = HTML(string=body).write_pdf()
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
          {html.escape(FORM_DISCLAIMER)} Submitted electronically {datetime.now(timezone.utc).isoformat()}.
        </div>
      </body>
    </html>
    """
    pdf = HTML(string=body).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned no PDF bytes")
    return pdf
