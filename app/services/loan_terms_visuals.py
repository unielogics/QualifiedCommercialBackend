"""Self-contained, client-safe charts of an immutable loan schedule.

These views consume the canonical schedule; they do not calculate a second loan
or fetch current file data. All principal cash, including maturity, is counted
once. Elapsed loan months are used when contractual calendar dates are unknown.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from typing import Any

from app.services.loan_repayment_display import schedule_cadence_label

LOAN_VISUALS_CSS = """
.loan-visuals { margin: 16px 0; }
.loan-visuals--schedule { margin-top: 0; break-before: page; }
.loan-visuals--schedule + .collateral { break-before: page; }
.loan-visuals h2, .closing-preparation h2 { break-after: avoid; }
.loan-chart { border: 1px solid #dce3ec; border-radius: 9px; padding: 8px 12px; margin: 7px 0; break-inside: avoid; }
.loan-chart h3 { margin: 0 0 5px; font-size: 10px; color: #0b1d3a; }
.loan-chart svg { display: block; width: 100%; height: auto; }
.loan-chart p, .loan-visuals .schedule-note, .closing-preparation p { color: #52637a; font-size: 8px; margin: 6px 0; }
.loan-chart-legend { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 8px; margin: 5px 0; }
.loan-chart-legend i { display: inline-block; width: 8px; height: 8px; margin-right: 4px; border-radius: 2px; }
.loan-risk { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 10px 0; break-inside: avoid; }
.loan-risk > div { background: #f4f8fb; border-radius: 7px; padding: 8px; }
.loan-risk span { display: block; color: #52637a; font-size: 7px; }
.loan-risk b { display: block; margin: 3px 0; font-size: 12px; color: #0b1d3a; }
.loan-risk small { color: #52637a; font-size: 7px; }
.loan-amort-table { width: 100%; border-collapse: collapse; font-size: 8px; margin: 8px 0; }
.loan-amort-table thead { display: table-header-group; }
.loan-amort-table th { color: #52637a; background: #f4f7fb; font-size: 7px; }
.loan-amort-table th, .loan-amort-table td { padding: 7px 4px; border-bottom: 1px solid #dce3ec; text-align: right; font-weight: normal; }
.loan-amort-table th:first-child, .loan-amort-table td:first-child { text-align: left; }
.loan-amort-table tr { break-inside: avoid; }
.loan-amort-table .loan-total td { font-weight: bold; background: #edf8f7; }
.loan-amort-table caption { text-align: left; font-weight: bold; color: #0b1d3a; padding: 8px 0; break-after: avoid; }
.closing-preparation { margin: 10px 0; border: 1px solid #dce3ec; border-radius: 9px; padding: 8px 12px; break-inside: avoid; }
.closing-preparation .closing-reference { color: #52637a; font-size: 8px; margin-bottom: 8px; }
.closing-preparation table { width: 100%; border-collapse: collapse; font-size: 8.5px; }
.closing-preparation tr { break-inside: avoid; }
.closing-preparation table td { text-align: left; font-weight: normal; padding: 4px 3px; border-bottom: 1px solid #e5eaf0; vertical-align: top; overflow-wrap: anywhere; }
.closing-preparation td:first-child { width: 25%; color: #52637a; }
.loan-payment-phases { margin: 12px 0; break-inside: avoid; }
.loan-payment-phases h2 { break-after: avoid; }
.loan-phase-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.loan-phase { border: 1px solid #bfe6e2; border-radius: 9px; padding: 10px 11px; background: #f4fbfa; }
.loan-phase .phase-title { display: block; color: #52637a; font-size: 8px; font-weight: bold; }
.loan-phase .phase-months { display: block; color: #0f7a73; font-size: 9px; margin: 4px 0; font-weight: bold; }
.loan-phase strong { display: block; color: #0f7a73; font-size: 18px; line-height: 1.2; margin: 5px 0; }
.loan-phase small { display: block; color: #52637a; font-size: 8px; margin: 3px 0; }
.loan-payment-totals { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px; padding: 8px 11px; background: #f4f7fb; border-radius: 7px; }
.loan-payment-totals span { display: block; color: #52637a; font-size: 7.5px; }
.loan-payment-totals b { display: block; color: #0b1d3a; font-size: 12px; margin-top: 3px; }
.loan-payment-phases p { color: #52637a; font-size: 8px; margin: 6px 0; }
"""

_COLORS = ("#0f7a73", "#7694b4", "#cf8b1b", "#ad4e52")
_COMPONENTS = ("regular_principal", "interest", "scheduled_balloon", "maturity_balloon")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (ValueError, TypeError):
        return default
    return result if math.isfinite(result) else default


def _cents(value: Any) -> int:
    return int(Decimal(str(_number(value))).quantize(Decimal(".01"), rounding=ROUND_HALF_UP) * 100)


def _money(value: Any) -> str:
    return "Not recorded" if value is None else f"${_number(value):,.2f}"


def payment_phases_html(summary: dict[str, Any] | None) -> str:
    """Show IO, later regular payments and maturity cash from one saved schedule.

    Only actual canonical phase rows establish the step-up. Legacy scalar
    payments deliberately retain their original presentation.
    """
    phases = summary.get("payment_phases") if isinstance(summary, dict) else None
    if not phases or not any(phase.get("phase") == "interest_only" for phase in phases):
        return ""
    cadence = schedule_cadence_label(summary)
    count = float(summary["periods_per_year"])
    term = int(summary.get("requested_maturity_months") or summary["term_months"])
    requested_io = int(summary.get("requested_interest_only_months") or summary.get("interest_only_months") or term)
    cards = []
    for index, phase in enumerate(phases, start=1):
        io = phase["phase"] == "interest_only"
        title = "Interest-only payments" if io else "Principal and interest payments"
        low, high = float(phase["payment_min"]), float(phase["payment_max"])
        amount = _money(low) if low == high else f"{_money(low)} - {_money(high)}"
        monthly = _money(_cents(low * count / 12) / 100) if low == high else f"{_money(_cents(low * count / 12) / 100)} - {_money(_cents(high * count / 12) / 100)}"
        start = 1 if io else requested_io + 1
        end = min(term, requested_io if io else term, math.ceil(float(phase["end_month"]) - 1e-7))
        timing = f"Months {start}-{end}" if start != end else f"Month {start}"
        variable = " Regular payments vary after principal events; see the amortization report." if low != high else ""
        detail = "Interest only; principal is not reduced by these regular payments." if io else "Regular principal and interest; extra principal is shown separately."
        cards.append(f'<div class="loan-phase"><span class="phase-title">{index}. {title}</span><span class="phase-months">{timing}</span><strong>{amount}</strong><small>Per payment - {escape(cadence)}</small><small>{int(phase["payment_count"])} payments; {monthly} monthly equivalent.</small><small>{detail}{variable}</small></div>')
    maturity = float(summary.get("maturity_balloon") or 0)
    maturity_detail = "In addition to the final scheduled payment; not another recurring installment." if maturity else "No residual principal remains after the final scheduled payment."
    cards.append(f'<div class="loan-phase"><span class="phase-title">{len(cards) + 1}. Principal at maturity</span><span class="phase-months">Month {term}</span><strong>{_money(maturity)}</strong><small>{maturity_detail}</small></div>')
    note = "The peak 12-month figure is an underwriting cash requirement, not your regular payment. It includes regular payments, scheduled extra principal and the maturity balance once. Total repayment includes all regular payment phases and all principal due once."
    mapping = f' {escape(str(summary["cadence_adjustment_note"]))}' if summary.get("cadence_adjustment_note") else ""
    return '<section class="loan-payment-phases"><h2>Your payment phases</h2><div class="loan-phase-grid">' + "".join(cards) + f'</div><div class="loan-payment-totals"><div><span>Peak rolling 12-month cash due</span><b>{_money(summary.get("peak_rolling_12_month_debt_service"))}</b></div><div><span>Estimated total repayment</span><b>{_money(summary.get("total_paid"))}</b></div></div><p>{note}{mapping}</p></section>'


def _short_money(value: float) -> str:
    return f"${value / 1_000_000:.1f}m" if value >= 1_000_000 else f"${value / 1000:.0f}k" if value >= 10000 else f"${value / 1000:.1f}k" if value >= 1000 else f"${value:.0f}"


def _cash_rows(summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(summary, dict) or not isinstance(summary.get("rows"), list):
        return []
    source = summary["rows"]
    result = []
    for index, row in enumerate(source):
        if not isinstance(row, dict):
            continue
        scheduled = _cents(row.get("scheduled_balloon"))
        regular = _cents(row.get("regular_principal")) if row.get("regular_principal") is not None else _cents(row.get("principal")) - scheduled
        maturity = _cents(row.get("maturity_balloon")) if row.get("maturity_balloon") is not None else _cents(row.get("balloon", summary.get("balloon") if index == len(source) - 1 else 0))
        interest = _cents(row.get("interest"))
        after = _cents(row.get("balance_after_cash")) if row.get("balance_after_cash") is not None else max(0, _cents(row.get("balance")) - maturity)
        cash = _cents(row.get("cash_payment")) if row.get("cash_payment") is not None else regular + interest + scheduled + maturity
        result.append({"month": _number(row.get("month")), "regular_principal": regular, "interest": interest,
                       "scheduled_balloon": scheduled, "maturity_balloon": maturity, "cash_due": cash,
                       "ending_balance": after, "balance_before_maturity": _cents(row.get("balance"))})
    return result


def annual_cash_rows(summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Annual cash totals from exact cents; year 1 is elapsed months (0, 12]."""
    groups: dict[int, dict[str, Any]] = OrderedDict()
    last_year = max(1, math.ceil(_number((summary or {}).get("requested_maturity_months") or (summary or {}).get("term_months")) / 12))
    for row in _cash_rows(summary):
        year = min(last_year, max(1, math.ceil((row["month"] - 1e-7) / 12)))
        block = groups.setdefault(year, {"year": year, "start_month": row["month"], "end_month": row["month"], "payments": 0,
                                        **{key: 0 for key in (*_COMPONENTS, "cash_due", "ending_balance")}})
        block["payments"] += 1
        block["end_month"] = row["month"]
        for key in (*_COMPONENTS, "cash_due"):
            block[key] += row[key]
        block["ending_balance"] = row["ending_balance"]
    return [{**block, **{key: block[key] / 100 for key in (*_COMPONENTS, "cash_due", "ending_balance")}} for block in groups.values()]


def _svg_start(label: str, chart: str) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 680 132" role="img" aria-label="{label}" data-chart="{chart}"><title>{label}</title>'


def _payment_chart(rows: list[dict[str, Any]], *, include_events: bool = True) -> str:
    months = max(row["month"] for row in rows)
    span = max(1, math.ceil(months / 36))
    components = _COMPONENTS if include_events else _COMPONENTS[:2]
    colors = _COLORS if include_events else _COLORS[:2]
    groups: dict[int, dict[str, Any]] = OrderedDict()
    for row in rows:
        bucket = max(1, math.ceil((row["month"] - 1e-7) / span))
        group = groups.setdefault(bucket, {key: 0 for key in components})
        for key in components:
            group[key] += row[key]
    peak = max((sum(group.values()) for group in groups.values()), default=1) or 1
    pieces = [_svg_start("Payments by loan month", "payments") if include_events else _svg_start("Regular principal and interest by loan month", "regular-payments")]
    for fraction in (0, .5, 1):
        y = 94 - fraction * 66
        pieces.append(f'<line x1="68" y1="{y:g}" x2="662" y2="{y:g}" stroke="#e5eaf0"/><text x="60" y="{y + 3:g}" text-anchor="end" font-size="10" fill="#52637a">{_short_money(peak * fraction / 100)}</text>')
    width = 588 / len(groups)
    step = max(1, math.ceil(len(groups) / 8))
    for index, (bucket, group) in enumerate(groups.items()):
        x = 71 + index * width
        y = 94.0
        for key, color in zip(components, colors, strict=True):
            height = group[key] / peak * 66
            y -= height
            if height:
                pieces.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{max(1, width - 3):.2f}" height="{height:.2f}" fill="{color}"/>')
        if index % step == 0 or index == len(groups) - 1:
            label = f"{min(bucket * span, math.ceil(months))}"
            pieces.append(f'<text x="{x + width / 2:.2f}" y="110" text-anchor="middle" font-size="10" fill="#52637a">{label}</text>')
    pieces.append('<text x="365" y="125" text-anchor="middle" font-size="10" fill="#52637a">Elapsed loan month</text></svg>')
    labels = ("Regular principal", "Interest", "Scheduled extra principal", "Maturity principal") if include_events else ("Regular principal", "Interest")
    legend = "".join(f'<span><i style="background:{color}"></i>{label}</span>' for color, label in zip(colors, labels, strict=True))
    grouping = "Monthly totals" if span == 1 else f"Totals per {span}-month group"
    title = "Total cash due, including extra and maturity principal" if include_events else "Regular payments: principal and interest"
    note = "all payments at the saved cadence are included. Extra and maturity principal are separate from regular payments." if include_events else "regular payments at the saved cadence, on their own scale. Extra and maturity principal are excluded here and shown in the total-cash chart."
    return f'<div class="loan-chart"><h3>{title}</h3>' + "".join(pieces) + f'<div class="loan-chart-legend">{legend}</div><p>{grouping}; {note}</p></div>'


def _balance_chart(rows: list[dict[str, Any]], principal: float) -> str:
    end = max(row["month"] for row in rows) or 1
    points = [(0.0, principal)]
    # Keep the final balance before maturity visible; an explicit vertical final
    # payment takes it to zero, rather than implying it was amortized away.
    for row in rows:
        points.append((row["month"], row["balance_before_maturity"] / 100))
        if row["maturity_balloon"]:
            points.append((row["month"], row["ending_balance"] / 100))
    pieces = [_svg_start("Outstanding principal and principal paid down", "principal")]
    for fraction in (0, .5, 1):
        y = 94 - fraction * 66
        pieces.append(f'<line x1="68" y1="{y:g}" x2="610" y2="{y:g}" stroke="#e5eaf0"/><text x="60" y="{y + 3:g}" text-anchor="end" font-size="10" fill="#52637a">{_short_money(principal * fraction)}</text><text x="619" y="{y + 3:g}" font-size="10" fill="#0f7a73">{(1 - fraction) * 100:g}% paid</text>')
    path = " ".join(f'{"M" if index == 0 else "L"} {68 + month / end * 542:.2f},{94 - balance / principal * 66:.2f}' for index, (month, balance) in enumerate(points))
    pieces.append(f'<path d="{path}" fill="none" stroke="#0f7a73" stroke-width="2.5"/>')
    for fraction in (0, .25, .5, .75, 1):
        pieces.append(f'<text x="{68 + fraction * 542:g}" y="110" text-anchor="middle" font-size="10" fill="#52637a">{end * fraction:g}</text>')
    pieces.append('<text x="340" y="125" text-anchor="middle" font-size="10" fill="#52637a">Elapsed loan month</text></svg>')
    final = rows[-1]
    note = f'At maturity, {_money(final["balance_before_maturity"] / 100)} remains after the regular and scheduled extra principal; the separate maturity payment brings the modeled balance to {_money(final["ending_balance"] / 100)}.' if final["maturity_balloon"] else "Principal paid down measures principal returned, excluding interest and fees. The ending balance follows the saved repayment schedule."
    return '<div class="loan-chart"><h3>Outstanding principal / principal paid down</h3>' + "".join(pieces) + f'<p>{note}</p></div>'


def schedule_visuals_html(summary: dict[str, Any] | None) -> str:
    rows = _cash_rows(summary)
    if not rows or not isinstance(summary, dict) or _number(summary.get("amount")) <= 0:
        return '<section class="loan-visuals"><h2>Payment and amortization overview</h2><p class="schedule-note">A payment-by-payment principal and interest allocation was not recorded with this version. The stated payment and maturity terms above remain the recorded terms; no amortization curve has been assumed.</p></section>'
    principal = _number(summary["amount"])
    annual = annual_cash_rows(summary)
    first = _number(summary.get("first_year_actual_debt_service"))
    peak = _number(summary.get("peak_rolling_12_month_debt_service"))
    maturity = sum(row["maturity_balloon"] for row in rows) / 100
    risk = f'<div class="loan-risk"><div><span>First-year cash due</span><b>{_money(first)}</b><small>Actual scheduled cash in the first 12 loan months</small></div><div><span>Peak rolling 12-month cash due</span><b>{_money(peak)}</b><small>Includes extra and maturity principal once</small></div><div><span>Maturity principal exposure</span><b>{_money(maturity)}</b><small>{maturity / principal * 100:.1f}% of original principal; separate from regular payment</small></div></div>'
    body = []
    for row in annual:
        body.append(f'<tr><td>Year {row["year"]}<br><small>{row["payments"]} payments</small></td>' + "".join(f'<td>{_money(row[key])}</td>' for key in (*_COMPONENTS, "cash_due", "ending_balance")) + '</tr>')
    sums = {key: sum(_cents(row[key]) for row in annual) / 100 for key in (*_COMPONENTS, "cash_due")}
    body.append('<tr class="loan-total"><td>Lifetime total</td>' + "".join(f'<td>{_money(sums[key])}</td>' for key in (*_COMPONENTS, "cash_due")) + f'<td>{_money(annual[-1]["ending_balance"])}</td></tr>')
    table = '<table class="loan-amort-table"><caption>Annual amortization and cash due</caption><thead><tr><th>Loan year</th><th>Regular<br>principal</th><th>Interest</th><th>Extra<br>principal</th><th>Maturity<br>principal</th><th>Total<br>cash due</th><th>Ending<br>principal</th></tr></thead><tbody>' + "".join(body) + '</tbody></table>'
    note = '<p class="schedule-note">Loan years follow elapsed months from origination, not calendar tax years; a final cadence-rounded payment belongs to the contractual maturity year. Principal columns reconcile to the original funded principal; cash due is principal plus interest. Fees and reserves are disclosed in the closing budget and are not duplicated here. Curves assume scheduled payments are made; actual balances may differ. No payment dates are inferred.</p>'
    return '<section class="loan-visuals loan-visuals--schedule"><h2>Payment and amortization overview</h2>' + risk + _payment_chart(rows, include_events=False) + _payment_chart(rows) + _balance_chart(rows, principal) + table + note + '</section>'


def closing_preparation_html(*, version: Any, prepared: str, expires: str | None = None,
                             collateral_revision: Any = None, budget_revision: Any = None,
                             collateral_snapshot: Any = None, budget: Any = None,
                             conditions: list[Any] | None = None) -> str:
    snapshot = collateral_snapshot if isinstance(collateral_snapshot, dict) else {}
    items = [item for item in [*(snapshot.get("properties") or []), *(snapshot.get("assets") or [])] if isinstance(item, dict)] if snapshot.get("enabled") and collateral_revision is not None else []
    collateral = f'{len(items)} declared asset(s), recorded revision {collateral_revision}. Review the collateral schedule and its linked valuation, ownership and lien evidence.' if items else "No collateral schedule included in this terms version; security remains subject to final documents."
    calc = budget.get("calculation") if isinstance(budget, dict) and isinstance(budget.get("calculation"), dict) else {}
    costs = f'Recorded cost budget revision {budget_revision}. Gross loan {_money(calc.get("gross_loan_amount"))}; net proceeds {_money(calc.get("net_client_cash"))}.' if calc and budget_revision is not None else "No cost budget was recorded with this version. Confirm fees, reserves and final net proceeds."
    condition_text = f'{len(conditions)} condition(s) recorded with this version. See the Conditions section for the complete requirements.' if conditions else "No specific client conditions were recorded. Final underwriting and funding-source approval remain required."
    rows = [("Terms reference", f'Version {version} - prepared {prepared}' + (f'; valid through {expires}.' if expires else ".")),
            ("Security", collateral), ("Funds at closing", costs), ("Recorded conditions", condition_text)]
    content = "".join(f'<tr><td>{escape(label)}</td><td>{escape(value)}</td></tr>' for label, value in rows)
    return '<section class="closing-preparation"><h2>Closing preparation</h2><div class="closing-reference">Carry this saved terms version, its repayment schedule, collateral schedule and closing budget into closing review.</div><table>' + content + '</table><p>Preparation summary only. Acceptance, satisfaction of conditions, title/lien clearance, execution and funding are not established by this PDF. Final executed loan and security documents control.</p></section>'
