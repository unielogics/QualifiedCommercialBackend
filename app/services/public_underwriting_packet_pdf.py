from __future__ import annotations

import logging
from datetime import datetime
from html import escape
from typing import Any


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _records(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _text(value: Any, fallback: str = "Awaiting evidence") -> str:
    if value is None:
        return fallback
    # Flatten nested structures into readable prose so no PDF cell ever renders a
    # Python repr like {'dscr': 1.2} or ['a', 'b'].
    if isinstance(value, dict):
        pairs = []
        for k, v in value.items():
            flat = _text(v, fallback="")
            if flat:
                pairs.append(f"{str(k).replace('_', ' ').strip().title()}: {flat}")
        text = "; ".join(pairs)
        return text or fallback
    if isinstance(value, (list, tuple)):
        items = [_text(item, fallback="") for item in value]
        text = "; ".join(item for item in items if item)
        return text or fallback
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = str(value).strip()
    return text or fallback


def _money(value: Any) -> str:
    if value is None or value == "":
        return "Awaiting evidence"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"${number:,.0f}"


def _field(label: str, value: Any) -> str:
    return (
        '<div class="field">'
        f'<span>{escape(label)}</span>'
        f'<strong>{escape(_text(value))}</strong>'
        "</div>"
    )


def _list(title: str, items: list[str], tone: str = "") -> str:
    if not items:
        body = '<div class="empty">Awaiting evidence.</div>'
    else:
        body = "".join(f'<li>{escape(item)}</li>' for item in items[:12])
    return f'<section class="card {tone}"><h2>{escape(title)}</h2><ul>{body}</ul></section>'


def _table(title: str, rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    if not rows:
        body = f'<tr><td colspan="{len(columns)}" class="empty-cell">Awaiting evidence.</td></tr>'
    else:
        body = "".join(
            "<tr>"
            + "".join(f"<td>{escape(_text(row.get(key)))}</td>" for key, _ in columns)
            + "</tr>"
            for row in rows[:16]
        )
    headers = "".join(f"<th>{escape(label)}</th>" for _, label in columns)
    return f'<section class="card wide"><h2>{escape(title)}</h2><table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table></section>'


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(value: str, width: int = 96) -> list[str]:
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
    chunks = [lines[index:index + 48] for index in range(0, len(lines), 48)] or [["Qualified Commercial Underwriting Packet"]]
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
        page_id = add_object(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
        )
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


def render_underwriting_packet_pdf(
    *,
    intake: Any,
    files: list[Any],
    missing_docs: list[Any],
    result: dict[str, Any] | None,
    executive_summary: dict[str, Any] | None = None,
) -> bytes:
    result = result or {}
    executive_summary = executive_summary or {}
    variant = str(getattr(intake, "variant", "") or "")
    is_real_estate = variant.startswith("real_estate")
    key_metrics = _record(result.get("key_metrics"))
    bankability = _record(result.get("bankability_assessment"))
    title = "Qualified Commercial Underwriting Packet"
    subtitle = "Real estate / DSCR funding review" if is_real_estate else "Dealer capital funding review"
    borrower_name = getattr(intake, "full_name", None)
    business_name = getattr(intake, "business_name", None)
    requested_amount = _money(getattr(intake, "requested_loan_amount", None))
    purpose = getattr(intake, "loan_purpose", None)
    status = result.get("probability_status") or result.get("fundability_status") or bankability.get("status")
    program_fit = result.get("program_fit") or executive_summary.get("suggested_product_path") or executive_summary.get("recommended_approach")
    summary_text = (
        executive_summary.get("executive_summary")
        or result.get("executive_summary")
        or bankability.get("reason")
        or "The file has not produced a complete AI underwriting summary yet."
    )
    recommended_angle = (
        executive_summary.get("vendor_submission_angle")
        or executive_summary.get("submission_angle")
        or result.get("one_next_step")
        or "Awaiting final submission angle."
    )
    risks = _strings(executive_summary.get("risks")) or _strings(result.get("risks"))
    mitigants = _strings(executive_summary.get("mitigants")) or _strings(result.get("mitigants"))
    strengths = _strings(executive_summary.get("strengths")) or _strings(result.get("strengths"))
    missing_rows = _records(result.get("missing_or_incomplete_items"))
    if not missing_rows:
        missing_rows = [
            {"title": getattr(doc, "name", "Missing item"), "detail": getattr(doc, "description", ""), "priority": "open"}
            for doc in missing_docs
        ]
    evidence = _record(result.get("document_evidence_map"))
    coverage_rows = _records(evidence.get("baseline_coverage")) or _records(evidence.get("file_classifications"))
    reviewed_docs = [
        {
            "file": getattr(file, "zip_entry_path", None) or getattr(file, "file_name", "Uploaded file"),
            "type": getattr(file, "content_type", ""),
            "status": getattr(file, "status", ""),
        }
        for file in files
    ]
    metric_rows = [
        {"metric": "Probability status", "value": status or "Awaiting review", "source": "AI review"},
        {"metric": "Suggested path", "value": program_fit or "Awaiting evidence", "source": "AI review"},
        {"metric": "Requested amount", "value": requested_amount, "source": "Intake"},
    ]
    # Prefer the executive summary's curated scalar metrics (list of {label,value,note});
    # fall back to the raw review key_metrics dict for older records.
    exec_metrics = executive_summary.get("key_metrics")
    if isinstance(exec_metrics, list) and exec_metrics:
        for m in exec_metrics:
            if isinstance(m, dict):
                label = _text(m.get("label"), fallback="")
                value = _text(m.get("value"), fallback="")
                note = _text(m.get("note"), fallback="")
                if label or value:
                    metric_rows.append({"metric": label or "Metric", "value": value, "source": note or "AI extraction"})
    else:
        for key, value in key_metrics.items():
            metric_rows.append({"metric": str(key).replace("_", " ").title(), "value": _text(value), "source": "AI extraction"})

    # Rich narrative sections from the executive summary (previously generated but
    # never rendered) — these turn the packet from a table dump into an exec doc.
    def _prose(value: Any) -> str | None:
        text = _text(value, fallback="")
        return text if text and text.lower() != "awaiting evidence" else None

    borrower_profile = _prose(executive_summary.get("borrower_profile"))
    entity_vesting = _prose(executive_summary.get("entity_vesting_notes"))
    property_collateral = _prose(executive_summary.get("property_collateral"))
    requested_terms = _prose(executive_summary.get("requested_terms"))
    application_types = _strings(executive_summary.get("suggested_application_types"))
    narrative_cards = "".join(
        f'<section class="card"><h2>{escape(label)}</h2><p>{escape(text)}</p></section>'
        for label, text in (
            ("Borrower profile", borrower_profile),
            ("Entity & vesting", entity_vesting),
            ("Property / collateral", property_collateral),
            ("Requested terms", requested_terms),
        )
        if text
    )

    fallback_lines = [
        title,
        subtitle,
        f"Generated: {datetime.utcnow().strftime('%b %d, %Y %I:%M %p UTC')}",
        f"Borrower/guarantor: {_text(borrower_name)}",
        f"Business/entity: {_text(business_name)}",
        f"Requested amount: {requested_amount}",
        f"Loan purpose: {_text(purpose)}",
        f"Status: {_text(status)}",
        "",
        "Executive summary:",
    ]
    fallback_lines.extend(_wrap(_text(summary_text)))
    fallback_lines.extend(["", "Recommended submission angle:"])
    fallback_lines.extend(_wrap(_text(recommended_angle)))
    fallback_lines.extend(["", "Missing confirmations:"])
    for row in missing_rows[:12]:
        fallback_lines.extend(_wrap(f"- {row.get('title') or 'Missing item'}: {row.get('detail') or ''}"))
    fallback_lines.extend(["", "Disclaimer: This packet is not an official 1003, lender-specific application, commitment to lend, or final credit decision."])

    html_doc = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    @page {{ size: Letter; margin: 28px; }}
    body {{ font-family: Inter, Arial, sans-serif; background:#f7f8fb; color:#111827; font-size:12px; }}
    h1 {{ font-size:28px; margin:0; letter-spacing:-.02em; }}
    h2 {{ font-size:13px; margin:0 0 9px; color:#334155; text-transform:uppercase; letter-spacing:.08em; }}
    p {{ color:#334155; line-height:1.45; }}
    .header {{ display:flex; justify-content:space-between; gap:18px; border-bottom:2px solid #111827; padding-bottom:16px; margin-bottom:16px; }}
    .brand {{ font-weight:900; color:#0f766e; text-transform:uppercase; letter-spacing:.08em; }}
    .pill {{ border:1px solid #99f6e4; border-radius:999px; padding:7px 11px; color:#0f766e; background:#ecfeff; font-weight:800; }}
    .grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:10px; margin:14px 0; }}
    .field {{ border:1px solid #d8dee9; border-radius:12px; padding:10px; background:white; min-height:52px; }}
    .field span {{ color:#64748b; display:block; font-size:10px; text-transform:uppercase; letter-spacing:.08em; font-weight:800; }}
    .field strong {{ display:block; margin-top:5px; color:#111827; }}
    .card {{ border:1px solid #d8dee9; border-radius:16px; padding:14px; background:white; margin-bottom:12px; }}
    .wide {{ page-break-inside:avoid; }}
    ul {{ margin:0; padding-left:18px; }}
    li {{ margin:5px 0; line-height:1.4; }}
    table {{ width:100%; border-collapse:collapse; margin-top:8px; }}
    td, th {{ border-bottom:1px solid #e2e8f0; padding:8px 6px; vertical-align:top; text-align:left; }}
    th {{ color:#64748b; text-transform:uppercase; font-size:10px; letter-spacing:.06em; }}
    .empty, .empty-cell {{ color:#64748b; font-style:italic; }}
    .disclaimer {{ background:#fff7ed; border-color:#fed7aa; color:#7c2d12; }}
    .footer {{ color:#64748b; font-size:10px; margin-top:16px; border-top:1px solid #d8dee9; padding-top:10px; }}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <div class="brand">Qualified Commercial</div>
      <h1>{escape(title)}</h1>
      <p>{escape(subtitle)} - generated {datetime.utcnow().strftime("%b %d, %Y %I:%M %p UTC")}</p>
    </div>
    <div class="pill">{escape(_text(status, "Preliminary review"))}</div>
  </div>

  <section class="grid">
    {_field("Borrower / guarantor", borrower_name)}
    {_field("Business / entity", business_name)}
    {_field("Email", getattr(intake, "email", None))}
    {_field("Phone", getattr(intake, "phone", None))}
    {_field("Requested amount", requested_amount)}
    {_field("Loan purpose", purpose)}
  </section>

  <section class="card">
    <h2>Executive summary</h2>
    <p>{escape(_text(summary_text))}</p>
  </section>

  <section class="card">
    <h2>Recommended lender/vendor approach</h2>
    <p>{escape(_text(recommended_angle))}</p>
  </section>

  {narrative_cards}
  {_list("Applications suggested", application_types) if application_types else ""}

  {_table("Application-style fields and metrics", metric_rows, [("metric", "Field"), ("value", "Value"), ("source", "Source")])}
  {_table("Documents reviewed", reviewed_docs, [("file", "Document"), ("type", "Type"), ("status", "Status")])}
  {_table("Evidence coverage", coverage_rows, [("category", "Category"), ("status", "Status"), ("gap", "Evidence / gap")])}
  {_table("Missing confirmations", missing_rows, [("title", "Item"), ("priority", "Priority"), ("detail", "Detail")])}
  {_list("Strengths", strengths, "green")}
  {_list("Risks", risks, "amber")}
  {_list("Mitigants", mitigants, "green")}

  <section class="card disclaimer">
    <h2>Important disclaimer</h2>
    <p>This packet is a Qualified Commercial underwriting support package generated from submitted evidence, chat answers, and AI analysis. It is not an official 1003, not a lender-specific application, not a commitment to lend, and not final underwriting approval. Unsupported values are marked as awaiting evidence.</p>
  </section>

  <div class="footer">Qualified Commercial LLC - internal and vendor underwriting support only.</div>
</body>
</html>
"""
    log = logging.getLogger(__name__)
    # 1) WeasyPrint — best CSS fidelity, but needs native libs (Pango/Cairo/GTK).
    try:
        from weasyprint import HTML

        return HTML(string=html_doc).write_pdf()
    except Exception:
        log.warning("lender-packet: WeasyPrint unavailable, trying PyMuPDF Story renderer")
    # 2) PyMuPDF Story — pure wheel, no system deps. Renders the same styled HTML
    #    to a real multi-page document, so the packet looks like a document even
    #    where WeasyPrint's native stack is absent (e.g. the deployed container).
    try:
        pdf = _render_html_pymupdf(html_doc)
        if pdf:
            return pdf
    except Exception:
        log.exception("lender-packet: PyMuPDF Story render failed; using plain-text fallback")
    # 3) Last resort — sectioned plain-text PDF.
    return _minimal_pdf(fallback_lines)


def _render_html_pymupdf(html_doc: str) -> bytes | None:
    """Render HTML to a paginated PDF using PyMuPDF's Story API (no native deps)."""
    import io

    import fitz

    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    story = fitz.Story(html=html_doc)
    media = fitz.paper_rect("letter")
    frame = media + (36, 36, -36, -36)  # 0.5" margins
    more = 1
    guard = 0
    while more and guard < 200:
        guard += 1
        device = writer.begin_page(media)
        more, _ = story.place(frame)
        story.draw(device)
        writer.end_page()
    writer.close()
    return buf.getvalue() or None
