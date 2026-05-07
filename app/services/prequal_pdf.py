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

# Default validity window for the pre-qualification letter. The
# underwriter can override on a per-letter basis from the review modal.
DEFAULT_LETTER_VALIDITY_DAYS = 90


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


def _resolve_borrower_entity(loan: Loan | None, request: PrequalRequest | None = None) -> str:
    """Best-effort borrower entity name for the letter. Priority:
       1. request.borrower_entity   (borrower-typed LLC, or admin override)
       2. loan.entity_name          (when promoted to a Loan)
       3. loan.client.name          (legal individual name)
       4. "Borrower"                (last-resort generic)
    Borrowers who left the LLC field as TBD will fall through to the
    individual legal name on the rendered letter."""
    if request is not None:
        be = getattr(request, "borrower_entity", None)
        if be and str(be).strip():
            return str(be).strip()
    if loan is not None:
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
    loan: Loan | None = None,
    *,
    settings: Settings,
    expiration_days: int | None = None,
    quote_number: str | None = None,
) -> str:
    """Render the letter to PDF and upload to S3. Returns the s3_key.

    Uses the *approved* numbers if the admin set them, falling back to
    what the borrower originally requested. Caller must have already
    validated LTV against the matrix cap before invoking this — this
    function trusts the inputs.

    `loan` is optional now — under the new lifecycle, no Loan exists at
    approve time. We bucket the PDF under the requester's user id when
    no loan is available.

    `expiration_days` defaults to 90; underwriter can override per
    letter via the review modal.

    `quote_number` is rendered into the title as 'Purchase
    Pre-Qualification ({quote_number})'. Underwriter notes are NEVER
    shown in the PDF — they live on the prequal record for the borrower
    to read in-app, but they don't go on the letter.
    """
    # weasyprint imports cairo at module load; defer until we actually need
    # it so unit tests / dev environments without the native deps don't
    # break the whole module import.
    from weasyprint import HTML

    purchase = float(request.approved_purchase_price or request.purchase_price)
    loan_amount = float(request.approved_loan_amount or request.requested_loan_amount)
    ltv_pct = round((loan_amount / purchase) * 100) if purchase > 0 else 0

    today = datetime.now(timezone.utc).date()
    valid_for = expiration_days if expiration_days and expiration_days > 0 else DEFAULT_LETTER_VALIDITY_DAYS
    expires = today + timedelta(days=valid_for)

    template_name = (
        "prequal_dscr.html" if request.loan_type == "dscr" else "prequal_bridge.html"
    )
    tpl = _jinja.get_template(template_name)
    html_str = tpl.render(
        today_date=_format_date(today),
        expiration_date=_format_date(expires),
        borrower_entity=_resolve_borrower_entity(loan, request),
        target_property_address=request.target_property_address,
        purchase_price=_format_usd(purchase),
        max_loan_amount=_format_usd(loan_amount),
        max_ltv=ltv_pct,
        quote_number=quote_number or request.quote_number,
        # admin_notes intentionally NOT passed — borrower sees them in
        # the dashboard, never on the printable letter.
    )

    pdf_bytes = HTML(string=html_str).write_pdf()
    if pdf_bytes is None:
        raise RuntimeError("weasyprint returned None for write_pdf()")

    # S3 key prefix uses the loan's deal_id when available, otherwise
    # the requester's user id (since no loan exists yet under the new
    # lifecycle until the borrower marks the seller's offer accepted).
    bucket_path = (
        f"loans/{loan.deal_id}" if loan is not None
        else f"prequals/{request.requester_id}"
    )
    s3_key = f"{bucket_path}/pre_quals/{request.id}.pdf"

    s3 = _s3_client(settings)
    s3.put_object(
        Bucket=settings.s3_bucket,
        Key=s3_key,
        Body=pdf_bytes,
        ContentType="application/pdf",
        ServerSideEncryption="AES256",
    )
    log.info(
        "prequal_pdf.uploaded request_id=%s key=%s ltv_pct=%s expires_in_days=%s size_bytes=%s",
        request.id, s3_key, ltv_pct, valid_for, len(pdf_bytes),
    )
    return s3_key


def presign_get(
    s3_key: str,
    *,
    settings: Settings,
    ttl_seconds: int = PRESIGN_TTL_SECONDS,
) -> str | None:
    """Return a presigned GET URL for the uploaded PDF, or None if S3
    isn't reachable. Always called fresh on read so URLs in API responses
    never go stale.

    boto3 walks its credential provider chain (env vars → ~/.aws → EC2
    instance metadata) so we don't have to gate on settings.aws_*. On
    this prod box auth comes from the qcbackend-instance-role, not env
    vars."""
    if not settings.s3_bucket:
        return None
    try:
        s3 = _s3_client(settings)
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": s3_key},
            ExpiresIn=ttl_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("prequal_pdf.presign failed key=%s: %s", s3_key, exc)
        return None
