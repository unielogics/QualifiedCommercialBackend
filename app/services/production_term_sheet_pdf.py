from __future__ import annotations

import base64
import re
import unicodedata
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Any

from app.models.production_package import ProductionTermSheet
from app.services.production_arrangement import USE_OF_FUNDS_KEYS

_APP = Path(__file__).resolve().parents[1]
_QC_MARK = _APP / "templates" / "_qcmark.svg.html"
_URCHOICE_LOGO = _APP / "assets" / "urchoice_logo.png"


def _money(value: float | None) -> str:
    return "-" if value is None else f"${value:,.2f}"


def _date(value: date | datetime | None) -> str:
    if value is None:
        return "To be confirmed"
    if isinstance(value, datetime):
        value = value.date()
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def _data_uri(path: Path) -> str | None:
    if not path.exists():
        return None
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _safe_name(value: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return "-".join(part for part in "".join(ch if ch.isalnum() else " " for ch in ascii_name).split() if part)


def filename_for(sheet: ProductionTermSheet, business_name: str) -> str:
    return f"{_safe_name(business_name) or 'Client'}-Loan-Terms-v{sheet.version}.pdf"


def _brand_html(sponsor_name: str | None) -> str:
    qc_svg = re.sub(r"\{#.*?#\}", "", _QC_MARK.read_text(encoding="utf-8"), flags=re.S).strip()
    sponsor = ""
    if sponsor_name:
        normalized = sponsor_name.lower().replace(" ", "")
        logo = _data_uri(_URCHOICE_LOGO) if "urchoice" in normalized else None
        visual = (
            f'<img src="{logo}" alt="{escape(sponsor_name)}">'
            if logo
            else f'<strong>{escape(sponsor_name)}</strong>'
        )
        sponsor = f'<div class="sponsor"><span>Presented with</span>{visual}</div>'
    return f"""
      <div class="brand-lockup">
        <div class="qc-mark">{qc_svg}</div>
        <div><div class="qc-name">Qualified Commercial</div><div class="qc-sub">Capital solutions, clearly presented</div></div>
      </div>
      {sponsor}
    """


def _use_of_funds_rows(raw: dict[str, Any] | None) -> str:
    values = raw if isinstance(raw, dict) else {}
    rows: list[str] = []
    for key, default_label in USE_OF_FUNDS_KEYS:
        try:
            amount = float(values.get(key) or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            continue
        label = str(values.get("other_label") or default_label) if key == "other" else default_label
        rows.append(f"<tr><td>{escape(label)}</td><td>{_money(amount)}</td></tr>")
    if not rows:
        return '<div class="empty">Use-of-funds allocations will be confirmed during final underwriting.</div>'
    return f'<table><tbody>{"".join(rows)}</tbody></table>'


def _condition_items(conditions: str | None) -> str:
    text = (conditions or "").strip()
    if not text:
        return "<li>Final verification, underwriting approval, and documentation satisfactory to the funding source.</li>"
    items = [part.strip(" -\t") for part in text.replace("\r", "\n").split("\n") if part.strip(" -\t")]
    if not items:
        items = [text]
    return "".join(f"<li>{escape(item)}</li>" for item in items)


def render_term_sheet_html(
    sheet: ProductionTermSheet,
    *,
    business_name: str,
    client_name: str | None = None,
    sponsor_name: str | None = "UrChoice",
) -> str:
    """Render one immutable ProductionTermSheet version as a client-safe document.

    `notes` is deliberately excluded: it is an operator field and can contain
    internal underwriting commentary. Only the client-facing conditions print.
    """
    payment_label = "Monthly debt service" if sheet.debt_service_is_level_payment else "Estimated monthly debt service"
    funding_party = sheet.funding_party_name or sheet.funding_party_kind
    prepared_for = client_name or business_name
    date_rows = (
        ("Expected funding", sheet.expected_funding_date),
        ("Program activation", sheet.activation_date),
        ("Repayment begins", sheet.commencement_date),
        ("Maturity", sheet.maturity_date),
    )
    schedule = "".join(
        f'<div class="milestone"><span>{escape(label)}</span><strong>{_date(value)}</strong></div>'
        for label, value in date_rows
    )
    html = f"""
<!doctype html>
<html><head><meta charset="utf-8"><style>
  @page {{ size: Letter; margin: 0; @bottom-right {{ content: "Page " counter(page) " of " counter(pages); color: #6d7e91; font: 8px Arial; margin: 0 32px 17px 0; }} }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; color: #14243c; font-family: Arial, Helvetica, sans-serif; font-size: 10px; line-height: 1.42; background: white; }}
  .hero {{ background: #0b1d3a; color: white; padding: 24px 34px 22px; min-height: 164px; }}
  .brand-row {{ display: flex; align-items: center; justify-content: space-between; gap: 28px; }}
  .brand-lockup {{ display: flex; align-items: center; gap: 11px; }}
  .qc-mark {{ width: 37px; height: 37px; flex: none; }}
  .qc-name {{ font-size: 14px; font-weight: 800; letter-spacing: .02em; }}
  .qc-sub {{ color: #a7c7d0; font-size: 7.5px; letter-spacing: .08em; text-transform: uppercase; margin-top: 2px; }}
  .sponsor {{ border-left: 1px solid rgba(255,255,255,.3); padding-left: 18px; text-align: right; min-width: 150px; }}
  .sponsor span {{ display: block; color: #a7c7d0; font-size: 7px; letter-spacing: .12em; text-transform: uppercase; margin-bottom: 5px; }}
  .sponsor img {{ width: 134px; max-height: 42px; object-fit: contain; object-position: right center; }}
  .sponsor strong {{ display: block; color: white; font-size: 15px; }}
  .eyebrow {{ margin-top: 20px; color: #45d7cb; font-size: 8px; font-weight: 800; letter-spacing: .15em; text-transform: uppercase; }}
  h1 {{ margin: 4px 0 3px; font-family: Georgia, 'Times New Roman', serif; font-size: 27px; font-weight: 600; letter-spacing: -.02em; }}
  .hero-meta {{ color: #c6d5e6; font-size: 9px; }}
  .content {{ padding: 20px 34px 28px; }}
  .prepared {{ display: grid; grid-template-columns: 1.4fr .8fr .8fr; border: 1px solid #dce3ec; border-radius: 9px; overflow: hidden; margin-bottom: 15px; }}
  .prepared > div {{ padding: 9px 12px; border-right: 1px solid #dce3ec; }}
  .prepared > div:last-child {{ border-right: 0; }}
  .label {{ color: #718096; font-size: 7.2px; font-weight: 800; letter-spacing: .09em; text-transform: uppercase; }}
  .prepared strong {{ display: block; color: #0b1d3a; font-size: 10.5px; margin-top: 3px; }}
  h2 {{ color: #0f7a73; font-size: 8px; letter-spacing: .13em; text-transform: uppercase; margin: 0 0 8px; }}
  .terms-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 11px; }}
  .term {{ border: 1px solid #dce3ec; border-radius: 9px; padding: 10px 11px; min-height: 60px; background: #fbfcfe; }}
  .term strong {{ display: block; font-size: 14px; margin-top: 4px; color: #0b1d3a; }}
  .term strong.compact {{ font-size: 11px; line-height: 1.25; }}
  .payment {{ border-radius: 11px; background: #edf8f7; border: 1px solid #bfe6e2; padding: 12px 15px; display: grid; grid-template-columns: 1.3fr 1fr 1fr; gap: 14px; margin-bottom: 15px; }}
  .payment .primary strong {{ color: #0f7a73; font-size: 21px; }}
  .payment strong {{ color: #0b1d3a; font-size: 13px; display: block; margin-top: 4px; }}
  .schedule {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 15px; }}
  .milestone {{ border-left: 2px solid #8bd4ce; padding-left: 8px; min-height: 35px; }}
  .milestone span {{ color: #718096; font-size: 7.3px; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; }}
  .milestone strong {{ display: block; color: #20334e; font-size: 9px; margin-top: 3px; }}
  .two {{ display: grid; grid-template-columns: .92fr 1.08fr; gap: 14px; }}
  .card {{ border-top: 2px solid #0b1d3a; padding-top: 9px; page-break-inside: avoid; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ border-bottom: 1px solid #e5eaf0; padding: 4px 2px; }}
  td:last-child {{ text-align: right; color: #0b1d3a; font-weight: 700; }}
  .empty {{ color: #718096; font-size: 8.5px; }}
  ul {{ margin: 5px 0 0; padding-left: 16px; }} li {{ margin: 0 0 4px; }}
  .fine {{ border-top: 1px solid #dce3ec; margin-top: 15px; padding-top: 9px; color: #718096; font-size: 7.4px; line-height: 1.5; }}
  .fine b {{ color: #41546e; }}
</style></head><body>
  <header class="hero">
    <div class="brand-row">{_brand_html(sponsor_name)}</div>
    <div class="eyebrow">Client loan terms - Version {sheet.version}</div>
    <h1>Conditional Financing Terms</h1>
    <div class="hero-meta">Prepared for {escape(business_name)} - {escape(prepared_for)}</div>
  </header>
  <main class="content">
    <div class="prepared">
      <div><span class="label">Prepared for</span><strong>{escape(prepared_for)}</strong></div>
      <div><span class="label">Prepared</span><strong>{_date(sheet.entered_at)}</strong></div>
      <div><span class="label">Version</span><strong>v{sheet.version}</strong></div>
    </div>
    <h2>Proposed facility</h2>
    <div class="terms-grid">
      <div class="term"><span class="label">Approved amount</span><strong>{_money(float(sheet.approved_amount))}</strong></div>
      <div class="term"><span class="label">Rate / APR</span><strong>{float(sheet.rate_pct):.2f}%</strong></div>
      <div class="term"><span class="label">Term</span><strong>{int(sheet.term_months)} months</strong></div>
      <div class="term"><span class="label">Facility</span><strong class="compact">{escape(sheet.facility_type)}</strong></div>
      <div class="term"><span class="label">Funding source</span><strong class="compact">{escape(funding_party)}</strong></div>
      <div class="term"><span class="label">Minimum activation</span><strong>{_money(float(sheet.min_activation_amount))}</strong></div>
    </div>
    <div class="payment">
      <div class="primary"><span class="label">{escape(payment_label)}</span><strong>{_money(float(sheet.monthly_debt_service))}</strong></div>
      <div><span class="label">Estimated annual debt service</span><strong>{_money(float(sheet.monthly_debt_service) * 12)}</strong></div>
      <div><span class="label">Funding party type</span><strong>{escape(sheet.funding_party_kind)}</strong></div>
    </div>
    <h2>Estimated timing</h2>
    <div class="schedule">{schedule}</div>
    <div class="two">
      <section class="card"><h2>Approved use of funds</h2>{_use_of_funds_rows(sheet.use_of_funds)}</section>
      <section class="card"><h2>Conditions</h2><ul>{_condition_items(sheet.conditions)}</ul></section>
    </div>
    <div class="fine"><b>Important:</b> This client-facing summary reflects Production Term Sheet v{sheet.version}. It is not a commitment to lend, a final credit approval, or a replacement for executed funding documents. Terms remain subject to verification, final underwriting, funding-source approval, satisfactory documentation, and closing conditions. Payment and annual debt-service figures are estimates. Fees, reserves, third-party charges, legal expenses, default charges, taxes, insurance, and closing costs are separate unless expressly stated in final documents. Qualified Commercial is not the lender unless identified as the funding party in the final loan documents.</div>
  </main>
</body></html>
"""
    return html


def render_term_sheet_pdf(
    sheet: ProductionTermSheet,
    *,
    business_name: str,
    client_name: str | None = None,
    sponsor_name: str | None = "UrChoice",
) -> bytes:
    from weasyprint import HTML

    pdf = HTML(
        string=render_term_sheet_html(
            sheet,
            business_name=business_name,
            client_name=client_name,
            sponsor_name=sponsor_name,
        ),
        base_url=str(_APP),
    ).write_pdf()
    if not pdf:
        raise RuntimeError("WeasyPrint returned no PDF bytes")
    return pdf
