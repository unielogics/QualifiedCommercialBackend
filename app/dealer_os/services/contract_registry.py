"""The contract package as data.

A template is a blank lender PDF plus two layers of knowledge about it: what
fields the PDF itself declares (discovered, automatic) and what each field
means on a case (mapped, human). Keeping those separate is the point — every
lender names fields differently, and a guessed mapping writes the wrong value
into a legal document, which is worse than an empty one.

Filling notes, learned the hard way elsewhere and kept here so they are not
re-learned:
- ``NeedAppearances`` must be set or filled values render blank in several
  viewers (Preview and some Chrome builds). pypdf's writer does this via
  ``set_need_appearances_writer`` semantics; we set the flag directly.
- A PDF with no AcroForm at all cannot be form-filled; it needs a coordinate
  overlay (PyMuPDF), which requires a per-template coordinate map that does not
  exist until someone draws it. Discovery records ``has_acroform`` so the UI
  can say which regime a template is in rather than failing at fill time.

E-signature compliance (ESIGN 15 U.S.C. §7001 / UETA), and where each
requirement is satisfied in this system rather than asserted:

1. **Intent to sign** — the signer performs an affirmative act (adopt/sign
   button with the signature drawn or typed). Captured by the signing flow.
2. **Consent to do business electronically** — recorded as its OWN act with
   its own timestamp and IP (`esign_consent_at/_ip` on the document), shown
   before signing with the §101(c) disclosures: right to a paper copy, right
   to withdraw consent, and how. Platform Terms §8 already establish the
   general consent; the per-document record is what answers "did THIS signer
   agree for THIS document".
3. **Association of signature with the record** — the SHA-256 of the exact
   filled PDF the signer saw (`filled_sha256`) is computed before the link
   goes out and the executed copy's hash (`executed_sha256`) after; the
   certificate binds them.
4. **Attribution** — name, IP, user agent, and possession of the one-time
   signing link delivered to the contact on file.
5. **Retention & delivery** — the executed copy is stored against the case
   and the signer can download it from the same link.
6. **Tamper evidence** — any later change to the stored PDF no longer matches
   the recorded hash.
"""

from __future__ import annotations

import hashlib
import io
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ContractTemplate
from . import storage

logger = logging.getLogger(__name__)

__all__ = ["discover_fields", "ingest_template", "fill_acroform", "TEMPLATE_KEYS"]

# The package the desk has named so far. Uploading is constrained to known
# keys so a typo creates an error, not a fifth document nobody meant to have.
TEMPLATE_KEYS: dict[str, str] = {
    "loan_app": "Loan application",
    "consulting_agreement": "Consulting agreement",
}


def discover_fields(pdf_bytes: bytes) -> dict[str, Any]:
    """What the PDF itself says it can hold. Pure, no IO."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    fields = reader.get_fields() or {}
    names = sorted(fields.keys())
    return {
        "page_count": len(reader.pages),
        "has_acroform": bool(names),
        "field_names": names,
    }


async def ingest_template(
    db: AsyncSession,
    *,
    key: str,
    pdf_bytes: bytes,
    uploaded_by,
    title: str | None = None,
) -> ContractTemplate:
    """Upload or replace one template. Flushes, never commits.

    Replacement bumps `revision` rather than editing in place, so an executed
    document can always say which paper it was signed on. The old S3 object is
    left where it is for the same reason.
    """
    if key not in TEMPLATE_KEYS:
        raise ValueError(
            f"Unknown template key {key!r}. Known: {', '.join(sorted(TEMPLATE_KEYS))}. "
            "Add the key to TEMPLATE_KEYS first; a typo must not mint a document."
        )
    if not pdf_bytes.startswith(b"%PDF"):
        raise ValueError("That file is not a PDF.")

    info = discover_fields(pdf_bytes)
    tpl = (
        await db.execute(select(ContractTemplate).where(ContractTemplate.key == key))
    ).scalar_one_or_none()
    if tpl is None:
        tpl = ContractTemplate(key=key, title=title or TEMPLATE_KEYS[key])
        db.add(tpl)
        await db.flush()
    else:
        if tpl.s3_key:
            tpl.revision = (tpl.revision or 1) + 1

    sha = hashlib.sha256(pdf_bytes).hexdigest()[:16]
    s3_key = f"contract-templates/{key}/r{tpl.revision}-{sha}.pdf"
    stored = storage.put_bytes(s3_key, pdf_bytes, "application/pdf")
    if not stored:
        # Without durable storage there is nothing to sign later. This is the
        # one place a soft-degrade would be a lie.
        raise RuntimeError("Could not store the template PDF (S3 unconfigured or failed).")

    tpl.s3_key = s3_key
    tpl.title = title or tpl.title
    tpl.page_count = info["page_count"]
    tpl.has_acroform = info["has_acroform"]
    tpl.field_names = info["field_names"]
    tpl.uploaded_by_user_id = getattr(uploaded_by, "id", None)
    tpl.active = True
    # A replaced PDF may rename fields; keep only mappings that still exist
    # rather than silently writing values into fields that vanished.
    if tpl.field_map:
        tpl.field_map = {
            k: v for k, v in tpl.field_map.items() if k in set(info["field_names"])
        }
    await db.flush()
    logger.info(
        "contract template %s r%s ingested: %s pages, acroform=%s, %s fields",
        key, tpl.revision, tpl.page_count, tpl.has_acroform, len(info["field_names"]),
    )
    return tpl


def fill_acroform(pdf_bytes: bytes, values: dict[str, str]) -> bytes:
    """Fill an AcroForm and return the new PDF. Pure.

    NeedAppearances is set so filled values actually render; without it several
    viewers show blank fields over correct data, which for a legal document is
    indistinguishable from wrong.
    """
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import BooleanObject, NameObject

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    writer.append(reader)
    for page in writer.pages:
        writer.update_page_form_field_values(page, values)
    try:
        root = writer._root_object  # noqa: SLF001 — pypdf's documented pattern
        if "/AcroForm" in root:
            root["/AcroForm"][NameObject("/NeedAppearances")] = BooleanObject(True)
    except Exception:  # noqa: BLE001 — cosmetic flag, never fail the fill
        logger.warning("could not set NeedAppearances; some viewers may render blanks")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
