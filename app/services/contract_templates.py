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

import hashlib
import html
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.enums import ContractType
from app.services.contract_templates_data import CONTRACT_RAW_DATA
from app.services.payment_authorization import (  # noqa: F401 re-exported for callers
    client_ip,
    decode_signature_data_url,
    presign_private_s3_object,
    put_private_s3_object,
)

# Maps ContractType -> the key used in contract_templates_data.py (both are
# snake_case and happen to match 1:1 today; kept as an explicit dict rather
# than relying on that coincidence so the two can diverge safely later).
_DATA_KEY_BY_CONTRACT_TYPE: dict[ContractType, str] = {
    ContractType.PLATFORM_ACCESS: "platform_access",
    ContractType.REFERRAL_PROTECTION: "referral_protection",
    ContractType.SBA_ENGAGEMENT: "sba_engagement",
    ContractType.CLIENT_ENGAGEMENT: "client_engagement",
    ContractType.CONSULTING_ADDENDUM: "consulting_addendum",
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
}

CONTRACT_TITLES: dict[ContractType, str] = {
    ContractType.PLATFORM_ACCESS: "Platform Access and Technology Use Agreement",
    ContractType.REFERRAL_PROTECTION: "Strategic Referral, Capital Advisory and Business Relationship Protection Agreement",
    ContractType.SBA_ENGAGEMENT: "SBA Advisory and Packaging Engagement Agreement",
    ContractType.CLIENT_ENGAGEMENT: "Capital Advisory and Placement Engagement Agreement",
    ContractType.CONSULTING_ADDENDUM: "Consulting and Fee Schedule Addendum",
}


@dataclass(frozen=True)
class ContractField:
    name: str
    label: str
    field_type: str  # "text" | "number" | "currency" | "percent" | "date" | "address" | "long_text"
    default: str
    row_group: str | None
    in_scope_for_initial_signing: bool


@dataclass(frozen=True)
class ContractSection:
    heading: str
    paragraphs: list[str]


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


def _field_from_raw(name: str, info: dict) -> ContractField:
    return ContractField(
        name=name,
        label=info.get("label") or name.replace("_", " ").title(),
        field_type=info.get("field_type", "text"),
        default=info.get("default", ""),
        row_group=info.get("row_group"),
        in_scope_for_initial_signing=info.get("in_scope_for_initial_signing", True),
    )


def get_template_spec(contract_type: ContractType) -> ContractTemplateSpec:
    key = _DATA_KEY_BY_CONTRACT_TYPE[contract_type]
    raw = CONTRACT_RAW_DATA[key]
    fields = {name: _field_from_raw(name, info) for name, info in raw["field_schema"].items()}
    return ContractTemplateSpec(
        contract_type=contract_type,
        title=CONTRACT_TITLES[contract_type],
        document_version=CONTRACT_DOCUMENT_VERSIONS[contract_type],
        party_facing_notice=raw.get("notice"),
        internal_notice=raw.get("internal_notice"),
        preamble=list(raw.get("preamble") or []),
        sections=[ContractSection(heading=s["heading"], paragraphs=list(s["paragraphs"])) for s in raw["sections"]],
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


def render_contract_document(contract_type: ContractType, field_values: dict[str, Any]) -> RenderedContract:
    """Substitute every $placeholder in the template with the caller-supplied
    field_values (falling back to each field's own default for anything not
    supplied), returning the fully-filled document ready to show a signer
    and to hash. Uses stdlib string.Template — safe_substitute so a missing
    value renders as the literal token rather than raising, since partial
    fills (e.g. previewing before all blanks are known) should not crash."""
    spec = get_template_spec(contract_type)
    values = {name: field.default for name, field in spec.fields.items()}
    values.update({k: v for k, v in field_values.items() if v is not None})
    # string.Template requires string values.
    str_values = {k: ("" if v is None else str(v)) for k, v in values.items()}

    def fill(text: str) -> str:
        return string.Template(text).safe_substitute(str_values)

    filled_notice = fill(spec.party_facing_notice) if spec.party_facing_notice else None
    filled_preamble = [fill(p) for p in spec.preamble]
    filled_sections = [
        ContractSection(heading=sec.heading, paragraphs=[fill(p) for p in sec.paragraphs])
        for sec in spec.sections
    ]

    plain_parts = [spec.title]
    if filled_notice:
        plain_parts.append(filled_notice)
    plain_parts.extend(filled_preamble)
    for sec in filled_sections:
        plain_parts.append(sec.heading)
        plain_parts.extend(sec.paragraphs)
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


def render_contract_certificate_pdf(
    *,
    rendered: RenderedContract,
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

    notice_html = f'<div class="notice">{html.escape(rendered.party_facing_notice)}</div>' if rendered.party_facing_notice else ""
    preamble_html = "".join(para_html(p) for p in rendered.preamble)
    sections_html = "".join(
        f'<h2>{html.escape(sec.heading)}</h2>' + "".join(para_html(p) for p in sec.paragraphs)
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
