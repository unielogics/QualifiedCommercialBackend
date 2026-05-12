"""Term sheet PDF generation.

Produces a borrower-shareable PDF of a loan's configured terms plus a
month-by-month amortization schedule. Used by the new PDF download
button on the Criteria tab.

Uses WeasyPrint (already a dep — see prequal_pdf.py for the same
pattern). Imports cairo at module load, so we defer the import to
render time to keep test runs fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class AmortRow:
    period: int
    payment: float
    interest: float
    principal: float
    balance: float


def amortization_schedule(
    principal: float,
    annual_rate: float,
    term_months: int,
    interest_only_months: int = 0,
) -> list[AmortRow]:
    """Standard 30/360 amortization with an optional IO period.

    The IO period at the front of the loan pays interest only (no
    principal reduction); the remaining months amortize over the
    leftover term using the standard formula.
    """
    if principal <= 0 or annual_rate < 0 or term_months <= 0:
        return []
    monthly_rate = annual_rate / 12.0
    rows: list[AmortRow] = []
    balance = principal

    for n in range(1, min(interest_only_months, term_months) + 1):
        interest = balance * monthly_rate
        rows.append(AmortRow(period=n, payment=interest, interest=interest, principal=0.0, balance=balance))

    amort_months = term_months - interest_only_months
    if amort_months > 0 and balance > 0:
        if monthly_rate > 0:
            payment = balance * (monthly_rate / (1 - (1 + monthly_rate) ** -amort_months))
        else:
            payment = balance / amort_months
        for n in range(interest_only_months + 1, term_months + 1):
            interest = balance * monthly_rate
            principal_pay = max(payment - interest, 0.0)
            if principal_pay > balance:
                principal_pay = balance
                payment = principal_pay + interest
            balance = max(balance - principal_pay, 0.0)
            rows.append(AmortRow(period=n, payment=payment, interest=interest, principal=principal_pay, balance=balance))
            if balance <= 0:
                break
    return rows


def _fmt_money(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}"


def _fmt_pct(value: float | None, decimals: int = 3) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.{decimals}f}%"


def render_term_sheet_pdf(
    *,
    deal_id: str,
    address: str,
    city: str | None,
    state: str | None,
    loan_amount: float,
    base_rate: float | None,
    final_rate: float | None,
    discount_points: float,
    origination_pct: float,
    term_months: int | None,
    purpose: str | None,
    arv: float | None,
    ltv: float | None,
    annual_taxes: float,
    annual_insurance: float,
    monthly_hoa: float,
    monthly_rent: float | None,
    interest_only_months: int = 0,
    issued_on: date | None = None,
    company_name: str = "Qualified Commercial",
    # ── Underwriter fine-tuning (alembic 0044). All optional; surfaced
    # in dedicated rows on the term sheet when supplied. ──
    amortization_style: str | None = None,
    prepay_penalty: str | None = None,
    vacancy_pct: float | None = None,
    expense_ratio_pct: float | None = None,
    reserves_required: float | None = None,
    lender_fees: float | None = None,
    construction_holdback_pct: float | None = None,
    exit_strategy: str | None = None,
    entity_type: str | None = None,
    experience_tier: str | None = None,
    fico_override: int | None = None,
    cash_to_borrower: float | None = None,
    seasoning_months: int | None = None,
    property_count: int | None = None,
    draw_count: int | None = None,
) -> bytes:
    issued = issued_on or date.today()
    rate_for_amort = final_rate if final_rate is not None else base_rate
    # IO month count: if amortization_style is explicitly IO, treat the
    # entire term as interest-only (balloon at maturity). Otherwise honor
    # the explicit interest_only_months arg.
    io_months = interest_only_months
    if amortization_style == "interest_only" and term_months:
        io_months = term_months
    schedule: list[AmortRow] = []
    if rate_for_amort is not None and term_months is not None and term_months > 0:
        schedule = amortization_schedule(
            principal=loan_amount,
            annual_rate=rate_for_amort,
            term_months=term_months,
            interest_only_months=io_months,
        )

    # Summary stats for the cover ribbon.
    monthly_pmt = schedule[interest_only_months].payment if schedule and interest_only_months < len(schedule) else None
    total_paid = sum(r.payment for r in schedule)
    total_interest = sum(r.interest for r in schedule)

    rows_html = "".join(
        f"<tr><td>{r.period}</td><td>{_fmt_money(r.payment)}</td>"
        f"<td>{_fmt_money(r.interest)}</td><td>{_fmt_money(r.principal)}</td>"
        f"<td>{_fmt_money(r.balance)}</td></tr>"
        for r in schedule
    ) or "<tr><td colspan=5 style='text-align:center;color:#888;'>No amortization schedule (rate or term missing).</td></tr>"

    html_str = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Term sheet — {deal_id}</title>
<style>
  @page {{ size: Letter; margin: 0.55in 0.55in 0.65in 0.55in; }}
  body {{ font-family: 'Helvetica', 'Arial', sans-serif; color: #1a1f2e; font-size: 10.5pt; line-height: 1.4; }}
  h1 {{ font-size: 18pt; margin: 0 0 4px; letter-spacing: -0.3px; }}
  .eyebrow {{ font-size: 9pt; letter-spacing: 1.4px; text-transform: uppercase; color: #5a6678; font-weight: 700; }}
  .header-bar {{ display: flex; justify-content: space-between; align-items: flex-end;
                 border-bottom: 2px solid #1a1f2e; padding-bottom: 12px; margin-bottom: 22px; }}
  .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 22px; }}
  .summary .stat {{ padding: 12px 14px; border: 1px solid #d8dde6; border-radius: 8px; }}
  .summary .label {{ font-size: 8.5pt; letter-spacing: 0.8px; text-transform: uppercase; color: #5a6678; font-weight: 700; }}
  .summary .value {{ font-size: 14pt; font-weight: 800; margin-top: 2px; color: #1a1f2e; }}
  .section-title {{ font-size: 10pt; letter-spacing: 1.2px; text-transform: uppercase; color: #5a6678; font-weight: 700;
                    margin: 22px 0 8px; }}
  table.terms {{ width: 100%; border-collapse: collapse; font-size: 10pt; }}
  table.terms td {{ padding: 6px 8px; border-bottom: 1px solid #e6e9ef; }}
  table.terms td.k {{ color: #5a6678; width: 38%; }}
  table.terms td.v {{ color: #1a1f2e; font-weight: 700; text-align: right; }}
  table.amort {{ width: 100%; border-collapse: collapse; font-size: 9pt; }}
  table.amort th {{ background: #f4f6fa; padding: 6px 8px; text-align: right; font-weight: 700;
                    border-bottom: 1px solid #d8dde6; }}
  table.amort th:first-child {{ text-align: left; }}
  table.amort td {{ padding: 5px 8px; border-bottom: 1px solid #eef0f4; text-align: right;
                    font-variant-numeric: tabular-nums; }}
  table.amort td:first-child {{ text-align: left; color: #5a6678; }}
  .footer {{ position: fixed; bottom: 0; left: 0; right: 0; font-size: 8.5pt; color: #8a93a6;
             text-align: center; padding: 8px 0; border-top: 1px solid #e6e9ef; }}
  .footer .id {{ font-family: 'Courier New', monospace; }}
</style></head>
<body>
  <div class="header-bar">
    <div>
      <div class="eyebrow">Term sheet</div>
      <h1>{address}</h1>
      <div style="color:#5a6678; font-size:10.5pt; margin-top:2px;">
        {(city or '')}{(', ' + state) if state else ''} · File {deal_id}
      </div>
    </div>
    <div style="text-align:right;">
      <div style="font-size:9pt; color:#5a6678;">Issued</div>
      <div style="font-weight:700;">{issued.strftime('%B %d, %Y')}</div>
      <div style="margin-top:8px; font-size:9pt; color:#5a6678;">by {company_name}</div>
    </div>
  </div>

  <div class="summary">
    <div class="stat"><div class="label">Loan amount</div><div class="value">{_fmt_money(loan_amount)}</div></div>
    <div class="stat"><div class="label">Rate</div><div class="value">{_fmt_pct(final_rate or base_rate)}</div></div>
    <div class="stat"><div class="label">Term</div><div class="value">{term_months or '—'} mo</div></div>
    <div class="stat"><div class="label">Monthly P&amp;I</div><div class="value">{_fmt_money(monthly_pmt) if monthly_pmt else '—'}</div></div>
  </div>

  <div class="section-title">Loan terms</div>
  <table class="terms">
    <tr><td class="k">Purpose</td><td class="v">{(purpose or '—').replace('_', ' ').title()}</td></tr>
    <tr><td class="k">Amortization</td><td class="v">{(amortization_style or '—').replace('_', ' ').title()}</td></tr>
    <tr><td class="k">Base rate</td><td class="v">{_fmt_pct(base_rate)}</td></tr>
    <tr><td class="k">Final rate</td><td class="v">{_fmt_pct(final_rate)}</td></tr>
    <tr><td class="k">Discount points</td><td class="v">{discount_points:.2f}</td></tr>
    <tr><td class="k">Origination</td><td class="v">{_fmt_pct(origination_pct, 2)}</td></tr>
    <tr><td class="k">Lender fees</td><td class="v">{_fmt_money(lender_fees) if lender_fees else '—'}</td></tr>
    <tr><td class="k">Prepay penalty</td><td class="v">{(prepay_penalty or '—').replace('_', ' ').upper()}</td></tr>
    <tr><td class="k">Interest-only period</td><td class="v">{io_months} mo</td></tr>
    <tr><td class="k">LTV</td><td class="v">{_fmt_pct(ltv, 1) if ltv else '—'}</td></tr>
    <tr><td class="k">ARV / appraised value</td><td class="v">{_fmt_money(arv)}</td></tr>
  </table>

  <div class="section-title">Underwriting</div>
  <table class="terms">
    <tr><td class="k">Borrower entity</td><td class="v">{(entity_type or '—').replace('_', ' ').title()}</td></tr>
    <tr><td class="k">Experience tier</td><td class="v">{(experience_tier or '—').replace('_', ' ').title()}</td></tr>
    <tr><td class="k">FICO (UW override)</td><td class="v">{fico_override if fico_override else '—'}</td></tr>
    <tr><td class="k">Vacancy assumption</td><td class="v">{_fmt_pct(vacancy_pct, 1) if vacancy_pct else '—'}</td></tr>
    <tr><td class="k">Operating expense ratio</td><td class="v">{_fmt_pct(expense_ratio_pct, 1) if expense_ratio_pct else '—'}</td></tr>
    <tr><td class="k">Reserves required</td><td class="v">{_fmt_money(reserves_required) if reserves_required else '—'}</td></tr>
    <tr><td class="k">Construction holdback</td><td class="v">{_fmt_pct(construction_holdback_pct, 2) if construction_holdback_pct else '—'}</td></tr>
    <tr><td class="k">Draw count</td><td class="v">{draw_count if draw_count else '—'}</td></tr>
    <tr><td class="k">Exit strategy</td><td class="v">{(exit_strategy or '—').replace('_', ' ').title()}</td></tr>
    <tr><td class="k">Cash to borrower</td><td class="v">{_fmt_money(cash_to_borrower) if cash_to_borrower else '—'}</td></tr>
    <tr><td class="k">Seasoning</td><td class="v">{f'{seasoning_months} mo' if seasoning_months else '—'}</td></tr>
    <tr><td class="k">Property count</td><td class="v">{property_count if property_count else '—'}</td></tr>
  </table>

  <div class="section-title">Holding economics</div>
  <table class="terms">
    <tr><td class="k">Property taxes (annual)</td><td class="v">{_fmt_money(annual_taxes)}</td></tr>
    <tr><td class="k">Insurance (annual)</td><td class="v">{_fmt_money(annual_insurance)}</td></tr>
    <tr><td class="k">HOA (monthly)</td><td class="v">{_fmt_money(monthly_hoa)}</td></tr>
    <tr><td class="k">Monthly rent</td><td class="v">{_fmt_money(monthly_rent)}</td></tr>
    <tr><td class="k">Total paid over term</td><td class="v">{_fmt_money(total_paid) if schedule else '—'}</td></tr>
    <tr><td class="k">Total interest over term</td><td class="v">{_fmt_money(total_interest) if schedule else '—'}</td></tr>
  </table>

  <div class="section-title">Amortization schedule</div>
  <table class="amort">
    <thead>
      <tr><th>Month</th><th>Payment</th><th>Interest</th><th>Principal</th><th>Balance</th></tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>

  <div class="footer">
    Indicative term sheet — not a commitment to lend. Final pricing subject to underwriting and rate-lock.
    Generated for file <span class="id">{deal_id}</span> on {issued.strftime('%Y-%m-%d')}.
  </div>
</body></html>
"""

    # Defer cairo import to call-time.
    from weasyprint import HTML  # type: ignore
    pdf = HTML(string=html_str).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned None for write_pdf()")
    return pdf
