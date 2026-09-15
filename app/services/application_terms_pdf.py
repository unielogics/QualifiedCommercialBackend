from __future__ import annotations

import base64
import re
import unicodedata
from datetime import date, datetime
from html import escape
from pathlib import Path

from app.models.application_terms import ApplicationTermSheet

_APP = Path(__file__).resolve().parents[1]
_QC_MARK = _APP / "templates" / "_qcmark.svg.html"
_URCHOICE_LOGO = _APP / "assets" / "urchoice_logo.png"

_FUNDER_LABELS = {
    "bank": "Bank",
    "credit_union": "Credit union",
    "private_fund": "Private fund",
    "private_capital": "Private capital / family office",
    "family_office": "Family office",
    "balance_sheet": "Balance-sheet lender",
    "warehouse": "Warehouse lender",
    "table_funder": "Table funder",
    "other": "Other capital source",
}

_FREQUENCY_LABELS = {
    "daily": "Daily (business days)",
    "weekly": "Weekly",
    "biweekly": "Every two weeks",
    "monthly": "Monthly",
    "custom": "Custom",
}


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _ratio(value: float | None) -> str:
    return "Needs evidence" if value is None else f"{value:.2f}x"


def _date(value: date | datetime | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        value = value.date()
    return f"{value.strftime('%B')} {value.day}, {value.year}" if hasattr(value, "strftime") else str(value)


def _data_uri(path: Path) -> str | None:
    if not path.exists():
        return None
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _brand_html(row: ApplicationTermSheet) -> str:
    qc_svg = re.sub(r"\{#.*?#\}", "", _QC_MARK.read_text(encoding="utf-8"), flags=re.S).strip()
    sponsor = ""
    if row.co_brand_enabled and row.sponsor_name:
        logo = _data_uri(_URCHOICE_LOGO) if "urchoice" in row.sponsor_name.lower().replace(" ", "") else None
        sponsor_visual = (
            f'<img src="{logo}" alt="{escape(row.sponsor_name)}">'
            if logo
            else f'<span class="sponsor-name">{escape(row.sponsor_name)}</span>'
        )
        sponsor = f'<div class="sponsor"><span>Presented with</span>{sponsor_visual}</div>'
    return f"""
      <div class="brand-lockup">
        <div class="qc-mark">{qc_svg}</div>
        <div><div class="qc-name">Qualified Commercial</div><div class="qc-sub">Capital solutions, clearly presented</div></div>
      </div>
      {sponsor}
    """


def filename_for(row: ApplicationTermSheet, business_name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", business_name).encode("ascii", "ignore").decode("ascii")
    safe = "-".join(part for part in "".join(ch if ch.isalnum() else " " for ch in ascii_name).split() if part)
    return f"{safe or 'Client'}-Financing-Terms-v{row.version}.pdf"


def render_terms_html(
    row: ApplicationTermSheet,
    *,
    business_name: str,
    client_name: str | None = None,
) -> str:
    """Build the exact stored version as print-ready, self-contained HTML."""
    payment = float(row.periodic_payment)
    amount = float(row.amount)
    total = payment * row.payment_count
    conditions = list(row.conditions or [])
    conditions_html = "".join(f"<li>{escape(str(item))}</li>" for item in conditions)
    if not conditions_html:
        conditions_html = "<li>Final approval, documentation, and closing remain subject to the funding source’s review.</li>"
    custom = row.custom_repayment_label if row.repayment_frequency == "custom" else None
    cadence = custom or _FREQUENCY_LABELS.get(row.repayment_frequency, row.repayment_frequency.replace("_", " ").title())
    funder = _FUNDER_LABELS.get(row.funder_type, row.funder_type.replace("_", " ").title())
    if row.funder_name:
        funder = f"{funder} · {row.funder_name}"
    issue_date = row.issued_at or row.created_at
    status_label = "Issued terms" if row.status == "issued" else "Draft terms"
    client_line = escape(client_name) if client_name else escape(business_name)
    note_html = f'<div class="client-note">{escape(row.client_note)}</div>' if row.client_note else ""
    dscr_treatment = (
        "Refinance / payoff projection; retained obligations are included."
        if row.debt_service_treatment == "refinance"
        else "Additional-debt projection; the proposed payment is added to current debt service."
    )
    html = f"""
<!doctype html>
<html><head><meta charset="utf-8"><style>
  @page {{ size: Letter; margin: 0; @bottom-right {{ content: "Page " counter(page) " of " counter(pages); color: #718096; font: 8px Arial; margin: 0 32px 18px 0; }} }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; color: #13233d; font-family: Arial, Helvetica, sans-serif; font-size: 10px; line-height: 1.45; background: white; }}
  .hero {{ background: #0b1d3a; color: white; padding: 25px 34px 23px; min-height: 158px; }}
  .brand-row {{ display: flex; align-items: center; justify-content: space-between; gap: 28px; }}
  .brand-lockup {{ display: flex; align-items: center; gap: 11px; }}
  .qc-mark {{ width: 37px; height: 37px; flex: none; }}
  .qc-name {{ font-size: 14px; font-weight: 800; letter-spacing: .02em; }}
  .qc-sub {{ color: #a7c7d0; font-size: 7.5px; letter-spacing: .08em; text-transform: uppercase; margin-top: 2px; }}
  .sponsor {{ border-left: 1px solid rgba(255,255,255,.3); padding-left: 18px; text-align: right; min-width: 150px; }}
  .sponsor span {{ display: block; color: #a7c7d0; font-size: 7px; letter-spacing: .12em; text-transform: uppercase; margin-bottom: 5px; }}
  .sponsor img {{ width: 134px; max-height: 42px; object-fit: contain; object-position: right center; }}
  .sponsor .sponsor-name {{ color: white; font-size: 15px; font-weight: 800; letter-spacing: .01em; text-transform: none; }}
  .eyebrow {{ margin-top: 21px; color: #45d7cb; font-size: 8px; font-weight: 800; letter-spacing: .15em; text-transform: uppercase; }}
  h1 {{ margin: 4px 0 3px; font-family: Georgia, 'Times New Roman', serif; font-size: 27px; font-weight: 600; letter-spacing: -.02em; }}
  .hero-meta {{ color: #c6d5e6; font-size: 9px; }}
  .content {{ padding: 21px 34px 28px; }}
  .validity {{ display: grid; grid-template-columns: 1fr 1fr 1fr; border: 1px solid #dce3ec; border-radius: 9px; overflow: hidden; margin-bottom: 15px; }}
  .validity > div {{ padding: 9px 12px; border-right: 1px solid #dce3ec; }}
  .validity > div:last-child {{ border: 0; }}
  .label {{ color: #718096; font-size: 7.2px; font-weight: 800; letter-spacing: .09em; text-transform: uppercase; }}
  .validity strong {{ display: block; color: #0b1d3a; font-size: 10.5px; margin-top: 3px; }}
  h2 {{ color: #0f7a73; font-size: 8px; letter-spacing: .13em; text-transform: uppercase; margin: 0 0 8px; }}
  .terms-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 15px; }}
  .term {{ border: 1px solid #dce3ec; border-radius: 9px; padding: 10px 11px; min-height: 59px; background: #fbfcfe; }}
  .term strong {{ display: block; font-size: 14px; margin-top: 4px; color: #0b1d3a; }}
  .term small {{ display: block; margin-top: 2px; color: #718096; }}
  .payment {{ border-radius: 11px; background: #edf8f7; border: 1px solid #bfe6e2; padding: 13px 15px; display: grid; grid-template-columns: 1.2fr 1fr 1fr; gap: 14px; margin-bottom: 15px; }}
  .payment .primary strong {{ color: #0f7a73; font-size: 22px; display: block; margin-top: 2px; }}
  .payment strong {{ color: #0b1d3a; font-size: 13px; display: block; margin-top: 4px; }}
  .dscr {{ display: grid; grid-template-columns: 1fr 36px 1fr; align-items: stretch; gap: 8px; margin-bottom: 15px; }}
  .dscr-card {{ border: 1px solid #dce3ec; border-radius: 10px; padding: 12px 14px; }}
  .dscr-card.after {{ border-color: #99d9d3; background: #f4fbfa; }}
  .dscr-card strong {{ display: block; font-size: 22px; margin-top: 5px; color: #0b1d3a; }}
  .dscr-card p {{ color: #718096; font-size: 8.5px; margin: 4px 0 0; }}
  .arrow {{ align-self: center; justify-self: center; color: #0f7a73; font-size: 22px; font-weight: 800; }}
  .method {{ color: #52637a; font-size: 8px; margin: -8px 0 15px; }}
  .two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 13px; }}
  .card {{ border-top: 2px solid #0b1d3a; padding-top: 9px; page-break-inside: avoid; }}
  ul {{ margin: 5px 0 0; padding-left: 16px; }} li {{ margin: 0 0 4px; }}
  .client-note {{ border-left: 3px solid #0f7a73; background: #f6f9fc; padding: 9px 11px; margin: 12px 0; color: #31445e; }}
  .fine {{ border-top: 1px solid #dce3ec; margin-top: 16px; padding-top: 10px; color: #718096; font-size: 7.6px; line-height: 1.55; }}
  .fine b {{ color: #41546e; }}
</style></head><body>
  <header class="hero">
    <div class="brand-row">{_brand_html(row)}</div>
    <div class="eyebrow">{status_label} · Version {row.version}</div>
    <h1>Financing Terms</h1>
    <div class="hero-meta">Prepared for {escape(business_name)} · {client_line}</div>
  </header>
  <main class="content">
    <div class="validity">
      <div><span class="label">Prepared</span><strong>{_date(issue_date)}</strong></div>
      <div><span class="label">Valid through</span><strong>{_date(row.expires_on)}</strong></div>
      <div><span class="label">Estimated closing</span><strong>{row.closing_estimate_days} business day{'s' if row.closing_estimate_days != 1 else ''} after approval</strong></div>
    </div>
    <h2>Proposed financing</h2>
    <div class="terms-grid">
      <div class="term"><span class="label">Amount</span><strong>{_money(amount)}</strong></div>
      <div class="term"><span class="label">APR</span><strong>{float(row.apr_pct):.2f}%</strong></div>
      <div class="term"><span class="label">Time</span><strong>{row.term_months} months</strong></div>
      <div class="term"><span class="label">Loan type</span><strong style="font-size:11px">{escape(row.program_name)}</strong></div>
      <div class="term"><span class="label">Funder type</span><strong style="font-size:11px">{escape(funder)}</strong></div>
      <div class="term"><span class="label">Repayment</span><strong style="font-size:11px">{escape(cadence)}</strong><small>{row.payment_count} estimated payments</small></div>
    </div>
    <div class="payment">
      <div class="primary"><span class="label">Estimated {escape(cadence.lower())} payment</span><strong>{_money(payment)}</strong></div>
      <div><span class="label">Annual debt service</span><strong>{_money(float(row.annual_new_debt_service))}</strong></div>
      <div><span class="label">Estimated total repayment</span><strong>{_money(total)}</strong></div>
    </div>
    <h2>Debt-service coverage</h2>
    <div class="dscr">
      <div class="dscr-card"><span class="label">Before acceptance</span><strong>{_ratio(float(row.dscr_before) if row.dscr_before is not None else None)}</strong><p>Current file evidence and existing obligations.</p></div>
      <div class="arrow">→</div>
      <div class="dscr-card after"><span class="label">After acceptance</span><strong>{_ratio(float(row.dscr_after) if row.dscr_after is not None else None)}</strong><p>Projected with the proposed repayment schedule.</p></div>
    </div>
    <p class="method">{escape(row.dscr_explanation)} {escape(dscr_treatment)} Source: {escape(row.dscr_source)}.</p>
    {note_html}
    <div class="two">
      <section class="card"><h2>Conditions</h2><ul>{conditions_html}</ul></section>
      <section class="card"><h2>What happens next</h2><ul><li>Confirm the structure and any payoff assumptions.</li><li>Complete final underwriting and verification.</li><li>Coordinate closing with the selected funding source.</li></ul></section>
    </div>
    <div class="fine"><b>Important:</b> This is an indicative term summary, not a commitment to lend or a final credit approval. Terms may change following verification of information, underwriting, documentation, legal review, and funding-source approval. APR, payment, total repayment, and DSCR are estimates based on the stored inputs shown above. Fees, third-party costs, reserves, taxes, insurance, default charges, legal expenses, and closing costs are separate unless expressly included. Qualified Commercial is not the lender unless identified in the final loan documents.</div>
  </main>
</body></html>
"""
    return html


def render_terms_pdf(
    row: ApplicationTermSheet,
    *,
    business_name: str,
    client_name: str | None = None,
) -> bytes:
    """Render the exact stored version into a client-safe branded PDF."""
    from weasyprint import HTML

    pdf = HTML(
        string=render_terms_html(row, business_name=business_name, client_name=client_name),
        base_url=str(_APP),
    ).write_pdf()
    if not pdf:
        raise RuntimeError("WeasyPrint returned no PDF bytes")
    return pdf
