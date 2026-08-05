"""Generic e-sign endpoints for the platform's real contract templates (see
app.enums.ContractType, app.services.contract_templates).

  - GET  /contracts/{contract_type}/status — whether the CURRENT subject
    (the authenticated user, or their linked company for company-scoped
    types) needs to sign, and the fields/version they'd be signing.
  - POST /contracts/{contract_type}/sign   — sign it.
  - GET  /contracts/{contract_type}/certificate — super-admin: presigned
    download URL for a given subject's latest signed certificate.

Individual-scoped types (PLATFORM_ACCESS) resolve the subject to the
authenticated user. Company-scoped types (REFERRAL_PROTECTION) resolve the
subject to the user's linked ReferralPartnerCompany (via
referral_partner_company_id) for /status and /sign when called by an
authenticated user, OR accept an explicit company_name for the public,
token-free "fill and sign" portal flow — the public portal has no logged-in
user at all, so /sign there creates the company on demand (find-or-create by
name) rather than resolving one from a session.

The three client-facing types (SBA_ENGAGEMENT, CLIENT_ENGAGEMENT,
CONSULTING_ADDENDUM) are NOT exposed through this router at all — those are
delivered via the existing BucketRequestedDocument/chat sign flow in
dealer_ai_intake.py (an admin renders the filled text via
render_contract_document() and stores it as signature_document_text; the
client signs it through the mechanism that already exists, unchanged).
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
from app.enums import ContractSubjectType, ContractType, Role
from app.models.contract_agreement import ContractAgreement
from app.models.referral_partner_company import ReferralPartnerCompany
from app.models.user import User
from app.services import contract_templates as tpl

router = APIRouter(prefix="/contracts", tags=["contracts"])

# Which types are signed by an individual User vs. a ReferralPartnerCompany.
# The three client-facing types never reach this router (see module docstring).
_INDIVIDUAL_SCOPED = {ContractType.PLATFORM_ACCESS}
_COMPANY_SCOPED = {ContractType.REFERRAL_PROTECTION}
_ROUTABLE = _INDIVIDUAL_SCOPED | _COMPANY_SCOPED


def _require_super_admin(user: User) -> None:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")


def _require_routable(contract_type: ContractType) -> None:
    if contract_type not in _ROUTABLE:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "This contract type is not signed through this endpoint",
        )


class ContractFieldRead(BaseModel):
    name: str
    label: str
    field_type: str
    default: str
    row_group: str | None
    in_scope_for_initial_signing: bool


class ContractSectionRead(BaseModel):
    heading: str
    paragraphs: list[str]


class ContractDocumentRead(BaseModel):
    title: str
    party_facing_notice: str | None
    preamble: list[str]
    sections: list[ContractSectionRead]


class ContractStatus(BaseModel):
    required: bool
    signed_at: datetime | None
    document_version: str
    document: ContractDocumentRead
    fields: list[ContractFieldRead]
    certificate_download_url: str | None = None


class ContractSignRequest(BaseModel):
    typed_name: str = Field(min_length=1, max_length=160)
    esign_consent: bool
    signature_data_url: str = Field(min_length=1)
    field_values: dict[str, str] = Field(default_factory=dict)
    # Public-portal-only (no authenticated user to read an email from):
    # where to send the signed copy + certificate. Ignored for
    # individual-scoped types, which use the authenticated user's own email.
    signer_email: str | None = Field(default=None, max_length=320)
    # Company-scoped, public-portal-only: the company signing (find-or-create
    # by name). Ignored for individual-scoped types.
    company_name: str | None = Field(default=None, max_length=255)
    company_entity_type: str | None = Field(default=None, max_length=64)
    company_state_of_formation: str | None = Field(default=None, max_length=64)
    company_principal_address: str | None = Field(default=None, max_length=512)


class ContractAgreementRead(BaseModel):
    id: UUID
    contract_type: str
    contract_number: str
    subject_type: str
    subject_id: UUID
    typed_name: str
    esign_consent: bool
    signed_at: datetime | None
    certificate_download_url: str | None = None


async def _latest_agreement(
    db: AsyncSession, *, contract_type: ContractType, subject_type: ContractSubjectType, subject_id: UUID
) -> ContractAgreement | None:
    return (
        await db.execute(
            select(ContractAgreement)
            .where(
                ContractAgreement.contract_type == contract_type,
                ContractAgreement.subject_type == subject_type,
                ContractAgreement.subject_id == subject_id,
            )
            .order_by(ContractAgreement.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _document_read(contract_type: ContractType, field_values: dict[str, str] | None = None) -> ContractDocumentRead:
    rendered = tpl.render_contract_document(contract_type, field_values or {})
    return ContractDocumentRead(
        title=rendered.title,
        party_facing_notice=rendered.party_facing_notice,
        preamble=rendered.preamble,
        sections=[ContractSectionRead(heading=s.heading, paragraphs=s.paragraphs) for s in rendered.sections],
    )


def _fields_read(contract_type: ContractType) -> list[ContractFieldRead]:
    spec = tpl.get_template_spec(contract_type)
    return [
        ContractFieldRead(
            name=f.name,
            label=f.label,
            field_type=f.field_type,
            default=f.default,
            row_group=f.row_group,
            in_scope_for_initial_signing=f.in_scope_for_initial_signing,
        )
        for f in spec.fields.values()
    ]


class ContractPreview(BaseModel):
    document_version: str
    document: ContractDocumentRead
    fields: list[ContractFieldRead]


# Public, token-free, unauthenticated: powers the agreement.qualifiedcommercial.com
# portal, which has no logged-in user at all and needs the blank template +
# field schema before anyone fills anything in. Only company-scoped types are
# exposed here -- PLATFORM_ACCESS is signed from inside the app (its preview
# already comes from the authenticated /status endpoint) and the 3
# client-facing types never reach this router at all (see module docstring).
_PUBLIC_PREVIEWABLE = _COMPANY_SCOPED


@router.get("/{contract_type}/preview", response_model=ContractPreview)
async def contract_preview(contract_type: ContractType) -> ContractPreview:
    if contract_type not in _PUBLIC_PREVIEWABLE:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This contract type has no public preview")
    return ContractPreview(
        document_version=tpl.CONTRACT_DOCUMENT_VERSIONS[contract_type],
        document=_document_read(contract_type, None),
        fields=_fields_read(contract_type),
    )


async def _next_contract_number(db: AsyncSession, contract_type: ContractType) -> str:
    from sqlalchemy import text

    seq = (await db.execute(text("SELECT nextval('contract_number_seq')"))).scalar_one()
    year = datetime.now(timezone.utc).year
    code = tpl.CONTRACT_TYPE_CODE[contract_type]
    return f"QC-{code}-{year}-{seq:05d}"


@router.get("/{contract_type}/status", response_model=ContractStatus)
async def contract_status(
    contract_type: ContractType,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContractStatus:
    _require_routable(contract_type)
    required = user.role == Role.DEALER_PARTNER

    if contract_type in _INDIVIDUAL_SCOPED:
        agreement = await _latest_agreement(
            db, contract_type=contract_type, subject_type=ContractSubjectType.USER, subject_id=user.id
        )
    else:  # company-scoped
        agreement = None
        if user.referral_partner_company_id is not None:
            agreement = await _latest_agreement(
                db,
                contract_type=contract_type,
                subject_type=ContractSubjectType.COMPANY,
                subject_id=user.referral_partner_company_id,
            )

    signed_at = agreement.signed_at if agreement else None
    cert_url = tpl.presign_private_s3_object(agreement.certificate_s3_key) if agreement else None
    field_values = {k: str(v) for k, v in (agreement.field_values or {}).items()} if agreement else None
    return ContractStatus(
        required=required and signed_at is None,
        signed_at=signed_at,
        document_version=tpl.CONTRACT_DOCUMENT_VERSIONS[contract_type],
        document=_document_read(contract_type, field_values),
        fields=_fields_read(contract_type),
        certificate_download_url=cert_url,
    )


async def _find_or_create_company(db: AsyncSession, payload: ContractSignRequest) -> ReferralPartnerCompany:
    name = (payload.company_name or "").strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Company name is required")
    existing = (
        await db.execute(select(ReferralPartnerCompany).where(ReferralPartnerCompany.name.ilike(name)))
    ).scalar_one_or_none()
    if existing:
        return existing
    company = ReferralPartnerCompany(
        name=name,
        entity_type=(payload.company_entity_type or "").strip() or None,
        state_of_formation=(payload.company_state_of_formation or "").strip() or None,
        principal_address=(payload.company_principal_address or "").strip() or None,
    )
    db.add(company)
    await db.flush()
    return company


@router.post("/platform-access/sign", response_model=ContractAgreementRead, status_code=201)
async def sign_platform_access(
    payload: ContractSignRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContractAgreement:
    if user.role != Role.DEALER_PARTNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Dealer partner role required")
    return await _sign(
        db,
        request,
        contract_type=ContractType.PLATFORM_ACCESS,
        subject_type=ContractSubjectType.USER,
        subject_id=user.id,
        payload=payload,
        notify_email=user.email,
    )


@router.post("/referral-protection/sign", response_model=ContractAgreementRead, status_code=201)
async def sign_referral_protection(
    payload: ContractSignRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ContractAgreement:
    """Public, token-free endpoint (the agreement.qualifiedcommercial.com
    portal) — no authenticated user. Resolves/creates the company by name."""
    company = await _find_or_create_company(db, payload)
    return await _sign(
        db,
        request,
        contract_type=ContractType.REFERRAL_PROTECTION,
        subject_type=ContractSubjectType.COMPANY,
        subject_id=company.id,
        payload=payload,
        notify_email=payload.signer_email,
    )


async def _sign(
    db: AsyncSession,
    request: Request,
    *,
    contract_type: ContractType,
    subject_type: ContractSubjectType,
    subject_id: UUID,
    payload: ContractSignRequest,
    notify_email: str | None = None,
) -> ContractAgreement:
    if not payload.esign_consent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "E-SIGN consent is required")

    rendered = tpl.render_contract_document(contract_type, payload.field_values)
    doc_hash = tpl.contract_document_hash(rendered)

    sig_bytes, sig_hash, sig_content_type = tpl.decode_signature_data_url(payload.signature_data_url)
    if not sig_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A drawn signature is required")

    now = datetime.now(timezone.utc)
    contract_number = await _next_contract_number(db, contract_type)

    agreement = ContractAgreement(
        contract_type=contract_type,
        contract_number=contract_number,
        subject_type=subject_type,
        subject_id=subject_id,
        field_values=payload.field_values,
        document_version=rendered.document_version,
        document_hash=doc_hash,
        typed_name=payload.typed_name.strip(),
        esign_consent=True,
        ip_address=tpl.client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
        signed_at=now,
    )
    db.add(agreement)
    await db.flush()

    sig_ext = "png" if "png" in sig_content_type else "bin"
    sig_key = f"contracts/{contract_type}/{agreement.id}/signature.{sig_ext}"
    tpl.put_private_s3_object(key=sig_key, body=sig_bytes, content_type=sig_content_type)
    agreement.signature_s3_key = sig_key
    agreement.signature_hash = sig_hash

    pdf_bytes = tpl.render_contract_certificate_pdf(
        rendered=rendered,
        contract_number=contract_number,
        typed_name=agreement.typed_name,
        document_hash=doc_hash,
        ip_address=agreement.ip_address,
        user_agent=agreement.user_agent,
        signed_at=now,
    )
    cert_key = f"contracts/{contract_type}/{agreement.id}/certificate.pdf"
    tpl.put_private_s3_object(key=cert_key, body=pdf_bytes, content_type="application/pdf")
    agreement.certificate_s3_key = cert_key
    agreement.certificate_hash = hashlib.sha256(pdf_bytes).hexdigest()

    await db.commit()
    await db.refresh(agreement)

    if notify_email:
        _send_signed_copy_email(
            to_email=notify_email,
            title=rendered.title,
            contract_number=contract_number,
            typed_name=agreement.typed_name,
            pdf_bytes=pdf_bytes,
        )

    return agreement


def _send_signed_copy_email(*, to_email: str, title: str, contract_number: str, typed_name: str, pdf_bytes: bytes) -> None:
    """E-SIGN-compliant delivery of the signer's own copy: the certificate
    PDF attached directly to the confirmation email (not just a download
    link, which could expire or be inaccessible), plus the paper-copy and
    withdraw-consent instructions the checkbox disclosure already promised."""
    from app.services.email.ses_client import send_raw_email

    subject = f"Signed: {title} ({contract_number})"
    body_text = (
        f"Hello {typed_name},\n\n"
        f"Attached is your signed copy of the {title} (Contract No. {contract_number}), "
        "executed electronically under the U.S. E-SIGN Act and UETA.\n\n"
        "You may request a paper copy of this signed agreement at any time, or withdraw your consent "
        "to electronic records prospectively, by contacting support@qualifiedcommercial.com.\n\n"
        "Qualified Commercial LLC"
    )
    send_raw_email(
        to_emails=[to_email],
        subject=subject,
        body_text=body_text,
        attachments=[(f"{contract_number}.pdf", pdf_bytes, "application/pdf")],
    )


@router.get("/{contract_type}/certificate", response_model=None)
async def contract_certificate(
    contract_type: ContractType,
    subject_id: UUID,
    admin: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | None]:
    _require_routable(contract_type)
    _require_super_admin(admin)
    subject_type = ContractSubjectType.USER if contract_type in _INDIVIDUAL_SCOPED else ContractSubjectType.COMPANY
    agreement = await _latest_agreement(
        db, contract_type=contract_type, subject_type=subject_type, subject_id=subject_id
    )
    if agreement is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No signed agreement found for this subject")
    return {
        "download_url": tpl.presign_private_s3_object(agreement.certificate_s3_key),
        "signed_at": agreement.signed_at.isoformat() if agreement.signed_at else None,
        "contract_number": agreement.contract_number,
    }
