"""Deterministic investor slides; incomplete terms never imply an investor return."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from app.services.funding_requests import intake_requested_amount

NAVY = (0.04, 0.11, 0.23)
TEAL = (0.04, 0.48, 0.44)
INK = (0.09, 0.13, 0.19)
MUTE = (0.37, 0.43, 0.51)
PALE = (0.94, 0.96, 0.98)
RED = (0.72, 0.16, 0.17)
WIDTH, HEIGHT = 960, 540


def _record(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _money(value, currency="USD"):
    number = _number(value)
    if number is None:
        return "Pending evidence"
    code = str(currency or "").upper()
    if code == "USD":
        return ("-$" if number < 0 else "$") + f"{abs(number):,.0f}"
    return f"{code} {number:,.0f}" if len(code) == 3 and code.isalpha() and code != "XXX" else f"{number:,.0f} (currency unconfirmed)"


def _ratio(value):
    number = _number(value)
    return "Pending evidence" if number is None else f"{number:.2f}x"


def _clean(value):
    return str(value or "").translate(str.maketrans({"\u2014": "-", "\u2013": "-", "\u2212": "-", "\u2192": "to", "\u00a0": " "})).encode("cp1252", "replace").decode("cp1252")


def investment_metrics(terms):
    """Gross contractual return is a full-loan cash-flow illustration, not net ROI."""
    terms = _record(terms)
    amount = _number(terms.get("amount"))
    schedule = _record(terms.get("schedule"))
    total = _number(schedule.get("total_paid"))
    complete = amount is not None and amount > 0 and total is not None
    return {"investment_amount": amount, "total_contractual_receipts": total if complete else None,
            "gross_finance_income": round(total - amount, 2) if complete else None,
            "gross_contractual_roi_pct": round((total - amount) / amount * 100, 2) if complete else None,
            "net_investor_roi_pct": None, "term_months": terms.get("term_months"),
            "apr_pct": terms.get("rate_pct"), "maturity_balloon": schedule.get("balloon"),
            "note": "Full-loan contractual cash flows, assuming every payment is received. Gross return over the full term is not annualized, not APR and not a guaranteed or net investor return. Participation allocation, fees, losses, taxes and servicing remain to be confirmed."}


def operating_ebitda_projection(financials, key="proposed"):
    """Only project documented annual P&L EBITDA from the same reviewed basis.

    A tax-derived coverage numerator, older period, different currency or
    incomplete proposal must never occupy the validated-EBITDA comparison.
    """
    earnings = _record(financials.get("operating_earnings"))
    historical = _record(earnings.get("historical"))
    improvement = _record(earnings.get("improvement_plan"))
    baseline = _record(improvement.get("baseline"))
    projection = _record(_record(improvement.get("projection")).get(key))
    annual = _number(_record(financials.get("current_key_metrics")).get("annual_ebitda"))
    same_values = annual is not None and all(
        _number(value) is not None and abs(float(value) - annual) <= .01
        for value in (historical.get("annualized_value"), baseline.get("earnings")))
    if (not same_values or historical.get("kind") not in {"computed_ebitda", "reported_ebitda"}
            or baseline.get("earnings_kind") != historical.get("kind")
            or historical.get("complete") is not True or historical.get("provisional") is not False
            or baseline.get("provisional") is not False
            or historical.get("currency") != "USD" or baseline.get("currency") != "USD"
            or not historical.get("period") or baseline.get("period") != historical.get("period")
            or historical.get("basis") not in {"cash", "accrual"} or baseline.get("basis") != historical.get("basis")
            or projection.get("status") not in ({"reviewed_illustration"} if key == "approved" else {"unreviewed_illustration", "reviewed_illustration"})):
        return None
    return _number(projection.get("earnings"))


def _text(page, rect, text, *, size=12, color=INK, bold=False):
    import fitz

    text = _clean(text)
    for font_size in (size, size - 1, size - 2, size - 3):
        shape = page.new_shape()
        remaining = shape.insert_textbox(fitz.Rect(*rect), text, fontsize=font_size,
                                        fontname="hebo" if bold else "helv", color=color, lineheight=1.25)
        if remaining >= 0:
            shape.commit()
            return
    raise ValueError("Investor slide text exceeds its allocated area")


def _slide(doc, title, business, stamp):
    import fitz

    page = doc.new_page(width=WIDTH, height=HEIGHT)
    page.draw_rect(fitz.Rect(0, 0, WIDTH, 89), fill=NAVY, color=NAVY)
    page.draw_rect(fitz.Rect(32, 87, 115, 92), fill=TEAL, color=TEAL)
    _text(page, (32, 18, 900, 36), "QUALIFIED COMMERCIAL  /  INVESTOR REVIEW", size=10, color=(0.65, 0.86, 0.87), bold=True)
    _text(page, (32, 40, 920, 80), title, size=23, color=(1, 1, 1), bold=True)
    page.draw_line((32, 510), (928, 510), color=(0.82, 0.86, 0.90))
    _text(page, (32, 518, 760, 536), f"{business[:65]}  |  {stamp}  |  Indicative review - subject to underwriting", size=8, color=MUTE)
    return page


def _card(page, rect, label, value, detail="", *, negative=False):
    import fitz

    x0, y0, x1, y1 = rect
    page.draw_rect(fitz.Rect(*rect), fill=PALE, color=(0.84, 0.88, 0.92))
    _text(page, (x0 + 14, y0 + 12, x1 - 14, y0 + 42), label.upper(), size=10, color=MUTE, bold=True)
    _text(page, (x0 + 14, y0 + 47, x1 - 14, y0 + 87), value, size=25, bold=True, color=RED if negative else TEAL)
    if detail:
        _text(page, (x0 + 14, y0 + 93, x1 - 14, y1 - 9), detail, size=10, color=MUTE)


def _lines(text, width, *, size=11):
    import fitz

    result = []
    for paragraph in _clean(text).splitlines() or [""]:
        line = ""
        for word in paragraph.split():
            if fitz.get_text_length((line + " " + word).strip(), fontsize=size) <= width:
                line = (line + " " + word).strip()
            else:
                if line:
                    result.append(line)
                # Long identifiers must wrap instead of leaving the page.
                line = ""
                while fitz.get_text_length(word, fontsize=size) > width:
                    count = max(1, int(len(word) * width / fitz.get_text_length(word, fontsize=size)))
                    result.append(word[:count])
                    word = word[count:]
                line = word
        result.append(line)
    return result


def _appendix(doc, title, paragraphs, business, stamp):
    lines = []
    for paragraph in paragraphs:
        lines.extend(_lines(paragraph, 878))
        lines.append("")
    for start in range(0, len(lines), 25):
        page = _slide(doc, title + (" - continued" if start else ""), business, stamp)
        _text(page, (40, 112, 920, 493), "\n".join(lines[start:start + 25]), size=11)


def render_investor_presentation_pdf(*, intake: Any, financials: dict, generated_at=None) -> bytes:
    """Landscape PDF slides share the exact source snapshot of the lender packet."""
    import fitz

    doc = fitz.open()
    business = _clean(getattr(intake, "business_name", None) or "Business financing file")
    stamp = (generated_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    terms = _record(financials.get("proposed_terms"))
    returns = investment_metrics(terms)
    earnings = _record(financials.get("operating_earnings"))
    improvement = _record(earnings.get("improvement_plan"))
    metrics = _record(financials.get("current_key_metrics"))
    plan = _record(financials.get("plan"))
    impact = _record(plan.get("impact"))
    projection = _record(improvement.get("projection"))
    proposed = _record(projection.get("proposed"))
    historical = _record(earnings.get("historical"))
    source_currency = historical.get("currency")
    noi_currency = _record(_record(earnings.get("noi")).get("historical")).get("currency")
    plan_currency = improvement.get("currency") or _record(improvement.get("baseline")).get("currency")
    page = _slide(doc, "Investment and borrower impact", business, stamp)
    _text(page, (36, 108, 920, 145), business, size=21, bold=True)
    budget = _record(financials.get("use_of_funds"))
    requested = budget.get("requested_amount") if budget.get("requested_amount") is not None else intake_requested_amount(intake)
    _card(page, (36, 160, 319, 335), "Requested financing", _money(requested), "The current application request; it may differ from the offered investment.")
    _card(page, (337, 160, 620, 335), "Amount to invest", _money(returns["investment_amount"]), "Full proposed loan amount. Investor allocation is confirmed separately.")
    roi = returns["gross_contractual_roi_pct"]
    _card(page, (638, 160, 922, 335), "Gross contractual return", f"{roi:.2f}%" if roi is not None else "Terms needed", "Over the entire loan term; before fees, losses and taxes. Net investor ROI is pending.")
    _text(page, (40, 357, 915, 409), terms.get("describe") or "No complete repayment terms are recorded. Record amount, APR, maturity, amortization and balloon terms before evaluating investor return.", size=13)
    _text(page, (40, 432, 915, 486), returns["note"], size=10, color=MUTE)

    page = _slide(doc, "Financial effect: current, refinance and operating plan", business, stamp)
    _text(page, (40, 110, 920, 143), "Signed negative ratios remain losses. Refinancing changes debt service; operating actions require their own evidence.", size=12, color=MUTE)
    headings = [(40, 330, "METRIC"), (345, 535, "CURRENT"), (550, 735, "REFINANCE"), (750, 920, "OPERATING PROPOSALS")]
    for x0, x1, label in headings:
        _text(page, (x0, 160, x1, 189), label, size=10, bold=True, color=MUTE)
    earnings_value = metrics.get("dscr_earnings") if metrics.get("dscr_earnings") is not None else historical.get("annualized_value")
    ebitda_impact = _record(impact.get("ebitda"))
    rows = [("NOI / operating income", metrics.get("annual_noi"), _record(_record(earnings.get("noi")).get("scenario")).get("plan") if plan.get("active") else None, proposed.get("noi"), lambda value: _money(value, noi_currency)),
            ("Annual EBITDA (validated P&L)", metrics.get("annual_ebitda"), ebitda_impact.get("annual_after") if metrics.get("annual_ebitda") is not None else None, operating_ebitda_projection(financials), _money),
            ("Coverage earnings basis", earnings_value, _record(impact.get("income_annual")).get("after"), proposed.get("earnings"), lambda value: _money(value, source_currency)),
            ("Signed DSCR", metrics.get("dscr"), _record(impact.get("dscr")).get("after"), proposed.get("dscr_current"), _ratio),
            ("Monthly debt service", metrics.get("monthly_debt_service"), _record(impact.get("debt_service_monthly")).get("after"), None, _money)]
    for index, (label, current, after, operating, render) in enumerate(rows):
        y = 196 + index * 43
        if index % 2 == 0:
            page.draw_rect(fitz.Rect(34, y - 4, 924, y + 34), fill=PALE, color=PALE)
        _text(page, (40, y, 330, y + 30), label, size=12)
        for x0, x1, value in ((345, 535, current), (550, 735, after), (750, 920, operating)):
            _text(page, (x0, y, x1, y + 30), render(value), size=13, bold=True, color=RED if _number(value) is not None and float(value) < 0 else INK)
    reviewed_ebitda = operating_ebitda_projection(financials, "approved")
    reviewed_note = " Staff-reviewed operating EBITDA projection: " + _money(reviewed_ebitda) + "." if reviewed_ebitda is not None else ""
    _text(page, (40, 431, 920, 491), "Coverage source: " + _clean(metrics.get("dscr_earnings_label") or metrics.get("dscr_basis") or "Pending evidence") + ". Tax-derived earnings estimates are not validated P&L EBITDA. Operating proposals are separate conditional illustrations and are not combined with refinance coverage unless reconciled." + reviewed_note, size=10, color=MUTE)

    page = _slide(doc, "Repayment and principal exposure", business, stamp)
    _card(page, (36, 112, 319, 265), "Contractual receipts", _money(returns["total_contractual_receipts"]), "Principal plus finance income, assuming full performance.")
    _card(page, (337, 112, 620, 265), "Gross finance income", _money(returns["gross_finance_income"]), "Contractual receipts less original loan principal.")
    _card(page, (638, 112, 922, 265), "Due at maturity", _money(returns["maturity_balloon"]), "Residual principal must be repaid or refinanced at maturity.")
    schedule_rows = terms.get("schedule_rows") or []
    if schedule_rows and returns["investment_amount"]:
        values = [max(0, _number(row.get("balance_after_cash")) if row.get("balance_after_cash") is not None else (_number(row.get("balance")) or 0)) for row in schedule_rows]
        indices = list(range(0, len(values), max(1, math.ceil(len(values) / 48))))
        if indices[-1] != len(values) - 1:
            indices.append(len(values) - 1)
        sampled = [values[index] for index in indices]
        maximum = returns["investment_amount"]
        _text(page, (40, 282, 920, 308), "Remaining principal after each scheduled payment (including scheduled and maturity balloons)", size=11, color=MUTE)
        for index, value in enumerate(sampled):
            x = 42 + index * 876 / max(1, len(sampled))
            height = max(1, min(120, value / maximum * 120))
            page.draw_rect(fitz.Rect(x, 435 - height, x + max(2, 876 / len(sampled) - 3), 435), fill=TEAL, color=TEAL)
        _text(page, (40, 443, 920, 465), "First payment to maturity: " + _money(values[-1]) + " remains after all due principal, including the balloon, is received.", size=10, color=MUTE)
        _text(page, (40, 476, 920, 496), "Review balloon affordability, source currency, payment-period coverage and repayment evidence.", size=9, color=MUTE)
    else:
        _text(page, (40, 306, 920, 432), "A repayment schedule is not available yet. Amount, APR, payment frequency, loan maturity and amortization are needed to show principal and interest, return and balloon risk.", size=18, color=MUTE)

    disclosure = _record(improvement.get("disclosure"))
    page = _slide(doc, "Operating costs and earnings improvement", business, stamp)
    snapshot = _record(improvement.get("snapshot"))
    _text(page, (40, 110, 920, 155), snapshot.get("label") or ("Published operating plan" if improvement else "Operating plan not included or not yet available"), size=15, bold=True, color=TEAL)
    costs = disclosure.get("cost_mix") or []
    costs = sorted([row for row in costs if isinstance(row, dict)], key=lambda row: _number(row.get("annual_amount")) or 0, reverse=True)[:5]
    maximum = max([_number(row.get("annual_amount")) or 0 for row in costs] + [1])
    for index, row in enumerate(costs):
        y = 171 + index * 45
        _text(page, (40, y, 270, y + 27), str(row.get("category") or "Cost").replace("_", " ").title(), size=12)
        amount = _number(row.get("annual_amount"))
        if amount is not None and amount > 0:
            page.draw_rect(fitz.Rect(284, y, 284 + 440 * amount / maximum, y + 22), fill=TEAL, color=TEAL)
        _text(page, (744, y, 920, y + 29), _money(amount, plan_currency), size=11, bold=True)
        if row.get("missing_count") and amount is not None:
            _text(page, (284, y + 24, 725, y + 40), "Known subtotal; additional cost inputs are unresolved", size=8, color=MUTE)
    if not costs:
        _text(page, (40, 176, 920, 303), "No quantified operating cost disclosure is included. Collect the current cost table or explicitly include the working plan to show unresolved proposals.", size=17, color=MUTE)
    _text(page, (40, 416, 920, 489), f"{len(improvement.get('expenses') or [])} disclosed expense rows; {len(improvement.get('adjustments') or [])} proposed actions. Missing amounts are unknown, not zero. Current historical figures remain unchanged. A plan is recommended for signed DSCR below 1.10x or negative earnings; staff may request or include one at any coverage level.", size=12, color=MUTE)

    details = []
    for row in improvement.get("expenses") or []:
        amount = row.get("effective_period_amount") if "effective_period_amount" in row else row.get("current_amount")
        granular = ""
        if row.get("cost_basis") == "per_unit":
            granular = f"; per {row.get('unit_label') or 'unit'}: quantity {row.get('current_units_per_period')}, unit cost {_money(row.get('current_unit_cost'), plan_currency)}, fixed cost {_money(row.get('current_fixed_amount'), plan_currency)}"
        cadence = {"annual": "year", "monthly": "month", "weekly": "week", "quarterly": "quarter", "biweekly": "two weeks", "semi_monthly": "half month", "one_time": "one-time period"}.get(row.get("cadence"), "unknown period")
        details.append(f"{row.get('label') or 'Expense'} | {str(row.get('category') or '').replace('_', ' ')} | {_money(amount, plan_currency)} per {cadence}{granular}. {row.get('notes') or ''} Evidence: {', '.join(row.get('source_names') or []) or 'not linked'}.")
    if details:
        _appendix(doc, "Operating expense disclosure", details, business, stamp)
    actions = []
    for index, row in enumerate(improvement.get("adjustments") or [], 1):
        actions.append(f"{index}. {row.get('label') or 'Operating adjustment'} | {row.get('review_state') or 'proposed'} | annual change {_money(row.get('net_annual_impact'), plan_currency)}; first-year change {_money(row.get('first_year_impact'), plan_currency)}; effective {row.get('effective_date') or 'unknown'}. {row.get('explanation') or ''}")
        actions.append("Evidence and assumptions: " + str(row.get("source_note") or "not supplied") + "; linked files: " + ", ".join(row.get("source_names") or []))
        actions.extend("Still needed: " + str(issue) for issue in row.get("issues") or [])
    if actions:
        _appendix(doc, "Earnings actions and supporting explanations", actions, business, stamp)
    allocation = [f"{str(category).replace('_', ' ').title()}: {_money(amount)}" for category, amount in _record(budget.get("category_totals")).items()]
    if allocation:
        allocation.insert(0, f"Current requested financing {_money(requested)}; allocated {_money(budget.get('total'))}; unallocated {_money(budget.get('unallocated_amount'))}. Declared budget; payoff figures require current reviewed evidence.")
        _appendix(doc, "Use of financing", allocation, business, stamp)
    if _record(terms.get("risk")).get("words"):
        _appendix(doc, "Repayment and liquidity risk detail", [terms["risk"]["words"]], business, stamp)
    missing = [str(row.get("label") or row.get("requirement_key")) for row in _record(financials.get("package_readiness")).get("requirements", []) if row.get("effectively_open")]
    missing.extend(str(item) for item in improvement.get("missing") or [])
    missing.extend(str(item) for item in improvement.get("risk_flags") or [])
    if not terms:
        missing.append("Proposed investment terms, allocation, pricing, amortization and maturity are not recorded.")
    if metrics.get("annual_noi") is None:
        missing.append("A reconciled business operating-income statement is needed for NOI.")
    if metrics.get("annual_ebitda") is None:
        missing.append("Validated P&L EBITDA is unavailable; any tax-derived coverage earnings remain a separately labelled estimate.")
    _appendix(doc, "Open evidence and review conditions", missing or ["No open conditions recorded in this snapshot; final underwriting and investment review still apply."], business, stamp)
    if improvement.get("plan_notes"):
        _appendix(doc, "Staff working plan: assumptions and sensitivities", [improvement["plan_notes"]], business, stamp)
    for index, page in enumerate(doc):
        _text(page, (860, 518, 930, 536), f"{index + 1} / {len(doc)}", size=8, color=MUTE)
    doc.set_metadata({"title": business + " - Investor presentation", "author": "Qualified Commercial", "subject": "Source-bound financing, return and borrower-impact review"})
    return doc.tobytes(garbage=4, deflate=True)
