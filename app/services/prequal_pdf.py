"""Render the pre-qualification letter PDF, upload to S3, return the key.

  render_letter(request, loan, *, settings) -> str (s3_key)
  presign_get(s3_key, *, settings, ttl_seconds=86400) -> str (URL)

The render is intentionally synchronous on the backend after the admin
clicks Approve — total wall time is sub-2s for a one-page letter, well
within an HTTP request budget. Don't bother with a background task.

WeasyPrint depends on Cairo + Pango + gdk-pixbuf system libs, which the
Dockerfile installs in the runtime stage.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import Settings
from app.models.loan import Loan
from app.models.prequal_request import PrequalRequest

log = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_jinja = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)

# Presigned GET URLs are minted fresh on every API read so the borrower's
# UI never holds a stale link. 24h is generous for "download → email to
# seller's agent → seller views" without forcing a new call mid-share.
PRESIGN_TTL_SECONDS = 86400


def _s3_client(settings: Settings):
    """Build a boto3 S3 client. Mirrors the pattern in routers/documents.py."""
    return boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
        region_name=settings.aws_region,
    )


def _format_usd(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${float(value):,.2f}"


def _format_date(d) -> str:
    return d.strftime("%B %d, %Y")


def _resolve_borrower_entity(loan: Loan) -> str:
    """Best-effort borrower entity name for the letter:
    loan.entity_name → loan.client.name → "Borrower" (last-resort generic)."""
    entity = getattr(loan, "entity_name", None)
    if entity:
        return str(entity).strip()
    client = getattr(loan, "client", None)
    if client is not None:
        name = getattr(client, "name", None)
        if name:
            return str(name).strip()
    return "Borrower"


def render_letter(
    request: PrequalRequest,
    loan: Loan,
    *,
    settings: Settings,
) -> str:
    """Render the letter to PDF and upload to S3. Returns the s3_key.

    Uses the *approved* numbers if the admin set them, falling back to
    what the borrower originally requested. Caller must have already
    validated LTV against the matrix cap before invoking this — this
    function trusts the inputs.
    """
    # weasyprint imports cairo at module load; defer until we actually need
    # it so unit tests / dev environments without the native deps don't
    # break the whole module import.
    from weasyprint import HTML

    purchase = float(request.approved_purchase_price or request.purchase_price)
    loan_amount = float(request.approved_loan_amount or request.requested_loan_amount)
    ltv_pct = round((loan_amount / purchase) * 100) if purchase > 0 else 0

    today = datetime.now(timezone.utc).date()
    expires = today + timedelta(days=14)

    template_name = (
        "prequal_dscr.html" if request.loan_type == "dscr" else "prequal_bridge.html"
    )
    tpl = _jinja.get_template(template_name)
    html_str = tpl.render(
        today_date=_format_date(today),
        expiration_date=_format_date(expires),
        borrower_entity=_resolve_borrower_entity(loan),
        target_property_address=request.target_property_address,
        purchase_price=_format_usd(purchase),
        max_loan_amount=_format_usd(loan_amount),
        max_ltv=ltv_pct,
        admin_notes=request.admin_notes,
    )

    pdf_bytes = HTML(string=html_str).write_pdf()
    if pdf_bytes is None:
        raise RuntimeError("weasyprint returned None for write_pdf()")

    # Deterministic S3 key — re-approval (admin tweaks numbers + re-clicks)
    # overwrites cleanly without orphans.
    s3_key = f"loans/{loan.deal_id}/pre_quals/{request.id}.pdf"

    s3 = _s3_client(settings)
    s3.put_object(
        Bucket=settings.s3_bucket,
        Key=s3_key,
        Body=pdf_bytes,
        ContentType="application/pdf",
        ServerSideEncryption="AES256",
    )
    log.info(
        "prequal_pdf.uploaded request_id=%s loan_deal=%s key=%s ltv_pct=%s size_bytes=%s",
        request.id, loan.deal_id, s3_key, ltv_pct, len(pdf_bytes),
    )
    return s3_key


def presign_get(
    s3_key: str,
    *,
    settings: Settings,
    ttl_seconds: int = PRESIGN_TTL_SECONDS,
) -> str | None:
    """Return a presigned GET URL for the uploaded PDF, or None if S3 isn't
    configured (dev mode without AWS keys). Always called fresh on read so
    URLs in API responses never go stale."""
    if not settings.aws_access_key_id or not settings.aws_secret_access_key:
        return None
    s3 = _s3_client(settings)
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": s3_key},
        ExpiresIn=ttl_seconds,
    )
