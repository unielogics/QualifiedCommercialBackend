"""Real, attorney-drafted contract templates: dynamic field-fill, hashing,
and signed-certificate rendering for the 5 platform contract types (see
app.enums.ContractType).

Source text lives in contract_templates_data.py (mechanically transcribed
from the original .docx files by scripts/build_contract_templates.py — see
that script's docstring for the transcription method; every legal paragraph
there is byte-verified against the source, never rewritten).

This module is the only place that: (a) knows how to turn a document's
sections + field_schema into a filled LegalDocument-shaped structure ready
to render, (b) hashes the exact filled text a signer saw, (c) renders the
signed certificate PDF. It mirrors document_signature.py's evidentiary
pattern (document_hash + render_signature_certificate_pdf) but generalized
to a multi-section, multi-page contract instead of a single short
disclosure paragraph.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.enums import ContractType
from app.services.contract_templates_data import CONTRACT_RAW_DATA
from app.services.mutual_nda_template_data import MUTUAL_NDA_TEMPLATE_DATA
from app.services.payment_authorization import (  # noqa: F401 re-exported for callers
    client_ip,
    decode_signature_data_url,
    presign_private_s3_object,
    put_private_s3_object,
)

_SIGNATURE_IMAGE_PATH = Path(__file__).resolve().parent.parent / "assets" / "qc_signature.png"
_SIGNATURE_SECTION_HEADINGS = {"SIGNATURES", "ACKNOWLEDGMENT"}


def qc_signature_datauri() -> str | None:
    """Qualified Commercial's standing signature image, embedded as a data
    URI so a certificate PDF is self-contained (no external image fetch at
    render time). Returns None if the asset is missing rather than raising --
    a missing image should degrade to the plain-text "By: Name" line already
    rendered everywhere else, not break certificate rendering. Also used by
    document_signature.py for the 3 client-facing contract types, which are
    signed through the requested-document flow rather than this module's own
    render_contract_certificate_pdf()."""
    try:
        data = _SIGNATURE_IMAGE_PATH.read_bytes()
    except OSError:
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")

# Maps ContractType -> the key used in contract_templates_data.py (both are
# snake_case and happen to match 1:1 today; kept as an explicit dict rather
# than relying on that coincidence so the two can diverge safely later).
_DATA_KEY_BY_CONTRACT_TYPE: dict[ContractType, str] = {
    ContractType.PLATFORM_ACCESS: "platform_access",
    ContractType.REFERRAL_PROTECTION: "referral_protection",
    ContractType.SBA_ENGAGEMENT: "sba_engagement",
    ContractType.CLIENT_ENGAGEMENT: "client_engagement",
    ContractType.CONSULTING_ADDENDUM: "consulting_addendum",
    ContractType.MUTUAL_NDA_NON_CIRCUMVENTION: "mutual_nda_non_circumvention",
}

# One version string per contract type, bumped whenever the underlying
# template text changes so every ContractAgreement row stays tied to the
# exact version its signer saw. Kept alongside qcdesktop/src/lib/legal.ts's
# equivalent constants if/when a client-facing preview needs the same value.
CONTRACT_DOCUMENT_VERSIONS: dict[ContractType, str] = {
    ContractType.PLATFORM_ACCESS: "2026-08-05-1",
    ContractType.REFERRAL_PROTECTION: "2026-08-05-1",
    ContractType.SBA_ENGAGEMENT: "2026-08-05-1",
    ContractType.CLIENT_ENGAGEMENT: "2026-08-05-1",
    ContractType.CONSULTING_ADDENDUM: "2026-08-05-1",
    ContractType.MUTUAL_NDA_NON_CIRCUMVENTION: "2026-08-24-1",
}

# Short code used inside the human-readable contract number, e.g.
# QC-RPA-2026-00042. Kept separate from ContractType's own string value so
# the number stays short and stable even if the enum value ever changes.
CONTRACT_TYPE_CODE: dict[ContractType, str] = {
    ContractType.PLATFORM_ACCESS: "PAA",
    ContractType.REFERRAL_PROTECTION: "RPA",
    ContractType.SBA_ENGAGEMENT: "SBA",
    ContractType.CLIENT_ENGAGEMENT: "CEA",
    ContractType.CONSULTING_ADDENDUM: "CFA",
    ContractType.MUTUAL_NDA_NON_CIRCUMVENTION: "MNCA",
}

CONTRACT_TITLES: dict[ContractType, str] = {
    ContractType.PLATFORM_ACCESS: "Platform Access and Technology Use Agreement",
    ContractType.REFERRAL_PROTECTION: "Strategic Referral, Capital Advisory and Business Relationship Protection Agreement",
    ContractType.SBA_ENGAGEMENT: "SBA Advisory and Packaging Engagement Agreement",
    ContractType.CLIENT_ENGAGEMENT: "Capital Advisory and Placement Engagement Agreement",
    ContractType.CONSULTING_ADDENDUM: "Consulting and Fee Schedule Addendum",
    ContractType.MUTUAL_NDA_NON_CIRCUMVENTION: "Mutual Nondisclosure & Non-Circumvention Agreement",
}


@dataclass(frozen=True)
class TableColumn:
    key: str
    label: str
    input_type: str  # "text" | "date" | "checkbox" | "select"
    options: list[str] | None = None  # only for "select"


@dataclass(frozen=True)
class ContractField:
    name: str
    label: str
    field_type: str  # "text" | "number" | "currency" | "percent" | "date" | "address" | "long_text" | "disclosure_rows"
    default: str
    row_group: str | None
    in_scope_for_initial_signing: bool
    # Only set when field_type == "disclosure_rows" -- the static column
    # definition for a repeatable, signer-submitted table (e.g. Schedule A's
    # existing-capital-relationship disclosure). Design-time metadata, not
    # per-signer data; the actual submitted rows travel in field_values
    # under this field's own name as a list[dict] keyed by these columns.
    table_columns: list[TableColumn] | None = None


@dataclass(frozen=True)
class ContractSection:
    heading: str
    paragraphs: list[str]
    # Static reference tables (fee schedules, registries, Exhibit 1's
    # Field/Detail rows) -- when set, the frontend renders an actual <table>
    # instead of the paragraph loop. Every cell still passes through the
    # same $placeholder substitution as `paragraphs`.
    columns: list[str] | None = None
    rows: list[list[str]] | None = None
    # Only set on Schedule A's 3 disclosure sections: names the
    # "disclosure_rows"-type field whose signer-submitted rows replace
    # `rows` at render time (see render_contract_document below).
    disclosure_field: str | None = None


@dataclass(frozen=True)
class ContractTemplateSpec:
    contract_type: ContractType
    title: str
    document_version: str
    party_facing_notice: str | None
    internal_notice: str | None
    preamble: list[str]
    sections: list[ContractSection]
    fields: dict[str, ContractField]


def _table_columns_from_raw(raw_columns: list[dict] | None) -> list[TableColumn] | None:
    if not raw_columns:
        return None
    return [
        TableColumn(
            key=c["key"],
            label=c["label"],
            input_type=c.get("input_type", "text"),
            options=list(c["options"]) if c.get("options") else None,
        )
        for c in raw_columns
    ]


def _field_from_raw(name: str, info: dict) -> ContractField:
    return ContractField(
        name=name,
        label=info.get("label") or name.replace("_", " ").title(),
        field_type=info.get("field_type", "text"),
        default=info.get("default", ""),
        row_group=info.get("row_group"),
        in_scope_for_initial_signing=info.get("in_scope_for_initial_signing", True),
        table_columns=_table_columns_from_raw(info.get("table_columns")),
    )


def _section_from_raw(s: dict) -> ContractSection:
    return ContractSection(
        heading=s["heading"],
        paragraphs=list(s["paragraphs"]),
        columns=list(s["columns"]) if s.get("columns") else None,
        rows=[list(r) for r in s["rows"]] if s.get("rows") else None,
        disclosure_field=s.get("disclosure_field"),
    )


def get_template_spec(contract_type: ContractType) -> ContractTemplateSpec:
    key = _DATA_KEY_BY_CONTRACT_TYPE[contract_type]
    raw = (
        MUTUAL_NDA_TEMPLATE_DATA
        if contract_type == ContractType.MUTUAL_NDA_NON_CIRCUMVENTION
        else CONTRACT_RAW_DATA[key]
    )
    fields = {name: _field_from_raw(name, info) for name, info in raw["field_schema"].items()}
    return ContractTemplateSpec(
        contract_type=contract_type,
        title=CONTRACT_TITLES[contract_type],
        document_version=CONTRACT_DOCUMENT_VERSIONS[contract_type],
        party_facing_notice=raw.get("notice"),
        internal_notice=raw.get("internal_notice"),
        preamble=list(raw.get("preamble") or []),
        sections=[_section_from_raw(s) for s in raw["sections"]],
        fields=fields,
    )


@dataclass(frozen=True)
class RenderedContract:
    title: str
    document_version: str
    party_facing_notice: str | None
    preamble: list[str]
    sections: list[ContractSection]
    plain_text: str


def _coerce_disclosure_rows(raw: Any, columns: list[TableColumn] | None) -> list[list[str]] | None:
    """Defensive validation of signer-submitted disclosure rows (Schedule A) --
    never trusts the submitted shape: only a list of dicts is accepted, only
    declared column keys are read, and every value is coerced to the display
    string for its column's declared input_type. Returns None (not an empty
    list) when there's nothing usable, so the caller can render its own
    "none disclosed" default rather than an empty table."""
    if not columns or not isinstance(raw, list):
        return None
    rows: list[list[str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        row: list[str] = []
        has_value = False
        for col in columns:
            value = item.get(col.key)
            if col.input_type == "checkbox":
                cell = "Yes" if value else "No"
                if value:
                    has_value = True
            elif value is None:
                cell = ""
            else:
                cell = str(value).strip()
                if cell:
                    has_value = True
            row.append(cell)
        if has_value:
            rows.append(row)
    return rows or None


def render_contract_document(contract_type: ContractType, field_values: dict[str, Any]) -> RenderedContract:
    """Substitute every $placeholder in the template with the caller-supplied
    field_values (falling back to each field's own default for anything not
    supplied), returning the fully-filled document ready to show a signer
    and to hash. Uses stdlib string.Template — safe_substitute so a missing
    value renders as the literal token rather than raising, since partial
    fills (e.g. previewing before all blanks are known) should not crash.

    A field_values entry for a "disclosure_rows"-type field (Schedule A's
    signer-submitted capital-relationship disclosures) is a list[dict], not a
    scalar -- it never participates in $placeholder substitution and is
    handled separately, replacing its owning section's `rows`."""
    spec = get_template_spec(contract_type)
    disclosure_field_names = {f.name for f in spec.fields.values() if f.field_type == "disclosure_rows"}

    scalar_values = {name: field.default for name, field in spec.fields.items() if name not in disclosure_field_names}
    scalar_values.update(
        {k: v for k, v in field_values.items() if v is not None and k not in disclosure_field_names}
    )
    # string.Template requires string values.
    str_values = {k: ("" if v is None else str(v)) for k, v in scalar_values.items()}
    raw_disclosure_values = {k: v for k, v in field_values.items() if k in disclosure_field_names}

    def fill(text: str) -> str:
        return string.Template(text).safe_substitute(str_values)

    filled_notice = fill(spec.party_facing_notice) if spec.party_facing_notice else None
    filled_preamble = [fill(p) for p in spec.preamble]

    filled_sections: list[ContractSection] = []
    for sec in spec.sections:
        paragraphs = [fill(p) for p in sec.paragraphs]
        columns = list(sec.columns) if sec.columns else None
        rows = [[fill(cell) for cell in row] for row in sec.rows] if sec.rows else None

        if sec.disclosure_field:
            field = spec.fields.get(sec.disclosure_field)
            table_cols = field.table_columns if field else None
            if table_cols:
                columns = columns or [c.label for c in table_cols]
                submitted = _coerce_disclosure_rows(raw_disclosure_values.get(sec.disclosure_field), table_cols)
                rows = submitted or [["None disclosed as of the Effective Date."] + [""] * (len(table_cols) - 1)]

        filled_sections.append(
            ContractSection(
                heading=sec.heading,
                paragraphs=paragraphs,
                columns=columns,
                rows=rows,
                disclosure_field=sec.disclosure_field,
            )
        )

    plain_parts = [spec.title]
    if filled_notice:
        plain_parts.append(filled_notice)
    plain_parts.extend(filled_preamble)
    for sec in filled_sections:
        plain_parts.append(sec.heading)
        plain_parts.extend(sec.paragraphs)
        if sec.columns:
            plain_parts.append(" | ".join(sec.columns))
        if sec.rows:
            plain_parts.extend(" | ".join(row) for row in sec.rows)
    plain_text = "\n".join(plain_parts)

    return RenderedContract(
        title=spec.title,
        document_version=spec.document_version,
        party_facing_notice=filled_notice,
        preamble=filled_preamble,
        sections=filled_sections,
        plain_text=plain_text,
    )


def contract_document_hash(rendered: RenderedContract) -> str:
    return hashlib.sha256(rendered.plain_text.encode("utf-8")).hexdigest()


_BY_LINE_RE = re.compile(r"^By:\s*(.+)$")


def render_contract_certificate_pdf(
    *,
    rendered: RenderedContract,
    contract_type: ContractType,
    contract_number: str,
    typed_name: str,
    document_hash: str,
    ip_address: str | None,
    user_agent: str | None,
    signed_at: datetime | None,
    extra_rows: list[tuple[str, str]] | None = None,
) -> bytes:
    """Render the full signed contract (every section) plus an evidentiary
    signature block, paginated via weasyprint @page CSS. Extends
    document_signature.py's single-page certificate pattern with real
    pagination + a running header/footer, since these documents run many
    pages rather than one short disclosure paragraph."""
    from weasyprint import HTML

    ts = signed_at or datetime.now(timezone.utc)
    evidentiary_rows = [
        ("Contract number", contract_number),
        ("Signer", typed_name),
        ("Document version", rendered.document_version),
        ("Document SHA-256", document_hash),
        ("Signed at", ts.isoformat()),
        ("IP address", ip_address or ""),
        ("User agent", user_agent or ""),
        *(extra_rows or []),
    ]
    evidentiary_html = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>"
        for label, value in evidentiary_rows
    )

    def para_html(text: str) -> str:
        return f"<p>{html.escape(text)}</p>"

    # Qualified Commercial's standing signature image replaces its own "By:"
    # line inside the SIGNATURES/ACKNOWLEDGMENT section only -- matched
    # against the qc_signatory_name field's own default so the counterparty's
    # "By: <their name>" line on the same section is never affected.
    qc_name_default = get_template_spec(contract_type).fields.get("qc_signatory_name")
    qc_name_default = qc_name_default.default if qc_name_default else None
    signature_datauri = qc_signature_datauri() if qc_name_default else None

    def signature_section_para_html(text: str) -> str:
        m = _BY_LINE_RE.match(text)
        if m and signature_datauri and m.group(1).strip() == qc_name_default:
            return f'<p><img class="qc-sig" src="{signature_datauri}" alt="Qualified Commercial signature"/></p>'
        return para_html(text)

    def table_html(sec: ContractSection) -> str:
        if not sec.rows:
            return ""
        head = (
            "<thead><tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in sec.columns or []) + "</tr></thead>"
            if sec.columns
            else ""
        )
        body_rows = "".join(
            "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>" for row in sec.rows
        )
        return f'<table class="doc-table">{head}<tbody>{body_rows}</tbody></table>'

    notice_html = f'<div class="notice">{html.escape(rendered.party_facing_notice)}</div>' if rendered.party_facing_notice else ""
    preamble_html = "".join(para_html(p) for p in rendered.preamble)
    sections_html = "".join(
        f'<h2>{html.escape(sec.heading)}</h2>'
        + "".join(
            (signature_section_para_html if sec.heading in _SIGNATURE_SECTION_HEADINGS else para_html)(p)
            for p in sec.paragraphs
        )
        + table_html(sec)
        for sec in rendered.sections
    )

    body = f"""
    <html>
      <head>
        <style>
          @page {{
            size: letter;
            margin: 60px 50px 60px 50px;
            @top-center {{ content: "{html.escape(rendered.title)}"; font-size: 9px; color: #6b7280; }}
            @bottom-center {{ content: "Page " counter(page) " of " counter(pages) " — {html.escape(contract_number)}"; font-size: 9px; color: #6b7280; }}
          }}
          body {{ font-family: Inter, Arial, sans-serif; color: #111827; font-size: 11px; line-height: 1.5; }}
          h1 {{ font-size: 18px; margin-bottom: 4px; }}
          h2 {{ font-size: 13px; margin-top: 20px; color: #374151; page-break-after: avoid; }}
          p {{ margin: 6px 0; }}
          .muted {{ color: #6b7280; font-size: 11px; }}
          .notice {{ border: 1px solid #d1d5db; background: #f9fafb; padding: 10px 12px; font-size: 10px; margin: 12px 0; }}
          .cert {{ page-break-before: always; }}
          table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
          th {{ width: 34%; text-align: left; background: #f3f4f6; }}
          th, td {{ border: 1px solid #d1d5db; padding: 7px 9px; vertical-align: top; font-size: 10.5px; }}
          .doc-table th {{ width: auto; }}
          .doc-table {{ font-size: 10px; }}
          .qc-sig {{ height: 34px; margin: 2px 0; }}
        </style>
      </head>
      <body>
        <h1>{html.escape(rendered.title)}</h1>
        <div class="muted">Qualified Commercial LLC — Contract No. {html.escape(contract_number)}</div>
        {notice_html}
        {preamble_html}
        {sections_html}
        <div class="cert">
          <h1>Signature Certificate</h1>
          <div class="muted">Evidentiary record of electronic signature</div>
          <table>{evidentiary_html}</table>
        </div>
      </body>
    </html>
    """
    pdf = HTML(string=body).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned no PDF bytes")
    return pdf


def render_exhibit_pdf(*, registration_number: str, referral_partner_name: str, rows: list[tuple[str, str]]) -> bytes:
    """Renders a single-page PDF of Exhibit 1 (Deal Registration and
    Introduction Confirmation) for one issued Deal Registration -- a much
    smaller document than a full contract certificate, since this is just
    the specimen form filled with one deal's actual field/detail rows."""
    from weasyprint import HTML

    row_html = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>" for label, value in rows
    )
    body = f"""
    <html>
      <head>
        <style>
          body {{ font-family: Inter, Arial, sans-serif; color: #111827; margin: 44px; font-size: 11px; }}
          h1 {{ font-size: 18px; margin-bottom: 4px; }}
          .muted {{ color: #6b7280; font-size: 11px; margin-bottom: 16px; }}
          table {{ width: 100%; border-collapse: collapse; }}
          th {{ width: 40%; text-align: left; background: #f3f4f6; }}
          th, td {{ border: 1px solid #d1d5db; padding: 8px 10px; vertical-align: top; font-size: 11px; }}
        </style>
      </head>
      <body>
        <h1>Deal Registration and Introduction Confirmation</h1>
        <div class="muted">
          Issued by Qualified Commercial LLC under Article 4 of the Agreement — Registration No.
          {html.escape(registration_number)} — Referral Partner: {html.escape(referral_partner_name)}
        </div>
        <table>{row_html}</table>
      </body>
    </html>
    """
    pdf = HTML(string=body).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned no PDF bytes")
    return pdf
