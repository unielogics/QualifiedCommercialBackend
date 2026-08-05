"""Universal, read-only admin view over every e-signed agreement on the
platform, merged from the 3 tables that actually store signatures:

  - `contract_agreements` — Platform Access Agreement (individual User) and
    Referral Protection Agreement (ReferralPartnerCompany). See
    app/routers/contracts.py.
  - `bucket_document_signatures` — credit-authorization forms and the 3
    client-facing contract types (SBA/Client Engagement, Consulting
    Addendum), reached via a BucketRequestedDocument on a lead's bucket.
    See app/routers/dealer_ai_intake.py's `_sign_requested_document`.
  - `payment_authorizations` — the client payment pre-authorization / card-
    on-file consent. See app/services/payment_authorization.py.

`broker_nda_acceptances` is fully dropped (migration 0102, superseded by
`contract_agreements`) and has no data to include. `legal_acceptances`
(T&C/privacy click-through) has no typed-name/signature and is a different
evidentiary class entirely -- excluded.

No new table, no migration -- this is a pure aggregation over existing
data, merged and paginated in Python (same pattern as
`dealer_ai_intake.list_dealer_ai_leads`), since a single SQL UNION across
3 differently-shaped tables would be more complex than it's worth at this
data volume.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import ContractSubjectType, ContractType, Role
from app.models.billing import PaymentAuthorization
from app.models.bucket import Bucket, BucketDocumentSignature, BucketRequestedDocument
from app.models.client import Client
from app.models.contract_agreement import ContractAgreement
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.referral_partner_company import ReferralPartnerCompany
from app.models.user import User
from app.services import contract_templates as tpl
from app.services import payment_authorization as pay_auth

router = APIRouter(prefix="/admin/agreements", tags=["admin"])


def _require_super_admin(user: User) -> None:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")


class AgreementRow(BaseModel):
    id: UUID
    source: str  # "contract" | "requested_document" | "payment_authorization"
    agreement_type: str  # machine-readable: contract_type or signature_kind
    title: str
    contract_number: str | None
    party_name: str | None
    party_email: str | None
    party_company: str | None
    party_kind: str  # "user" | "company" | "lead" | "client" | "unknown"
    typed_name: str
    signed_at: datetime | None
    document_version: str
    certificate_available: bool
    certificate_download_url: str | None
    detail_url: str | None


class AgreementListResponse(BaseModel):
    items: list[AgreementRow]
    total: int
    limit: int
    offset: int


_SIGNATURE_KIND_TITLE: dict[str, str] = {
    "credit_authorization": "Credit Report Authorization",
    "contract_sba_engagement": tpl.CONTRACT_TITLES[ContractType.SBA_ENGAGEMENT],
    "contract_client_engagement": tpl.CONTRACT_TITLES[ContractType.CLIENT_ENGAGEMENT],
    "contract_consulting_addendum": tpl.CONTRACT_TITLES[ContractType.CONSULTING_ADDENDUM],
}


async def _rows_from_contract_agreements(db: AsyncSession) -> list[AgreementRow]:
    agreements = list((await db.execute(select(ContractAgreement))).scalars().all())
    if not agreements:
        return []

    user_ids = {a.subject_id for a in agreements if a.subject_type == ContractSubjectType.USER}
    company_ids = {a.subject_id for a in agreements if a.subject_type == ContractSubjectType.COMPANY}

    users_by_id: dict[UUID, User] = {}
    if user_ids:
        users_by_id = {
            u.id: u
            for u in (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
        }
    companies_by_id: dict[UUID, ReferralPartnerCompany] = {}
    if company_ids:
        companies_by_id = {
            c.id: c
            for c in (
                await db.execute(select(ReferralPartnerCompany).where(ReferralPartnerCompany.id.in_(company_ids)))
            ).scalars().all()
        }

    rows: list[AgreementRow] = []
    for a in agreements:
        party_name: str | None = None
        party_email: str | None = None
        party_company: str | None = None
        party_kind = "unknown"
        detail_url: str | None = None
        if a.subject_type == ContractSubjectType.USER:
            user = users_by_id.get(a.subject_id)
            if user is not None:
                party_name = user.name
                party_email = user.email
                party_company = (
                    companies_by_id.get(user.referral_partner_company_id).name
                    if user.referral_partner_company_id and user.referral_partner_company_id in companies_by_id
                    else None
                )
            party_kind = "user"
            detail_url = "/settings?section=team"
        elif a.subject_type == ContractSubjectType.COMPANY:
            company = companies_by_id.get(a.subject_id)
            if company is not None:
                party_name = company.name
                party_company = company.name
            party_kind = "company"
            detail_url = "/settings?section=team"

        try:
            contract_title = tpl.CONTRACT_TITLES[ContractType(a.contract_type)]
        except ValueError:
            contract_title = a.contract_type
        rows.append(
            AgreementRow(
                id=a.id,
                source="contract",
                agreement_type=a.contract_type,
                title=contract_title,
                contract_number=a.contract_number,
                party_name=party_name,
                party_email=party_email,
                party_company=party_company,
                party_kind=party_kind,
                typed_name=a.typed_name,
                signed_at=a.signed_at,
                document_version=a.document_version,
                certificate_available=a.certificate_s3_key is not None,
                certificate_download_url=tpl.presign_private_s3_object(a.certificate_s3_key),
                detail_url=detail_url,
            )
        )
    return rows


async def _rows_from_bucket_signatures(db: AsyncSession) -> list[AgreementRow]:
    signatures = list(
        (
            await db.execute(
                select(BucketDocumentSignature).join(
                    BucketRequestedDocument,
                    BucketDocumentSignature.requested_document_id == BucketRequestedDocument.id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not signatures:
        return []

    req_ids = {s.requested_document_id for s in signatures}
    reqs_by_id: dict[UUID, BucketRequestedDocument] = {
        r.id: r
        for r in (
            await db.execute(select(BucketRequestedDocument).where(BucketRequestedDocument.id.in_(req_ids)))
        ).scalars().all()
    }

    bucket_ids = {r.bucket_id for r in reqs_by_id.values()}
    buckets_by_id: dict[UUID, Bucket] = {}
    if bucket_ids:
        buckets_by_id = {
            b.id: b for b in (await db.execute(select(Bucket).where(Bucket.id.in_(bucket_ids)))).scalars().all()
        }

    intakes_by_bucket_id: dict[UUID, PublicUnderwritingIntake] = {}
    if bucket_ids:
        intakes_by_bucket_id = {
            i.bucket_id: i
            for i in (
                await db.execute(
                    select(PublicUnderwritingIntake).where(PublicUnderwritingIntake.bucket_id.in_(bucket_ids))
                )
            ).scalars().all()
        }

    rows: list[AgreementRow] = []
    for sig in signatures:
        req = reqs_by_id.get(sig.requested_document_id)
        bucket = buckets_by_id.get(req.bucket_id) if req else None
        intake = intakes_by_bucket_id.get(req.bucket_id) if req else None

        party_name: str | None = None
        party_email: str | None = None
        party_company: str | None = None
        party_kind = "unknown"
        detail_url: str | None = None
        if intake is not None:
            party_name = intake.full_name
            party_email = intake.email
            party_company = intake.business_name
            party_kind = "lead"
            detail_url = f"/admin/ai-underwriter-leads?intake_id={intake.id}"
        elif bucket is not None:
            party_name = bucket.client_name
            party_kind = "unknown"
            detail_url = f"/admin/buckets?bucket_id={bucket.id}"

        signature_kind = req.signature_kind if req else None
        title = (
            _SIGNATURE_KIND_TITLE.get(signature_kind or "", req.name if req else "Signed document")
            if req
            else "Signed document"
        )
        rows.append(
            AgreementRow(
                id=sig.id,
                source="requested_document",
                agreement_type=signature_kind or "custom",
                title=title,
                contract_number=None,
                party_name=party_name,
                party_email=party_email,
                party_company=party_company,
                party_kind=party_kind,
                typed_name=sig.typed_name,
                signed_at=sig.signed_at,
                document_version=sig.document_version,
                certificate_available=sig.certificate_s3_key is not None,
                certificate_download_url=pay_auth.presign_private_s3_object(sig.certificate_s3_key),
                detail_url=detail_url,
            )
        )
    return rows


async def _rows_from_payment_authorizations(db: AsyncSession) -> list[AgreementRow]:
    auths = list(
        (
            await db.execute(select(PaymentAuthorization).where(PaymentAuthorization.signed_at.is_not(None)))
        )
        .scalars()
        .all()
    )
    if not auths:
        return []

    user_ids = {a.user_id for a in auths}
    client_ids = {a.client_id for a in auths}
    users_by_id = {
        u.id: u for u in (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
    } if user_ids else {}
    clients_by_id = {
        c.id: c for c in (await db.execute(select(Client).where(Client.id.in_(client_ids)))).scalars().all()
    } if client_ids else {}

    rows: list[AgreementRow] = []
    for a in auths:
        user = users_by_id.get(a.user_id)
        client = clients_by_id.get(a.client_id)
        rows.append(
            AgreementRow(
                id=a.id,
                source="payment_authorization",
                agreement_type="payment_authorization",
                title="Payment Pre-Authorization and Electronic Signature Consent",
                contract_number=None,
                party_name=(client.name if client else None) or (user.name if user else None),
                party_email=(user.email if user else None) or (client.email if client else None),
                party_company=None,
                party_kind="client",
                typed_name=a.typed_name or "",
                signed_at=a.signed_at,
                document_version=a.document_version,
                certificate_available=a.certificate_s3_key is not None,
                certificate_download_url=pay_auth.presign_private_s3_object(a.certificate_s3_key),
                detail_url=f"/clients/{client.id}" if client else None,
            )
        )
    return rows


@router.get("", response_model=AgreementListResponse)
async def list_agreements(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    q: str | None = None,
    agreement_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AgreementListResponse:
    _require_super_admin(user)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    rows = [
        *(await _rows_from_contract_agreements(db)),
        *(await _rows_from_bucket_signatures(db)),
        *(await _rows_from_payment_authorizations(db)),
    ]
    rows.sort(key=lambda r: r.signed_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    if agreement_type and agreement_type != "all":
        rows = [r for r in rows if r.agreement_type == agreement_type]
    if q:
        needle = q.strip().lower()
        rows = [
            r
            for r in rows
            if needle in (r.party_name or "").lower()
            or needle in (r.party_email or "").lower()
            or needle in (r.party_company or "").lower()
            or needle in r.title.lower()
            or needle in (r.contract_number or "").lower()
        ]

    total = len(rows)
    page = rows[offset : offset + limit]
    return AgreementListResponse(items=page, total=total, limit=limit, offset=offset)
