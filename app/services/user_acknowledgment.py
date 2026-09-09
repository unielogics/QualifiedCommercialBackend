"""One rule for the team's platform-document acknowledgment.

Every Qualified Commercial login — Funding, Field Desk and Audit — works
under the same company-wide platform documents: the Terms and Conditions, the
Privacy Policy and Financial Privacy Notice, and the Funding, AI,
Communications and Platform Disclosure. A dealer partner e-signs the Platform
Access Agreement instead; clients, dealers, lenders and vendors are not staff.

Before this module nothing asked a team member to acknowledge them: the
sign-up page's checkbox wrote a ``legal_acceptances`` row for whoever came in
through app., and a field rep — whose invite lands on the rep app's sign-in —
accepted nothing at all. Now every console shows a team login one screen,
once, and once more whenever the versions below move. The record is the same
evidentiary class as the sign-up checkbox: a click-through with the time, the
IP address, the browser and the versions — not an electronic signature, and
no certificate is issued. The upgrade to a signed instrument is a ContractType
plus a PlatformAccessGate-style ceremony and counsel's wording.

The canonical versions live HERE, because the backend is what decides whether
a login is current. Bump them in lockstep with
``QCDashboard/src/lib/legal.ts``, where the text lives.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ContractSubjectType, ContractType, Role
from app.models.contract_agreement import ContractAgreement
from app.models.legal_acceptance import LegalAcceptance
from app.models.referral_partner_company import KIND_HOUSE, ReferralPartnerCompany

TERMS_VERSION = "2026-08-25"
PRIVACY_VERSION = "2026-09-02"
DISCLOSURE_VERSION = "2026-08-25"

# Everyone on the Team table except dealer partners, who e-sign the Platform
# Access Agreement at first login instead.
ACKNOWLEDGMENT_ROLES: frozenset[Role] = frozenset(
    {Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.REGIONAL_MANAGER, Role.BROKER, Role.FIELD_REP}
)

STATUS_CURRENT = "current"
STATUS_OUT_OF_DATE = "out_of_date"
STATUS_MISSING = "missing"
STATUS_NOT_ASKED = "not_asked"


def current_versions() -> dict[str, str]:
    """The three keys POST /legal/accept takes, at the versions in force."""
    return {
        "terms_version": TERMS_VERSION,
        "privacy_version": PRIVACY_VERSION,
        "disclosure_version": DISCLOSURE_VERSION,
    }


def is_current(row: Any) -> bool:
    """All three versions match. A row from before the disclosure existed
    (NULL disclosure_version) is therefore not current."""
    if row is None:
        return False
    return (
        getattr(row, "terms_version", None) == TERMS_VERSION
        and getattr(row, "privacy_version", None) == PRIVACY_VERSION
        and getattr(row, "disclosure_version", None) == DISCLOSURE_VERSION
    )


def is_gated(user: Any) -> bool:
    return getattr(user, "role", None) in ACKNOWLEDGMENT_ROLES


def acknowledgment_status(user: Any, latest: Any) -> str:
    """current | out_of_date | missing | not_asked. A role that is never
    gated reads not_asked even when it holds a row (a partner who came in
    through /sign-up), so the Team table never promises a re-prompt that
    will not happen."""
    if not is_gated(user):
        return STATUS_NOT_ASKED
    if latest is None:
        return STATUS_MISSING
    return STATUS_CURRENT if is_current(latest) else STATUS_OUT_OF_DATE


def needs_acknowledgment(user: Any, latest: Any) -> bool:
    return is_gated(user) and not is_current(latest)


def latest_acceptances_stmt(user_ids: set[UUID]) -> Select:
    # DISTINCT ON (user_id) … ORDER BY user_id, created_at DESC: the newest row
    # per person, one query for the whole Team table.
    return (
        select(LegalAcceptance)
        .distinct(LegalAcceptance.user_id)
        .where(LegalAcceptance.user_id.in_(user_ids))
        .order_by(LegalAcceptance.user_id, LegalAcceptance.created_at.desc())
    )


async def latest_acceptances(db: AsyncSession, user_ids: set[UUID]) -> dict[UUID, LegalAcceptance]:
    if not user_ids:
        return {}
    rows = (await db.execute(latest_acceptances_stmt(user_ids))).scalars().all()
    return {row.user_id: row for row in rows}


async def latest_acceptance(db: AsyncSession, user_id: UUID) -> LegalAcceptance | None:
    return (
        await db.execute(
            select(LegalAcceptance)
            .where(LegalAcceptance.user_id == user_id)
            .order_by(LegalAcceptance.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def documents(base_url: str) -> list[dict[str, str]]:
    """The three documents as the screen lists them, linking to the public
    pages on the funding app (bare there, so a gated login can read them)."""
    base = base_url.rstrip("/")
    return [
        {"key": "terms", "title": "Terms and Conditions", "version": TERMS_VERSION, "url": f"{base}/terms"},
        {"key": "privacy", "title": "Privacy Policy and Financial Privacy Notice", "version": PRIVACY_VERSION, "url": f"{base}/privacy"},
        {"key": "disclosure", "title": "Funding, AI, Communications, and Platform Disclosure", "version": DISCLOSURE_VERSION, "url": f"{base}/disclosures"},
    ]


@dataclass(frozen=True)
class CompanyAgreementOnFile:
    company_name: str
    title: str
    contract_number: str
    signed_at: datetime | None


async def company_agreement_on_file(db: AsyncSession, user: Any) -> CompanyAgreementOnFile | None:
    """The signed Referral Protection agreement of the person's linked company,
    for the screen to name. Informational: the acknowledgment row does not
    record it, so the screen states it and asks nothing about it. The house
    never signs one."""
    company_id = getattr(user, "referral_partner_company_id", None)
    if company_id is None:
        return None
    company = await db.get(ReferralPartnerCompany, company_id)
    if company is None or getattr(company, "kind", None) == KIND_HOUSE:
        return None
    agreement = (
        await db.execute(
            select(ContractAgreement)
            .where(
                ContractAgreement.contract_type == ContractType.REFERRAL_PROTECTION,
                ContractAgreement.subject_type == ContractSubjectType.COMPANY,
                ContractAgreement.subject_id == company.id,
                ContractAgreement.signed_at.is_not(None),
            )
            .order_by(ContractAgreement.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if agreement is None:
        return None
    from app.services.contract_templates import CONTRACT_TITLES  # heavy import chain; only here

    return CompanyAgreementOnFile(
        company_name=company.name,
        title=CONTRACT_TITLES[ContractType.REFERRAL_PROTECTION],
        contract_number=agreement.contract_number,
        signed_at=agreement.signed_at,
    )
