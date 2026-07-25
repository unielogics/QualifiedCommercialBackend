"""Generic e-sign primitive for BucketRequestedDocument (credit-authorization
forms, and any future "have the client sign X" need). Mirrors
payment_authorization.py's typed-name + canvas-signature + rendered-certificate
pattern, generalized to an arbitrary title/document text instead of the
hardcoded payment-authorization copy, and reuses its S3/hash/decode helpers
directly rather than duplicating them.

The rendered certificate is stored as a normal BucketFile (private S3, KMS/
SSE-encrypted like every other bucket upload) that also satisfies the parent
BucketRequestedDocument via the existing upload-driven
_recalculate_requested_document_status — signing a document is, mechanically,
uploading a file. No new satisfaction/status-flip logic is needed anywhere.
"""

from __future__ import annotations

import hashlib
import html
import logging
from datetime import datetime, timezone

from app.models.bucket import BucketDocumentSignature

from app.services.payment_authorization import (  # noqa: F401 re-exported for callers
    client_ip,
    decode_signature_data_url,
    presign_private_s3_object,
    put_private_s3_object,
)

log = logging.getLogger(__name__)

# Default disclosure shown when a BucketRequestedDocument has no admin-uploaded
# template_file (real estate ships with this from day one; dealer leads are
# expected to get a template_file uploaded once that document is supplied —
# dropping it in is a plain admin upload, no code change needed).
CREDIT_AUTHORIZATION_DOCUMENT_VERSION = "2026-07-25-1"


def credit_authorization_document_text() -> str:
    return """
Qualified Commercial LLC Credit Report Authorization and Electronic Signature Consent

By signing this authorization, I authorize QC - Qualified Commercial LLC and its
underwriting partners to obtain a soft credit inquiry (a "soft pull") on my consumer
credit file from one or more consumer reporting agencies, for the purpose of evaluating
my application for financing. I understand a soft pull does not affect my credit score.

I certify that the identifying information I provide on this form (legal name, date of
birth, and address) is accurate and is my own information, provided for the purpose of
this credit inquiry.

I consent to use electronic records and electronic signatures under the U.S. E-SIGN Act
and UETA. I understand my typed legal name, checkbox acknowledgment, drawn signature,
timestamp, IP address, and device/browser information may be retained as evidence of
this authorization. I may request a copy of this authorization record.
""".strip()


def document_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_signature_certificate_pdf(
    *,
    signature: BucketDocumentSignature,
    title: str,
    document_text: str,
    extra_rows: list[tuple[str, str]] | None = None,
) -> bytes:
    """Render a generic signed-document certificate PDF. `extra_rows` lets a
    caller (e.g. the credit-authorization flow) append applicant identity rows
    without this module needing to know about credit-pull-specific fields."""
    from weasyprint import HTML

    signed_at = signature.signed_at or datetime.now(timezone.utc)
    rows = [
        ("Signer", signature.typed_name),
        ("Signature ID", str(signature.id)),
        ("Document version", signature.document_version),
        ("Document SHA-256", signature.document_hash),
        ("Signed at", signed_at.isoformat()),
        ("IP address", signature.ip_address or ""),
        ("User agent", signature.user_agent or ""),
        *(extra_rows or []),
    ]
    row_html = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>"
        for label, value in rows
    )
    doc_html = html.escape(document_text).replace("\n", "<br>")
    body = f"""
    <html>
      <head>
        <style>
          body {{ font-family: Inter, Arial, sans-serif; color: #111827; margin: 44px; }}
          h1 {{ font-size: 22px; margin-bottom: 4px; }}
          h2 {{ font-size: 15px; margin-top: 28px; color: #374151; }}
          .muted {{ color: #6b7280; font-size: 12px; }}
          table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
          th {{ width: 34%; text-align: left; background: #f3f4f6; }}
          th, td {{ border: 1px solid #d1d5db; padding: 8px 10px; vertical-align: top; font-size: 12px; }}
          .terms {{ border: 1px solid #d1d5db; padding: 14px; font-size: 11px; line-height: 1.45; }}
        </style>
      </head>
      <body>
        <h1>{html.escape(title)}</h1>
        <div class="muted">QC - Qualified Commercial LLC</div>
        <table>{row_html}</table>
        <h2>Authorized Terms</h2>
        <div class="terms">{doc_html}</div>
      </body>
    </html>
    """
    pdf = HTML(string=body).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned no PDF bytes")
    return pdf
