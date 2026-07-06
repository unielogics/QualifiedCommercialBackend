from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _records(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value if str(item).strip()] if isinstance(value, list) else []


def _num(value: Any) -> float | None:
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


def _money(value: float | None) -> str:
    if value is None:
        return "Awaiting evidence"
    return f"${value:,.0f}"


def _metric(value: Any, *, kind: str = "money") -> str:
    numeric = _num(value)
    if numeric is None:
        return "Awaiting evidence"
    if kind == "ratio":
        return f"{numeric:.2f}x"
    if kind == "percent":
        return f"{numeric:.1f}%"
    return _money(numeric)


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


def _bar(width: float, color: str) -> str:
    safe_width = max(0, min(width, 100))
    return f'<div class="track"><div class="fill" style="width:{safe_width:.1f}%;background:{color}"></div></div>'


def _rows(title: str, items: list[str], tone: str) -> str:
    if not items:
        body = '<div class="empty">Awaiting review extraction.</div>'
    else:
        body = "".join(f'<div class="row {tone}">{escape(item)}</div>' for item in items[:8])
    return f'<section class="card"><h2>{escape(title)}</h2>{body}</section>'


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
    chunks = [lines[index:index + 48] for index in range(0, len(lines), 48)] or [["Dealer Underwriting Intelligence"]]
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


def render_dealer_intelligence_pdf(*, intake: Any, files: list[Any], missing_docs: list[Any], result: dict[str, Any] | None) -> bytes:
    result = result or {}
    key_metrics = _record(result.get("key_metrics"))
    bankability = _record(result.get("bankability_assessment"))
    evidence = _record(result.get("document_evidence_map"))
    requested = _num(getattr(intake, "requested_loan_amount", None))
    annualized = _num(key_metrics.get("ytd_annualized_revenue")) or _num(key_metrics.get("annualized_adjusted_deposits"))
    debt = _num(key_metrics.get("estimated_debt_burden"))
    cash_flow = _num(key_metrics.get("estimated_ebitda_or_cash_flow"))
    dscr = _num(key_metrics.get("estimated_dscr"))
    collateral_value, collateral_debt = _asset_totals(intake)
    ltv = None
    if collateral_value and (collateral_debt or requested):
        ltv = ((collateral_debt or 0) + (requested or 0)) / collateral_value * 100
    equity = collateral_value - (collateral_debt or 0) if collateral_value else None
    status = str(result.get("probability_status") or bankability.get("status") or "Awaiting review")
    confidence = str(result.get("confidence") or "Awaiting review")
    next_step = str(result.get("one_next_step") or bankability.get("reason") or "Upload Stage 1 evidence to populate the cockpit.")
    max_bar = max(abs(annualized or 0), abs(cash_flow or 0), abs(debt or 0), 1)
    cash_rows = [
        ("Annualized revenue", annualized, "#21D3C7"),
        ("Estimated cash flow", cash_flow, "#34D399"),
        ("Debt burden", debt, "#F59E0B"),
    ]
    coverage_rows = _records(evidence.get("baseline_coverage"))
    if not coverage_rows:
        coverage_rows = [{"category": getattr(doc, "name", "Missing item"), "status": "missing", "gap": getattr(doc, "description", "")} for doc in missing_docs]
    missing_rows = _records(result.get("missing_or_incomplete_items"))
    if not missing_rows:
        missing_rows = [{"title": getattr(doc, "name", "Missing item"), "detail": getattr(doc, "description", ""), "priority": "high"} for doc in missing_docs]
    coverage_html = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('category') or 'Evidence'))}</td>"
        f"<td>{escape(str(row.get('status') or 'unclear'))}</td>"
        f"<td>{escape(' | '.join(map(str, row.get('evidence', []))) if isinstance(row.get('evidence'), list) else str(row.get('evidence') or row.get('gap') or ''))}</td>"
        "</tr>"
        for row in coverage_rows[:10]
    )
    missing_html = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('title') or 'Missing item'))}</td>"
        f"<td>{escape(str(row.get('priority') or 'open'))}</td>"
        f"<td>{escape(str(row.get('detail') or ''))}</td>"
        "</tr>"
        for row in missing_rows[:10]
    )
    cash_html = "".join(
        f'<div class="barrow"><strong>{escape(label)}</strong><span>{_money(value)}</span>{_bar((abs(value or 0) / max_bar) * 100 if value else 0, color)}</div>'
        for label, value, color in cash_rows
    )
    dscr_width = (min(max(dscr or 0, 0), 3) / 3) * 100
    ltv_width = min(max(ltv or 0, 0), 100)
    generated = datetime.utcnow().strftime("%b %d, %Y %I:%M %p UTC")
    fallback_lines = [
        "Dealer Underwriting Intelligence",
        f"Dealer: {getattr(intake, 'business_name', None) or getattr(intake, 'full_name', '') or 'Dealer review'}",
        f"Generated: {generated}",
        f"Status: {status}",
        f"Requested capital: {_money(requested)}",
        f"Annualized revenue: {_money(annualized)}",
        f"Debt burden: {_money(debt)}",
        f"DSCR estimate: {_metric(dscr, kind='ratio')}",
        f"Proposed LTV: {_metric(ltv, kind='percent')}",
        f"Collateral equity: {_money(equity)}",
        f"Confidence: {confidence}",
        "",
        "Evidence coverage:",
    ]
    for row in coverage_rows[:10]:
        fallback_lines.extend(_wrap(f"- {row.get('category') or 'Evidence'}: {row.get('status') or 'unclear'}; {row.get('gap') or row.get('evidence') or ''}"))
    fallback_lines.append("")
    fallback_lines.append("Still needed:")
    for row in missing_rows[:10]:
        fallback_lines.extend(_wrap(f"- {row.get('title') or 'Missing item'} ({row.get('priority') or 'open'}): {row.get('detail') or ''}"))
    fallback_lines.append("")
    fallback_lines.extend(_wrap(f"Next underwriting move: {next_step}"))
    fallback_lines.append("")
    fallback_lines.extend(_wrap("Preliminary screen only. This report is generated from uploaded documents, chat answers, and AI extraction. It is not a commitment to lend, final underwriting approval, or verified credit decision."))
    html = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    @page {{ size: Letter; margin: 28px; }}
    body {{ font-family: Inter, Arial, sans-serif; background:#070707; color:#f8fafc; font-size:12px; }}
    h1 {{ font-size:28px; margin:0; }}
    h2 {{ font-size:14px; margin:0 0 10px; color:#e9d58a; text-transform:uppercase; letter-spacing:.08em; }}
    p {{ color:#b8c4d6; line-height:1.45; }}
    .header {{ display:flex; justify-content:space-between; gap:18px; border-bottom:1px solid #252525; padding-bottom:16px; margin-bottom:16px; }}
    .pill {{ border:1px solid #334155; border-radius:999px; padding:6px 10px; color:#bffcf7; background:#06201f; font-weight:700; }}
    .grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin:14px 0; }}
    .metric {{ border:1px solid #2a2a2a; border-radius:14px; padding:12px; background:#111; min-height:72px; }}
    .metric span {{ color:#94a3b8; text-transform:uppercase; font-size:10px; font-weight:800; }}
    .metric strong {{ display:block; margin-top:8px; font-size:20px; }}
    .twocol {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
    .card {{ border:1px solid #2a2a2a; border-radius:16px; padding:14px; background:#101010; margin-bottom:12px; }}
    .track {{ height:10px; background:#222; border-radius:999px; overflow:hidden; margin-top:6px; }}
    .fill {{ height:100%; border-radius:999px; }}
    .barrow {{ margin:10px 0; }}
    .barrow span {{ float:right; color:#d9e5f5; font-weight:700; }}
    table {{ width:100%; border-collapse:collapse; margin-top:8px; }}
    td, th {{ border-bottom:1px solid #252525; padding:8px 6px; vertical-align:top; text-align:left; }}
    th {{ color:#94a3b8; text-transform:uppercase; font-size:10px; }}
    .row {{ border:1px solid #2a2a2a; border-radius:10px; padding:8px; margin:6px 0; line-height:1.35; }}
    .green {{ background:#06201f; color:#bffcf7; }}
    .amber {{ background:#241a05; color:#f6e7a6; }}
    .empty {{ color:#94a3b8; border:1px dashed #303030; border-radius:12px; padding:14px; text-align:center; }}
    .note {{ color:#8fa0b8; font-size:10px; margin-top:16px; }}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>Dealer Underwriting Intelligence</h1>
      <p>{escape(getattr(intake, "business_name", None) or getattr(intake, "full_name", "") or "Dealer review")} · generated {escape(generated)}</p>
    </div>
    <div class="pill">{escape(status)}</div>
  </div>
  <div class="grid">
    <div class="metric"><span>Requested capital</span><strong>{_money(requested)}</strong></div>
    <div class="metric"><span>Annualized revenue</span><strong>{_money(annualized)}</strong></div>
    <div class="metric"><span>Debt burden</span><strong>{_money(debt)}</strong></div>
    <div class="metric"><span>DSCR estimate</span><strong>{_metric(dscr, kind="ratio")}</strong></div>
    <div class="metric"><span>Proposed LTV</span><strong>{_metric(ltv, kind="percent")}</strong></div>
    <div class="metric"><span>Collateral equity</span><strong>{_money(equity)}</strong></div>
    <div class="metric"><span>Confidence</span><strong>{escape(confidence)}</strong></div>
    <div class="metric"><span>Files uploaded</span><strong>{len(files)}</strong></div>
  </div>
  <div class="twocol">
    <section class="card"><h2>Debt service coverage</h2><p>{_metric(dscr, kind="ratio")}</p>{_bar(dscr_width, "#34D399")}<p>Scale capped at 3.00x.</p></section>
    <section class="card"><h2>Real estate LTV</h2><p>{_metric(ltv, kind="percent")}</p>{_bar(ltv_width, "#F59E0B")}<p>Collateral value and debt evidence determine this chart.</p></section>
  </div>
  <section class="card"><h2>Cash flow stack</h2>{cash_html}</section>
  <section class="card"><h2>Evidence coverage</h2><table><tr><th>Category</th><th>Status</th><th>Evidence / gap</th></tr>{coverage_html}</table></section>
  <section class="card"><h2>Still needed</h2><table><tr><th>Item</th><th>Priority</th><th>Why it matters</th></tr>{missing_html}</table></section>
  <div class="twocol">
    {_rows("Strengths", _strings(result.get("strengths")), "green")}
    {_rows("Risks", _strings(result.get("risks")), "amber")}
  </div>
  <section class="card"><h2>Next underwriting move</h2><p>{escape(next_step)}</p></section>
  <p class="note">Preliminary screen only. This report is generated from uploaded documents, chat answers, and AI extraction. It is not a commitment to lend, final underwriting approval, or verified credit decision.</p>
</body>
</html>
"""
    try:
        from weasyprint import HTML

        pdf = HTML(string=html).write_pdf()
        if pdf is None:
            raise RuntimeError("weasyprint returned no PDF bytes")
        return pdf
    except ModuleNotFoundError:
        return _minimal_pdf(fallback_lines)
