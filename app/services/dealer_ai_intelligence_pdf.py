"""Underwriting Intelligence PDF for AI underwriter leads (dealer + real estate).

Print-first LIGHT report (white background — the on-screen product is dark, but
a PDF is printed/forwarded, so it uses a document palette with brand accents).
Variant-aware: dealer leads render dealer cash-flow metrics and (internally) the
deterministic program-fit screen; real-estate leads render the deterministic
DSCR-potential screen. `internal=True` (admin export) adds internal-only
sections — program fit, DSCR scenarios, credit pull, file inventory — which the
public/borrower export must never include.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any


BRAND_TEAL = "#0F766E"
INK = "#0f172a"
INK2 = "#475569"
INK3 = "#64748b"
LINE = "#e2e8f0"
SOFT = "#f8fafc"
GOOD = "#0c7a43"
GOOD_BG = "#e7f6ee"
WARN = "#92400e"
WARN_BG = "#fdf3e0"
BAD = "#b91c1c"
BAD_BG = "#fdecec"


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _records(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value if str(item).strip()] if isinstance(value, list) else []


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    lower = value.lower().strip()
    multiplier = 1_000_000_000 if "b" in lower else 1_000_000 if "m" in lower else 1_000 if "k" in lower else 1
    negative = lower.startswith("-") or ("(" in lower and ")" in lower)
    cleaned = (
        lower.replace("$", "")
        .replace(",", "")
        .replace("%", "")
        .replace("x", "")
        .replace("k", "")
        .replace("m", "")
        .replace("b", "")
        .replace("(", "")
        .replace(")", "")
        .strip()
    )
    try:
        parsed = float(cleaned)
        return abs(parsed) * multiplier * (-1 if negative else 1)
    except ValueError:
        return None


def _money(value: Any) -> str:
    numeric = _num(value)
    return f"${numeric:,.0f}" if numeric is not None else "—"


def _ratio(value: Any) -> str:
    numeric = _num(value)
    return f"{numeric:.2f}x" if numeric is not None else "—"


def _percent(value: Any, *, already_percent: bool = False) -> str:
    numeric = _num(value)
    if numeric is None:
        return "—"
    return f"{numeric:.1f}%" if already_percent else f"{numeric * 100:.1f}%"


def _size(num_bytes: Any) -> str:
    numeric = _num(num_bytes)
    if numeric is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if numeric < 1024 or unit == "GB":
            return f"{numeric:,.0f} {unit}" if unit == "B" else f"{numeric:,.1f} {unit}"
        numeric /= 1024
    return ""


def _asset_totals(intake: Any) -> tuple[float | None, float | None]:
    rows = getattr(intake, "asset_rows", None)
    if not isinstance(rows, list):
        return None, None
    value = 0.0
    debt = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        value += float(row.get("estimated_property_value") or 0)
        debt += float(row.get("estimated_loan_amount") or 0)
    return (value if value > 0 else None, debt if debt > 0 else None)


def _status_tone(status: str) -> tuple[str, str]:
    lowered = status.lower()
    if "good probability" in lowered:
        return GOOD, GOOD_BG
    if "poor" in lowered:
        return BAD, BAD_BG
    if "promising" in lowered or "clarification" in lowered:
        return WARN, WARN_BG
    return INK2, SOFT


def _chip(text: str, color: str, bg: str) -> str:
    return (
        f'<span style="display:inline-block;border:1px solid {color};border-radius:999px;'
        f'padding:3px 10px;color:{color};background:{bg};font-weight:700;font-size:10px;">{escape(text)}</span>'
    )


def _tile(label: str, value: str, *, hint: str = "") -> str:
    hint_html = f'<div class="tile-hint">{escape(hint)}</div>' if hint else ""
    return (
        '<div class="tile">'
        f'<div class="tile-label">{escape(label)}</div>'
        f'<div class="tile-value">{escape(value)}</div>'
        f"{hint_html}"
        "</div>"
    )


def _fact(label: str, value: Any) -> str:
    text = str(value).strip() if value not in (None, "") else "—"
    return f'<div class="fact"><span>{escape(label)}</span><strong>{escape(text)}</strong></div>'


def _bar(width: float, color: str) -> str:
    safe_width = max(0.0, min(width, 100.0))
    return f'<div class="track"><div class="fill" style="width:{safe_width:.1f}%;background:{color}"></div></div>'


def _list_card(title: str, items: list[str], tone_color: str, tone_bg: str, empty_copy: str) -> str:
    if not items:
        body = f'<div class="empty">{escape(empty_copy)}</div>'
    else:
        body = "".join(
            f'<div class="row" style="background:{tone_bg};border-color:{tone_color}22;color:{INK};">{escape(item)}</div>'
            for item in items[:8]
        )
    return f'<section class="card"><h2>{escape(title)}</h2>{body}</section>'


# --------------------------------------------------------------------------
# Text-only fallback PDF (no weasyprint available) — unchanged mechanism.
# --------------------------------------------------------------------------

def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(value: str, width: int = 94) -> list[str]:
    words = value.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            if current:
                lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


def _minimal_pdf(lines: list[str]) -> bytes:
    chunks = [lines[index:index + 48] for index in range(0, len(lines), 48)] or [["Underwriting Intelligence"]]
    objects: list[bytes] = []

    def add_object(body: str) -> int:
        objects.append(body.encode("latin-1", "replace"))
        return len(objects)

    catalog_id = add_object("<< /Type /Catalog /Pages 2 0 R >>")
    pages_id = add_object("<< /Type /Pages /Kids [] /Count 0 >>")
    font_id = add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    for chunk in chunks:
        text_ops = ["BT /F1 10 Tf 40 760 Td 14 TL"]
        for line in chunk:
            text_ops.append(f"({_pdf_escape(line)}) Tj T*")
        text_ops.append("ET")
        stream = "\n".join(text_ops)
        content_id = add_object(f"<< /Length {len(stream.encode('latin-1', 'replace'))} >>\nstream\n{stream}\nendstream")
        page_id = add_object(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>")
        page_ids.append(page_id)
    objects[pages_id - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{page_id} 0 R' for page_id in page_ids)}] /Count {len(page_ids)} >>".encode("latin-1")
    objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1")

    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("latin-1"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_at = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode("latin-1"))
    return bytes(output)


# --------------------------------------------------------------------------
# Section builders
# --------------------------------------------------------------------------

def _program_fit_section(program_fit: dict[str, Any] | None) -> str:
    """Internal-only deterministic program-fit table (dealer leads)."""
    if not isinstance(program_fit, dict) or not program_fit:
        return ""
    labels = {
        "sba": "SBA",
        "real_estate_backed": "Real-estate-backed",
        "reinsurance_backed": "Reinsurance-backed",
        "jumbo_dscr": "Jumbo / DSCR",
        "term_loan_10_year": "10-Year Term Loan",
        "term_loan_3_5_year": "3-5 Year Term Loan",
        "term_loan_loc_hybrid": "Term Loan / LOC Hybrid",
        "line_of_credit": "Line of Credit",
        "equipment_financing": "Equipment Financing",
        "merchant_processing": "Merchant Processing",
        "transportation_factoring": "Transportation Factoring",
        "debt_consulting": "Debt Consulting",
    }
    rows: list[str] = []
    for key, label in labels.items():
        program = program_fit.get(key)
        if not isinstance(program, dict):
            continue
        eligible = bool(program.get("eligible"))
        details: list[str] = []
        for field, formatter in (("revenue", _money), ("dscr", _ratio), ("cash_flow", _money), ("annualized_deposits", _money)):
            if _num(program.get(field)) is not None:
                details.append(f"{field.replace('_', ' ')} {formatter(program.get(field))}")
        if program.get("note"):
            details.append(str(program.get("note"))[:110])
        chip = _chip("Eligible", GOOD, GOOD_BG) if eligible else _chip("Not yet", INK3, SOFT)
        rows.append(
            f'<tr><td style="font-weight:700;">{escape(label)}</td>'
            f"<td>{chip}</td>"
            f'<td style="color:{INK2};">{escape(" · ".join(details))}</td></tr>'
        )
    if not rows:
        return ""
    return (
        '<section class="card"><h2>Program fit (internal)</h2>'
        '<p class="mini">Deterministic screen from uploaded evidence and stated facts — not a lending decision. Never share with the borrower.</p>'
        f'<table><tr><th>Program</th><th>Screen</th><th>Basis</th></tr>{"".join(rows)}</table></section>'
    )


def _dscr_potential_section(potential: dict[str, Any] | None) -> str:
    """Internal-only deterministic DSCR-potential math (real-estate leads)."""
    if not isinstance(potential, dict) or not potential:
        return ""
    if not potential.get("computed"):
        missing = _strings(potential.get("missing"))
        if not missing:
            return ""
        items = "".join(f"<li>{escape(item)}</li>" for item in missing)
        return (
            '<section class="card"><h2>DSCR potential (internal)</h2>'
            f'<p class="mini">Not enough facts yet for the deterministic screen. Still needed:</p><ul class="mini">{items}</ul></section>'
        )
    inputs = _record(potential.get("inputs"))
    assumptions = _record(potential.get("assumptions"))
    scenario_rows = "".join(
        f"<tr><td>{_percent(row.get('annual_rate'))}</td>"
        f"<td>{_money(row.get('monthly_principal_interest'))}</td>"
        f"<td>{_money(row.get('monthly_pitia'))}</td>"
        f'<td style="font-weight:700;color:{GOOD if (_num(row.get("dscr")) or 0) >= 1 else BAD};">{_ratio(row.get("dscr"))}</td></tr>'
        for row in _records(potential.get("scenarios"))
    )
    target_rows = "".join(
        f"<tr><td>DSCR ≥ {escape(target)}</td>"
        f"<td>{_money(row.get('max_loan'))}</td>"
        f"<td>{_percent(row.get('implied_ltv'))}</td>"
        f"<td>{_money(_record(potential.get('required_monthly_rent_at_requested')).get(target))}/mo</td></tr>"
        for target, row in _record(potential.get("max_loan_at_target_dscr")).items()
    )
    rate_bands = " / ".join(_percent(rate) for rate in assumptions.get("rate_bands") or [])
    return (
        '<section class="card"><h2>DSCR potential (internal)</h2>'
        f'<p class="mini">Deterministic math from stated facts and uploaded evidence — rent {_money(inputs.get("monthly_rent"))}/mo '
        f'({escape(str(inputs.get("monthly_rent_source") or ""))}), value {_money(inputs.get("property_value"))}, '
        f'taxes+insurance {_money(inputs.get("monthly_tax_insurance_hoa"))}/mo ({escape(str(inputs.get("tax_insurance_source") or ""))}).</p>'
        f'<table><tr><th>Rate</th><th>P&amp;I</th><th>PITIA</th><th>DSCR at requested</th></tr>{scenario_rows}</table>'
        '<div style="height:8px;"></div>'
        f'<table><tr><th>Target</th><th>Max loan</th><th>Implied LTV</th><th>Rent needed at requested</th></tr>{target_rows}</table>'
        f'<p class="mini">Assumptions: {assumptions.get("amortization_months") or 360}-month amortization, rate bands {rate_bands}. '
        f'{escape(str(assumptions.get("note") or ""))}</p></section>'
    )


def _credit_section(credit: dict[str, Any] | None) -> str:
    if not isinstance(credit, dict) or not credit:
        return ""
    fico = credit.get("fico")
    if fico is None:
        return ""
    facts = [
        _fact("Soft-pull FICO", fico),
        _fact("Bureau", credit.get("bureau") or credit.get("source")),
        _fact("Pulled", str(credit.get("pulled_at") or "")[:10]),
    ]
    return f'<section class="card"><h2>Credit (internal)</h2><div class="facts">{"".join(facts)}</div></section>'


def _files_section(files: list[Any]) -> str:
    if not files:
        return ""
    rows = "".join(
        "<tr>"
        f"<td>{escape(str(getattr(file, 'zip_entry_path', None) or getattr(file, 'file_name', '') or 'File'))}</td>"
        f'<td style="white-space:nowrap;">{_size(getattr(file, "size_bytes", None))}</td>'
        f'<td style="white-space:nowrap;">{escape(str(getattr(file, "created_at", ""))[:10])}</td>'
        "</tr>"
        for file in files[:24]
    )
    more = f'<p class="mini">+ {len(files) - 24} more files in the room.</p>' if len(files) > 24 else ""
    return (
        f'<section class="card"><h2>Uploaded files ({len(files)})</h2>'
        f"<table><tr><th>File</th><th>Size</th><th>Uploaded</th></tr>{rows}</table>{more}</section>"
    )


# --------------------------------------------------------------------------
# Main renderer
# --------------------------------------------------------------------------

def render_dealer_intelligence_pdf(
    *,
    intake: Any,
    files: list[Any],
    missing_docs: list[Any],
    result: dict[str, Any] | None,
    program_fit: dict[str, Any] | None = None,
    dscr_potential: dict[str, Any] | None = None,
    credit: dict[str, Any] | None = None,
    internal: bool = False,
) -> bytes:
    result = result or {}
    is_real_estate = getattr(intake, "variant", "") == "real_estate_dscr_v1"
    variant_label = "Real Estate / DSCR Review" if is_real_estate else "Dealer Funding Review"
    key_metrics = _record(result.get("key_metrics"))
    bankability = _record(result.get("bankability_assessment"))
    evidence = _record(result.get("document_evidence_map"))
    state = getattr(intake, "intake_state", None)
    state = state if isinstance(state, dict) else {}
    basics = _record(state.get("funding_review_basics"))

    requested = _num(getattr(intake, "requested_loan_amount", None)) or _num(basics.get("requested_amount")) or _num(key_metrics.get("requested_amount"))
    status = str(result.get("probability_status") or bankability.get("status") or "Awaiting AI review")
    confidence = str(result.get("confidence") or "")
    next_step = str(result.get("one_next_step") or bankability.get("reason") or "Upload Stage 1 evidence to advance the file.")
    executive_summary = str(result.get("executive_summary") or "").strip()
    status_color, status_bg = _status_tone(status)
    generated = datetime.utcnow().strftime("%b %d, %Y %I:%M %p UTC")
    lead_name = getattr(intake, "business_name", None) or getattr(intake, "full_name", "") or "Underwriting review"

    # -- Metrics tiles (variant-aware) --------------------------------------
    if is_real_estate:
        potential_inputs = _record(_record(dscr_potential).get("inputs"))
        tiles = [
            _tile("Requested amount", _money(requested)),
            _tile("Property value", _money(potential_inputs.get("property_value") or basics.get("estimated_value_or_purchase_price"))),
            _tile("Monthly rent", _money(potential_inputs.get("monthly_rent") or basics.get("monthly_rent"))),
            _tile("DSCR at requested", _ratio(_record(dscr_potential).get("dscr_at_requested") or key_metrics.get("estimated_dscr") or key_metrics.get("dscr"))),
            _tile("LTV", _percent(_record(dscr_potential).get("ltv") or key_metrics.get("ltv"))),
            _tile("Credit tier", str(basics.get("estimated_credit_tier") or getattr(intake, "estimated_credit_score", None) or "—")),
            _tile("Transaction", str(basics.get("transaction_type") or getattr(intake, "loan_purpose", None) or "—")),
            _tile("Files uploaded", str(len(files))),
        ]
    else:
        annualized = _num(key_metrics.get("ytd_annualized_revenue")) or _num(key_metrics.get("annualized_adjusted_deposits"))
        debt = _num(key_metrics.get("estimated_debt_burden"))
        cash_flow = _num(key_metrics.get("estimated_ebitda_or_cash_flow"))
        dscr = _num(key_metrics.get("estimated_dscr"))
        collateral_value, collateral_debt = _asset_totals(intake)
        ltv = None
        if collateral_value and (collateral_debt or requested):
            ltv = ((collateral_debt or 0) + (requested or 0)) / collateral_value * 100
        equity = collateral_value - (collateral_debt or 0) if collateral_value else None
        tiles = [
            _tile("Requested capital", _money(requested), hint="" if requested is not None else "Not yet on the file — confirm in chat"),
            _tile("Annualized revenue", _money(annualized)),
            _tile("Cash flow (est.)", _money(cash_flow)),
            _tile("Debt burden", _money(debt), hint="" if debt is not None else "Debt schedule unlocks this"),
            _tile("DSCR estimate", _ratio(dscr), hint="" if dscr is not None else "Debt schedule unlocks this"),
            _tile("Collateral equity", _money(equity), hint="" if equity is not None else "Real-estate schedule unlocks this"),
            _tile("Proposed LTV", _percent(ltv, already_percent=True)),
            _tile("Files uploaded", str(len(files))),
        ]
    tiles_html = "".join(tiles)

    # -- Deal snapshot -------------------------------------------------------
    facts = [
        _fact("Contact", getattr(intake, "full_name", None)),
        _fact("Business" if not is_real_estate else "Investor / entity", getattr(intake, "business_name", None)),
        _fact("Email", getattr(intake, "email", None)),
        _fact("Phone", getattr(intake, "phone", None)),
        _fact("Use of funds", getattr(intake, "loan_purpose", None) or basics.get("transaction_type")),
        _fact("Referral", getattr(intake, "referral_source", None)),
    ]
    if is_real_estate:
        facts.append(_fact("Property", basics.get("target_property_address")))
    facts_html = "".join(facts)

    # -- Cash flow stack (dealer only, only evidenced rows) ------------------
    cash_section = ""
    if not is_real_estate:
        cash_rows = [
            ("Annualized revenue", _num(key_metrics.get("ytd_annualized_revenue")), BRAND_TEAL),
            ("Adjusted deposits (annualized)", _num(key_metrics.get("annualized_adjusted_deposits")), "#2563eb"),
            ("Estimated cash flow", _num(key_metrics.get("estimated_ebitda_or_cash_flow")), GOOD),
            ("Debt burden", _num(key_metrics.get("estimated_debt_burden")), "#d97706"),
        ]
        evidenced = [(label, value, color) for label, value, color in cash_rows if value is not None]
        if evidenced:
            max_bar = max(abs(value) for _, value, _ in evidenced) or 1
            bars = "".join(
                f'<div class="barrow"><strong>{escape(label)}</strong><span>{_money(value)}</span>{_bar(abs(value) / max_bar * 100, color)}</div>'
                for label, value, color in evidenced
            )
            trend = str(key_metrics.get("revenue_trend") or "").strip()
            consistency = str(key_metrics.get("tax_return_revenue_vs_bank_deposits") or "").strip()
            notes = " · ".join(part for part in (trend, consistency) if part)
            notes_html = f'<p class="mini">{escape(notes)}</p>' if notes else ""
            cash_section = f'<section class="card"><h2>Cash flow</h2>{bars}{notes_html}</section>'

    # -- Evidence coverage + reconciled "still needed" -----------------------
    coverage_rows = _records(evidence.get("baseline_coverage"))
    satisfied_names = {
        str(row.get("category") or "").strip().lower()
        for row in coverage_rows
        if str(row.get("status") or "").lower() in ("satisfied", "uploaded", "complete", "present")
    }
    coverage_html = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('category') or 'Evidence'))}</td>"
        f"<td>{_chip(str(row.get('status') or 'unclear'), *((GOOD, GOOD_BG) if str(row.get('status') or '').lower() in ('satisfied', 'uploaded', 'complete', 'present') else (WARN, WARN_BG)))}</td>"
        f'<td style="color:{INK2};">{escape((" | ".join(map(str, row.get("evidence", []))) if isinstance(row.get("evidence"), list) else str(row.get("evidence") or row.get("gap") or ""))[:220])}</td>'
        "</tr>"
        for row in coverage_rows[:12]
    )
    coverage_section = (
        f'<section class="card"><h2>Evidence coverage</h2><table><tr><th>Category</th><th>Status</th><th>Evidence / gap</th></tr>{coverage_html}</table></section>'
        if coverage_html
        else ""
    )

    missing_rows = _records(result.get("missing_or_incomplete_items"))
    if not missing_rows:
        missing_rows = [
            {"title": getattr(doc, "name", "Missing item"), "detail": getattr(doc, "description", ""), "priority": "high"}
            for doc in missing_docs
        ]
    # Reconcile: never list an item the evidence map already marks satisfied
    # (requested-document statuses can lag behind analyzed uploads).
    missing_rows = [
        row for row in missing_rows
        if str(row.get("title") or "").strip().lower() not in satisfied_names
    ]
    missing_html = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('title') or 'Missing item'))}</td>"
        f"<td>{_chip(str(row.get('priority') or 'open'), WARN, WARN_BG)}</td>"
        f'<td style="color:{INK2};">{escape(str(row.get("detail") or "")[:220])}</td>'
        "</tr>"
        for row in missing_rows[:12]
    )
    missing_section = (
        f'<section class="card"><h2>Still needed</h2><table><tr><th>Item</th><th>Priority</th><th>Why it matters</th></tr>{missing_html}</table></section>'
        if missing_html
        else '<section class="card"><h2>Still needed</h2><div class="empty">Nothing outstanding — the evidence checklist is complete.</div></section>'
    )

    summary_section = (
        f'<section class="card"><h2>Executive summary</h2><p style="margin:0;line-height:1.55;">{escape(executive_summary)}</p></section>'
        if executive_summary
        else ""
    )

    internal_sections = ""
    if internal:
        internal_sections = (
            (_dscr_potential_section(dscr_potential) if is_real_estate else _program_fit_section(program_fit))
            + _credit_section(credit)
            + _files_section(files)
        )

    confidence_chip = _chip(f"Confidence: {confidence}", INK2, SOFT) if confidence else ""

    # The AI's one_next_step can lag the live checklist (e.g. claim Stage 1 is
    # complete while items are still outstanding). Pin the deterministic
    # checklist state next to it so the report never contradicts itself.
    outstanding_note = ""
    if missing_rows:
        outstanding_titles = ", ".join(str(row.get("title") or "item") for row in missing_rows[:6])
        outstanding_note = (
            f'<p class="mini">Checklist still outstanding: {escape(outstanding_titles)}.</p>'
        )

    html = f"""
<html>
<head>
  <meta charset="utf-8">
  <style>
    @page {{
      size: Letter;
      margin: 34px 34px 44px;
      @bottom-left {{ content: "Qualified Commercial — Underwriting Intelligence"; font-size: 8px; color: {INK3}; }}
      @bottom-right {{ content: "Page " counter(page) " of " counter(pages); font-size: 8px; color: {INK3}; }}
    }}
    body {{ font-family: Helvetica, Arial, sans-serif; background: #ffffff; color: {INK}; font-size: 11px; margin: 0; }}
    h1 {{ font-family: Georgia, 'Times New Roman', serif; font-size: 25px; margin: 2px 0 4px; color: {INK}; }}
    h2 {{ font-size: 11px; margin: 0 0 8px; color: {BRAND_TEAL}; text-transform: uppercase; letter-spacing: .09em; }}
    .brand {{ color: {BRAND_TEAL}; font-weight: 800; letter-spacing: .18em; text-transform: uppercase; font-size: 9px; }}
    .sub {{ color: {INK2}; margin: 0; }}
    .header {{ border-bottom: 2px solid {BRAND_TEAL}; padding-bottom: 12px; margin-bottom: 14px; }}
    .header-row {{ width: 100%; }}
    .chips {{ margin-top: 8px; }}
    .chips span {{ margin-right: 6px; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 12px 0; }}
    .tile {{ border: 1px solid {LINE}; border-radius: 10px; padding: 9px 10px; background: {SOFT}; }}
    .tile-label {{ color: {INK3}; text-transform: uppercase; font-size: 8px; font-weight: 800; letter-spacing: .05em; }}
    .tile-value {{ margin-top: 5px; font-size: 15px; font-weight: 800; }}
    .tile-hint {{ margin-top: 3px; color: {INK3}; font-size: 8px; }}
    .card {{ border: 1px solid {LINE}; border-radius: 12px; padding: 12px 14px; margin-bottom: 10px; background: #ffffff; page-break-inside: avoid; }}
    .facts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 4px 18px; }}
    .fact span {{ color: {INK3}; font-size: 9px; text-transform: uppercase; letter-spacing: .04em; display: inline-block; width: 108px; }}
    .fact strong {{ color: {INK}; font-weight: 600; }}
    table {{ width: 100%; border-collapse: collapse; }}
    td, th {{ border-bottom: 1px solid {LINE}; padding: 6px 6px; vertical-align: top; text-align: left; }}
    th {{ color: {INK3}; text-transform: uppercase; font-size: 8px; letter-spacing: .05em; }}
    tr:last-child td {{ border-bottom: none; }}
    .track {{ height: 8px; background: {LINE}; border-radius: 999px; margin-top: 4px; }}
    .fill {{ height: 100%; border-radius: 999px; }}
    .barrow {{ margin: 8px 0; }}
    .barrow span {{ float: right; color: {INK}; font-weight: 700; }}
    .twocol {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
    .row {{ border: 1px solid {LINE}; border-radius: 8px; padding: 7px 9px; margin: 5px 0; line-height: 1.4; }}
    .empty {{ color: {INK3}; border: 1px dashed {LINE}; border-radius: 10px; padding: 12px; text-align: center; }}
    .mini {{ color: {INK3}; font-size: 9px; line-height: 1.5; margin: 6px 0 0; }}
    ul.mini {{ margin: 4px 0 0; padding-left: 16px; }}
    .note {{ color: {INK3}; font-size: 8.5px; margin-top: 12px; line-height: 1.5; }}
  </style>
</head>
<body>
  <div class="header">
    <div class="brand">Qualified Commercial</div>
    <h1>Underwriting Intelligence Report</h1>
    <p class="sub">{escape(str(lead_name))} · {escape(variant_label)} · generated {escape(generated)}</p>
    <div class="chips">{_chip(status, status_color, status_bg)}{confidence_chip}{_chip("Internal copy" if internal else "Client copy", BRAND_TEAL, "#e6f4f2")}</div>
  </div>
  <section class="card"><h2>Deal snapshot</h2><div class="facts">{facts_html}</div></section>
  <div class="grid">{tiles_html}</div>
  {summary_section}
  {internal_sections}
  {cash_section}
  {coverage_section}
  {missing_section}
  <div class="twocol">
    {_list_card("Strengths", _strings(result.get("strengths")), GOOD, GOOD_BG, "None extracted in the latest AI review yet.")}
    {_list_card("Risks", _strings(result.get("risks")), WARN, WARN_BG, "None extracted in the latest AI review yet.")}
  </div>
  <section class="card"><h2>Next underwriting move</h2><p style="margin:0;line-height:1.5;">{escape(next_step)}</p>{outstanding_note}</section>
  <p class="note">Preliminary screen only. This report is generated from uploaded documents, chat answers, and AI extraction. It is not a commitment to lend, final underwriting approval, or verified credit decision.</p>
</body>
</html>
"""

    fallback_lines = [
        "Underwriting Intelligence Report",
        f"Lead: {lead_name}",
        f"Variant: {variant_label}",
        f"Generated: {generated}",
        f"Status: {status}",
        f"Requested: {_money(requested)}",
        "",
    ]
    if executive_summary:
        fallback_lines.extend(_wrap(f"Summary: {executive_summary}"))
        fallback_lines.append("")
    for row in coverage_rows[:10]:
        fallback_lines.extend(_wrap(f"- {row.get('category') or 'Evidence'}: {row.get('status') or 'unclear'}"))
    fallback_lines.append("")
    for row in missing_rows[:10]:
        fallback_lines.extend(_wrap(f"Needed: {row.get('title') or 'Missing item'} — {row.get('detail') or ''}"))
    fallback_lines.append("")
    fallback_lines.extend(_wrap(f"Next underwriting move: {next_step}"))

    try:
        from weasyprint import HTML

        pdf = HTML(string=html).write_pdf()
        if pdf is None:
            raise RuntimeError("weasyprint returned no PDF bytes")
        return pdf
    except ModuleNotFoundError:
        return _minimal_pdf(fallback_lines)
