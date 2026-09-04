"""Production Package orchestration: access, drafts, sponsors, share links.

Sending, signing and execution live in production_signing.py; the PDF
builders in production_presentation.py. This module owns the package row and
everything that decides who may touch it.
"""

from __future__ import annotations

import hashlib
import secrets
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dealer_os.deps import is_rep
from app.dealer_os.models import DealerBusiness
from app.dealer_os.services import consent_delivery
from app.dealer_os.services import sms_consent as sms_consent_svc
from app.enums import ContractSubjectType, ContractType, Role
from app.models.application_profile import ApplicationProfile
from app.models.contract_agreement import ContractAgreement
from app.models.production_package import (
    ProductionPackage,
    ProductionPackageRevision,
    ProductionPackageShareLink,
    ProductionPackageSignature,
    ProductionTermSheet,
)
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.referral_partner_company import ReferralPartnerCompany
from app.models.user import User
from app.schemas.production_package import (
    ProductionCapabilities,
    ProductionComparisonRead,
    ProductionPackageRead,
    ProductionPresentationRead,
    ProductionRevisionRead,
    ProductionShareLinkRead,
    ProductionSignatureRead,
    ProductionSmsConsentRead,
    ProductionTermSheetRead,
    SponsorAgreementRead,
    SponsorOptionRead,
)
from app.services import application_profiles as profiles
from app.services import production_arrangement as pa
from app.services import production_prefill as prefill_svc
from app.services import production_term_sheets as sheets_svc
from app.services.payment_authorization import client_ip, presign_private_s3_object

OPERATOR_ROLES: frozenset[Role] = frozenset({Role.SUPER_ADMIN, Role.LOAN_EXEC})
SEND_ROLES = OPERATOR_ROLES
RECORD_ROLES: frozenset[Role] = frozenset({Role.SUPER_ADMIN})
SHARE_LINK_MAX_DAYS = 30
SHARE_LINK_DEFAULT_DAYS = 14
_MISS_LIMIT = 10
_MISS_WINDOW = timedelta(minutes=15)
_MISSES: dict[UUID, list[datetime]] = defaultdict(list)

Mode = Literal["operator", "rep", "partner"]
Via = Literal["operator", "share_link", "ownership"]


def _now() -> datetime:
    return datetime.now(UTC)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Production package not found")


# ---------------------------------------------------------------------------
# access
# ---------------------------------------------------------------------------

@dataclass
class PackageAccess:
    package: ProductionPackage
    profile: ApplicationProfile
    user: User
    mode: Mode
    link: ProductionPackageShareLink | None = None
    dealer: DealerBusiness | None = None
    via: Via = "operator"
    # The family: the executed stage one (parent) / the live final (child) / the current term sheet.
    parent: ProductionPackage | None = None
    child: ProductionPackage | None = None
    term_sheet: ProductionTermSheet | None = None

    @property
    def is_operator(self) -> bool:
        return self.mode == "operator"

    @property
    def is_agent(self) -> bool:
        """A field rep holding a live link or a dealer partner on their own lead."""
        return (self.mode == "rep" and self.link is not None) or self.mode == "partner"

    @property
    def role(self) -> Role:
        return self.user.role

    @property
    def stage(self) -> int:
        return int(getattr(self.package, "stage", 1) or 1)

    @property
    def editable(self) -> bool:
        return self.package.status == "draft"

    def capabilities(self) -> ProductionCapabilities:
        op = self.is_operator
        role = self.role
        pkg = self.package
        stage = self.stage
        desk = op and role in SEND_ROLES
        rec = op and role in RECORD_ROLES
        agent_s1 = self.is_agent and stage == 1
        sent_by_me = getattr(pkg, "sent_by_user_id", None) == self.user.id
        return ProductionCapabilities(
            can_edit=self.editable and (op or agent_s1),
            can_confirm=self.editable and (op or agent_s1),
            can_generate=stage == 1 and pkg.status in ("draft", "out_for_signature", "executed") and (op or self.is_agent),
            can_send=self.editable and (desk or agent_s1),
            can_remind=pkg.status == "out_for_signature" and (desk or (agent_s1 and sent_by_me)),
            can_reopen=desk and pkg.status == "out_for_signature",
            can_void=rec and pkg.status in ("draft", "out_for_signature"),
            # Fallback only: a party with no signature on file.
            can_record=rec and pkg.status == "out_for_signature",
            can_execute=desk and pkg.status == "out_for_signature" and bool(getattr(pkg, "execution_pending", False)),
            can_share=desk and self.editable and stage == 1,
            can_pick_sponsor=op and self.editable and stage == 1,
            can_capture_consent=desk,
            can_manage_terms=desk,
            can_draft_final=desk and stage == 1 and pkg.status == "executed" and self.child is None and self.term_sheet is not None,
            can_compare=op and (stage == 2 or self.child is not None),
            can_adopt_sponsor_signature=rec,
        )


def require_dealer_profile(profile: ApplicationProfile) -> None:
    if profile.vertical != "dealer":
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "not_dealer_vertical", "message": "Production packages exist only on car-industry files."},
        )


async def _dealer_for(db: AsyncSession, profile: ApplicationProfile) -> DealerBusiness | None:
    return await db.get(DealerBusiness, profile.dealer_id) if profile.dealer_id else None


def _mode_for(user: User) -> Mode:
    """Mode is derived from the role, never from the route."""
    if user.role in OPERATOR_ROLES:
        return "operator"
    if is_rep(user):
        return "rep"
    if user.role == Role.DEALER_PARTNER:
        return "partner"
    raise not_found()


async def _require_partner_standing(db: AsyncSession, user: User) -> None:
    """Dealer partners are hard-gated behind their signed Platform Access and
    Referral Protection agreements everywhere else; the package honours the same gate."""
    if user.role != Role.DEALER_PARTNER:
        return
    from app.routers.dealer_ai_intake import _require_dealer_partner

    await _require_dealer_partner(user, db)


async def _load_family(db: AsyncSession, access: PackageAccess) -> None:
    """Attach the parent / child / current term sheet to an access object."""
    pkg = access.package
    if int(getattr(pkg, "stage", 1) or 1) == 1:
        access.parent = None
        access.child = (
            await db.execute(
                select(ProductionPackage).where(
                    ProductionPackage.parent_package_id == pkg.id, ProductionPackage.status != "void"
                )
            )
        ).scalar_one_or_none()
    else:
        access.parent = await db.get(ProductionPackage, pkg.parent_package_id) if pkg.parent_package_id else None
        access.child = None
    access.term_sheet = await sheets_svc.current_sheet(db, access.profile.id)


async def resolve_package(db: AsyncSession, profile_id: UUID, user: User) -> PackageAccess:
    """Get-or-create the stage-one draft for a dealer-vertical profile the user can see."""
    profile = await profiles.load_profile(db, profile_id, user)
    require_dealer_profile(profile)
    mode = _mode_for(user)
    await _require_partner_standing(db, user)
    package = (
        await db.execute(
            select(ProductionPackage).where(ProductionPackage.profile_id == profile.id, ProductionPackage.stage == 1)
        )
    ).scalar_one_or_none()
    if package is None:
        # The desk or a dealer partner on their own lead may open the package; reps arrive by link.
        if mode == "rep":
            raise not_found()
        result = await prefill_svc.build_prefill(db, profile, user if mode == "operator" else None)
        arrangement, provenance, _applied, _skipped = prefill_svc.apply_prefill(
            pa.empty_arrangement(), {}, result
        )
        computed = pa.compute(arrangement)
        package = ProductionPackage(
            profile_id=profile.id,
            intake_id=profile.intake_id,
            dealer_id=profile.dealer_id,
            arrangement=pa.jsonable(arrangement),
            prefill_provenance=provenance,
            attention=computed["attention"],
            computed_cache=pa.jsonable(computed),
            created_by_user_id=user.id,
            updated_by_user_id=user.id,
            updated_via="operator",
        )
        db.add(package)
        await db.flush()
        await profiles.log_profile_action(
            db, profile, user, "production_package.created",
            "Production package opened from the file",
            target_type="production_package", target_id=package.id,
            metadata={"prefilled": sorted(provenance), "missing": result.missing, "via": mode},
        )
    access = PackageAccess(package=package, profile=profile, user=user, mode=mode, dealer=await _dealer_for(db, profile),
                           via="operator" if mode == "operator" else "ownership")
    await _load_family(db, access)
    return access


async def load_package_access(db: AsyncSession, package_id: UUID, user: User) -> PackageAccess:
    """Any user the profile admits, with the mode derived from their role.
    Reps arriving by ownership (no link) are read-only; agents never see a stage-two row."""
    package = await db.get(ProductionPackage, package_id)
    if package is None:
        raise not_found()
    try:
        profile = await profiles.load_profile(db, package.profile_id, user)
    except HTTPException as exc:
        raise not_found() from exc
    mode = _mode_for(user)
    await _require_partner_standing(db, user)
    if mode != "operator" and int(getattr(package, "stage", 1) or 1) != 1:
        raise not_found()
    access = PackageAccess(package=package, profile=profile, user=user, mode=mode, dealer=await _dealer_for(db, profile),
                           via="operator" if mode == "operator" else "ownership")
    await _load_family(db, access)
    return access


# Kept for callers written against the shipped name.
load_operator_access = load_package_access


def _note_miss(user_id: UUID) -> None:
    now = _now()
    hits = [t for t in _MISSES[user_id] if now - t < _MISS_WINDOW]
    hits.append(now)
    _MISSES[user_id] = hits


def _locked(user_id: UUID) -> bool:
    now = _now()
    hits = [t for t in _MISSES[user_id] if now - t < _MISS_WINDOW]
    _MISSES[user_id] = hits
    return len(hits) >= _MISS_LIMIT


async def resolve_rep_share(db: AsyncSession, user: User, token: str) -> PackageAccess:
    """A signed-in rep plus the unique link they were issued. Misses are
    indistinguishable (404) whether the token is unknown or belongs to another
    rep; revoked and expired links say so (410) because identity is proven."""
    if not is_rep(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Field-rep role required")
    if _locked(user.id):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again in a few minutes.")
    link = (
        await db.execute(
            select(ProductionPackageShareLink).where(ProductionPackageShareLink.token_hash == hash_token(token or ""))
        )
    ).scalar_one_or_none()
    if link is None or link.rep_user_id != user.id:
        _note_miss(user.id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    if link.revoked_at is not None:
        raise HTTPException(status.HTTP_410_GONE, "This link was revoked. Ask the desk for a new one.")
    if link.expires_at <= _now():
        raise HTTPException(status.HTTP_410_GONE, "This link has expired. Ask the desk for a new one.")
    package = await db.get(ProductionPackage, link.package_id)
    profile = await db.get(ApplicationProfile, package.profile_id) if package else None
    if package is None or profile is None or profile.vertical != "dealer":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    dealer = await _dealer_for(db, profile)
    if dealer is not None and dealer.is_training:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    if dealer is not None and dealer.archived_at is not None:
        raise HTTPException(status.HTTP_410_GONE, "This file has been archived.")
    if int(getattr(package, "stage", 1) or 1) != 1:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    link.last_used_at = _now()
    link.use_count = (link.use_count or 0) + 1
    await db.flush()
    access = PackageAccess(package=package, profile=profile, user=user, mode="rep", link=link, dealer=dealer, via="share_link")
    await _load_family(db, access)
    return access


# ---------------------------------------------------------------------------
# sponsors = signed Referral Protection companies
# ---------------------------------------------------------------------------

async def _latest_rpa(db: AsyncSession, company_id: UUID) -> ContractAgreement | None:
    return (
        await db.execute(
            select(ContractAgreement)
            .where(
                ContractAgreement.contract_type == ContractType.REFERRAL_PROTECTION,
                ContractAgreement.subject_type == ContractSubjectType.COMPANY,
                ContractAgreement.subject_id == company_id,
            )
            .order_by(ContractAgreement.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _agreement_read(agreement: ContractAgreement | None, *, user: User | None) -> SponsorAgreementRead | None:
    if agreement is None:
        return None
    fv = agreement.field_values or {}
    super_admin = user is not None and user.role == Role.SUPER_ADMIN
    cfg = get_settings()
    return SponsorAgreementRead(
        id=agreement.id,
        contract_number=agreement.contract_number,
        document_version=agreement.document_version,
        signed_at=agreement.signed_at,
        signer_name=agreement.typed_name or fv.get("counterparty_signatory_name"),
        signer_title=fv.get("counterparty_signatory_title"),
        certificate_url=(
            presign_private_s3_object(
                agreement.certificate_s3_key, ttl_seconds=900, download_filename=f"{agreement.contract_number}.pdf"
            )
            if super_admin else None
        ),
        admin_url=(f"{cfg.frontend_app_url.rstrip('/')}/admin/agreements?q={agreement.contract_number}" if super_admin else None),
    )


def _sponsor_option(company: ReferralPartnerCompany, agreement: ContractAgreement | None, *, user: User | None) -> SponsorOptionRead:
    fv = (agreement.field_values if agreement is not None else None) or {}
    return SponsorOptionRead(
        company_id=company.id,
        name=company.name,
        entity_type=company.entity_type or fv.get("referral_partner_entity_type") or None,
        state_of_formation=company.state_of_formation or fv.get("referral_partner_state_of_organization") or None,
        principal_address=company.principal_address or fv.get("referral_partner_principal_place_of_business") or None,
        notice_email=(fv.get("referral_partner_notice_email") or "").strip() or None,
        notice_attention=(fv.get("referral_partner_notice_attn") or "").strip() or None,
        agreement=_agreement_read(agreement, user=user),
    )


async def sponsor_options(db: AsyncSession, *, user: User) -> list[SponsorOptionRead]:
    rows = (
        await db.execute(
            select(ReferralPartnerCompany)
            .join(
                ContractAgreement,
                (ContractAgreement.subject_id == ReferralPartnerCompany.id)
                & (ContractAgreement.subject_type == ContractSubjectType.COMPANY)
                & (ContractAgreement.contract_type == ContractType.REFERRAL_PROTECTION),
            )
            .distinct()
            .order_by(ReferralPartnerCompany.name)
        )
    ).scalars().all()
    out: list[SponsorOptionRead] = []
    for company in rows:
        out.append(_sponsor_option(company, await _latest_rpa(db, company.id), user=user))
    return out


async def sponsor_option_for(db: AsyncSession, company_id: UUID | None, *, user: User | None) -> SponsorOptionRead | None:
    if company_id is None:
        return None
    company = await db.get(ReferralPartnerCompany, company_id)
    if company is None:
        return None
    return _sponsor_option(company, await _latest_rpa(db, company.id), user=user)


async def require_signed_sponsor(db: AsyncSession, package: ProductionPackage) -> tuple[ReferralPartnerCompany, ContractAgreement]:
    if package.sponsor_company_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "sponsor_missing", "message": "Choose the sponsor before requesting a signature."},
        )
    company = await db.get(ReferralPartnerCompany, package.sponsor_company_id)
    agreement = await _latest_rpa(db, package.sponsor_company_id) if company else None
    if company is None or agreement is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "sponsor_agreement_missing",
                "message": "The selected company does not have a signed Referral Protection Agreement.",
            },
        )
    return company, agreement


def sponsor_snapshot(company: ReferralPartnerCompany, agreement: ContractAgreement, arrangement: dict[str, Any]) -> dict[str, Any]:
    fv = agreement.field_values or {}
    return pa.jsonable({
        "company_id": company.id,
        "name": company.name,
        "entity_type": arrangement.get("sponsor_entity") or company.entity_type,
        "state_of_formation": arrangement.get("sponsor_state") or company.state_of_formation,
        "principal_address": arrangement.get("sponsor_address") or company.principal_address,
        "platform": arrangement.get("sponsor_platform"),
        "notice_email": arrangement.get("sponsor_email") or fv.get("referral_partner_notice_email"),
        "signer_name": agreement.typed_name or fv.get("counterparty_signatory_name"),
        "signer_title": fv.get("counterparty_signatory_title"),
        "agreement": {
            "id": agreement.id,
            "contract_number": agreement.contract_number,
            "document_version": agreement.document_version,
            "document_hash": agreement.document_hash,
            "signed_at": agreement.signed_at,
        },
    })


async def _apply_sponsor(db: AsyncSession, access: PackageAccess, company_id: UUID | None) -> dict[str, Any]:
    """Copy the chosen company onto the sponsor block with provenance."""
    package = access.package
    arrangement = {**pa.empty_arrangement(), **(package.arrangement or {})}
    provenance = dict(package.prefill_provenance or {})
    if company_id is None:
        package.sponsor_company_id = None
        for key in pa.SPONSOR_KEYS:
            arrangement[key] = ""
            provenance.pop(key, None)
        return {"arrangement": arrangement, "provenance": provenance}
    option = await sponsor_option_for(db, company_id, user=access.user)
    if option is None or option.agreement is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "sponsor_agreement_missing",
                "message": "The selected company does not have a signed Referral Protection Agreement.",
            },
        )
    package.sponsor_company_id = option.company_id
    values = {
        "sponsor_name": option.name,
        "sponsor_state": option.state_of_formation or "",
        "sponsor_entity": prefill_svc.normalize_entity_type(option.entity_type) if option.entity_type else "",
        "sponsor_address": option.principal_address or "",
        "sponsor_email": option.notice_email or "",
    }
    for key, value in values.items():
        arrangement[key] = value
        if value:
            provenance[key] = {"source": "sponsor", "label": prefill_svc.SOURCE_LABELS["sponsor"], "confirmed": True}
        else:
            provenance.pop(key, None)
    return {"arrangement": arrangement, "provenance": provenance}


# ---------------------------------------------------------------------------
# drafts
# ---------------------------------------------------------------------------

def _require_editable(package: ProductionPackage) -> None:
    if package.status != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "package_frozen",
                "status": package.status,
                "message": "This package has been sent for signature. Reopen it to edit.",
            },
        )


def _check_version(package: ProductionPackage, version: int) -> None:
    if version != package.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "stale_version", "current_version": package.version,
                    "message": "Someone else saved this package. Reload before editing."},
        )


def _clip(value: Any, limit: int = 200) -> Any:
    value = pa.jsonable(value)
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    if isinstance(value, dict):
        return {k: _clip(v, 80) for k, v in list(value.items())[:12]}
    return value


async def apply_changes(
    db: AsyncSession,
    access: PackageAccess,
    *,
    changes: dict[str, Any],
    version: int,
    confirm: list[str] | None = None,
    request: Request | None = None,
) -> ProductionPackage:
    package = await db.get(ProductionPackage, access.package.id, with_for_update=True)
    if package is None:
        raise not_found()
    access.package = package
    _require_editable(package)
    _check_version(package, version)
    changes = dict(changes or {})
    sponsor_change = "sponsor_company_id" in changes
    sponsor_company_id = changes.pop("sponsor_company_id", None)
    if not access.is_operator:
        if int(getattr(package, "stage", 1) or 1) != 1 or not access.capabilities().can_edit:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This package cannot be edited from this account.")
        blocked = sorted(k for k in changes if k in pa.SPONSOR_KEYS) + (["sponsor_company_id"] if sponsor_change else [])
        if blocked:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "maintained_by_desk", "fields": blocked,
                        "message": "The sponsor is chosen by the desk."},
            )
    if int(getattr(package, "stage", 1) or 1) == 2:
        locked = sorted(k for k in changes if k in pa.TERM_SHEET_KEYS) + (["sponsor_company_id"] if sponsor_change else []) + sorted(k for k in changes if k in pa.SPONSOR_KEYS)
        if locked:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "maintained_by_term_sheet", "fields": locked,
                        "message": "Loan terms are changed on the term sheet; the sponsor is carried from stage one."},
            )
    before = {**pa.empty_arrangement(), **(package.arrangement or {})}
    provenance = dict(package.prefill_provenance or {})
    arrangement = before
    if sponsor_change:
        cid = UUID(str(sponsor_company_id)) if sponsor_company_id else None
        applied = await _apply_sponsor(db, access, cid)
        arrangement, provenance = applied["arrangement"], applied["provenance"]
    normalized = pa.normalize_changes(changes)
    arrangement = pa.merge_changes(arrangement, normalized)
    diff: dict[str, Any] = {}
    for key in normalized:
        if before.get(key) != arrangement.get(key):
            diff[key] = {"before": _clip(before.get(key)), "after": _clip(arrangement.get(key))}
            if key in provenance and key not in pa.SPONSOR_KEYS:
                provenance[key] = {"source": "user", "label": "Edited", "confirmed": True}
    for key in confirm or []:
        if key in provenance:
            provenance[key] = {**provenance[key], "confirmed": True}
    if sponsor_change:
        diff["sponsor_company_id"] = {"before": _clip(before.get("sponsor_name")), "after": _clip(arrangement.get("sponsor_name"))}
    computed = pa.compute(arrangement, stage=int(getattr(package, "stage", 1) or 1))
    package.arrangement = pa.jsonable(arrangement)
    package.prefill_provenance = provenance
    package.attention = computed["attention"]
    package.computed_cache = pa.jsonable(computed)
    package.version = (package.version or 1) + 1
    package.updated_by_user_id = access.user.id
    package.updated_via = "operator" if access.is_operator else ("partner" if access.mode == "partner" else "share_link")
    package.updated_share_link_id = access.link.id if access.link else None
    await db.flush()
    if diff or confirm:
        await profiles.log_profile_action(
            db, access.profile, access.user, "production_package.edited",
            f"Production package edited ({len(diff)} field{'s' if len(diff) != 1 else ''})",
            target_type="production_package", target_id=package.id,
            metadata={
                "changes": diff, "confirmed": list(confirm or []),
                "via": package.updated_via, "share_link_id": str(access.link.id) if access.link else None,
                "ip": client_ip(request), "user_agent": (request.headers.get("user-agent", "")[:200] if request else None),
                "version": package.version,
            },
        )
    return package


async def run_prefill(
    db: AsyncSession, access: PackageAccess, *, force: bool, fields: list[str] | None, apply: bool
) -> dict[str, Any]:
    package = access.package
    result = await prefill_svc.build_prefill(db, access.profile, access.user)
    arrangement, provenance, applied, skipped = prefill_svc.apply_prefill(
        package.arrangement or {}, package.prefill_provenance or {}, result, force=force, fields=fields
    )
    if apply and applied:
        # The only mutating path that used to gate on _require_editable alone.
        # Harmless while agents are 404'd at stage two; a write hole the moment
        # they are not.
        if not access.capabilities().can_edit:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This package cannot be edited from this account.")
        _require_editable(package)
        computed = pa.compute(arrangement, stage=int(getattr(package, "stage", 1) or 1))
        package.arrangement = pa.jsonable(arrangement)
        package.prefill_provenance = provenance
        package.attention = computed["attention"]
        package.computed_cache = pa.jsonable(computed)
        package.version = (package.version or 1) + 1
        package.updated_by_user_id = access.user.id
        package.updated_via = "operator" if access.is_operator else ("partner" if access.mode == "partner" else "share_link")
        await db.flush()
        await profiles.log_profile_action(
            db, access.profile, access.user, "production_package.prefilled",
            f"Prefilled {len(applied)} field{'s' if len(applied) != 1 else ''} from the file",
            target_type="production_package", target_id=package.id,
            metadata={"applied": applied, "skipped": skipped, "force": force},
        )
    return {
        "values": pa.jsonable(result.values), "provenance": result.provenance,
        "applied": applied if apply else [], "skipped": skipped if apply else list(result.values),
        "missing": result.missing,
    }


# ---------------------------------------------------------------------------
# share links
# ---------------------------------------------------------------------------

def share_link_url(token: str) -> str:
    return f"{get_settings().rep_app_url.rstrip('/')}/production-package/{token}"


async def mint_share_link(
    db: AsyncSession, access: PackageAccess, *, rep_user_id: UUID, label: str | None,
    expires_in_days: int, outside_book: bool,
) -> tuple[ProductionPackageShareLink, str]:
    if not access.capabilities().can_share:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the desk can share a stage-one draft")
    rep = await db.get(User, rep_user_id)
    if rep is None or not is_rep(rep) or getattr(rep, "deleted_at", None) is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose a field representative")
    if access.dealer is not None and access.dealer.owner_user_id != rep.id and not outside_book:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "outside_book", "rep_name": rep.name,
                    "message": f"{rep.name} does not own this dealer file. Confirm to share it anyway."},
        )
    days = max(1, min(SHARE_LINK_MAX_DAYS, int(expires_in_days or SHARE_LINK_DEFAULT_DAYS)))
    now = _now()
    existing = (
        await db.execute(
            select(ProductionPackageShareLink).where(
                ProductionPackageShareLink.package_id == access.package.id,
                ProductionPackageShareLink.rep_user_id == rep.id,
                ProductionPackageShareLink.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    for row in existing:
        row.revoked_at = now
        row.revoked_by_user_id = access.user.id
    token = new_token()
    link = ProductionPackageShareLink(
        package_id=access.package.id, token_hash=hash_token(token), rep_user_id=rep.id,
        label=(label or "").strip()[:120] or None, outside_book=bool(outside_book and access.dealer is not None and access.dealer.owner_user_id != rep.id),
        created_by_user_id=access.user.id, expires_at=now + timedelta(days=days),
    )
    db.add(link)
    await db.flush()
    await profiles.log_profile_action(
        db, access.profile, access.user, "production_package.share_link_minted",
        f"Production package shared with {rep.name}",
        target_type="production_package", target_id=access.package.id,
        metadata={"link_id": str(link.id), "rep_user_id": str(rep.id), "expires_at": link.expires_at.isoformat(),
                  "outside_book": link.outside_book, "re_minted": len(existing)},
    )
    return link, token


async def revoke_share_link(db: AsyncSession, access: PackageAccess, link_id: UUID) -> None:
    if not access.is_operator or access.role not in SEND_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the desk can revoke a share link")
    link = await db.get(ProductionPackageShareLink, link_id)
    if link is None or link.package_id != access.package.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share link not found")
    if link.revoked_at is None:
        link.revoked_at = _now()
        link.revoked_by_user_id = access.user.id
        await db.flush()
        await profiles.log_profile_action(
            db, access.profile, access.user, "production_package.share_link_revoked",
            "Production package share link revoked",
            target_type="production_package", target_id=access.package.id,
            metadata={"link_id": str(link.id), "rep_user_id": str(link.rep_user_id)},
        )


async def revoke_all_share_links(db: AsyncSession, package: ProductionPackage, user: User) -> int:
    rows = (
        await db.execute(
            select(ProductionPackageShareLink).where(
                ProductionPackageShareLink.package_id == package.id,
                ProductionPackageShareLink.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    now = _now()
    for row in rows:
        row.revoked_at = now
        row.revoked_by_user_id = user.id
    return len(rows)


# ---------------------------------------------------------------------------
# SMS consent for the dealer signer's number
# ---------------------------------------------------------------------------

async def sms_consent_status(db: AsyncSession, phone: str | None) -> ProductionSmsConsentRead:
    from app.services.sms.optout import is_opted_out

    normalized = consent_delivery.normalize_phone(phone)
    if not normalized:
        return ProductionSmsConsentRead(phone=None, status="no_phone", detail="No mobile number on file.")
    if await is_opted_out(db, normalized):
        return ProductionSmsConsentRead(phone=normalized, status="opted_out", detail="This number replied STOP; texts are blocked.")
    grant = await sms_consent_svc.consent_for(db, phone_e164=normalized, kind="transactional")
    if grant is None:
        return ProductionSmsConsentRead(
            phone=normalized, status="missing",
            detail="No text consent on file for this number — record consent or send by email.",
        )
    return ProductionSmsConsentRead(phone=normalized, status="granted", detail="Transactional texts are cleared for this number.")


async def capture_sms_consent(
    db: AsyncSession, access: PackageAccess, *, phone: str, consenter_name: str, method: str, request: Request | None,
) -> ProductionSmsConsentRead:
    if not access.capabilities().can_capture_consent:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the desk can record consent")
    normalized = consent_delivery.normalize_phone(phone)
    if not normalized:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That phone number does not look complete.")
    await sms_consent_svc.record_consent(
        db, dealer_id=access.profile.dealer_id, profile_id=access.profile.id, phone_e164=normalized,
        kind="transactional", method=method, captured_by_user_id=access.user.id,
        captured_by_name=access.user.name, consenter_name=consenter_name,
        ip_address=client_ip(request), user_agent=(request.headers.get("user-agent") if request else None),
    )
    await profiles.log_profile_action(
        db, access.profile, access.user, "production_package.sms_consent_recorded",
        f"Text consent recorded for {normalized}",
        target_type="production_package", target_id=access.package.id,
        metadata={"phone": normalized, "method": method, "consenter_name": consenter_name},
    )
    return await sms_consent_status(db, normalized)


# ---------------------------------------------------------------------------
# read model
# ---------------------------------------------------------------------------

def probe_capabilities() -> dict[str, bool]:
    try:
        import weasyprint  # noqa: F401
        pdf = True
    except Exception:  # pragma: no cover - environment dependent
        pdf = False
    return {"pdf": pdf, "storage": bool(get_settings().s3_bucket)}


async def client_contact(db: AsyncSession, access: PackageAccess) -> tuple[str, str | None, str | None]:
    """(business_name, email, phone) — the intake room login is keyed on the intake email."""
    profile = access.profile
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    arrangement = access.package.arrangement or {}
    name = (arrangement.get("dealer_name") or "").strip()
    if not name and access.dealer is not None:
        name = access.dealer.legal_name or access.dealer.name
    if not name and intake is not None:
        name = intake.business_name or intake.full_name
    email = intake.email if intake is not None else (access.dealer.email if access.dealer is not None else None)
    phone = intake.phone if intake is not None else (access.dealer.phone if access.dealer is not None else None)
    return name or "the business", email, phone


async def _user_names(db: AsyncSession, ids: set[UUID | None]) -> dict[UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = (await db.execute(select(User.id, User.name).where(User.id.in_(wanted)))).all()
    return {row[0]: row[1] for row in rows}


def _signature_read(sig: ProductionPackageSignature, names: dict[UUID, str], *, operator: bool) -> ProductionSignatureRead:
    return ProductionSignatureRead(
        id=sig.id, party=sig.party, method=sig.method, status=sig.status,
        initials=sig.initials, stored_signature_id=sig.stored_signature_id, placed_at=sig.placed_at,
        expected_signer_name=sig.expected_signer_name, typed_name=sig.typed_name if operator else None,
        sent_at=sig.sent_at, viewed_at=sig.viewed_at, signed_at=sig.signed_at,
        signer_name=sig.signer_name, signer_title=sig.signer_title, signed_on=sig.signed_on,
        recorded_at=sig.recorded_at, recorded_by_name=names.get(sig.recorded_by_user_id) if operator else None,
        scan_available=bool(sig.scan_s3_key),
        scan_url=(presign_private_s3_object(sig.scan_s3_key, ttl_seconds=900) if operator and sig.scan_s3_key else None),
        note=sig.note if operator else None, voided_at=sig.voided_at, void_reason=sig.void_reason,
    )


def _revision_read(
    rev: ProductionPackageRevision, sigs: list[ProductionPackageSignature], names: dict[UUID, str],
    *, operator: bool, executed_key: str | None,
) -> ProductionRevisionRead:
    snap = rev.snapshot or {}
    return ProductionRevisionRead(
        id=rev.id, revision_no=rev.revision_no, stage=rev.stage, status=rev.status,
        document_key=rev.document_key, document_title=rev.document_title, document_version=rev.document_version,
        content_sha256=rev.content_sha256, rendered_pdf_sha256=rev.rendered_pdf_sha256,
        current_pdf_sha256=rev.current_pdf_sha256,
        unsigned_url=presign_private_s3_object(rev.rendered_pdf_s3_key, ttl_seconds=900) if operator else None,
        current_url=presign_private_s3_object(rev.current_pdf_s3_key, ttl_seconds=900) if operator else None,
        executed_url=(presign_private_s3_object(executed_key, ttl_seconds=3600) if operator and executed_key else None),
        sent_at=rev.sent_at, completed_at=rev.completed_at, voided_at=rev.voided_at, void_reason=rev.void_reason,
        sponsor_snapshot=snap.get("sponsor") if operator else ({"name": (snap.get("sponsor") or {}).get("name")} if snap.get("sponsor") else None),
        signatures=[_signature_read(s, names, operator=operator) for s in sigs],
        funding=rev.funding if operator else None,
        arrangement=snap.get("arrangement") if operator else None,
        original=snap.get("original") if operator else None,
    )


def _share_link_read(link: ProductionPackageShareLink, names: dict[UUID, str]) -> ProductionShareLinkRead:
    now = _now()
    return ProductionShareLinkRead(
        id=link.id, rep_user_id=link.rep_user_id, rep_name=names.get(link.rep_user_id), label=link.label,
        outside_book=link.outside_book, created_at=link.created_at, expires_at=link.expires_at,
        revoked_at=link.revoked_at, last_used_at=link.last_used_at, use_count=link.use_count or 0,
        active=link.revoked_at is None and link.expires_at > now,
    )


async def serialize(db: AsyncSession, access: PackageAccess) -> ProductionPackageRead:
    package = access.package
    # Every writing route commits before serialising. `updated_at` carries a
    # server-side onupdate, so the UPDATE flush expires it however
    # expire_on_commit is set, and reading it back lazily raises MissingGreenlet
    # in an async session — the write lands, the response 500s, and the client
    # keeps a stale version until the next PATCH 409s. Reload it once.
    if "updated_at" in sa_inspect(package).unloaded:
        await db.refresh(package, ["updated_at"])
    operator = access.is_operator
    arrangement = {**pa.empty_arrangement(), **(package.arrangement or {})}
    computed = package.computed_cache or pa.jsonable(pa.compute(arrangement))
    revisions = list(
        (
            await db.execute(
                select(ProductionPackageRevision)
                .where(ProductionPackageRevision.package_id == package.id)
                .order_by(ProductionPackageRevision.revision_no.desc())
            )
        ).scalars().all()
    )
    sigs = list(
        (
            await db.execute(
                select(ProductionPackageSignature)
                .where(ProductionPackageSignature.package_id == package.id)
                .order_by(ProductionPackageSignature.created_at.asc())
            )
        ).scalars().all()
    )
    links = (
        list(
            (
                await db.execute(
                    select(ProductionPackageShareLink)
                    .where(ProductionPackageShareLink.package_id == package.id)
                    .order_by(ProductionPackageShareLink.created_at.desc())
                )
            ).scalars().all()
        )
        if operator else []
    )
    names = await _user_names(
        db, {package.updated_by_user_id} | {s.recorded_by_user_id for s in sigs} | {link.rep_user_id for link in links}
        | ({access.term_sheet.entered_by_user_id} if access.term_sheet is not None else set())
    )
    by_rev: dict[UUID, list[ProductionPackageSignature]] = defaultdict(list)
    for s in sigs:
        by_rev[s.revision_id].append(s)
    rev_reads = [
        _revision_read(r, by_rev.get(r.id, []), names, operator=operator,
                       executed_key=package.executed_pdf_s3_key if r.id == package.frozen_revision_id else None)
        for r in revisions
    ]
    active = next((r for r in rev_reads if r.id == package.frozen_revision_id), None)
    business_name, email, phone = await client_contact(db, access)
    sponsor = await sponsor_option_for(db, package.sponsor_company_id, user=access.user if operator else None)
    if sponsor is not None and not operator:
        sponsor = SponsorOptionRead(company_id=sponsor.company_id, name=sponsor.name)
    signer_phone = phone
    consent = await sms_consent_status(db, signer_phone) if operator else ProductionSmsConsentRead()
    stale = bool(package.presentation_s3_key) and package.presentation_snapshot_sha256 != pa.snapshot_hash(arrangement)
    cfg = get_settings()
    caps = access.capabilities()
    sent_names = await _user_names(db, {package.sent_by_user_id})
    original: dict[str, Any] | None = None
    comparison: ProductionComparisonRead | None = None
    if operator and int(getattr(package, "stage", 1) or 1) == 2 and package.source_revision_id:
        src = await db.get(ProductionPackageRevision, package.source_revision_id)
        if src is not None:
            original = {
                "package_id": str(package.parent_package_id), "revision_id": str(src.id), "revision_no": src.revision_no,
                "content_sha256": src.content_sha256, "executed_at": (access.parent.executed_at.isoformat() if access.parent and access.parent.executed_at else None),
                "executed_url": presign_private_s3_object(src.current_pdf_s3_key, ttl_seconds=3600, download_filename=f"{src.document_title}.pdf"),
                "title": src.document_title,
            }
            frozen = (next((r for r in revisions if r.id == package.frozen_revision_id), None) or {}) if package.frozen_revision_id else None
            frozen_snap = (frozen.snapshot if frozen else None) or {}
            if frozen_snap.get("comparison"):
                comparison = ProductionComparisonRead(**frozen_snap["comparison"], source="frozen")
            else:
                diff = pa.arrangement_diff(
                    {"arrangement": (src.snapshot or {}).get("arrangement"), "computed": (src.snapshot or {}).get("computed"), "sponsor": (src.snapshot or {}).get("sponsor")},
                    {"arrangement": arrangement, "computed": computed, "sponsor": await _sponsor_snapshot_or_none(db, package, arrangement)},
                )
                comparison = ProductionComparisonRead(**diff, source="live")
    previous_finals: list[dict[str, Any]] = []
    if operator and int(getattr(package, "stage", 1) or 1) == 1:
        rows = (
            await db.execute(
                select(ProductionPackage).where(ProductionPackage.parent_package_id == package.id).order_by(ProductionPackage.created_at.desc())
            )
        ).scalars().all()
        previous_finals = [{"id": str(r.id), "status": r.status, "created_at": r.created_at.isoformat(), "voided_at": r.voided_at.isoformat() if r.voided_at else None} for r in rows if r.status == "void"]
    term_sheet_read = _term_sheet_read(access.term_sheet, names) if operator and access.term_sheet is not None else None
    sof = await signatures_on_file_status(db, access) if operator else {}
    recipient_preview = None
    if not operator and caps.can_send:
        from app.routers.application_profiles import _masked_recipient

        recipient_preview = _masked_recipient("email", email, None)
    return ProductionPackageRead(
        id=package.id, profile_id=package.profile_id, intake_id=package.intake_id, dealer_id=package.dealer_id,
        stage=package.stage, status=package.status, version=package.version, business_name=business_name,
        client_email=email if operator else None, client_phone=phone if operator else None,
        arrangement=arrangement, prefill_provenance=package.prefill_provenance or {},
        computed=computed, attention=computed.get("attention", []),
        attention_presentation=computed.get("attention_presentation", []),
        sponsor=sponsor,
        presentation=ProductionPresentationRead(
            url=presign_private_s3_object(
                package.presentation_s3_key, ttl_seconds=3600,
                download_filename=f"Production-Arrangement-{business_name}.pdf".replace("/", "-"),
            ) if package.presentation_s3_key else None,
            sha256=package.presentation_sha256, generated_at=package.presentation_generated_at,
            stale=stale, available=bool(package.presentation_s3_key),
        ),
        active_revision=active, revisions=rev_reads,
        share_links=[_share_link_read(link, names) for link in links],
        delivery_history=list(package.delivery_history or []) if operator else [],
        capabilities=caps, sms_consent=consent,
        sent_at=package.sent_at, executed_at=package.executed_at, voided_at=package.voided_at,
        void_reason=package.void_reason,
        executed_url=(presign_private_s3_object(package.executed_pdf_s3_key, ttl_seconds=3600) if operator and package.executed_pdf_s3_key else None),
        updated_at=package.updated_at, updated_by_name=names.get(package.updated_by_user_id),
        sponsor_signing_url=f"{cfg.frontend_app_url.rstrip('/')}/agreement/referral-protection",
        mode=access.mode, access_via=access.via,
        sent_by_name=sent_names.get(package.sent_by_user_id), sent_via=package.sent_via,
        recipient_preview=recipient_preview, execution_pending=bool(getattr(package, "execution_pending", False)),
        parent_package_id=package.parent_package_id,
        final_package_id=access.child.id if access.child is not None else None,
        final_status=access.child.status if access.child is not None else None,
        term_sheet=term_sheet_read, original=original, comparison=comparison,
        previous_finals=previous_finals, signatures_on_file=sof,
    )


async def history(db: AsyncSession, access: PackageAccess, limit: int = 250) -> list[Any]:
    events = await profiles.audit_events(db, access.profile, limit=limit)
    return [e for e in events if (e.action or "").startswith("production_package") or (e.action or "").startswith("production_term_sheet")]


# ---------------------------------------------------------------------------
# term sheet read models
# ---------------------------------------------------------------------------

def _term_sheet_read(sheet: ProductionTermSheet, names: dict[UUID, str]) -> ProductionTermSheetRead:
    return ProductionTermSheetRead(
        id=sheet.id, version=sheet.version, status=sheet.status, funding_party_kind=sheet.funding_party_kind, lender_id=sheet.lender_id,
        funding_party_name=sheet.funding_party_name, facility_type=sheet.facility_type, approved_amount=float(sheet.approved_amount),
        min_activation_amount=float(sheet.min_activation_amount), rate_pct=float(sheet.rate_pct), term_months=int(sheet.term_months),
        monthly_debt_service=float(sheet.monthly_debt_service), debt_service_is_level_payment=bool(sheet.debt_service_is_level_payment),
        expected_funding_date=sheet.expected_funding_date, activation_date=sheet.activation_date, commencement_date=sheet.commencement_date,
        maturity_date=sheet.maturity_date, use_of_funds=sheet.use_of_funds, conditions=sheet.conditions, notes=sheet.notes,
        entered_at=sheet.entered_at, entered_by_name=names.get(sheet.entered_by_user_id), superseded_at=sheet.superseded_at,
        withdrawn_at=sheet.withdrawn_at, consumed_by_package_id=sheet.consumed_by_package_id,
        level_payment=round(pa.level_payment(float(sheet.approved_amount), float(sheet.rate_pct), int(sheet.term_months)), 2),
    )


async def term_sheet_state(db: AsyncSession, access: PackageAccess) -> dict[str, Any]:
    history_rows = await sheets_svc.sheet_history(db, access.profile.id)
    names = await _user_names(db, {r.entered_by_user_id for r in history_rows})
    parent = access.package if int(getattr(access.package, "stage", 1) or 1) == 1 else access.parent
    defaults = await sheets_svc.defaults(db, access.profile, parent)
    current = next((r for r in history_rows if r.status == "current"), None)
    return {
        "current": _term_sheet_read(current, names) if current is not None else None,
        "history": [_term_sheet_read(r, names) for r in history_rows],
        "defaults": defaults["values"], "defaults_source": defaults["sources"], "lenders": defaults["lenders"],
        "can_edit": access.is_operator and access.role in SEND_ROLES,
        "facility_types": list(pa.FACILITY_TYPES), "funding_party_kinds": list(pa.FUNDING_PARTIES),
    }


# ---------------------------------------------------------------------------
# signatures on file (resolution used by the read model and by send)
# ---------------------------------------------------------------------------

async def rm_user_id_for(db: AsyncSession, arrangement: dict[str, Any]) -> UUID | None:
    """The relationship manager named on the package, matched to a team user by stored id, then email, then name."""
    raw = arrangement.get("rm_user_id")
    if raw:
        try:
            return UUID(str(raw))
        except ValueError:
            pass
    email = str(arrangement.get("rm_email") or "").strip().lower()
    if email:
        row = (await db.execute(select(User.id).where(func.lower(User.email) == email, User.deleted_at.is_(None)))).scalar_one_or_none()
        if row:
            return row
    name = str(arrangement.get("rm_name") or "").strip()
    if name:
        row = (await db.execute(select(User.id).where(func.lower(User.name) == name.lower(), User.deleted_at.is_(None)).limit(1))).scalar_one_or_none()
        if row:
            return row
    return None


async def signatures_on_file_status(db: AsyncSession, access: PackageAccess) -> dict[str, Any]:
    """Which counterparties have a signature on file for this package: qc, sponsor, rm."""
    from app.services import stored_signatures as sigs_svc

    arrangement = {**pa.empty_arrangement(), **(access.package.arrangement or {})}
    out: dict[str, Any] = {}
    qc = await sigs_svc.current(db, "qc", None)
    out["qc"] = {"present": qc is not None, "typed_name": getattr(qc, "typed_name", None), "adopted_at": (qc.adopted_at.isoformat() if qc is not None and qc.adopted_at else None),
                 "how_to_fix": None if qc else "Adopt the company signature in Settings → Company signature (super admin)."}
    sponsor = await sigs_svc.current(db, "company", access.package.sponsor_company_id) if access.package.sponsor_company_id else None
    out["sponsor"] = {"present": sponsor is not None, "typed_name": getattr(sponsor, "typed_name", None), "adopted_at": (sponsor.adopted_at.isoformat() if sponsor is not None and sponsor.adopted_at else None),
                      "how_to_fix": None if sponsor else ("Choose the sponsor first." if not access.package.sponsor_company_id else "Authorize the sponsor's signature on file (super admin) from the sponsor row.")}
    rm_id = await rm_user_id_for(db, arrangement)
    rm = await sigs_svc.current(db, "user", rm_id) if rm_id else None
    out["rm"] = {"present": rm is not None, "user_id": str(rm_id) if rm_id else None, "typed_name": getattr(rm, "typed_name", None),
                 "adopted_at": (rm.adopted_at.isoformat() if rm is not None and rm.adopted_at else None),
                 "how_to_fix": None if rm else (f"Ask {arrangement.get('rm_name') or 'the relationship manager'} to adopt their signature (Profile → My signature)." if rm_id else "Pick the relationship manager from the team list so their signature on file can be used.")}
    out["ready"] = all(out[p]["present"] for p in ("qc", "sponsor", "rm"))
    return out


async def _sponsor_snapshot_or_none(db: AsyncSession, package: ProductionPackage, arrangement: dict[str, Any]) -> dict[str, Any] | None:
    if not package.sponsor_company_id:
        return None
    company = await db.get(ReferralPartnerCompany, package.sponsor_company_id)
    agreement = await _latest_rpa(db, package.sponsor_company_id) if company else None
    return sponsor_snapshot(company, agreement, arrangement) if company and agreement else None


# ---------------------------------------------------------------------------
# the final (stage two)
# ---------------------------------------------------------------------------

async def load_file_context(db: AsyncSession, access: PackageAccess) -> dict[str, Any]:
    """Facts the agreements print that the arrangement does not hold: identity, owners, notice information, QC constants."""
    profile = access.profile
    cfg = get_settings()
    dealer = access.dealer
    dap = None
    if dealer is not None:
        from app.dealer_os.models import DealerApplicationProfile

        dap = (await db.execute(select(DealerApplicationProfile).where(DealerApplicationProfile.dealer_id == dealer.id).limit(1))).scalar_one_or_none()
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    owners = await profiles.owner_rows(db, profile)
    business_name, email, phone = await client_contact(db, access)
    # One definition, shared with build_prefill: the stage-one form and the PDF
    # builders must derive identity and ownership from the same columns.
    identity = prefill_svc.file_identity(dealer, dap, profile, intake)
    owner_rows = prefill_svc.file_owner_rows(owners)
    return pa.jsonable({
        "identity": identity, "owners": owner_rows,
        "qc": {"notice_email": getattr(cfg, "qc_notice_email", None), "notice_address": getattr(cfg, "qc_notice_address", None),
               "signer_name": getattr(cfg, "qc_signer_name", None), "signer_title": getattr(cfg, "qc_signer_title", None),
               "address": getattr(cfg, "qc_notice_address", None)},
        "dealer_notice": {"email": email, "address": identity["address"]},
        "sponsor_notice": {"email": (access.package.arrangement or {}).get("sponsor_email"), "address": (access.package.arrangement or {}).get("sponsor_address")},
        "business_name": business_name, "client_email": email, "client_phone": phone,
    })


async def reapply_terms(db: AsyncSession, child: ProductionPackage, sheet: ProductionTermSheet, user: User) -> ProductionPackage:
    """The single write path for term keys on a draft final."""
    if child.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "package_frozen", "message": "Reopen the final before changing the terms."})
    arrangement, applied = pa.apply_term_sheet(child.arrangement or {}, sheets_svc.sheet_terms(sheet))
    provenance = dict(child.prefill_provenance or {})
    for key in applied:
        provenance[key] = {"source": "term_sheet", "label": f"Term sheet v{sheet.version}", "confirmed": True}
    computed = pa.compute(arrangement, stage=2)
    child.arrangement = pa.jsonable(arrangement)
    child.prefill_provenance = provenance
    child.attention = computed["attention"]
    child.computed_cache = pa.jsonable(computed)
    child.term_sheet_id = sheet.id
    child.version = (child.version or 1) + 1
    child.updated_by_user_id = user.id
    child.updated_via = "operator"
    sheet.consumed_by_package_id = child.id
    sheet.consumed_at = _now()
    await db.flush()
    profile = await db.get(ApplicationProfile, child.profile_id)
    await profiles.log_profile_action(
        db, profile, user, "production_package.terms_reapplied", f"Term sheet v{sheet.version} applied to the final",
        target_type="production_package", target_id=child.id, metadata={"term_sheet_id": str(sheet.id), "version": sheet.version, "changes": applied},
    )
    return child


async def draft_final(db: AsyncSession, access: PackageAccess) -> PackageAccess:
    """Create the stage-two child from the executed stage-one revision and the current term sheet."""
    parent = await db.get(ProductionPackage, access.package.id, with_for_update=True)
    if not (access.is_operator and access.role in SEND_ROLES):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin or underwriter role required to draft the final")
    if int(parent.stage or 1) != 1 or parent.status != "executed" or not parent.frozen_revision_id:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "stage_one_not_executed", "message": "Execute the production commitment before drafting the final."})
    source = await db.get(ProductionPackageRevision, parent.frozen_revision_id)
    if source is None or source.status != "executed":
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "stage_one_not_executed", "message": "The executed stage-one revision is missing."})
    existing = (
        await db.execute(
            select(ProductionPackage).where(ProductionPackage.parent_package_id == parent.id, ProductionPackage.status != "void")
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "final_exists", "final_package_id": str(existing.id), "message": "A final package already exists."})
    sheet = await sheets_svc.current_sheet(db, access.profile.id)
    if sheet is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "terms_missing", "message": "Record the term sheet first — the final package is drafted from it."})
    snapshot = source.snapshot or {}
    arrangement = {**pa.empty_arrangement(), **(snapshot.get("arrangement") or {})}
    provenance: dict[str, Any] = {}
    label = f"Executed stage one (R{source.revision_no})"
    for key, rule in pa.FIELD_RULES_BY_KEY.items():
        if not pa.is_blank(rule, arrangement.get(key)):
            provenance[key] = {"source": "stage_one", "label": label, "confirmed": True}
    for pk in pa.PRODUCT_KEYS:
        provenance[f"products.{pk}"] = {"source": "stage_one", "label": label, "confirmed": True}
    arrangement, applied = pa.apply_term_sheet(arrangement, sheets_svc.sheet_terms(sheet))
    for key in applied:
        provenance[key] = {"source": "term_sheet", "label": f"Term sheet v{sheet.version}", "confirmed": True}
    # File-derived closing prefills (unconfirmed).
    ctx = await load_file_context(db, access)
    ident = ctx.get("identity") or {}
    for key, val in (("identity_formation_date", ident.get("formation_date")), ("identity_ein", ident.get("ein")), ("identity_naics", ident.get("naics")),
                     ("identity_license", ident.get("license")), ("identity_website", ident.get("website")), ("dealer_notice_email", ctx.get("client_email"))):
        if val and pa.is_blank(pa.FIELD_RULES_BY_KEY[key], arrangement.get(key)):
            arrangement[key] = val
            provenance[key] = {"source": "file", "label": "From the file", "confirmed": False}
    if ctx.get("owners") and not arrangement.get("owners"):
        arrangement["owners"] = ctx["owners"]
        provenance["owners"] = {"source": "owners", "label": "Owners", "confirmed": False}
    exclusions = str(arrangement.get("exclusions") or "").strip()
    if exclusions and not arrangement.get("exclusion_1"):
        parts = [p.strip() for p in exclusions.replace("\n", ";").split(";") if p.strip()][:3]
        for i, part in enumerate(parts, start=1):
            arrangement[f"exclusion_{i}"] = part
            provenance[f"exclusion_{i}"] = {"source": "stage_one", "label": label, "confirmed": True}
    computed = pa.compute(arrangement, stage=2)
    child = ProductionPackage(
        profile_id=parent.profile_id, intake_id=parent.intake_id, dealer_id=parent.dealer_id, stage=2, status="draft",
        arrangement=pa.jsonable(arrangement), prefill_provenance=provenance, attention=computed["attention"],
        computed_cache=pa.jsonable(computed), sponsor_company_id=parent.sponsor_company_id, version=1,
        parent_package_id=parent.id, source_revision_id=source.id, term_sheet_id=sheet.id,
        created_by_user_id=access.user.id, updated_by_user_id=access.user.id, updated_via="operator",
    )
    db.add(child)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "final_exists", "message": "A final package already exists."}) from exc
    sheet.consumed_by_package_id = child.id
    sheet.consumed_at = _now()
    await db.flush()
    await profiles.log_profile_action(
        db, access.profile, access.user, "production_package.final_drafted", f"Final package drafted from stage one R{source.revision_no} and term sheet v{sheet.version}",
        target_type="production_package", target_id=child.id,
        metadata={"parent_package_id": str(parent.id), "source_revision_id": str(source.id), "source_revision_no": source.revision_no,
                  "source_content_sha256": source.content_sha256, "term_sheet_id": str(sheet.id), "term_sheet_version": sheet.version, "applied": applied},
    )
    await profiles.log_profile_action(
        db, access.profile, access.user, "production_package.final_drafted_from", "A final package was drafted from this executed commitment",
        target_type="production_package", target_id=parent.id, metadata={"final_package_id": str(child.id)},
    )
    result = PackageAccess(package=child, profile=access.profile, user=access.user, mode="operator", dealer=access.dealer, via="operator")
    await _load_family(db, result)
    return result


async def adopt_sponsor_signature(db: AsyncSession, access: PackageAccess, *, authorization_note: str, request: Request | None) -> dict[str, Any]:
    """Authorize the sponsor's officer signature (captured on their Referral Protection Agreement) for use on production agreements."""
    from app.services import stored_signatures as sigs_svc

    if not access.capabilities().can_adopt_sponsor_signature:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")
    company, agreement = await require_signed_sponsor(db, access.package)
    if not agreement.signature_s3_key:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "no_agreement_signature", "message": "The agreement on file carries no signature image; upload one instead."})
    sig = await sigs_svc.adopt_company_signature_from_agreement(
        db, company_id=company.id, agreement=agreement, admin=access.user, authorization_note=authorization_note, request=request,
    )
    await profiles.log_profile_action(
        db, access.profile, access.user, "production_package.sponsor_signature_adopted", f"Sponsor signature on file authorized for {company.name}",
        target_type="production_package", target_id=access.package.id,
        metadata={"company_id": str(company.id), "agreement": agreement.contract_number, "stored_signature_id": str(sig.id)},
    )
    return sigs_svc.read_model(sig, presign=True)
