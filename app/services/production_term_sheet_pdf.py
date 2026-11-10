from __future__ import annotations

import base64
import re
import unicodedata
from datetime import date, datetime
from html import escape
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.models.production_package import ProductionTermSheet
from app.services import production_term_sheets as sheet_service
from app.services import production_term_structure as term_structure
from app.services.application_terms_pdf import _collateral_html
from app.services.financing_budget_disclosure import BUDGET_CSS, frozen_budget, render_budget_html
from app.services.loan_repayment_display import configured_structure_label, repayment_lines
from app.services.loan_terms_visuals import (
    LOAN_VISUALS_CSS,
    closing_preparation_html,
    payment_phases_html,
    schedule_visuals_html,
)
from app.services.production_arrangement import USE_OF_FUNDS_KEYS

_APP = Path(__file__).resolve().parents[1]
_QC_MARK = _APP / "templates" / "_qcmark.svg.html"
_URCHOICE_LOGO = _APP / "assets" / "urchoice_logo.png"


def _money(value: float | None) -> str:
    return "-" if value is None else f"${value:,.2f}"


def _date(value: date | datetime | str | None) -> str:
    if value is None:
        return "To be confirmed"
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return value
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def _term_card(label: str, value: str, *, compact: bool = False) -> str:
    css = ' class="compact"' if compact else ""
    return (
        f'<div class="term"><span class="label">{escape(label)}</span>'
        f'<strong{css}>{escape(value)}</strong></div>'
    )


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


def _condition_lines(conditions: str | None) -> list[str]:
    text = (conditions or "").strip()
    if not text:
        return []
    items = [part.strip(" -\t") for part in text.replace("\r", "\n").split("\n") if part.strip(" -\t")]
    return items or [text]


def _condition_items(conditions: str | None) -> str:
    items = _condition_lines(conditions)
    if not items:
        return "<li>Final verification, underwriting approval, and documentation satisfactory to the funding source.</li>"
    return "".join(f"<li>{escape(item)}</li>" for item in items)


def _visual_schedule(structure: dict[str, Any]) -> dict[str, Any] | None:
    """Rebuild only a calculated schedule from this version's stored terms.

    Production public values intentionally omit rows. Fixed/custom payments,
    explicit nonconfigured balloon targets and legacy unstructured snapshots do
    not establish an allocation compatible with the shared schedule engine.
    """
    from app.services.loan_schedule import build_schedule

    kind = structure.get("repayment_structure")
    if kind in {"fixed_payment", "custom"} or structure.get("lender_payment_override"):
        return None
    config = structure.get("repayment_config")
    if config is None:
        # Older production pricing used unrounded target-payment formulas and
        # explicit maturity amounts. Never replace those historical cash flows
        # with a newly inferred rounded schedule.
        return None
    return build_schedule(
        structure.get("payment_basis_amount"), structure.get("effective_rate_pct"), structure.get("term_months"),
        structure="interest_only" if kind in {"interest_only", "revolving_interest_only"} else "simple_interest" if kind == "simple_interest" else "amortizing",
        interest_only_months=structure.get("interest_only_months") or 0,
        periods_per_year=structure.get("payments_per_year") or 12, repayment_config=config,
    )


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
    structure = sheet_service.sheet_structure(sheet)
    extra = sheet.extra if isinstance(sheet.extra, dict) else {}
    collateral_html = _collateral_html(SimpleNamespace(collateral_snapshot=extra.get("collateral_snapshot"), collateral_revision=extra.get("collateral_revision")))
    from app.services.plan_terms_link import recorded_from_production_sheet

    budget = frozen_budget(extra.get("deal_budget_snapshot"), extra.get("deal_budget_revision"), recorded_from_production_sheet(sheet), extra.get("deal_budget_calculation"))
    visual_summary = _visual_schedule(structure)
    phases_html = payment_phases_html(visual_summary)
    advanced = bool(structure.get("repayment_config"))
    advanced_lines = repayment_lines(structure.get("schedule"))
    repayment_html = (
        '<section class="repayment"><h2>Advanced repayment schedule</h2><ul>'
        + "".join(f"<li>{escape(line)}</li>" for line in advanced_lines)
        + "</ul></section>"
    ) if advanced_lines else ""
    annual_label = "Peak rolling 12-month cash due" if advanced else "Annual scheduled debt service"
    coverage_note = (
        "advanced repayment coverage includes scheduled principal balloons and residual maturity principal once in the highest rolling 12 months of scheduled cash due."
        if advanced else "any balloon is shown separately and is not included in annual scheduled debt service."
    )
    repayment_label = configured_structure_label(structure, term_structure.structure_label(str(structure["repayment_structure"])))
    cadence = term_structure.cadence_label(structure)
    rate = term_structure.rate_label(structure)
    facility_kind = str(structure.get("facility_kind") or "term_loan")
    amount_label = "Credit limit" if facility_kind in {"revolving_loc", "heloc", "hybrid"} else "Approved amount"
    terms: list[str] = [
        _term_card(amount_label, _money(float(sheet.approved_amount))),
        _term_card("Stated note rate", rate, compact=True),
        _term_card("Contract term", f"{int(sheet.term_months)} months"),
        _term_card("Facility", sheet.facility_type, compact=True),
        _term_card("Repayment structure", repayment_label, compact=True),
        _term_card("Payment cadence", cadence, compact=True),
    ]
    if structure.get("apr_pct") is not None:
        terms.append(_term_card("Lender-disclosed APR", f"{float(structure['apr_pct']):.2f}%"))
    initial_draw = structure.get("initial_draw_amount")
    if initial_draw is not None and (
        facility_kind in {"revolving_loc", "heloc", "hybrid"}
        or abs(float(initial_draw) - float(sheet.approved_amount)) >= 0.005
    ):
        terms.append(_term_card("Initial draw", _money(float(initial_draw))))
    basis = structure.get("payment_basis_amount")
    if basis is not None and (
        initial_draw is None or abs(float(basis) - float(initial_draw)) >= 0.005
    ):
        terms.append(_term_card("Payment basis", _money(float(basis))))
    if structure.get("draw_period_months") is not None:
        terms.append(_term_card("Draw period", f"{int(structure['draw_period_months'])} months"))
    if int(structure.get("interest_only_months") or 0) > 0 and not phases_html:
        terms.append(_term_card("Interest-only period", f"{int(structure['interest_only_months'])} months"))
    if structure.get("amortization_months") is not None and not phases_html:
        terms.append(_term_card("Amortization", f"{int(structure['amortization_months'])} months"))
    balloon = float(structure.get("balloon_amount") or 0)
    if balloon > 0 and not phases_html:
        terms.append(_term_card("Balloon due at maturity", _money(balloon)))
    coverage_amount = structure.get("monthly_program_coverage_amount")
    if coverage_amount is not None:
        terms.append(_term_card("Monthly program coverage", _money(float(coverage_amount))))
    if structure.get("dscr_status") or structure.get("dscr_before") is not None or structure.get("dscr_after") is not None:
        before = "Needs evidence" if structure.get("dscr_before") is None else f"{float(structure['dscr_before']):.2f}x"
        after = "Needs evidence" if structure.get("dscr_after") is None else f"{float(structure['dscr_after']):.2f}x"
        terms.append(_term_card("DSCR before", before))
        terms.append(_term_card("DSCR after", after))
    terms.extend(
        [
            _term_card("Funding source", sheet.funding_party_name or sheet.funding_party_kind, compact=True),
            _term_card("Funder type", str(structure.get("funder_type") or sheet.funding_party_kind).replace("_", " ").title(), compact=True),
            _term_card("Minimum activation", _money(float(sheet.min_activation_amount))),
        ]
    )
    term_cards = "".join(terms)

    periodic_label = f"{'Starting regular' if advanced else 'Estimated'} {cadence.lower()} payment"
    if structure.get("post_io_payment") is not None:
        periodic_label = f"Initial IO payment ({cadence.lower()})"
    payment_middle_label = (
        f"Payment after IO ({cadence.lower()})"
        if structure.get("post_io_payment") is not None
        else "Monthly payment equivalent"
    )
    payment_middle_value = (
        float(structure["post_io_payment"])
        if structure.get("post_io_payment") is not None
        else float(structure["monthly_equivalent_payment"] if structure.get("monthly_equivalent_payment") is not None else sheet.monthly_debt_service)
    )
    payment_html = phases_html or f'''<div class="payment">
      <div class="primary"><span class="label">{escape(periodic_label)}</span><strong>{_money(float(structure.get("periodic_payment") or 0))}</strong></div>
      <div><span class="label">{escape(payment_middle_label)}</span><strong>{_money(payment_middle_value)}</strong></div>
      <div><span class="label">{annual_label}</span><strong>{_money(float(structure.get("annual_debt_service") or 0))}</strong></div>
    </div>'''
    payment_summary = term_structure.payment_summary_text(structure)
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
    if structure.get("first_payment_date"):
        schedule += (
            '<div class="milestone"><span>First payment</span>'
            f'<strong>{escape(_date(structure["first_payment_date"]))}</strong></div>'
        )
    if structure.get("closing_estimate_days") is not None:
        schedule += (
            '<div class="milestone"><span>Estimated closing</span>'
            f'<strong>{int(structure["closing_estimate_days"])} business days after final approval</strong></div>'
        )
    if structure.get("expiration_days") is not None:
        schedule += (
            '<div class="milestone"><span>Offer validity</span>'
            f'<strong>{int(structure["expiration_days"])} days after issuance</strong></div>'
        )
    html = f"""
<!doctype html>
<html><head><meta charset="utf-8"><style>
  {BUDGET_CSS}
  {LOAN_VISUALS_CSS}
  @page {{ size: Letter; margin: 28px 0 44px; @bottom-right {{ content: "Page " counter(page) " of " counter(pages); color: #6d7e91; font: 8px Arial; margin: 0 32px 8px 0; }} }}
  @page:first {{ margin-top: 0; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; color: #14243c; font-family: Arial, Helvetica, sans-serif; font-size: 10px; line-height: 1.42; background: white; }}
  .watermark {{ position: fixed; top: 44%; left: 13%; transform: rotate(-32deg); color: rgba(11,29,58,.055); font-size: 70px; font-weight: 800; letter-spacing: .12em; z-index: -1; }}
  .collateral {{ border: 1px solid #dce3ec; border-radius: 9px; padding: 10px 12px; margin-bottom: 15px; }}
  .collateral h2 {{ break-after: avoid; }}
  .collateral table {{ border-collapse: collapse; width: 100%; font-size: 9px; }}
  .collateral tr {{ break-inside: avoid; }}
  .collateral td {{ padding: 7px; border-bottom: 1px solid #dce3ec; vertical-align: top; overflow-wrap: anywhere; }}
  .collateral table td, .deal-budget table td {{ text-align: left; font-weight: normal; }}
  .collateral td:first-child {{ width: 43%; }}
  .collateral p {{ color: #52637a; font-size: 8px; }}
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
  .content {{ padding: 18px 34px 20px; }}
  .prepared {{ display: grid; grid-template-columns: 1.4fr .8fr .8fr; border: 1px solid #dce3ec; border-radius: 9px; overflow: hidden; margin-bottom: 12px; }}
  .prepared > div {{ padding: 9px 12px; border-right: 1px solid #dce3ec; }}
  .prepared > div:last-child {{ border-right: 0; }}
  .label {{ color: #718096; font-size: 7.2px; font-weight: 800; letter-spacing: .09em; text-transform: uppercase; }}
  .prepared strong {{ display: block; color: #0b1d3a; font-size: 10.5px; margin-top: 3px; }}
  h2 {{ color: #0f7a73; font-size: 8px; letter-spacing: .13em; text-transform: uppercase; margin: 0 0 8px; }}
  .terms-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px 8px; margin-bottom: 9px; }}
  .term {{ border: 1px solid #dce3ec; border-radius: 9px; padding: 9px 11px; min-height: 56px; background: #fbfcfe; }}
  .term strong {{ display: block; font-size: 14px; margin-top: 4px; color: #0b1d3a; }}
  .term strong.compact {{ font-size: 11px; line-height: 1.25; }}
  .payment {{ border-radius: 11px; background: #edf8f7; border: 1px solid #bfe6e2; padding: 12px 15px; display: grid; grid-template-columns: 1.3fr 1fr 1fr; gap: 14px; margin-bottom: 12px; }}
  .payment .primary strong {{ color: #0f7a73; font-size: 21px; }}
  .payment strong {{ color: #0b1d3a; font-size: 13px; display: block; margin-top: 4px; }}
  .schedule {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 12px; }}
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
  .repayment {{ border: 1px solid #dce3ec; border-radius: 9px; padding: 10px 12px; margin: 12px 0; }}
  .repayment li {{ break-inside: avoid; overflow-wrap: anywhere; }}
  .fine {{ border-top: 1px solid #dce3ec; margin-top: 10px; padding-top: 7px; color: #718096; font-size: 7.2px; line-height: 1.38; }}
  .fine b {{ color: #41546e; }}
</style></head><body>
  <div class="watermark">CONFIDENTIAL</div>
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
    <div class="terms-grid">{term_cards}</div>
    {payment_html}
    {f'<div class="fine"><b>Payment structure:</b> {escape(str(payment_summary))}</div>' if payment_summary else ''}
    {repayment_html}
    {schedule_visuals_html(visual_summary)}
    {collateral_html}
    {render_budget_html(budget)}
    {closing_preparation_html(version=sheet.version, prepared=_date(sheet.entered_at), expires=_date(structure.get("expires_on")) if structure.get("expires_on") else None, collateral_revision=extra.get("collateral_revision"), budget_revision=extra.get("deal_budget_revision"), collateral_snapshot=extra.get("collateral_snapshot"), budget=budget, conditions=_condition_lines(sheet.conditions))}
    <h2>Estimated timing</h2>
    <div class="schedule">{schedule}</div>
    <div class="two">
      <section class="card"><h2>Approved use of funds</h2>{_use_of_funds_rows(sheet.use_of_funds)}</section>
      <section class="card"><h2>Conditions</h2><ul>{_condition_items(sheet.conditions)}</ul></section>
    </div>
    <div class="fine"><b>Important:</b> This client-facing summary reflects Production Term Sheet v{sheet.version}. It is not a commitment to lend, a final credit approval, or a replacement for executed funding documents. Terms remain subject to verification, final underwriting, funding-source approval, satisfactory documentation, and closing conditions. Payment and debt-service figures are estimates; {coverage_note} Interest on revolving facilities is based on the outstanding balance, so actual payments change with draws, repayments, and rate changes. Variable-rate figures use the current index snapshot shown above. Fees, reserves, third-party charges, legal expenses, default charges, taxes, insurance, and closing costs are separate unless expressly stated in final documents. Qualified Commercial is not the lender unless identified as the funding party in the final loan documents.</div>
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
