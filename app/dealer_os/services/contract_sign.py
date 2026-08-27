"""Executing a prepopulated agreement from the client's own device.

The signing interface is the one the desk approved: signature box by default
(typed name adopted with a checkmark), the full agreement one toggle away, a
sideways drawing pad as the alternative. This module is everything that
happens after "Sign the agreement" is pressed.

Two signature forms, one evidentiary standard:
- A DRAWN signature is stamped onto the client signature line as the image the
  signer drew.
- A TYPED signature is stamped as a conformed signature — "/s/ Name" — which
  is the standard legal convention for an adopted typed signature, rather than
  a script font pretending to be handwriting.

The executed artifact is ONE PDF: the filled agreement with the signature and
date stamped on the client line, plus an appended certificate page carrying
the full evidence (document hash before signing, executed hash, signer, IP,
device, timestamps, consents). One file that travels whole, because a
certificate separated from its agreement is two things to lose track of.

Hard rules:
- Only a document in `out_for_signature` can be signed: the rep's send is what
  freezes the paper, and signing anything still editable would mean the signer
  and the desk could be looking at different documents.
- The pre-signing hash is verified against the stored PDF before stamping. If
  the bytes changed since the fill was generated, signing refuses — that
  mismatch is exactly the tampering the hash exists to catch.
- E-SIGN consent is a precondition, recorded with its own timestamp and IP.
- The signer's copy is emailed immediately. Retention and delivery are part of
  compliance, not courtesy.
"""

from __future__ import annotations

import hashlib
import html as html_mod
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ContractDocument, DealerBusiness
from . import qc_master_application, storage

logger = logging.getLogger(__name__)

__all__ = ["execute", "agreement_text"]

# Client signature line geometry on both one-page agreements: the CLIENT block
# sits at x=320.4 with SIGNATURE labelled at y678-685 (value line above the
# label) and DATE at y748. The loan application's single signature line sits
# left at x=277-320, y~721. Keyed per template; verified on revision 1.
_SIGN_SPOTS: dict[str, dict[str, tuple[float, float]]] = {
    "consulting_agreement": {"signature": (322.0, 674.0), "date": (322.0, 745.0)},
    "loan_app": {"signature": (277.3, 718.0), "date": (432.7, 718.0)},
    "qc_program_application": {"signature": (277.3, 756.0), "date": (432.7, 756.0)},
}


def agreement_text(pdf_bytes: bytes) -> str:
    """The full text a signer reviews in Agreement mode — extracted from the
    exact filled PDF, so what is shown is what is signed."""
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    return "\n\n".join(page.get_text().strip() for page in doc)


def _stamp(
    pdf_bytes: bytes,
    template_key: str,
    *,
    typed_name: str,
    signature_png: bytes | None,
    signed_at: datetime,
    signature_spots: dict[str, tuple[float, float]] | None = None,
) -> bytes:
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if template_key == qc_master_application.MASTER_TEMPLATE_KEY:
        page = doc[-1]
        marker = []
        for page_index in range(doc.page_count - 1, -1, -1):
            candidate = doc[page_index]
            marker = candidate.search_for("SIGNATURE OF AUTHORIZED REPRESENTATIVE")
            if marker:
                page = candidate
                break
        anchor = marker[0] if marker else fitz.Rect(40, page.rect.height - 180, 300, page.rect.height - 160)
        signature_placeholder = page.search_for("Electronic signature")
        date_placeholder = page.search_for("Signed electronically after review")
        signature_rect = signature_placeholder[0] if signature_placeholder else fitz.Rect(
            anchor.x0, anchor.y1 + 20, min(anchor.x0 + 210, page.rect.width - 260), anchor.y1 + 62
        )
        date_rect = date_placeholder[0] if date_placeholder else fitz.Rect(
            page.rect.width - 230, signature_rect.y0, page.rect.width - 40, signature_rect.y1
        )
        # Remove the unsigned placeholders before laying down the adopted signature and date.
        # Leaving them visible makes the executed application look as if it has two signatures.
        for placeholder in (*signature_placeholder, *date_placeholder):
            page.add_redact_annot(
                fitz.Rect(
                    placeholder.x0 - 2,
                    placeholder.y0 - 2,
                    placeholder.x1 + 2,
                    placeholder.y1 + 2,
                ),
                fill=(1, 1, 1),
            )
        if signature_placeholder or date_placeholder:
            page.apply_redactions()
        sx, sy = signature_rect.x0, signature_rect.y1 - 2
        date_spot = (date_rect.x0, date_rect.y1 - 2)
    else:
        page = doc[0]
        spots = signature_spots or _SIGN_SPOTS.get(
            template_key, _SIGN_SPOTS["consulting_agreement"]
        )
        sx, sy = spots["signature"]
        date_spot = spots["date"]
    if signature_png:
        height = 38 if template_key == qc_master_application.MASTER_TEMPLATE_KEY else 28
        rect = fitz.Rect(sx, sy - height, min(sx + 190, page.rect.width - 36), sy + 2)
        page.insert_image(rect, stream=signature_png, keep_proportion=True)
    else:
        # The conformed-signature convention for an adopted typed signature.
        page.insert_text((sx, sy), f"/s/ {typed_name}", fontname="helv", fontsize=11,
                         color=(0.08, 0.15, 0.36))
    dx, dy = date_spot
    page.insert_text((dx, dy), signed_at.strftime("%B %d, %Y"), fontname="helv",
                     fontsize=8, color=(0.08, 0.15, 0.36))
    return doc.tobytes(deflate=True)


def _certificate_html(rows: list[tuple[str, str]], title: str) -> str:
    row_html = "".join(
        f"<tr><th>{html_mod.escape(k)}</th><td>{html_mod.escape(v)}</td></tr>" for k, v in rows
    )
    return f"""
    <html><head><style>
      body {{ font-family: Inter, Arial, sans-serif; color: #111827; margin: 44px; }}
      h1 {{ font-size: 20px; margin-bottom: 2px; }}
      .muted {{ color: #6b7280; font-size: 12px; margin-bottom: 18px; }}
      table {{ width: 100%; border-collapse: collapse; }}
      th {{ width: 32%; text-align: left; background: #f3f4f6; }}
      th, td {{ border: 1px solid #d1d5db; padding: 8px 10px; font-size: 12px;
                vertical-align: top; word-break: break-all; }}
      .foot {{ margin-top: 22px; color: #6b7280; font-size: 10.5px; line-height: 1.5; }}
    </style></head><body>
      <h1>Certificate of Completion</h1>
      <div class="muted">{html_mod.escape(title)} — Qualified Commercial LLC</div>
      <table>{row_html}</table>
      <div class="foot">This certificate is bound to the agreement it follows. The pre-signing
      SHA-256 was computed on the exact populated agreement presented to the signer. The signed
      agreement SHA-256 was computed after the signature was embedded and before this certificate
      was appended. The final executed-document SHA-256 is retained with the audit and delivery
      record. Any alteration will no longer match the applicable recorded hash. Electronic
      signature adopted under the U.S. E-SIGN Act and UETA with the signer's recorded consent.</div>
    </body></html>"""


def _certificate_page(rows: list[tuple[str, str]], title: str) -> bytes:
    from weasyprint import HTML

    pdf = HTML(string=_certificate_html(rows, title)).write_pdf()
    if pdf is None:
        raise RuntimeError("certificate render failed")
    return pdf


async def execute(
    db: AsyncSession,
    dealer: DealerBusiness,
    doc: ContractDocument,
    *,
    typed_name: str,
    signature_png: bytes | None,
    signature_sha256: str | None,
    ip: str | None,
    user_agent: str | None,
    title: str,
) -> tuple[bytes, str]:
    """Stamp, seal, store. Returns (executed_pdf, executed_sha256). Flushes,
    never commits — the route owns the transaction and the email."""
    import fitz  # noqa: F401 — imported here so a missing wheel fails loudly at sign time

    if doc.status != "out_for_signature":
        raise ValueError(
            "This document is not out for signature."
            if doc.status != "executed"
            else "This document has already been signed."
        )
    if not doc.filled_s3_key or not doc.filled_sha256:
        raise ValueError("No prepopulated copy exists to sign.")

    raw = storage.get_bytes(doc.filled_s3_key)
    if raw is None:
        raise RuntimeError("The agreement PDF could not be read from storage.")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != doc.filled_sha256:
        # The one mismatch that must never be signed through.
        raise ValueError(
            "The stored agreement no longer matches its recorded fingerprint. "
            "Signing is refused; ask the desk to regenerate the document."
        )

    now = datetime.now(UTC)
    stamped = _stamp(
        raw, doc.template_key,
        typed_name=typed_name,
        signature_png=signature_png,
        signed_at=now,
        signature_spots=(doc.field_values or {}).get("_signature_spots"),
    )
    signed_body_sha = hashlib.sha256(stamped).hexdigest()

    cert_rows = [
        ("Agreement", title),
        ("Case", getattr(dealer, "case_ref", None) or str(dealer.id)),
        ("Client", dealer.legal_name or dealer.name or ""),
        ("Signer", typed_name),
        ("Signer title", str((doc.field_values or {}).get("signer_title") or "")),
        ("Signature method", "Drawn on device" if signature_png else "Typed and adopted (/s/)"),
        ("Signature SHA-256", signature_sha256 or "typed adoption, no image"),
        ("Document SHA-256 (pre-signing)", doc.filled_sha256),
        ("Signed agreement SHA-256 (before certificate)", signed_body_sha),
        ("E-SIGN consent recorded", now.isoformat()),
        ("Signed at", now.isoformat()),
        ("IP address", ip or ""),
        ("Device", (user_agent or "")[:220]),
    ]
    cert = _certificate_page(cert_rows, title)

    executed = fitz.open(stream=stamped, filetype="pdf")
    cert_doc = fitz.open(stream=cert, filetype="pdf")
    executed.insert_pdf(cert_doc)
    final = executed.tobytes(deflate=True)
    final_sha = hashlib.sha256(final).hexdigest()

    s3_key = f"contract-executed/{dealer.id}/{doc.template_key}/{final_sha[:16]}.pdf"
    if not storage.put_bytes(s3_key, final, "application/pdf"):
        raise RuntimeError("The executed document could not be stored.")

    if signature_png:
        sig_key = f"contract-executed/{dealer.id}/{doc.template_key}/{final_sha[:16]}-signature.png"
        storage.put_bytes(sig_key, signature_png, "image/png")

    doc.executed_s3_key = s3_key
    doc.executed_sha256 = final_sha
    doc.esign_consent_at = now
    doc.esign_consent_ip = ip
    doc.signed_at = now
    doc.signer_name = typed_name
    doc.signer_title = str((doc.field_values or {}).get("signer_title") or "") or None
    doc.signature_sha256 = signature_sha256
    doc.signer_ip = ip
    doc.signer_user_agent = (user_agent or "")[:400]
    doc.status = "executed"
    await db.flush()
    logger.info(
        "contract executed: dealer=%s key=%s signer=%s sha=%s",
        dealer.id, doc.template_key, typed_name, final_sha[:12],
    )
    return final, final_sha
