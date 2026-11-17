"""The real-estate loan application as a Qualified Commercial PDF.

The frozen body renders the same way every time: applicable fields only, in
the schema's order, with the last four of the SSN masked. The signature blocks
carry invisible anchors (``[[SIG:borrower:1]]``, ``[[DATE:borrower:1]]``...)
that the signing step stamps onto (app/services/pdf_stamping.py), so the
executed copy is this same document with the signatures on it.

Production renders with WeasyPrint (Dockerfile installs Pango and DejaVu);
a box without it falls back to PyMuPDF's HTML story, which lays out text and
tables only and is good enough for tests and previews here.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Any

from app.services import re_application_schema as schema

log = logging.getLogger(__name__)

_APP = Path(__file__).resolve().parents[1]
_QC_MARK = _APP / "templates" / "_qcmark.svg.html"
PLACEHOLDER_STREET = "123 Financial Way"


def _text(value: Any) -> str:
    return escape(str(value)) if value not in (None, "") else ""


def _qc_mark() -> str:
    try:
        return re.sub(r"\{#.*?#\}", "", _QC_MARK.read_text(encoding="utf-8"), flags=re.S).strip()
    except OSError:
        return ""


def letterhead_lines(letterhead: Any) -> list[str]:
    """The office lines to print; the placeholder street is never printed."""
    lines = [
        getattr(letterhead, "office_address_line_1", "") or "",
        getattr(letterhead, "office_address_line_2", "") or "",
        getattr(letterhead, "office_address_line_3", "") or "",
    ]
    if lines[0].startswith(PLACEHOLDER_STREET):
        lines = [lines[2]]
    return [line for line in lines if line.strip()]


def letterhead_is_placeholder(letterhead: Any) -> bool:
    return str(getattr(letterhead, "office_address_line_1", "") or "").startswith(PLACEHOLDER_STREET)


def _rows(body: dict[str, Any], paths: list[tuple[str, dict[str, Any]]]) -> str:
    cells = []
    for path, spec in paths:
        shown = schema.display(spec, schema.get_at(body, path))
        cells.append(
            f"<tr><th>{escape(spec['label'])}</th><td>{escape(shown) if shown else '<span class=\"blank\">Not provided</span>'}</td></tr>"
        )
    return f"<table class='kv'><tbody>{''.join(cells)}</tbody></table>" if cells else ""


def _section_paths(body: dict[str, Any]) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for section_key, path, spec in schema.applicable_paths(body):
        grouped.setdefault(section_key, []).append((path, spec))
    return grouped


def _other_properties(body: dict[str, Any]) -> str:
    rows = schema.get_at(body, "experience.other_properties") or []
    if not rows:
        return ""
    specs = next(section for section in schema.SECTIONS if section["key"] == "experience")["collections"]["other_properties"]
    head = "".join(f"<th>{escape(spec['label'])}</th>" for spec in specs)
    lines = "".join(
        "<tr>" + "".join(f"<td>{escape(schema.display(spec, row.get(spec['key'])))}</td>" for spec in specs) + "</tr>"
        for row in rows
    )
    return f"<h3>Other properties owned</h3><table class='grid'><thead><tr>{head}</tr></thead><tbody>{lines}</tbody></table>"


def _signature_blocks(body: dict[str, Any], consents: list[Any], *, signed: dict[str, dict[str, Any]] | None = None) -> str:
    agreements = "".join(
        f"<div class='consent'><b>{escape(consent.label)}.</b> {escape(consent.text)}</div>" for consent in consents
    )
    blocks = []
    for index, role in enumerate(schema.active_roles(body), start=1):
        person = schema.get_at(body, f"borrowers.{role}") or {}
        name = " ".join(part for part in (person.get("first_name"), person.get("middle_name"), person.get("last_name"), person.get("suffix")) if part)
        record = (signed or {}).get(role) or {}
        blocks.append(
            f"""<div class='sign'>
              <div class='sign-label'>{escape(schema.ROLE_LABELS[role])}</div>
              <div class='sign-line'><span class='anchor'>[[SIG:{role}:{index}]]</span></div>
              <div class='sign-meta'><span>{escape(name) or '&nbsp;'}</span><span class='date'>Date: <span class='anchor'>[[DATE:{role}:{index}]]</span>{escape(record.get('date', ''))}</span></div>
            </div>"""
        )
    return f"<h2>Authorizations and signatures</h2>{agreements}<div class='signs'>{''.join(blocks)}</div>"


_CSS = """
@page { size: Letter; margin: 0.55in 0.55in 0.62in; @bottom-left { content: "Qualified Commercial | Real Estate Loan Application"; color:#667085; font-size:8px; } @bottom-right { content: "Page " counter(page) " of " counter(pages); color:#667085; font-size:8px; } }
* { box-sizing: border-box; }
body { font-family: Arial, sans-serif; color:#101828; font-size:9.2px; line-height:1.4; margin:0; }
.brand { display:table; width:100%; border-bottom:3px solid #173b7a; padding-bottom:10px; margin-bottom:10px; }
.brand > div { display:table-cell; vertical-align:middle; }
.brand .mark svg { width:34px; height:34px; }
.brand .name { font-size:13px; font-weight:bold; color:#173b7a; }
.brand .office { text-align:right; color:#475467; font-size:8.4px; }
h1 { font-size:18px; margin:4px 0 2px; color:#101828; }
h2 { font-size:12px; margin:14px 0 6px; padding-bottom:3px; border-bottom:1.5px solid #173b7a; color:#173b7a; break-after:avoid; }
h3 { font-size:10px; margin:8px 0 4px; break-after:avoid; }
.meta { color:#475467; }
.notice { background:#f7f9fc; border-left:3px solid #173b7a; padding:6px 9px; margin:8px 0; }
table { width:100%; border-collapse:collapse; margin:3px 0 8px; table-layout:fixed; }
tr { break-inside:avoid; }
table.kv th { width:42%; text-align:left; font-weight:normal; color:#475467; padding:3px 6px 3px 0; border-bottom:1px solid #e4e7ec; vertical-align:top; }
table.kv td { padding:3px 0; border-bottom:1px solid #e4e7ec; overflow-wrap:anywhere; }
table.grid th { background:#eef2f8; text-align:left; padding:4px 5px; font-size:8.4px; }
table.grid td { padding:4px 5px; border-bottom:1px solid #e4e7ec; overflow-wrap:anywhere; }
.blank { color:#98a2b3; }
.consent { margin:5px 0; text-align:justify; }
.signs { margin-top:10px; }
.sign { break-inside:avoid; border:1px solid #cfd7e6; padding:10px 12px; margin:8px 0; }
.sign-label { font-weight:bold; color:#173b7a; }
.sign-line { height:44px; border-bottom:1px solid #101828; position:relative; }
.sign-meta { display:table; width:100%; margin-top:4px; }
.sign-meta span { display:table-cell; }
.sign-meta .date { text-align:right; }
.anchor { color:#ffffff; font-size:1px; }
"""


def build_html(
    body: dict[str, Any],
    *,
    reference: str | None = None,
    version_number: int | None = None,
    generated_at: datetime | date | None = None,
    letterhead: Any = None,
    consents: list[Any] | None = None,
) -> str:
    """The application as HTML (the PDF's single source)."""
    when = generated_at or datetime.now()
    office = "".join(f"<div>{escape(line)}</div>" for line in letterhead_lines(letterhead)) if letterhead is not None else ""
    grouped = _section_paths(body)
    sections_html: list[str] = []
    for section in schema.SECTIONS:
        paths = grouped.get(section["key"], [])
        if section.get("per_role"):
            for role in schema.active_roles(body):
                role_paths = [(path, spec) for path, spec in paths if path.startswith(f"borrowers.{role}.")]
                if role_paths:
                    sections_html.append(f"<h2>{escape(schema.ROLE_LABELS[role])}</h2>{_rows(body, role_paths)}")
            continue
        extra = _other_properties(body) if section["key"] == "experience" else ""
        if paths or extra:
            sections_html.append(f"<h2>{escape(section['label'])}</h2>{_rows(body, paths)}{extra}")
    from app.services import form_kinds

    consent_rows = consents if consents is not None else [form_kinds.consent(key) for key in ("business_purpose", "accuracy", "credit_authorization", "information_sharing", "esign")]
    signatures = _signature_blocks(body, [consent for consent in consent_rows if consent is not None])
    stamp = when.strftime("%B %d, %Y") if hasattr(when, "strftime") else str(when)
    version = f"Version {version_number}" if version_number else "Draft for review"
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{_CSS}</style></head><body>
      <div class="brand">
        <div class="mark">{_qc_mark()}</div>
        <div><div class="name">Qualified Commercial LLC</div><div class="meta">Business-purpose real estate financing</div></div>
        <div class="office">{office}</div>
      </div>
      <h1>{escape(schema.TITLE)}</h1>
      <div class="meta">{_text(reference)}{' | ' if reference else ''}{escape(version)} | Prepared {escape(stamp)}</div>
      <div class="notice">This application requests business-purpose financing. It is not a commitment to lend, an approval or a rate quote. Final terms depend on underwriting by Qualified Commercial or the lender that reviews it.</div>
      {''.join(sections_html)}
      {signatures}
    </body></html>"""


def _render_pymupdf(document: str) -> bytes:
    import fitz

    buffer = io.BytesIO()
    writer = fitz.DocumentWriter(buffer)
    story = fitz.Story(html=document)
    media = fitz.paper_rect("letter")
    frame = media + (36, 36, -36, -40)
    more, count = 1, 0
    try:
        while more and count < 200:
            count += 1
            device = writer.begin_page(media)
            more, _ = story.place(frame)
            story.draw(device)
            writer.end_page()
    finally:
        writer.close()
    return buffer.getvalue()


def render_pdf(html_document: str) -> bytes:
    try:
        from weasyprint import HTML
    except (ImportError, OSError):
        log.info("WeasyPrint unavailable; rendering the application with PyMuPDF")
        return _render_pymupdf(html_document)
    pdf = HTML(string=html_document, base_url=str(_APP)).write_pdf()
    if not pdf:
        raise RuntimeError("WeasyPrint returned no PDF bytes")
    return pdf
