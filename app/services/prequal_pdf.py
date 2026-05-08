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

import base64
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import Settings
from app.models.app_settings import AppSettings
from app.models.loan import Loan
from app.models.prequal_request import PrequalRequest
from app.schemas.settings import AppSettingsData, LetterheadSettings

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


def _load_letterhead(settings_row: AppSettings | None) -> LetterheadSettings:
    """Pull the letterhead section from app_settings.data, tolerating
    older blobs that didn't have the section. Returns the default
    LetterheadSettings if nothing is configured yet."""
    if settings_row is None:
        return LetterheadSettings()
    raw: dict[str, Any] = settings_row.data or {}
    try:
        return AppSettingsData.model_validate(raw).letterhead
    except Exception:  # noqa: BLE001 — never block a render on a settings parse error
        log.warning("letterhead settings parse failed; using defaults")
        return LetterheadSettings()


def _signature_data_uri(settings: Settings, s3_key: str | None) -> str | None:
    """Fetch the firm's signature image from S3 and return a
    data:image/...;base64 URI suitable for inline <img src=...>.

    WeasyPrint renders inline base64 images natively — embedding rather
    than referencing keeps the PDF self-contained and avoids any
    network dependency at render time.

    Returns None on any failure (missing key, S3 unreachable, etc.). The
    template falls back to a plain underline + typed name when this is
    absent, so a render is never blocked by a missing signature."""
    return _s3_image_data_uri(settings, s3_key)


def _s3_image_data_uri(settings: Settings, s3_key: str | None) -> str | None:
    """Generic S3-image-to-data-URI helper, used by the firm signature
    AND the broker headshot path. Returns None on any failure."""
    if not s3_key or not settings.s3_bucket:
        return None
    try:
        s3 = _s3_client(settings)
        obj = s3.get_object(Bucket=settings.s3_bucket, Key=s3_key)
        body = obj["Body"].read()
        content_type = obj.get("ContentType") or "image/png"
        b64 = base64.b64encode(body).decode("ascii")
        return f"data:{content_type};base64,{b64}"
    except Exception as exc:  # noqa: BLE001
        log.warning("s3 image fetch failed key=%s: %s", s3_key, exc)
        return None


def _broker_letterhead_for(loan: Loan | None) -> dict[str, Any] | None:
    """Return the broker's letterhead JSONB blob if the loan has a
    broker with settings_data.letterhead set. The caller composites
    `headshot_s3_key` (preferred) or `headshot_data_url` (legacy)
    onto the prequal header.

    Tolerant: if the JSONB is malformed, returns None rather than
    blocking the render."""
    if loan is None or getattr(loan, "broker", None) is None:
        return None
    raw = getattr(loan.broker, "settings_data", None) or {}
    letterhead = raw.get("letterhead") if isinstance(raw, dict) else None
    if isinstance(letterhead, dict):
        return letterhead
    return None


def _broker_headshot_data_uri(
    settings: Settings, letterhead: dict[str, Any] | None
) -> str | None:
    """Resolve the broker's headshot to an inline data URI.

    Priority:
      1. `headshot_s3_key` — S3 fetch + base64 (production path)
      2. `headshot_data_url` — pass-through (v1 base64 inline)
      3. None
    """
    if not letterhead:
        return None
    s3_key = letterhead.get("headshot_s3_key")
    if s3_key:
        uri = _s3_image_data_uri(settings, s3_key)
        if uri:
            return uri
    inline = letterhead.get("headshot_data_url")
    if isinstance(inline, str) and inline.startswith("data:"):
        return inline
    return None


# Common variants of "TBD" the borrower / admin might have typed instead
# of leaving the LLC field blank. Treated identically to None.
_TBD_TOKENS = {"tbd", "t.b.d.", "to be determined", "n/a", "na", "none", "-"}


def _looks_like_tbd(value: str | None) -> bool:
    if value is None:
        return True
    return value.strip().lower() in _TBD_TOKENS


def _resolve_borrower_entity(
    loan: Loan | None,
    request: PrequalRequest | None = None,
    *,
    fallback_individual_name: str | None = None,
) -> str:
    """Resolve the name printed on the letter's "Borrower / Entity Name"
    row. Priority:

       1. request.borrower_entity     (borrower-typed LLC, or admin
                                       override). Skipped if it looks
                                       like a TBD placeholder.
       2. loan.entity_name            (set when promoted to a Loan)
       3. individual legal name       (loan.client.name, or the
                                       fallback supplied by the caller
                                       when no loan exists yet)
                                       → suffixed with "and/or
                                       Assignee LLC (TBD)" so the
                                       seller's attorney sees the
                                       guarantor while the borrower
                                       keeps the right to assign the
                                       contract to a freshly-formed
                                       LLC at closing.
       4. "Borrower" (last-resort generic).

    The TBD-suffix in (3) is the "Negotiation Shield" pattern — by
    listing the individual *and/or* a future assignee LLC, the borrower
    doesn't tip the seller on whether the entity exists yet, and they
    keep the option to drop a new LLC into the contract on closing day
    without renegotiating."""
    if request is not None:
        be = getattr(request, "borrower_entity", None)
        if be and not _looks_like_tbd(be):
            return str(be).strip()

    if loan is not None:
        entity = getattr(loan, "entity_name", None)
        if entity and not _looks_like_tbd(entity):
            return str(entity).strip()

    individual: str | None = None
    if loan is not None:
        client = getattr(loan, "client", None)
        if client is not None:
            individual = getattr(client, "name", None)
    if not individual:
        individual = fallback_individual_name

    if individual:
        return f"{individual.strip()} and/or Assignee LLC (TBD)"

    return "Borrower"


def render_letter(
    request: PrequalRequest,
    loan: Loan | None = None,
    *,
    settings: Settings,
    expiration_days: int | None = None,
    quote_number: str | None = None,
    settings_row: AppSettings | None = None,
    fallback_individual_name: str | None = None,
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

    # Template selection — DSCR variants share one template (refi just
    # flips the title via is_refi); F&F uses its own; Bridge uses the
    # short-term template.
    if request.loan_type in ("dscr_purchase", "dscr_refi"):
        template_name = "prequal_dscr.html"
    elif request.loan_type == "fix_flip":
        template_name = "prequal_fix_flip.html"
    else:
        template_name = "prequal_bridge.html"

    # Letterhead — configurable by super admin via Settings → Firm
    # letterhead. When the row hasn't been edited yet this falls back to
    # the typed defaults in LetterheadSettings.
    letterhead = _load_letterhead(settings_row)
    signature_data_uri = _signature_data_uri(settings, letterhead.signature_s3_key)

    # Co-branded "from your agent" card. Pulled off the loan's broker
    # row when present; absent on direct-borrower deals.
    broker_letterhead = _broker_letterhead_for(loan)
    broker_headshot_data_uri = _broker_headshot_data_uri(settings, broker_letterhead)
    broker_user = getattr(loan.broker, "user", None) if loan and getattr(loan, "broker", None) else None
    broker_name = getattr(broker_user, "name", None) if broker_user else None
    broker_title = (broker_letterhead or {}).get("title")
    broker_brokerage = (broker_letterhead or {}).get("brokerage_name")
    broker_license = (broker_letterhead or {}).get("license_number")

    tpl = _jinja.get_template(template_name)
    html_str = tpl.render(
        today_date=_format_date(today),
        expiration_date=_format_date(expires),
        borrower_entity=_resolve_borrower_entity(
            loan, request, fallback_individual_name=fallback_individual_name
        ),
        target_property_address=request.target_property_address,
        purchase_price=_format_usd(purchase),
        max_loan_amount=_format_usd(loan_amount),
        max_ltv=ltv_pct,
        quote_number=quote_number or request.quote_number,
        # DSCR template branches title/intro on this. None elsewhere.
        is_refi=(request.loan_type == "dscr_refi"),
        # Configurable letterhead — see app/schemas/settings.py
        # LetterheadSettings. None means "use the template default".
        officer_name=letterhead.officer_name,
        officer_title=letterhead.officer_title,
        office_address_line_1=letterhead.office_address_line_1,
        office_address_line_2=letterhead.office_address_line_2,
        office_address_line_3=letterhead.office_address_line_3,
        # Inline base64 PNG (or None). Templates render the image when
        # present, or fall back to a plain signature line when absent.
        signature_data_uri=signature_data_uri,
        signed_date=_format_date(today),
        # Broker / agent co-branding card. Header renders when
        # broker_headshot_data_uri is present; fields cascade off
        # the broker row + their letterhead JSONB.
        broker_headshot_data_uri=broker_headshot_data_uri,
        broker_name=broker_name,
        broker_title=broker_title,
        broker_brokerage=broker_brokerage,
        broker_license=broker_license,
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
