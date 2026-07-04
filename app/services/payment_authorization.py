from __future__ import annotations

import base64
import hashlib
import html
import logging
from datetime import datetime, timezone
from uuid import UUID

import boto3
from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import Role
from app.models.billing import ClientPaymentMethod, ESignEvent, PaymentAuthorization
from app.models.client import Client
from app.models.user import User

log = logging.getLogger(__name__)

PAYMENT_AUTH_DOCUMENT_VERSION = "2026-07-04-3"


def payment_authorization_document() -> str:
    return """
Qualified Commercial LLC Payment Pre-Authorization and Electronic Signature Consent

By signing this authorization, I authorize QC - Qualified Commercial LLC to keep a payment
method on file through Stripe for expenses related to building, processing, underwriting,
and submitting my funding file.

Authorized expense categories include soft credit pulls, hard credit pulls, property
inspections, appraisals, vendor hard costs, third-party services, document and file
processing costs, and other costs needed to build or advance the funding file. Some costs
may be placed on the HUD or closing statement when permitted by the lender or settlement
workflow. If my loan does not close, accumulated authorized costs may be charged to the
saved payment method.

Qualified Commercial may collect funds from me and remit payment to third-party vendors
or service providers for approved services. Charges will appear under QC - Qualified
Commercial LLC. The system will prompt for payments that require approval before they are
charged, and I may contact my agent or loan underwriter with questions about a charge.
I acknowledge that authorized expenses incurred for my file, including third-party vendor
costs, credit-related costs, inspections, appraisals, and other funding-file costs, remain
my responsibility even if the loan does not close. I agree not to initiate an improper
chargeback, card dispute, reversal, or payment challenge for charges that match this
authorization and were actually incurred or paid for my file. Any billing question or
dispute should be raised directly with Qualified Commercial first so the charge can be
reviewed against the signed authorization, vendor records, and file activity.

I consent to use electronic records and electronic signatures under the U.S. E-SIGN Act
and UETA. I understand my typed legal name, checkbox acknowledgments, drawn signature,
timestamp, IP address, device/browser information, payment token reference, and signed
certificate may be retained as evidence of authorization. I may request a copy of the
authorization record. Withdrawing electronic consent may delay or prevent credit actions,
payment authorization, or funding-file processing.

Qualified Commercial does not store raw card numbers, CVC/CVV, magnetic stripe data, or
full card payloads in its systems. Card details are collected and tokenized by Stripe.
Qualified Commercial stores only Stripe references, card brand, last four digits,
expiration, billing snapshot, and authorization audit records.
""".strip()


def payment_authorization_hash() -> str:
    return hashlib.sha256(payment_authorization_document().encode("utf-8")).hexdigest()


def client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else None


async def latest_authorization(
    db: AsyncSession,
    *,
    user_id: UUID | None = None,
    client_id: UUID | None = None,
) -> PaymentAuthorization | None:
    stmt = select(PaymentAuthorization)
    if user_id is not None:
        stmt = stmt.where(PaymentAuthorization.user_id == user_id)
    if client_id is not None:
        stmt = stmt.where(PaymentAuthorization.client_id == client_id)
    return (
        await db.execute(stmt.order_by(PaymentAuthorization.created_at.desc()).limit(1))
    ).scalar_one_or_none()


async def active_payment_method(
    db: AsyncSession,
    *,
    client_id: UUID,
) -> ClientPaymentMethod | None:
    return (
        await db.execute(
            select(ClientPaymentMethod)
            .where(ClientPaymentMethod.client_id == client_id)
            .where(ClientPaymentMethod.status == "active")
            .order_by(ClientPaymentMethod.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def client_has_completed_payment_authorization(db: AsyncSession, user: User) -> bool:
    if user.role != Role.CLIENT:
        return True
    cid = user.client.id if user.client else None
    if cid is None:
        return False
    auth = (
        await db.execute(
            select(PaymentAuthorization)
            .where(PaymentAuthorization.user_id == user.id)
            .where(PaymentAuthorization.client_id == cid)
            .where(PaymentAuthorization.status == "active")
            .where(PaymentAuthorization.stripe_payment_method_id.is_not(None))
            .order_by(PaymentAuthorization.completed_at.desc().nullslast())
            .limit(1)
        )
    ).scalar_one_or_none()
    if auth is None:
        return False
    method = await active_payment_method(db, client_id=cid)
    return method is not None


async def require_payment_authorized_for_credit(db: AsyncSession, user: User) -> None:
    if user.role != Role.CLIENT:
        return
    if await client_has_completed_payment_authorization(db, user):
        return
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail={
            "code": "payment_authorization_required",
            "message": "Complete the payment pre-authorization before activating credit features.",
        },
    )


async def primary_super_admin(db: AsyncSession) -> User | None:
    settings = get_settings()
    email = (settings.primary_super_admin_email or "").strip().lower()
    if email:
        user = (
            await db.execute(select(User).where(User.email.ilike(email)).limit(1))
        ).scalar_one_or_none()
        if user is not None:
            return user
    return (
        await db.execute(select(User).where(User.role == Role.SUPER_ADMIN).limit(1))
    ).scalar_one_or_none()


def decode_signature_data_url(value: str | None) -> tuple[bytes | None, str | None, str]:
    if not value:
        return None, None, "application/octet-stream"
    content_type = "image/png"
    if "," in value and value.lower().startswith("data:"):
        header, encoded = value.split(",", 1)
        content_type = header[5:].split(";", 1)[0] or content_type
    else:
        encoded = value
    raw = base64.b64decode(encoded, validate=True)
    return raw, hashlib.sha256(raw).hexdigest(), content_type


def put_private_s3_object(*, key: str, body: bytes, content_type: str) -> None:
    settings = get_settings()
    if not settings.s3_bucket:
        raise RuntimeError("S3 bucket is not configured")
    boto3.client("s3", region_name=settings.aws_region).put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        ServerSideEncryption="AES256",
    )


def presign_private_s3_object(key: str | None, *, ttl_seconds: int = 900) -> str | None:
    if not key:
        return None
    settings = get_settings()
    if not settings.s3_bucket:
        return None
    try:
        return boto3.client("s3", region_name=settings.aws_region).generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("payment authorization certificate presign failed key=%s: %s", key, exc)
        return None


def render_certificate_pdf(
    *,
    authorization: PaymentAuthorization,
    user: User,
    client: Client,
    payment_method: ClientPaymentMethod,
) -> bytes:
    from weasyprint import HTML

    signed_at = authorization.signed_at or datetime.now(timezone.utc)
    rows = [
        ("Signer", authorization.typed_name or user.name),
        ("Client email", user.email),
        ("Authorization ID", str(authorization.id)),
        ("Document version", authorization.document_version),
        ("Document SHA-256", authorization.document_hash),
        ("Signed at", signed_at.isoformat()),
        ("IP address", authorization.ip_address or ""),
        ("User agent", authorization.user_agent or ""),
        ("Payment token", payment_method.stripe_payment_method_id),
        ("Card", f"{payment_method.brand or 'card'} ending {payment_method.last4 or '----'}"),
        ("Billing address", ", ".join(x for x in [
            payment_method.billing_line1,
            payment_method.billing_line2,
            payment_method.billing_city,
            payment_method.billing_state,
            payment_method.billing_postal_code,
            payment_method.billing_country,
        ] if x)),
    ]
    row_html = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>"
        for label, value in rows
    )
    doc_text = html.escape(payment_authorization_document()).replace("\n", "<br>")
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
        <h1>Qualified Commercial Payment Authorization Certificate</h1>
        <div class="muted">QC - Qualified Commercial LLC</div>
        <table>{row_html}</table>
        <h2>Authorized Terms</h2>
        <div class="terms">{doc_text}</div>
      </body>
    </html>
    """
    pdf = HTML(string=body).write_pdf()
    if pdf is None:
        raise RuntimeError("weasyprint returned no PDF bytes")
    return pdf


async def log_esign_event(
    db: AsyncSession,
    *,
    authorization: PaymentAuthorization | None,
    user: User | None,
    client_id: UUID | None,
    event_type: str,
    request: Request | None = None,
    metadata: dict | None = None,
) -> None:
    db.add(
        ESignEvent(
            authorization_id=authorization.id if authorization else None,
            user_id=user.id if user else None,
            client_id=client_id,
            event_type=event_type,
            ip_address=client_ip(request),
            user_agent=((request.headers.get("user-agent") or "")[:512] if request else None),
            metadata_json=metadata,
        )
    )
