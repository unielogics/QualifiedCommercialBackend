"""Deterministic investor slides; incomplete terms never imply an investor return."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from functools import lru_cache
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
    value = str(value or "").translate(str.maketrans({"\u2014": "-", "\u2013": "-", "\u2212": "-", "\u2192": "to", "\u00a0": " "}))
    # Source names and staff explanations can contain identifiers. The credit
    # section additionally uses an explicit aggregate-only field allowlist.
    value = re.sub(r"\b\d{3}[- ]\d{2}[- ]\d{4}\b", "[identifier withheld]", value)
    value = re.sub(r"\b\d{2}-\d{7}\b", "[identifier withheld]", value)
    value = re.sub(r"\b\d{9,19}\b", "[identifier withheld]", value)
    value = re.sub(r"\b(?:\d{4}[- ]){3}\d{4}\b", "[account withheld]", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[contact withheld]", value)
    value = re.sub(r"(?:\+1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b", "[contact withheld]", value)
    value = re.sub(r"(?i)\b(?:ssn|ein|account|acct|routing)\s*(?:number|no\.?|#)?\s*[:#-]?\s*[\dXx*][\dXx* .-]{2,}", "[identifier withheld]", value)
    value = re.sub(r"(?i)\b(?:dob|date of birth)\s*[:#-]?\s*[0-9/-]+", "[private date]", value)
    value = re.sub(r"https?://\S*(?:X-Amz-Signature|X-Amz-Credential|AWSAccessKeyId|[?&]token=)\S*", "[access-controlled source link]", value, flags=re.I)
    return value.encode("cp1252", "replace").decode("cp1252")


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


def _text(page, rect, text, *, size=10, color=INK, bold=False):
    """Fit readable summaries into fixed frames; full detail stays in the file."""
    import fitz

    text = _clean(text)
    font = "hebo" if bold else "helv"
    box = fitz.Rect(*rect)
    if box.height < 18 and "\n" not in text:
        font_size = size if size * 1.375 <= box.height else max(8.5, size - 1)
        if font_size * 1.375 > box.height:
            raise ValueError("Investor report row is too short for readable text")
        if fitz.get_text_length(text, fontsize=font_size, fontname=font) > box.width:
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                if fitz.get_text_length(text[:middle] + "...", fontsize=font_size, fontname=font) <= box.width:
                    low = middle
                else:
                    high = middle - 1
            text = text[:low].rstrip() + "..."
        page.insert_text((box.x0, box.y0 + font_size * 1.075), text,
                         fontsize=font_size, fontname=font, color=color)
        return

    def insert(value, font_size, commit=False):
        shape = page.new_shape()
        remaining = shape.insert_textbox(fitz.Rect(*rect), value, fontsize=font_size,
                                        fontname=font, color=color, lineheight=1.2)
        if commit and remaining >= 0:
            shape.commit()
        return remaining >= 0

    for font_size in (size, max(8.5, size - 1)):
        if insert(text, font_size, True):
            return
    font_size = max(8.5, size - 1)
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if insert(text[:middle].rstrip() + "...", font_size):
            low = middle
        else:
            high = middle - 1
    shortened = text[:low].rstrip()
    if " " in shortened and low < len(text):
        shortened = shortened.rsplit(" ", 1)[0]
    if not insert(shortened + "...", font_size, True):
        raise ValueError("Investor report frame cannot fit its readable summary")


def _rect(page, rect, *, fill=PALE, stroke=None):
    import fitz

    page.draw_rect(fitz.Rect(*rect), fill=fill, color=stroke or fill)


@lru_cache(maxsize=1)
def _logo_image():
    import fitz

    # Same QCMark geometry as the shared brand helper. Flat fills avoid SVG
    # engines that render gradients as black, concealing the teal C.
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="132" height="132"><rect width="512" height="512" rx="115" fill="#0b1c3b"/><circle cx="200" cy="240" r="120" fill="none" stroke="#ffffff" stroke-width="52"/><path d="M280 320 L350 400" stroke="#ffffff" stroke-width="52"/><path d="M460 140 A130 130 0 1 0 460 370" fill="none" stroke="#18a89f" stroke-width="52"/></svg>'
    document = fitz.open(stream=svg.encode(), filetype="svg")
    return document[0].get_pixmap(alpha=True).tobytes("png")


def _logo(page):
    page.insert_image((30, 17, 64, 51), stream=_logo_image())


def _slide(doc, title, business, stamp, number):
    import fitz

    page = doc.new_page(width=WIDTH, height=HEIGHT)
    _rect(page, (0, 0, WIDTH, 73), fill=NAVY)
    _logo(page)
    _text(page, (78, 16, 874, 32), "QUALIFIED COMMERCIAL  /  INVESTOR REVIEW", size=9, color=(.62, .84, .85), bold=True)
    _text(page, (78, 34, 920, 65), title, size=21, color=(1, 1, 1), bold=True)
    _rect(page, (30, 72, 119, 76), fill=TEAL)
    # This is deliberately not a best-effort post-processing step. Each page
    # is created with its watermark and CONFIDENTIAL footer before content.
    pivot = fitz.Point(WIDTH / 2, HEIGHT / 2)
    page.insert_text((WIDTH / 2 - 230, HEIGHT / 2 + 20), "CONFIDENTIAL", fontsize=66,
                     fontname="hebo", color=(.62, .68, .78), fill_opacity=.10,
                     morph=(pivot, fitz.Matrix(1, 0, 0, 1, 0, 0).prerotate(25)))
    page.draw_line((30, 508), (930, 508), color=(.83, .87, .91))
    _text(page, (30, 516, 625, 535), business, size=8, color=MUTE)
    _text(page, (630, 516, 930, 535), f"CONFIDENTIAL  |  {stamp}  |  {number} / 5", size=8, color=MUTE)
    return page


def _card(page, rect, label, value, detail="", *, negative=False):
    x0, y0, x1, y1 = rect
    _rect(page, rect)
    _text(page, (x0 + 12, y0 + 10, x1 - 12, y0 + 30), label.upper(), size=9, color=MUTE, bold=True)
    _text(page, (x0 + 12, y0 + 34, x1 - 12, y0 + 76), value,
          size=23 if len(str(value)) < 20 else 15, bold=True, color=RED if negative else TEAL)
    if detail:
        _text(page, (x0 + 12, y0 + 78, x1 - 12, y1 - 5), detail, size=9, color=MUTE)


def _heading(page, x, y, width, title, refs=""):
    _text(page, (x, y, x + width, y + 23), title, size=12, bold=True)
    if refs:
        _text(page, (x, y + 23, x + width, y + 42), refs, size=8.5, color=MUTE)


def _records(value):
    return [item for item in value or [] if isinstance(item, dict)] if isinstance(value, list) else []


def _citations(report, section=None, values=None):
    refs = _records(report.get("references"))
    known = {str(item.get("id")): item for item in refs}
    file_ids = {str(item.get("file_id")): str(item.get("id")) for item in refs if item.get("file_id")}
    values = values if values is not None else _record(report.get("section_reference_ids")).get(section, [])
    ids = sorted({str(value) if str(value) in known else file_ids.get(str(value), "") for value in values or []} - {""},
                 key=lambda value: (int(value[1:]) if re.fullmatch(r"S\d+", value) else 999999, value))
    if not ids:
        return "Source references pending"
    groups = []
    for value in ids:
        number = int(value[1:]) if re.fullmatch(r"S\d+", value) else None
        if groups and number is not None and groups[-1][1] is not None and number == groups[-1][1] + 1:
            groups[-1] = (groups[-1][0], number)
        else:
            groups.append((value, number))
    labels = [first if end is None or first == f"S{end}" else f"{first}-S{end}" for first, end in groups]
    detail = ", ".join(labels)
    return f"Sources [{detail}]" if len(detail) <= 100 else f"Sources [{', '.join(labels[:5])}] + {len(ids)} indexed records in the file"


def _company_address(company):
    if company.get("formatted_address"):
        return _clean(company["formatted_address"])
    address = company.get("address")
    if isinstance(address, str):
        return _clean(address)
    address = _record(address)
    city = ", ".join(str(address.get(key) or "").strip() for key in ("city", "state") if address.get(key))
    return ", ".join(filter(None, [str(address.get("street") or "").strip(), " ".join(filter(None, [city, str(address.get("postal_code") or "").strip()]))])) or "Business address awaiting confirmation"


def _compact(value):
    value = _number(value)
    if value is None:
        return "Unknown"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:,.1f}m"
    if abs(value) >= 1000:
        return f"{value / 1000:,.0f}k"
    return f"{value:,.0f}"


def _months(report):
    return sorted(_records(_record(report.get("banking")).get("months")), key=lambda row: str(row.get("period") or ""))[-12:]


def _plain_terms(terms):
    if not terms:
        return "No complete repayment terms are recorded. Amount, APR, maturity, amortization and balloon terms are needed."
    schedule = _record(terms.get("schedule"))
    rate, maturity = _number(terms.get("rate_pct")), _number(terms.get("term_months"))
    io = _number(terms.get("interest_only_months"))
    amortization = _number(schedule.get("amortization_months"))
    if amortization is None and schedule and terms.get("structure") != "interest_only" and maturity is not None:
        amortization = max(0, maturity - (io or 0))
    cadence = _number(terms.get("payments_per_year")) or _number(schedule.get("periods_per_year"))
    period = {12: "monthly", 24: "twice monthly", 26: "every two weeks", 52: "weekly", 252: "per business day", 365: "daily"}.get(cadence, f"{cadence:g} payments/year" if cadence else "frequency pending")
    payment = _number(schedule.get("payment_after"))
    if payment is None:
        payment = _number(schedule.get("payment_io"))
    if payment is None:
        payment = _number(terms.get("monthly_payment"))
        period = "monthly equivalent"
    payment_text = f"${payment:,.2f}" if payment is not None else "pending"
    balloon = _number(schedule.get("balloon"))
    return (f"APR {rate:g}%" if rate is not None else "APR pending") + (f" | maturity {maturity:g} months" if maturity is not None else " | maturity pending") + ".\n" + (f"Amortization {amortization:g} months" if amortization is not None else "Amortization pending") + f" | regular payment {payment_text} {period}.\n" + (f"Interest-only first {io:g} months. " if io else "") + (f"Maturity balloon ${balloon:,.2f}." if balloon is not None else "Maturity balloon pending.")


def _reference_rows(references):
    if len(references) <= 12:
        return references
    groups = {}
    for row in references:
        used = row.get("used_for") or row.get("metric_keys") or []
        key = (row.get("kind"), tuple(sorted(str(value) for value in used)))
        groups.setdefault(key, []).append(row)
    result = []
    for (kind, used), rows in groups.items():
        if len(rows) < 3:
            result.extend(rows)
            continue
        ids = [str(row.get("id") or "") for row in rows]
        numbers = [int(value[1:]) for value in ids if re.fullmatch(r"S\d+", value)]
        consecutive = len(numbers) == len(ids) and sorted(numbers) == list(range(min(numbers), max(numbers) + 1))
        label = "Business bank statements" if kind == "bank_statement" else str(kind or "Source").replace("_", " ").title()
        periods = sorted({str(row.get("period")) for row in rows if row.get("period")})
        result.append({"id": f"S{min(numbers)}-S{max(numbers)}" if consecutive else ", ".join(ids[:3]) + ", ...",
            "label": f"{label} ({len(rows)} indexed records)", "kind": kind,
            "used_for": list(used), "period": " to ".join([periods[0], periods[-1]]) if len(periods) > 1 else periods[0] if periods else None,
            "version_label": "Individual versions", "status": "File source set"})
    # All normal deal-critical sources stay locally resolvable before an
    # overflow pointer; statement sets are already indexed month by month.
    return sorted(result, key=lambda row: (not bool(set(row.get("used_for") or []) - {"banking"}), str(row.get("id"))))


def _activity_charts(page, months):
    """Raw cash movement is not relabelled as earned revenue."""
    import fitz

    left, top, right, bottom = 44, 235, 565, 303
    _text(page, (40, 205, 565, 227), "MONTHLY DEPOSITS / WITHDRAWALS", size=9, bold=True, color=MUTE)
    _rect(page, (337, 209, 348, 216), fill=TEAL)
    _text(page, (354, 204, 420, 225), "Deposits", size=8.5, color=MUTE)
    _rect(page, (425, 209, 436, 216), fill=(.49, .58, .72))
    _text(page, (442, 204, 566, 225), "Withdrawals", size=8.5, color=MUTE)
    maximum = max([_number(row.get(key)) or 0 for row in months for key in ("deposits", "withdrawals")] + [1])
    page.draw_line((left, bottom), (right, bottom), color=(.80, .84, .89))
    group_width = (right - left) / max(1, len(months))
    for index, row in enumerate(months):
        x = left + index * group_width
        for offset, key, color in ((.13, "deposits", TEAL), (.52, "withdrawals", (.49, .58, .72))):
            value = _number(row.get(key))
            if value is not None and value >= 0:
                height = value / maximum * (bottom - top)
                page.draw_rect(fitz.Rect(x + group_width * offset, bottom - height, x + group_width * (offset + .31), bottom), fill=color, color=color)
        _text(page, (x, 307, x + group_width, 324), str(row.get("period") or "")[-5:], size=8.5, color=MUTE)
    _text(page, (left, 224, right, 240), f"Peak cash-movement scale {_compact(maximum)}", size=8.5, color=MUTE)

    left, top, right, bottom = 627, 238, 915, 303
    _text(page, (621, 205, 920, 228), "MONTH-END BALANCE", size=9, bold=True, color=MUTE)
    values = [_number(row.get("ending_balance")) for row in months]
    known = [value for value in values if value is not None]
    if not known:
        _text(page, (627, 256, 915, 301), "Ending balances are not available.", size=12, color=MUTE)
        return
    minimum, maximum = min([0, *known]), max([0, *known])
    span = maximum - minimum or 1
    zero = bottom - (0 - minimum) / span * (bottom - top)
    page.draw_line((left, zero), (right, zero), color=(.80, .84, .89))
    previous = None
    for index, value in enumerate(values):
        if value is None:
            previous = None
            continue
        point = (left + index * (right - left) / max(1, len(values) - 1), bottom - (value - minimum) / span * (bottom - top))
        if previous:
            page.draw_line(previous, point, color=TEAL, width=2)
        page.draw_circle(point, 2.4, fill=RED if value < 0 else TEAL, color=None)
        previous = point
    _text(page, (625, 307, 920, 324), f"Low {_compact(min(known))}  /  High {_compact(max(known))}; gaps remain unknown", size=8.5, color=MUTE)


def _credit_lines(credit):
    # Never iterate or serialize a raw credit object. Names, reports, account
    # data, DOB, SSN and personal addresses have no rendering path here.
    count = _number(credit.get("report_count"))
    required = _number(credit.get("required_owner_count"))
    lines = [f"Reports linked: {int(count) if count is not None else 'unknown'}"]
    current = _number(credit.get("current_required_report_count"))
    if required is not None and required > 0:
        lines.append(f"Current required reports: {int(current) if current is not None else 'pending'} / {int(required)}")
    elif required == 0 and credit.get("coverage_status") == "not_required":
        lines.append("Required-owner coverage: not required under current rule")
    else:
        lines.append("Required-owner coverage: not established")
    band = credit.get("score_band")
    bands = credit.get("score_bands") or []
    safe_bands = [str(value) for value in ([band] if band else []) + list(bands) if re.fullmatch(r"\d{3}\s*[-+]\s*\d{0,3}", str(value))]
    lines.append("Score band: " + (", ".join(dict.fromkeys(safe_bands)) or "not established"))
    allowed = {"transunion": "TransUnion", "equifax": "Equifax", "experian": "Experian"}
    raw_bureaus = credit.get("bureaus") or []
    if isinstance(raw_bureaus, str):
        raw_bureaus = [raw_bureaus]
    bureaus = [allowed[str(value).lower()] for value in raw_bureaus if str(value).lower() in allowed]
    lines.append("Bureau: " + (", ".join(dict.fromkeys(bureaus)) or "not documented"))
    raw_models = credit.get("models") or []
    if isinstance(raw_models, str):
        raw_models = [raw_models]
    models = [str(value) for value in raw_models if re.fullmatch(r"(?:FICO|VantageScore)[A-Za-z0-9 .()-]{0,24}", str(value), re.I)]
    lines.append("Model: " + (", ".join(dict.fromkeys(models)) or "not documented"))
    for key, label in (("derogatory_count", "Derogatory items"), ("delinquency_count", "Delinquent items"), ("collection_count", "Collections"), ("bankruptcy_count", "Bankruptcies")):
        value = _number(credit.get(key))
        if value is not None and value >= 0:
            lines.append(f"{label}: {int(value)}")
    flags = _number(credit.get("risk_flag_count"))
    high = _number(credit.get("high_risk_flag_count"))
    if flags is not None:
        lines.append(f"Review flags: {int(flags)}" + (f" ({int(high)} high priority)" if high is not None else ""))
    if credit.get("as_of"):
        lines.append("As of: " + _clean(credit["as_of"])[:32])
    return lines


def _open_conditions(financials, improvement, terms):
    missing = [str(row.get("label") or row.get("requirement_key")) for row in _records(_record(financials.get("package_readiness")).get("requirements")) if row.get("effectively_open")]
    missing.extend(str(item) for item in improvement.get("missing") or [])
    missing.extend(str(item) for item in improvement.get("risk_flags") or [])
    missing.extend(str(issue) for row in _records(improvement.get("adjustments")) for issue in row.get("issues") or [])
    if not terms:
        missing.append("Proposed investment amount, pricing, amortization and maturity terms are not recorded.")
    metrics = _record(financials.get("current_key_metrics"))
    if metrics.get("annual_noi") is None:
        missing.append("Reconciled business operating-income statement needed for NOI.")
    if metrics.get("annual_ebitda") is None:
        missing.append("Validated P&L EBITDA is unavailable; tax-derived coverage earnings remain a separately labelled estimate.")
    return list(dict.fromkeys(filter(None, missing)))


def render_investor_presentation_pdf(*, intake: Any, financials: dict, generated_at=None) -> bytes:
    """A fixed five-page investor brief; supporting detail stays in the file."""
    import fitz

    report = _record(financials.get("investor_report"))
    company = _record(report.get("company"))
    business = _clean(company.get("name") or getattr(intake, "business_name", None) or "Business financing file")
    stamp = (generated_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    doc = fitz.open()
    terms = _record(financials.get("proposed_terms"))
    returns = investment_metrics(terms)
    budget = _record(financials.get("use_of_funds"))
    requested = budget.get("requested_amount") if budget.get("requested_amount") is not None else intake_requested_amount(intake)
    earnings = _record(financials.get("operating_earnings"))
    improvement = _record(earnings.get("improvement_plan"))
    metrics = _record(financials.get("current_key_metrics"))
    plan = _record(financials.get("plan"))
    impact = _record(plan.get("impact"))
    historical = _record(earnings.get("historical"))
    proposed = _record(_record(improvement.get("projection")).get("proposed"))
    source_currency = historical.get("currency")
    noi_currency = _record(_record(earnings.get("noi")).get("historical")).get("currency")
    plan_currency = improvement.get("currency") or _record(improvement.get("baseline")).get("currency")

    # 1 - Identity, ask, uses, and contractual return remain distinct.
    page = _slide(doc, "Investment overview", business, stamp, 1)
    _text(page, (34, 94, 925, 137), business, size=20 if len(business) <= 90 else 16 if len(business) <= 180 else 13, bold=True)
    _text(page, (35, 139, 925, 162), _company_address(company), size=10, color=MUTE)
    profile = "  |  ".join(str(value) for value in (company.get("website"), company.get("industry"), "NAICS " + str(company["naics"]) if company.get("naics") else None) if value)
    _text(page, (35, 165, 925, 185), profile or "Business website / industry details awaiting confirmation", size=9, color=MUTE)
    roi = returns["gross_contractual_roi_pct"]
    _card(page, (34, 197, 319, 316), "Requested financing", _money(requested), "Current application request")
    _card(page, (337, 197, 622, 316), "Proposed investment", _money(returns["investment_amount"]), "Full loan; participation allocation separate")
    _card(page, (640, 197, 925, 316), "Gross contractual return", f"{roi:.2f}%" if roi is not None else "Terms needed", "Full term, before fees / losses / taxes")
    _heading(page, 35, 333, 425, "Use of funds", _citations(report, "use_of_funds"))
    allocations = list(_record(budget.get("category_totals")).items())
    colors = [TEAL, (.32, .45, .65), (.56, .68, .76), (.70, .77, .82)]
    amounts = [_number(value) for _, value in allocations]
    total = sum(value for value in amounts if value is not None and value > 0)
    x = 37
    for index, (_, value) in enumerate(allocations):
        amount = _number(value)
        if amount is not None and amount > 0 and total:
            width = amount / total * 405
            _rect(page, (x, 378, x + width, 389), fill=colors[index % len(colors)])
            x += width
    if not allocations:
        _text(page, (37, 380, 448, 421), "Allocation awaiting confirmation.", size=11, color=MUTE)
    for index, (category, amount) in enumerate(allocations[:4]):
        label = str(category).replace("_", " ").title()
        _rect(page, (37, 403 + index * 19, 44, 410 + index * 19), fill=colors[index % len(colors)])
        _text(page, (51, 397 + index * 19, 448, 418 + index * 19), f"{label}: {_money(amount)}", size=10)
    _heading(page, 486, 333, 435, "Proposed terms and return basis", _citations(report, "terms"))
    _text(page, (488, 380, 918, 432), _plain_terms(terms), size=10.5)
    _text(page, (488, 439, 918, 493), returns["note"], size=9, color=MUTE)
    allocation_note = f"Allocated {_money(budget.get('total'))}; unallocated {_money(budget.get('unallocated_amount'))}."
    if len(allocations) > 4:
        allocation_note += f" {len(allocations) - 4} additional use categories in the file."
    _text(page, (36, 482, 450, 503), allocation_note, size=8.5, color=MUTE)

    # 2 - A bounded twelve-month view with explicit missing values.
    banking = _record(report.get("banking"))
    months = _months(report)
    currency = banking.get("currency")
    page = _slide(doc, "Statement activity - month by month", business, stamp, 2)
    deposits = [_number(row.get("deposits")) for row in months]
    average = sum(deposits) / len(deposits) if deposits and all(value is not None for value in deposits) else None
    ending = months[-1].get("ending_balance") if months else None
    _card(page, (34, 94, 319, 194), "Average monthly deposits", _money(average, currency))
    _card(page, (337, 94, 622, 194), "Latest ending balance", _money(ending, currency), negative=_number(ending) is not None and float(ending) < 0)
    count = banking.get("account_count")
    shown = banking.get("displayed_account_count")
    coverage = f"{shown} of {count} counted accounts shown" if shown is not None and count is not None else f"{count if count is not None else 'Unknown'} counted accounts"
    _card(page, (640, 94, 925, 194), "Statement coverage", f"{len(months)} months", f"{coverage}; {currency or 'currency unconfirmed'}")
    if months:
        _activity_charts(page, months)
        columns = [(38, 119, "MONTH"), (123, 252, "DEPOSITS"), (256, 385, "WITHDRAWALS"), (389, 519, "NET CASH"), (523, 660, "ENDING BALANCE"), (664, 738, "NSF / OD"), (742, 920, "SOURCES")]
        for x0, x1, label in columns:
            _text(page, (x0, 328, x1, 345), label, size=8.5, color=MUTE, bold=True)
        for index, row in enumerate(months):
            y = 347 + index * 11.4  # At most twelve rows at a readable 8.5pt.
            if index % 2 == 0:
                _rect(page, (34, y - 1, 925, y + 11), fill=PALE)
            values = [str(row.get("period") or "Unknown"), *[f"{float(row[key]):,.0f}" if _number(row.get(key)) is not None else "Unknown" for key in ("deposits", "withdrawals", "net_cash_flow", "ending_balance")], str(row.get("nsf_count")) if row.get("nsf_count") is not None else "Unknown", _citations(report, values=row.get("source_ids") or [])]
            for (x0, x1, _), value in zip(columns, values, strict=True):
                _text(page, (x0, y, x1, y + 13), value, size=8.5)
    else:
        _text(page, (41, 237, 915, 334), "Current, comparable statement activity is not available yet. Upload or re-read statements and confirm the counted business accounts to populate monthly activity.", size=18, color=MUTE)
    notes = banking.get("coverage_notes") or banking.get("notes") or []
    note = " ".join(str(value) for value in notes) if isinstance(notes, list) else str(notes)
    scope_note = f"Only {shown} of {count} counted accounts shown. " if shown is not None and count is not None and shown < count else ""
    _text(page, (36, 491, 925, 505), scope_note + "Cash movement includes transfers and is not earned revenue. " + _citations(report, "banking") + ". " + note, size=8.5, color=MUTE)

    # 3 - Current and conditional projections share one clearly labelled table.
    page = _slide(doc, "Earnings, coverage and operating actions", business, stamp, 3)
    snapshot = _record(improvement.get("snapshot"))
    _text(page, (35, 94, 925, 121), snapshot.get("label") or "Published financial evidence; operating plan not included or unavailable", size=11, bold=True, color=TEAL)
    headings = [(40, 333, "ANNUAL / MONTHLY BASIS"), (345, 530, "CURRENT"), (546, 730, "REFINANCE"), (745, 922, "OPERATING PROPOSALS")]
    for x0, x1, label in headings:
        _text(page, (x0, 131, x1, 152), label, size=9, bold=True, color=MUTE)
    earnings_value = metrics.get("dscr_earnings") if metrics.get("dscr_earnings") is not None else historical.get("annualized_value")
    rows = [
        ("NOI / operating income", metrics.get("annual_noi"), _record(_record(earnings.get("noi")).get("scenario")).get("plan") if plan.get("active") else None, proposed.get("noi"), lambda value: _money(value, noi_currency)),
        ("Annual EBITDA (validated P&L)", metrics.get("annual_ebitda"), _record(impact.get("ebitda")).get("annual_after") if metrics.get("annual_ebitda") is not None else None, operating_ebitda_projection(financials), _money),
        ("Coverage earnings basis", earnings_value, _record(impact.get("income_annual")).get("after"), proposed.get("earnings"), lambda value: _money(value, source_currency)),
        ("Signed DSCR", metrics.get("dscr"), _record(impact.get("dscr")).get("after"), proposed.get("dscr_current"), _ratio),
        ("Monthly debt service", metrics.get("monthly_debt_service"), _record(impact.get("debt_service_monthly")).get("after"), None, _money),
    ]
    for index, (label, current, after, operating, render) in enumerate(rows):
        y = 159 + index * 30
        if index % 2 == 0:
            _rect(page, (34, y - 3, 925, y + 25))
        _text(page, (40, y, 333, y + 24), label, size=10)
        for x0, x1, value in ((345, 530, current), (546, 730, after), (745, 922, operating)):
            _text(page, (x0, y, x1, y + 24), render(value), size=11, bold=True, color=RED if _number(value) is not None and float(value) < 0 else INK)
    source_note = "Coverage source: " + str(metrics.get("dscr_earnings_label") or metrics.get("dscr_basis") or "Pending evidence")
    _text(page, (36, 314, 925, 345), source_note + ". Signed negative ratios remain losses; tax-derived earnings are not validated P&L EBITDA. " + ("Validated P&L EBITDA is unavailable. " if metrics.get("annual_ebitda") is None else "") + _citations(report, "financials"), size=9, color=MUTE)
    _heading(page, 36, 352, 406, "Operating cost mix and disclosure", _citations(report, "operating_plan"))
    costs = sorted(_records(_record(improvement.get("disclosure")).get("cost_mix")), key=lambda row: _number(row.get("annual_amount")) or 0, reverse=True)[:4]
    maximum = max([_number(row.get("annual_amount")) or 0 for row in costs] + [1])
    for index, row in enumerate(costs):
        y = 399 + index * 19
        amount = _number(row.get("annual_amount"))
        _text(page, (38, y, 172, y + 18), str(row.get("category") or "Cost").replace("_", " ").title(), size=9)
        if amount is not None and amount > 0:
            _rect(page, (181, y + 2, 181 + 136 * amount / maximum, y + 10), fill=TEAL)
        _text(page, (326, y, 449, y + 18), _money(amount, plan_currency), size=9, bold=True)
    if not costs:
        _text(page, (38, 400, 445, 445), "Cost table or quantified amounts are not available.", size=11, color=MUTE)
    expenses = _records(improvement.get("expenses"))
    granular = next((row for row in expenses if row.get("cost_basis") == "per_unit"), None)
    cost_note = f"{len(expenses)} cost rows; full disclosure in the file."
    if granular:
        amount = granular.get("effective_period_amount") if "effective_period_amount" in granular else granular.get("current_amount")
        cadence = {"annual": "year", "monthly": "month", "weekly": "week", "quarterly": "quarter"}.get(granular.get("cadence"), "period")
        cost_note += f" Per {granular.get('unit_label') or 'unit'}: quantity {granular.get('current_units_per_period')}; {_money(amount, plan_currency)} per {cadence}."
    _text(page, (37, 479, 449, 502), cost_note, size=8.5, color=MUTE)
    _heading(page, 486, 352, 437, "Earnings actions - conditional illustrations")
    actions = _records(improvement.get("adjustments"))
    for index, row in enumerate(actions[:3]):
        y = 380 + index * 31
        explanation = str(row.get("explanation") or row.get("source_note") or "Evidence and implementation details require review.")
        _text(page, (487, y, 921, y + 29), f"{index + 1}. {row.get('label') or 'Operating adjustment'} | {row.get('review_state') or 'proposed'} | annual change {_money(row.get('net_annual_impact'), plan_currency)}. {explanation}", size=9.5)
    reviewed = operating_ebitda_projection(financials, "approved")
    detail = f"{len(actions)} total actions. Missing amounts remain unknown; timing and evidence require review."
    if reviewed is not None:
        detail = "Staff-reviewed operating EBITDA projection: " + _money(reviewed) + ". " + detail
    _text(page, (487, 477, 922, 503), detail, size=8.5, color=MUTE)

    # 4 - Principal exposure, aggregate credit and explicitly declared collateral.
    page = _slide(doc, "Repayment, credit and collateral", business, stamp, 4)
    _card(page, (34, 94, 319, 195), "Contractual receipts", _money(returns["total_contractual_receipts"]))
    _card(page, (337, 94, 622, 195), "Gross finance income", _money(returns["gross_finance_income"]))
    _card(page, (640, 94, 925, 195), "Due at maturity", _money(returns["maturity_balloon"]))
    _heading(page, 36, 208, 545, "Principal after scheduled cash payments", _citations(report, "terms"))
    schedule = _records(terms.get("schedule_rows"))
    values = [_number(row.get("balance_after_cash")) if row.get("balance_after_cash") is not None else _number(row.get("balance")) for row in schedule]
    known = [value for value in values if value is not None]
    if known and returns["investment_amount"]:
        maximum = max([float(returns["investment_amount"]), *known, 1])
        indices = list(range(0, len(values), max(1, math.ceil(len(values) / 48))))
        if indices[-1] != len(values) - 1:
            indices.append(len(values) - 1)
        for index, position in enumerate(indices):
            value = values[position]
            if value is not None and value >= 0:
                x = 39 + index * 538 / len(indices)
                height = min(69, value / maximum * 69)
                _rect(page, (x, 328 - height, x + max(2, 538 / len(indices) - 2), 328), fill=TEAL)
        if len(values) > 1 and (_number(returns.get("maturity_balloon")) or 0) > 0:
            last_x = 39 + (len(indices) - .5) * 538 / len(indices)
            previous_value = values[-2]
            if previous_value is not None:
                page.draw_line((last_x - 538 / len(indices), 328 - max(0, previous_value) / maximum * 69), (last_x, 328), color=RED, width=2)
            page.draw_circle((last_x, 328), 3, fill=RED, color=RED)
            _text(page, (280, 251, 580, 269), f"Maturity balloon {_money(returns['maturity_balloon'])} in month {terms.get('term_months') or 'pending'}", size=9, color=RED, bold=True)
        _text(page, (39, 333, 580, 358), _money(values[-1]) + " remains after all due principal, including the balloon, is received.", size=9, color=MUTE)
    else:
        _text(page, (39, 267, 580, 329), "A complete payment, amortization and balloon schedule is needed to show principal exposure.", size=13, color=MUTE)
    credit = _record(report.get("credit"))
    _heading(page, 621, 208, 300, "Credit overview", _citations(report, "credit"))
    _text(page, (623, 252, 921, 346), "\n".join(_credit_lines(credit)), size=9.5)
    _text(page, (623, 347, 921, 364), "Aggregate summary; personal identifiers withheld.", size=8.5, color=MUTE)
    collateral = _record(report.get("collateral"))
    properties = _records(collateral.get("properties")) if collateral.get("enabled") else []
    _heading(page, 36, 371, 885, "Real estate collateral", _citations(report, "collateral"))
    if not properties:
        _text(page, (38, 418, 918, 454), "No real estate collateral has been offered in this file. This does not establish that the business or its owners have no property.", size=11, color=MUTE)
    else:
        for index, row in enumerate(properties[:2]):
            y = 417 + index * 32
            if index % 2 == 0:
                _rect(page, (34, y - 3, 925, y + 29))
            address = row.get("address")
            if isinstance(address, dict):
                address = _company_address({"address": address})
            _text(page, (39, y, 370, y + 29), str(address or "Address awaiting confirmation"), size=9.5, bold=True)
            _text(page, (381, y, 629, y + 29), f"Est. value {_money(row.get('value'), row.get('currency'))}\nExisting liens {_money(row.get('existing_liens'), row.get('currency'))}", size=9)
            _text(page, (645, y, 921, y + 29), f"Declared available lien {_money(row.get('available_lien_amount'), row.get('currency'))}\nPosition {row.get('lien_position') or 'unconfirmed'}; valuation {row.get('valuation_date') or 'not supplied'}", size=9)
    _text(page, (37, 485, 922, 503), (f"{min(2, len(properties))} of {len(properties)} properties shown; full collateral schedule in the file. " if properties else "") + "Declared lien capacity is subject to title, ownership, valuation and lien review; it is not approved security.", size=8.5, color=MUTE)

    # 5 - A legible source index plus clear pointers to all retained detail.
    page = _slide(doc, "Evidence references and review conditions", business, stamp, 5)
    references = _records(report.get("references"))
    _text(page, (35, 94, 925, 130), f"{len(references)} indexed source records. Figures cite this register. Full references, all operating rows, complete notes and collateral details are retained in the originating file.", size=11)
    columns = [(39, 90, "REF"), (99, 450, "SOURCE / RECORD"), (460, 665, "USED FOR"), (675, 805, "PERIOD / VERSION"), (816, 922, "PAGE / STATUS")]
    for x0, x1, label in columns:
        _text(page, (x0, 138, x1, 157), label, size=8.5, bold=True, color=MUTE)
    indexed_rows = _reference_rows(references)
    for index, row in enumerate(indexed_rows[:12]):
        y = 162 + index * 16
        if index % 2 == 0:
            _rect(page, (34, y - 2, 925, y + 14))
        used_for = row.get("used_for") or []
        version = row.get("version")
        period_version = " / ".join(str(value) for value in (row.get("period"), row.get("version_label") or (f"v{version}" if version is not None else None)) if value)
        page_value = row.get("pages") or row.get("page")
        source_page = ",".join(str(value) for value in page_value) if isinstance(page_value, list) else str(page_value or row.get("status") or "Not cited")
        values = [row.get("id") or "", row.get("label") or "Source record", ", ".join(str(value).replace("_", " ") for value in used_for) if isinstance(used_for, list) else str(used_for), period_version or "Not supplied", source_page]
        for (x0, x1, _), value in zip(columns, values, strict=True):
            _text(page, (x0, y, x1, y + 15), value, size=8.5)
    if not references:
        _text(page, (40, 170, 917, 225), "Document-level source references have not been supplied for this snapshot. The deal team must link the supporting evidence before relying on these figures.", size=12, color=MUTE)
    reference_note = f"{len(indexed_rows)} source rows summarize {len(references)} indexed records; individual versions, pages and full IDs remain in the file register." if len(indexed_rows) <= 12 and len(indexed_rows) < len(references) else f"Showing 12 of {len(indexed_rows)} source rows; the complete {len(references)}-record register remains in the file." if len(indexed_rows) > 12 else "Exact pages, source versions and hashes are retained when supplied; missing provenance is not invented."
    reference_note = reference_note.replace("in the file register", "in the full file register")
    reference_y = max(238, min(360, 162 + min(12, len(indexed_rows)) * 16 + 10))
    _text(page, (37, reference_y, 924, reference_y + 23), reference_note, size=9, color=MUTE)
    conditions = _open_conditions(financials, improvement, terms)
    conditions_y = reference_y + 31
    items_y = conditions_y + 28
    visible_conditions = max(4, 2 * int((477 - items_y) // 29))
    _text(page, (38, conditions_y, 922, conditions_y + 22), f"REVIEW CONDITIONS  /  {len(conditions)} open items" if conditions else "REVIEW CONDITIONS", size=10, bold=True, color=TEAL)
    for index, condition in enumerate(conditions[:visible_conditions]):
        x = 38 if index % 2 == 0 else 489
        y = items_y + index // 2 * 29
        _text(page, (x, y, x + 433, y + 27), f"- {condition}", size=9)
    if not conditions:
        _text(page, (38, items_y, 920, items_y + 29), "No open conditions recorded in this snapshot. Final underwriting and investment review still apply.", size=10, color=MUTE)
    _text(page, (38, 482, 923, 503), (f"{len(conditions) - visible_conditions} additional open items are listed in the file. " if len(conditions) > visible_conditions else "") + "Indicative review; not a commitment to lend, investment recommendation or final credit approval.", size=8.5, color=MUTE)
    assert len(doc) == 5
    doc.set_metadata({"title": business + " - Confidential investor report", "author": "Qualified Commercial", "subject": "Five-page source-bound financing, activity and borrower-impact review"})
    return doc.tobytes(garbage=4, deflate=True)
