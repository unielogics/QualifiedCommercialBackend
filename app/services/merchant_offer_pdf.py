"""Client-safe merchant-processing offer PDF.

The partner's uploaded PDF is an internal source document.  It may contain
residuals, bonuses, or notes meant only for the desk, so it must never be used
as the attachment sent from the client email composer.  This renderer starts
from ``merchant_processing.client_view`` -- the same explicit allowlist used
by the secure room -- and therefore has no path to ``desk_terms``.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from html import escape
from pathlib import Path
from typing import Any

from app.models.merchant_processing_offer import MerchantProcessingOffer
from app.services import merchant_processing

_APP = Path(__file__).resolve().parents[1]
_QC_MARK = _APP / "templates" / "_qcmark.svg.html"
_URCHOICE_LOGO = _APP / "assets" / "urchoice_logo.png"


def _safe_name(value: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return "-".join(
        part for part in "".join(ch if ch.isalnum() else " " for ch in ascii_name).split() if part
    )


def filename_for(offer: MerchantProcessingOffer, business_name: str) -> str:
    business = _safe_name(business_name) or "Client"
    return f"{business}-Merchant-Processing-Offer-v{offer.terms_version}.pdf"


def _money(value: Any, *, monthly: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    suffix = " / month" if monthly else ""
    return f"${number:,.2f}{suffix}"


def _pct(value: Any) -> str:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "-"


def _text(value: Any) -> str:
    return escape(str(value).strip()) if value not in (None, "") else "-"


def _data_uri(path: Path) -> str | None:
    if not path.exists():
        return None
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _brand_html() -> str:
    urchoice = _data_uri(_URCHOICE_LOGO)
    sponsor = (
        f'<img src="{urchoice}" alt="UrChoice">'
        if urchoice
        else '<strong class="sponsor-name">UrChoice</strong>'
    )
    if not _QC_MARK.exists():
        mark = '<div class="qc-name">Qualified Commercial</div>'
    else:
        svg = re.sub(r"\{#.*?#\}", "", _QC_MARK.read_text(encoding="utf-8"), flags=re.S).strip()
        mark = (
            '<div class="brand-lockup">'
            f'<div class="qc-mark">{svg}</div>'
            '<div><div class="qc-name">Qualified Commercial</div>'
            '<div class="qc-sub">Capital solutions, clearly presented</div></div></div>'
        )
    return (
        '<div class="brand-row">'
        f"{mark}"
        '<div class="sponsor"><span>Presented with</span>'
        f"{sponsor}</div></div>"
    )


def render_merchant_offer_html(
    offer: MerchantProcessingOffer,
    *,
    business_name: str,
    partner_name: str | None,
) -> str:
    """Return client-facing HTML built solely from the secure-room allowlist."""

    view = merchant_processing.client_view(offer, partner_name=partner_name)
    terms = dict(view.get("terms") or {})
    options = [item for item in (terms.get("options") or []) if isinstance(item, dict)]

    summary_rows = [
        ("Prepared for", _text(terms.get("prepared_for") or business_name)),
        ("Processing partner", _text(view.get("partner_name"))),
        ("Proposal date", _text(terms.get("prepared_on"))),
        ("Current processor", _text(terms.get("current_processor"))),
    ]
    pricing_rows = [
        ("Monthly card volume", _money(terms.get("current_monthly_volume"))),
        ("Current monthly fees", _money(terms.get("current_monthly_fees"), monthly=True)),
        ("Current effective rate", _pct(terms.get("current_effective_rate_pct"))),
        ("Proposed monthly fees", _money(terms.get("proposed_monthly_fees"), monthly=True)),
        ("Proposed effective rate", _pct(terms.get("proposed_effective_rate_pct"))),
        ("Pricing model", _text(terms.get("proposed_pricing_model")).replace("_", " ").title()),
    ]
    detail_rows = [
        (
            "Contract term",
            f"{int(float(terms['contract_term_months']))} months"
            if terms.get("contract_term_months") is not None
            else "-",
        ),
        ("Early termination fee", _money(terms.get("early_termination_fee"))),
        ("Equipment", _text(terms.get("equipment_notes"))),
    ]

    def rows(items: list[tuple[str, Any]]) -> str:
        return "".join(
            f"<tr><th>{escape(label)}</th><td>{value}</td></tr>" for label, value in items
        )

    option_html = ""
    if options:
        option_rows = "".join(
            "<tr>"
            f"<td>{_text(item.get('label'))}</td>"
            f"<td>{_pct(item.get('effective_rate_pct'))}</td>"
            f"<td>{_money(item.get('monthly_fees'), monthly=True)}</td>"
            f"<td>{_money(item.get('monthly_savings'), monthly=True)}</td>"
            "</tr>"
            for item in options
        )
        option_html = f"""
          <section>
            <h2>Available options</h2>
            <table class="options"><thead><tr><th>Option</th><th>Effective rate</th><th>Monthly fees</th><th>Monthly savings</th></tr></thead>
            <tbody>{option_rows}</tbody></table>
          </section>
        """

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  @page {{ size: Letter; margin: 0; @bottom-right {{ content: "Page " counter(page) " of " counter(pages); color: #6d7e91; font: 8px Arial; margin: 0 32px 17px 0; }} }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: Arial, Helvetica, sans-serif; color: #14243c; font-size: 10px; line-height: 1.42; background: white; }}
  .hero {{ background: #0b1d3a; color: white; padding: 24px 34px 23px; min-height: 170px; }}
  .brand-row {{ display: flex; align-items: center; justify-content: space-between; gap: 28px; }}
  .brand-lockup {{ display: flex; align-items: center; gap: 11px; }}
  .qc-mark {{ width: 37px; height: 37px; flex: none; }} .qc-mark svg {{ width: 100%; height: 100%; }}
  .qc-name {{ font-size: 14px; font-weight: 800; letter-spacing: .02em; color: white; }}
  .qc-sub {{ color: #a7c7d0; font-size: 7.5px; letter-spacing: .08em; text-transform: uppercase; margin-top: 2px; }}
  .sponsor {{ border-left: 1px solid rgba(255,255,255,.3); padding-left: 18px; text-align: right; min-width: 150px; }}
  .sponsor span {{ display: block; color: #a7c7d0; font-size: 7px; letter-spacing: .12em; text-transform: uppercase; margin-bottom: 5px; }}
  .sponsor img {{ width: 134px; max-height: 42px; object-fit: contain; object-position: right center; }}
  .sponsor-name {{ display: block; color: white; font-size: 15px; text-transform: none; }}
  .eyebrow {{ margin-top: 20px; color: #45d7cb; text-transform: uppercase; letter-spacing: .15em; font-size: 8px; font-weight: 800; }}
  h1 {{ margin: 4px 0 3px; font-family: Georgia, 'Times New Roman', serif; font-size: 27px; font-weight: 600; letter-spacing: -.02em; color: white; }}
  .hero-meta {{ color: #c6d5e6; font-size: 9px; }}
  .content {{ padding: 20px 34px 28px; }}
  h2 {{ color: #0f7a73; font-size: 8px; letter-spacing: .13em; text-transform: uppercase; margin: 20px 0 8px; }}
  .saving {{ margin: 15px 0; padding: 13px 15px; background: #edf8f7; border: 1px solid #bfe6e2; border-radius: 11px; }}
  .saving strong {{ display: block; color: #0f7a73; font-size: 22px; line-height: 1.08; }}
  .saving span {{ color: #41546e; font-size: 9px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 6px 8px; border-bottom: 1px solid #e5eaf0; vertical-align: top; text-align: left; }}
  th {{ width: 42%; color: #718096; font-weight: 700; }}
  td {{ color: #20334e; }}
  .summary {{ border: 1px solid #dce3ec; border-radius: 9px; overflow: hidden; }}
  .options {{ border: 1px solid #dce3ec; border-radius: 9px; }}
  .options th {{ width: auto; background: #f7fafc; color: #41546e; }}
  .fine {{ margin-top: 20px; padding-top: 10px; border-top: 1px solid #dce3ec; color: #718096; font-size: 7.4px; line-height: 1.5; }}
</style></head><body>
  <header class="hero">
    {_brand_html()}
    <div class="eyebrow">Client processing offer &middot; Version {int(view.get("terms_version") or 1)}</div>
    <h1>Merchant Processing Savings</h1>
    <div class="hero-meta">Prepared for {_text(business_name)}</div>
  </header>
  <main class="content">
    <table class="summary">{rows(summary_rows)}</table>
    <div class="saving"><strong>{_money(view.get("estimated_annual_savings"))}</strong><span>Estimated annual savings &middot; {_money(view.get("estimated_monthly_savings"), monthly=True)}</span></div>
    <section><h2>Pricing comparison</h2><table>{rows(pricing_rows)}</table></section>
    {option_html}
    <section><h2>Additional terms</h2><table>{rows(detail_rows)}</table></section>
    <p class="fine">{_text(view.get("disclaimer_text"))}<br>Offer version {int(view.get("terms_version") or 1)} &middot; Disclosure {_text(view.get("disclaimer_version"))}.</p>
  </main>
</body></html>"""


def render_merchant_offer_pdf(
    offer: MerchantProcessingOffer,
    *,
    business_name: str,
    partner_name: str | None,
) -> bytes:
    from weasyprint import HTML

    pdf = HTML(
        string=render_merchant_offer_html(
            offer,
            business_name=business_name,
            partner_name=partner_name,
        ),
        base_url=str(_APP),
    ).write_pdf()
    if not pdf:
        raise RuntimeError("weasyprint returned no merchant offer PDF bytes")
    return pdf
