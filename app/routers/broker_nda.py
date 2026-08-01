"""Broker NDA / non-solicitation e-signature endpoints.

  - GET  /broker/nda/status      — whether the current user needs to sign,
                                    and the version they'd be signing.
  - POST /broker/nda/sign        — sign it (dealer-partner only).
  - GET  /broker/nda/certificate — super-admin: presigned download URL for a
                                    given user's latest signed certificate
                                    (dispute/audit use).

The hard access-block itself lives in dealer_ai_intake.py's
`_require_nda_signed`, chained onto every broker_router endpoint — this
router only exposes the sign/status/read primitives.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.broker_nda_acceptance import BrokerNdaAcceptance
from app.models.user import User
from app.schemas.common import ORMModel
from app.services import broker_nda as nda_service

router = APIRouter(prefix="/broker/nda", tags=["broker-nda"])


def _require_super_admin(user: User) -> None:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")


class NdaStatus(BaseModel):
    required: bool
    signed_at: datetime | None
    document_version: str


class NdaSignRequest(BaseModel):
    typed_name: str = Field(min_length=1, max_length=160)
    esign_consent: bool
    signature_data_url: str = Field(min_length=1)
    prior_relationships_disclosure: str | None = Field(default=None, max_length=8000)


class NdaAcceptanceRead(ORMModel):
    id: UUID
    user_id: UUID
    document_version: str
    typed_name: str
    esign_consent: bool
    prior_relationships_disclosure: str | None
    ip_address: str | None
    user_agent: str | None
    signed_at: datetime | None
    created_at: datetime


@router.get("/status", response_model=NdaStatus)
async def nda_status(user: CurrentUser) -> NdaStatus:
    return NdaStatus(
        required=user.role == Role.DEALER_PARTNER and user.nda_signed_at is None,
        signed_at=user.nda_signed_at,
        document_version=nda_service.BROKER_NDA_DOCUMENT_VERSION,
    )


@router.post("/sign", response_model=NdaAcceptanceRead, status_code=201)
async def sign_nda(
    payload: NdaSignRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BrokerNdaAcceptance:
    if user.role != Role.DEALER_PARTNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Dealer partner role required")
    if not payload.esign_consent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "E-SIGN consent is required")

    document_text = nda_service.broker_nda_document_text()
    doc_hash = nda_service.document_hash(document_text)

    sig_bytes, sig_hash, sig_content_type = nda_service.decode_signature_data_url(payload.signature_data_url)
    if not sig_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A drawn signature is required")

    now = datetime.now(timezone.utc)
    disclosure = (payload.prior_relationships_disclosure or "").strip() or None

    acceptance = BrokerNdaAcceptance(
        user_id=user.id,
        document_version=nda_service.BROKER_NDA_DOCUMENT_VERSION,
        document_hash=doc_hash,
        typed_name=payload.typed_name.strip(),
        esign_consent=True,
        prior_relationships_disclosure=disclosure,
        ip_address=nda_service.client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
        signed_at=now,
    )
    db.add(acceptance)
    await db.flush()

    sig_ext = "png" if "png" in sig_content_type else "bin"
    sig_key = f"broker-nda/{user.id}/{acceptance.id}/signature.{sig_ext}"
    nda_service.put_private_s3_object(key=sig_key, body=sig_bytes, content_type=sig_content_type)
    acceptance.signature_s3_key = sig_key
    acceptance.signature_hash = sig_hash

    extra_rows: list[tuple[str, str]] = []
    if disclosure:
        extra_rows.append(("Prior relationships disclosed", disclosure))
    pdf_bytes = nda_service.render_signature_certificate_pdf(
        signature=acceptance,
        title="Dealer Partner NDA — Signed Certificate",
        document_text=document_text,
        extra_rows=extra_rows,
    )
    cert_key = f"broker-nda/{user.id}/{acceptance.id}/certificate.pdf"
    nda_service.put_private_s3_object(key=cert_key, body=pdf_bytes, content_type="application/pdf")
    acceptance.certificate_s3_key = cert_key
    acceptance.certificate_hash = hashlib.sha256(pdf_bytes).hexdigest()

    user.nda_signed_at = now

    await db.commit()
    await db.refresh(acceptance)
    return acceptance


@router.get("/certificate", response_model=None)
async def nda_certificate(
    user_id: UUID,
    admin: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | None]:
    _require_super_admin(admin)
    acceptance = (
        await db.execute(
            select(BrokerNdaAcceptance)
            .where(BrokerNdaAcceptance.user_id == user_id)
            .order_by(BrokerNdaAcceptance.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if acceptance is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No signed NDA found for this user")
    return {
        "download_url": nda_service.presign_private_s3_object(acceptance.certificate_s3_key),
        "signed_at": acceptance.signed_at.isoformat() if acceptance.signed_at else None,
    }
