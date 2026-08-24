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
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import ContractSubjectType, ContractType, Role
from app.models.contract_agreement import ContractAgreement
from app.models.agreement_counterparty import AgreementCounterparty
from app.models.public_contract_sign_session import PublicContractSignSession
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


class TableColumnRead(BaseModel):
    key: str
    label: str
    input_type: str
    options: list[str] | None = None


class ContractFieldRead(BaseModel):
    name: str
    label: str
    field_type: str
    default: str
    row_group: str | None
    in_scope_for_initial_signing: bool
    table_columns: list[TableColumnRead] | None = None


class ContractSectionRead(BaseModel):
    heading: str
    paragraphs: list[str]
    columns: list[str] | None = None
    rows: list[list[str]] | None = None
    disclosure_field: str | None = None


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
    # Any: a "disclosure_rows"-type field's value is a list[dict] (Schedule
    # A's signer-submitted capital-relationship rows), not a scalar string --
    # render_contract_document() validates the shape defensively per field.
    field_values: dict[str, Any] = Field(default_factory=dict)
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
    email_delivery_status: str | None = None


class PublicContractSessionRequest(BaseModel):
    honeypot: str = Field(default="", max_length=200)


class PublicContractSessionRead(BaseModel):
    token: str
    expires_at: datetime


class MutualNdaSignRequest(ContractSignRequest):
    signer_email: EmailStr
    signature_data_url: str = Field(min_length=1, max_length=2_000_000)
    public_session_token: str = Field(min_length=32, max_length=256)
    honeypot: str = Field(default="", max_length=200)
    no_preexisting_relationships: bool = False


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


def _document_read(contract_type: ContractType, field_values: dict[str, Any] | None = None) -> ContractDocumentRead:
    rendered = tpl.render_contract_document(contract_type, field_values or {})
    return ContractDocumentRead(
        title=rendered.title,
        party_facing_notice=rendered.party_facing_notice,
        preamble=rendered.preamble,
        sections=[
            ContractSectionRead(
                heading=s.heading,
                paragraphs=s.paragraphs,
                columns=s.columns,
                rows=s.rows,
                disclosure_field=s.disclosure_field,
            )
            for s in rendered.sections
        ],
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
            table_columns=(
                [
                    TableColumnRead(key=c.key, label=c.label, input_type=c.input_type, options=c.options)
                    for c in f.table_columns
                ]
                if f.table_columns
                else None
            ),
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
_PUBLIC_PREVIEWABLE = _COMPANY_SCOPED | {ContractType.MUTUAL_NDA_NON_CIRCUMVENTION}


@router.get("/{contract_type}/preview", response_model=ContractPreview)
async def contract_preview(contract_type: ContractType) -> ContractPreview:
    if contract_type not in _PUBLIC_PREVIEWABLE:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This contract type has no public preview")
    return ContractPreview(
        document_version=tpl.CONTRACT_DOCUMENT_VERSIONS[contract_type],
        document=_document_read(contract_type, None),
        fields=_fields_read(contract_type),
    )


class ContractRenderRequest(BaseModel):
    field_values: dict[str, Any] = Field(default_factory=dict)


@router.post("/{contract_type}/render", response_model=ContractPreview)
async def contract_render(contract_type: ContractType, payload: ContractRenderRequest) -> ContractPreview:
    """Same public, token-free surface as /preview, but folds the signer's
    own typed values into the document so the portal can show them the
    actual filled agreement (review step) before they sign it."""
    if contract_type not in _PUBLIC_PREVIEWABLE:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This contract type has no public preview")
    return ContractPreview(
        document_version=tpl.CONTRACT_DOCUMENT_VERSIONS[contract_type],
        document=_document_read(contract_type, payload.field_values),
        fields=_fields_read(contract_type),
    )


_PUBLIC_SESSION_TTL = timedelta(minutes=20)
_PUBLIC_SESSION_RATE_WINDOW = timedelta(minutes=15)
_PUBLIC_SESSION_RATE_LIMIT = 5
_PUBLIC_SESSION_MAX_ATTEMPTS = 5


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _ip_hash(request: Request) -> str:
    ip = tpl.client_ip(request) or "unknown"
    return hashlib.sha256(f"public-contract:{ip}".encode("utf-8")).hexdigest()


@router.post(
    "/mutual-nda-non-circumvention/public-session",
    response_model=PublicContractSessionRead,
    status_code=201,
)
async def create_mutual_nda_public_session(
    payload: PublicContractSessionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PublicContractSessionRead:
    if payload.honeypot:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unable to create signing session")

    now = datetime.now(timezone.utc)
    ip_hash = _ip_hash(request)
    recent_count = (
        await db.execute(
            select(func.count(PublicContractSignSession.id)).where(
                PublicContractSignSession.ip_hash == ip_hash,
                PublicContractSignSession.created_at >= now - _PUBLIC_SESSION_RATE_WINDOW,
            )
        )
    ).scalar_one()
    if recent_count >= _PUBLIC_SESSION_RATE_LIMIT:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Please wait before starting another signing session")

    raw_token = secrets.token_urlsafe(48)
    expires_at = now + _PUBLIC_SESSION_TTL
    session = PublicContractSignSession(
        contract_type=ContractType.MUTUAL_NDA_NON_CIRCUMVENTION,
        token_hash=_token_hash(raw_token),
        ip_hash=ip_hash,
        expires_at=expires_at,
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
    )
    db.add(session)
    await db.commit()
    return PublicContractSessionRead(token=raw_token, expires_at=expires_at)


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


def _normalize_identity(value: str) -> str:
    return " ".join(value.casefold().split())


async def _fail_public_session(
    db: AsyncSession,
    session: PublicContractSignSession,
    detail: str,
    *,
    status_code: int = status.HTTP_400_BAD_REQUEST,
) -> None:
    session.attempt_count += 1
    if session.attempt_count >= _PUBLIC_SESSION_MAX_ATTEMPTS:
        session.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    raise HTTPException(status_code, detail)


async def _find_or_create_counterparty(
    db: AsyncSession,
    *,
    legal_name: str,
    entity_type: str,
    state_of_formation: str,
    principal_address: str,
    signer_email: str,
) -> AgreementCounterparty:
    normalized_name = _normalize_identity(legal_name)
    normalized_state = _normalize_identity(state_of_formation)
    counterparty = (
        await db.execute(
            select(AgreementCounterparty).where(
                AgreementCounterparty.normalized_legal_name == normalized_name,
                AgreementCounterparty.normalized_state_of_formation == normalized_state,
            )
        )
    ).scalar_one_or_none()
    if counterparty is None:
        counterparty = AgreementCounterparty(
            legal_name=legal_name,
            normalized_legal_name=normalized_name,
            entity_type=entity_type,
            state_of_formation=state_of_formation,
            normalized_state_of_formation=normalized_state,
            principal_business_address=principal_address,
            signer_email=signer_email,
        )
        db.add(counterparty)
        await db.flush()
    else:
        counterparty.legal_name = legal_name
        counterparty.entity_type = entity_type
        counterparty.state_of_formation = state_of_formation
        counterparty.principal_business_address = principal_address
        counterparty.signer_email = signer_email
    return counterparty


def _agreement_read(agreement: ContractAgreement) -> ContractAgreementRead:
    return ContractAgreementRead(
        id=agreement.id,
        contract_type=agreement.contract_type,
        contract_number=agreement.contract_number,
        subject_type=agreement.subject_type,
        subject_id=agreement.subject_id,
        typed_name=agreement.typed_name,
        esign_consent=agreement.esign_consent,
        signed_at=agreement.signed_at,
        certificate_download_url=tpl.presign_private_s3_object(agreement.certificate_s3_key),
        email_delivery_status=agreement.email_delivery_status,
    )


@router.post(
    "/mutual-nda-non-circumvention/sign",
    response_model=ContractAgreementRead,
    status_code=201,
)
async def sign_mutual_nda_non_circumvention(
    payload: MutualNdaSignRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ContractAgreementRead:
    if payload.honeypot:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unable to sign agreement")

    now = datetime.now(timezone.utc)
    session = (
        await db.execute(
            select(PublicContractSignSession)
            .where(PublicContractSignSession.token_hash == _token_hash(payload.public_session_token))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if session is None or session.contract_type != ContractType.MUTUAL_NDA_NON_CIRCUMVENTION:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Signing session is invalid")
    if session.revoked_at is not None or session.attempt_count >= _PUBLIC_SESSION_MAX_ATTEMPTS:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Signing session is no longer valid")
    if session.used_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Signing session has already been used")
    if session.expires_at <= now:
        await _fail_public_session(db, session, "Signing session has expired", status_code=status.HTTP_410_GONE)
    if session.ip_hash != _ip_hash(request):
        await _fail_public_session(db, session, "Signing session is invalid", status_code=status.HTTP_401_UNAUTHORIZED)

    required_fields = (
        "effective_date",
        "counterparty_legal_name",
        "counterparty_entity_type",
        "counterparty_state_of_formation",
        "counterparty_principal_address",
        "counterparty_signer_name",
        "counterparty_signer_title",
        "counterparty_signer_email",
    )
    clean: dict[str, Any] = {}
    maximum_lengths = {
        "effective_date": 10,
        "counterparty_legal_name": 255,
        "counterparty_entity_type": 80,
        "counterparty_state_of_formation": 80,
        "counterparty_principal_address": 512,
        "counterparty_signer_name": 160,
        "counterparty_signer_title": 160,
        "counterparty_signer_email": 320,
    }
    for field_name in required_fields:
        value = str(payload.field_values.get(field_name) or "").strip()
        if not value:
            await _fail_public_session(db, session, f"{field_name.replace('_', ' ').title()} is required")
        if len(value) > maximum_lengths[field_name]:
            await _fail_public_session(db, session, f"{field_name.replace('_', ' ').title()} is too long")
        clean[field_name] = value

    if _normalize_identity(clean["counterparty_signer_name"]) != _normalize_identity(payload.typed_name):
        await _fail_public_session(db, session, "Typed legal name must match the signer name")
    if clean["counterparty_signer_email"].casefold() != str(payload.signer_email).casefold():
        await _fail_public_session(db, session, "Signer email does not match the agreement")

    submitted_rows = payload.field_values.get("preexisting_relationship_rows") or []
    if not isinstance(submitted_rows, list):
        await _fail_public_session(db, session, "Exhibit A relationships are invalid")
    if len(submitted_rows) > 25:
        await _fail_public_session(db, session, "Exhibit A supports up to 25 relationships")
    clean_rows: list[dict[str, str]] = []
    for row in submitted_rows:
        if not isinstance(row, dict):
            await _fail_public_session(db, session, "Exhibit A relationships are invalid")
        clean_row = {
            key: str(row.get(key) or "").strip()
            for key in ("name", "category", "description", "start_date")
        }
        if any(clean_row.values()) and not all(clean_row.values()):
            await _fail_public_session(db, session, "Complete every field in each Exhibit A row")
        if any(len(value) > 500 for value in clean_row.values()):
            await _fail_public_session(db, session, "An Exhibit A value is too long")
        if all(clean_row.values()):
            clean_rows.append(clean_row)

    if payload.no_preexisting_relationships and clean_rows:
        await _fail_public_session(db, session, "Choose either disclosed relationships or no relationships")
    if not payload.no_preexisting_relationships and not clean_rows:
        await _fail_public_session(db, session, "Complete Exhibit A or confirm there are no relationships")

    clean.update(
        {
            "counterparty_signature_date": clean["effective_date"],
            "qc_signatory_name": "Jonathan Franco",
            "qc_signature_date": clean["effective_date"],
            "preexisting_relationship_declaration": (
                "No pre-existing relationships to disclose."
                if payload.no_preexisting_relationships
                else "The following pre-existing relationships are disclosed before execution of this Agreement."
            ),
            "preexisting_relationship_rows": clean_rows,
        }
    )
    signed_payload = payload.model_copy(update={"field_values": clean})
    counterparty = await _find_or_create_counterparty(
        db,
        legal_name=clean["counterparty_legal_name"],
        entity_type=clean["counterparty_entity_type"],
        state_of_formation=clean["counterparty_state_of_formation"],
        principal_address=clean["counterparty_principal_address"],
        signer_email=str(payload.signer_email),
    )
    session.used_at = now
    agreement = await _sign(
        db,
        request,
        contract_type=ContractType.MUTUAL_NDA_NON_CIRCUMVENTION,
        subject_type=ContractSubjectType.COUNTERPARTY,
        subject_id=counterparty.id,
        payload=signed_payload,
        notify_email=str(payload.signer_email),
    )
    return _agreement_read(agreement)


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
        contract_type=contract_type,
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
        delivery = _send_signed_copy_email(
            to_email=notify_email,
            title=rendered.title,
            contract_number=contract_number,
            typed_name=agreement.typed_name,
            pdf_bytes=pdf_bytes,
        )
        agreement.email_delivery_status = "sent" if delivery.ok else "failed"
        agreement.email_delivery_message_id = delivery.message_id
        agreement.email_delivery_error = None if delivery.ok else delivery.detail[:1000]
    else:
        agreement.email_delivery_status = "not_requested"
    await db.commit()
    await db.refresh(agreement)

    return agreement


def _send_signed_copy_email(*, to_email: str, title: str, contract_number: str, typed_name: str, pdf_bytes: bytes):
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
    return send_raw_email(
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
