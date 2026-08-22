"""Unified Operator Console read model.

The product stores different moments of the lifecycle in different tables:
agent-side Deals, funding Loans, document Buckets, public AI intakes, and
Dealer OS rep files. The dashboard should not have to rediscover those joins on
every screen, so this router projects them into one normalized file shape.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import false as sql_false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.dealer_os.models import DealerBusiness, DealerRepLead
from app.deps import CurrentUser
from app.enums import Role
from app.models.bucket import (
    Bucket,
    BucketActivityLog,
    BucketFile,
    BucketRequestedDocument,
    BucketVendorAccess,
)
from app.models.client import Client
from app.models.deal import Deal
from app.models.loan import Loan
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.scoping import regional_manager_broker_ids_subquery, scope_client_query, scope_loan_query
from app.schemas.operator_file import (
    BucketIntakeLinkRequest,
    BucketIntakeLinkResult,
    UnifiedAuditItem,
    UnifiedDocumentProgress,
    UnifiedFileDetail,
    UnifiedFilePage,
    UnifiedFileRow,
    UnifiedRollup,
    UnifiedStage,
)

router = APIRouter(prefix="/operator-files", tags=["operator-files"])

INTERNAL_ROLES = {Role.SUPER_ADMIN, Role.LOAN_EXEC}
OPERATOR_ROLES = {Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.BROKER, Role.REGIONAL_MANAGER}

VERTICAL_LABELS = {
    "real_estate": "Real estate",
    "main_street": "Main Street",
    "dealer": "Dealer",
    "mca": "MCA",
}

ORIGIN_LABELS = {
    "console": "Console",
    "agent": "Agent / realtor",
    "rep": "Rep desk",
    "dealer": "Dealer partner",
    "ai_intake": "AI intake",
}

REAL_ESTATE_LADDER = [
    ("lead", "Lead"),
    ("contacted", "Contacted"),
    ("verified", "Verified"),
    ("ready_for_lending", "Ready for lending"),
]
APPLICATION_LADDER = [
    ("applicant_intake", "Applicant intake"),
    ("verification", "Verification"),
    ("financial_profile", "Financial profile"),
    ("credit_application", "Credit application"),
    ("contracts_execution", "Contracts and execution"),
]
FUNDING_LADDER = [
    ("prequalified", "Prequalified"),
    ("collecting_docs", "Collecting docs"),
    ("lender_connected", "Lender connected"),
    ("processing", "Processing"),
    ("closing", "Closing"),
    ("funded", "Funded"),
]

CLIENT_STAGE_TO_WORKING = {
    "lead": "lead",
    "contacted": "contacted",
    "verified": "verified",
    "ready_for_lending": "ready_for_lending",
    "processing": "ready_for_lending",
    "funded": "ready_for_lending",
    "lost": "lead",
}

INTAKE_STATUS_TO_WORKING = {
    "collecting": "applicant_intake",
    "submitted": "verification",
    "reviewing": "financial_profile",
    "reviewed": "credit_application",
    "completed": "contracts_execution",
    "closed": "contracts_execution",
    "denied": "verification",
}

REP_STATUS_TO_WORKING = {
    "draft": "applicant_intake",
    "info_collected": "verification",
    "awaiting_docs": "financial_profile",
    "analyzing": "financial_profile",
    "decision_ready": "credit_application",
    "forms_out": "contracts_execution",
    "signed": "contracts_execution",
    "complete": "contracts_execution",
    "declined": "verification",
    "stalled": "verification",
}


def _role_value(user: User) -> str:
    return user.role.value if hasattr(user.role, "value") else str(user.role)


def _stage(ladder: list[tuple[str, str]], key: str, family: str) -> UnifiedStage:
    idx = next((i for i, (candidate, _label) in enumerate(ladder) if candidate == key), 0)
    resolved_key, label = ladder[idx]
    return UnifiedStage(
        key=resolved_key,
        label=label,
        index=idx + 1,
        total=len(ladder),
        family=family,  # type: ignore[arg-type]
    )


def _funding_stage(key: str | None) -> UnifiedStage | None:
    if not key:
        return None
    return _stage(FUNDING_LADDER, key, "funding")


def _working_stage(vertical: str, key: str) -> UnifiedStage:
    ladder = REAL_ESTATE_LADDER if vertical == "real_estate" else APPLICATION_LADDER
    return _stage(ladder, key, "working")


def _stage_tone(label: str, health: str | None = None) -> str:
    text = f"{label} {health or ''}".lower()
    if any(term in text for term in ("funded", "complete", "signed", "closing", "ready")):
        return "ok"
    if any(term in text for term in ("missing", "stalled", "denied", "declined", "bad")):
        return "bad"
    if any(term in text for term in ("collecting", "awaiting", "verification", "review")):
        return "warn"
    if any(term in text for term in ("processing", "connected", "credit")):
        return "acc"
    return "mut"


def _health(label: str, tone_hint: str | None = None) -> tuple[str, str]:
    text = f"{label} {tone_hint or ''}".lower()
    if any(term in text for term in ("denied", "declined", "stalled", "missing")):
        return ("Needs attention", "bad")
    if any(term in text for term in ("collecting", "awaiting", "review", "verification")):
        return ("Needs follow-up", "warn")
    if any(term in text for term in ("funded", "complete", "signed", "ready", "closing")):
        return ("On track", "ok")
    return ("On track", "mut")


def _money(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _money_label(value: Any) -> str | None:
    amount = _money(value)
    if amount is None:
        return None
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.0f}k"
    return f"${amount:,.0f}"


def _titleize(value: str | None) -> str:
    if not value:
        return "Unknown"
    return value.replace("_", " ").replace("-", " ").title()


def _variant_vertical(variant: str | None) -> str:
    if variant == "main_street_v1":
        return "main_street"
    if variant == "mca_refi_v1":
        return "mca"
    if variant == "dealer_gatekeeper_v1":
        return "dealer"
    return "real_estate"


def _loan_vertical(loan: Loan) -> str:
    kind = (loan.funding_file_kind or "").lower()
    if "mca" in kind:
        return "mca"
    if "floorplan" in kind or "dealer" in kind:
        return "dealer"
    if "business" in kind or "equipment" in kind:
        return "main_street"
    return "real_estate"


def _dealer_vertical(dealer: DealerBusiness) -> str:
    if (dealer.industry or "") == "auto_dealer" or (dealer.funding_purpose or "") == "floorplan":
        return "dealer"
    if (dealer.funding_purpose or "") == "refinance":
        return "mca"
    return "main_street"


def _bucket_vertical(bucket: Bucket, intake: PublicUnderwritingIntake | None = None) -> str:
    if intake is not None:
        return _variant_vertical(intake.variant)
    ctx = bucket.ai_context or {}
    review_type = str(ctx.get("review_type") or ctx.get("variant") or "")
    if review_type:
        return _variant_vertical(review_type)
    bucket_type = (bucket.bucket_type or "").lower()
    if "main_street" in bucket_type:
        return "main_street"
    if "mca" in bucket_type:
        return "mca"
    if "dealer" in bucket_type:
        return "dealer"
    return "real_estate"


def _bucket_links(bucket: Bucket | None) -> tuple[list[UUID], list[UUID]]:
    if bucket is None:
        return [], []
    links = (bucket.ai_context or {}).get("unified_links")
    if not isinstance(links, dict):
        return [], []
    bucket_ids: list[UUID] = []
    intake_ids: list[UUID] = []
    for value in links.get("bucket_ids") or []:
        try:
            bucket_ids.append(UUID(str(value)))
        except (TypeError, ValueError):
            pass
    for value in links.get("intake_ids") or []:
        try:
            intake_ids.append(UUID(str(value)))
        except (TypeError, ValueError):
            pass
    return bucket_ids, intake_ids


def _document_progress(bucket: Bucket | None) -> UnifiedDocumentProgress:
    if bucket is None:
        return UnifiedDocumentProgress()
    requested: list[BucketRequestedDocument] = list(bucket.requested_documents or [])
    files: list[BucketFile] = [f for f in list(bucket.files or []) if f.deleted_at is None]
    docs = [doc for doc in requested if not doc.requires_signature]
    sigs = [doc for doc in requested if doc.requires_signature]
    docs_total = len(docs) or len(files)
    docs_uploaded = len([doc for doc in docs if doc.status == "uploaded"]) if docs else len(files)
    sigs_total = len(sigs)
    sigs_uploaded = len([doc for doc in sigs if doc.status == "uploaded"])
    total = docs_total + sigs_total
    uploaded = docs_uploaded + sigs_uploaded
    label = f"{uploaded} of {total}" if total else f"{len(files)} files"
    return UnifiedDocumentProgress(
        docs_uploaded=docs_uploaded,
        docs_total=docs_total,
        signatures_uploaded=sigs_uploaded,
        signatures_total=sigs_total,
        bucket_progress_label=label,
    )


def _program_tags_for_intake(intake: PublicUnderwritingIntake) -> list[str]:
    tags = [_titleize(_variant_vertical(intake.variant))]
    if intake.loan_purpose:
        tags.append(_titleize(intake.loan_purpose))
    state = intake.intake_state or {}
    if isinstance(state.get("main_street_details"), dict):
        details = state["main_street_details"]
        for key in ("industry", "intent"):
            if details.get(key):
                tags.append(_titleize(str(details[key])))
    return _dedupe(tags)


def _program_tags_for_loan(loan: Loan) -> list[str]:
    return _dedupe([
        _titleize(str(loan.type) if loan.type else None),
        _titleize(str(loan.purpose) if loan.purpose else None),
        _titleize(loan.funding_file_kind),
    ])


def _dedupe(values: list[str | None]) -> list[str]:
    out: list[str] = []
    for value in values:
        if not value:
            continue
        if value not in out:
            out.append(value)
    return out


def _client_stage_value(client: Client | None) -> str:
    raw = getattr(client, "stage", None)
    return raw.value if hasattr(raw, "value") else str(raw or "lead")


def _source_ref(prefix: str, value: UUID | None) -> str:
    if value is None:
        return prefix
    return f"{prefix}-{str(value)[:8].upper()}"


def _with_bucket_relationships(stmt):
    return stmt.options(
        selectinload(Bucket.requested_documents),
        selectinload(Bucket.files),
    )


def _visible_client_stmt(user: User):
    return scope_client_query(user, select(Client.id))


async def _bucket_map(db: AsyncSession, bucket_ids: set[UUID]) -> dict[UUID, Bucket]:
    if not bucket_ids:
        return {}
    rows = (
        await db.execute(
            _with_bucket_relationships(select(Bucket).where(Bucket.id.in_(bucket_ids), Bucket.archived_at.is_(None)))
        )
    ).scalars().all()
    return {row.id: row for row in rows}


async def _intake_by_bucket_map(db: AsyncSession, bucket_ids: set[UUID]) -> dict[UUID, PublicUnderwritingIntake]:
    if not bucket_ids:
        return {}
    rows = (
        await db.execute(
            select(PublicUnderwritingIntake).where(PublicUnderwritingIntake.bucket_id.in_(bucket_ids))
        )
    ).scalars().all()
    return {row.bucket_id: row for row in rows}


async def _deal_rows(user: User, db: AsyncSession) -> list[UnifiedFileRow]:
    stmt = (
        select(Deal, Client)
        .join(Client, Client.id == Deal.client_id)
        .order_by(Deal.updated_at.desc())
    )
    stmt = scope_client_query(user, stmt)
    pairs = list((await db.execute(stmt)).all())
    loan_ids = {deal.promoted_loan_id for deal, _client in pairs if deal.promoted_loan_id}
    loan_map = {
        loan.id: loan
        for loan in (
            (await db.execute(select(Loan).where(Loan.id.in_(loan_ids)))).scalars().all()
            if loan_ids
            else []
        )
    }
    rows: list[UnifiedFileRow] = []
    for deal, client in pairs:
        loan = loan_map.get(deal.promoted_loan_id)
        vertical = "real_estate"
        working_key = CLIENT_STAGE_TO_WORKING.get(_client_stage_value(client), "lead")
        working = _working_stage(vertical, working_key)
        funding = _funding_stage(str(loan.stage) if loan is not None else None)
        normalized = funding.label if funding else working.label
        health, health_tone = _health(normalized, deal.status)
        rows.append(
            UnifiedFileRow(
                id=f"deal:{deal.id}",
                source_kind="deal",
                source_id=deal.id,
                ref=_source_ref("QC-D", deal.id),
                title=deal.title,
                subtitle=deal.summary,
                principal=client.name,
                phone=client.phone,
                client_id=client.id,
                client_name=client.name,
                deal_id=deal.id,
                loan_id=loan.id if loan else None,
                vertical=vertical,  # type: ignore[arg-type]
                vertical_label=VERTICAL_LABELS[vertical],
                origin="agent",
                origin_label=ORIGIN_LABELS["agent"],
                source_label="Realtor / mobile pipeline",
                amount=_money(getattr(loan, "amount", None) or deal.target_price or deal.list_price),
                amount_label=_money_label(getattr(loan, "amount", None) or deal.target_price or deal.list_price),
                working_stage=working,
                funding_stage=funding,
                normalized_stage=normalized,
                stage_tone=_stage_tone(normalized, health),
                health=health,
                health_tone=health_tone,  # type: ignore[arg-type]
                program_tags=_program_tags_for_loan(loan) if loan else [_titleize(deal.deal_type)],
                owner_name=None,
                source_deal_id=loan.source_deal_id if loan else None,
                promoted_loan_id=deal.promoted_loan_id,
                updated_at=deal.updated_at,
            )
        )
    return rows


async def _loan_rows(user: User, db: AsyncSession) -> list[UnifiedFileRow]:
    stmt = (
        select(Loan, Client)
        .join(Client, Client.id == Loan.client_id)
        .where(Loan.source_deal_id.is_(None))
        .order_by(Loan.updated_at.desc())
    )
    stmt = scope_loan_query(user, stmt)
    pairs = list((await db.execute(stmt)).all())
    rows: list[UnifiedFileRow] = []
    for loan, client in pairs:
        vertical = _loan_vertical(loan)
        funding = _funding_stage(str(loan.stage))
        normalized = funding.label if funding else "Prequalified"
        health, health_tone = _health(normalized, str(loan.deal_health) if loan.deal_health else None)
        origin = "agent" if loan.source_deal_id else "console"
        rows.append(
            UnifiedFileRow(
                id=f"loan:{loan.id}",
                source_kind="loan",
                source_id=loan.id,
                ref=loan.deal_id,
                title=loan.entity_name or client.name,
                subtitle=loan.address,
                principal=client.name,
                phone=client.phone,
                client_id=client.id,
                client_name=client.name,
                loan_id=loan.id,
                vertical=vertical,  # type: ignore[arg-type]
                vertical_label=VERTICAL_LABELS[vertical],
                origin=origin,  # type: ignore[arg-type]
                origin_label=ORIGIN_LABELS[origin],
                source_label="Funding desk",
                amount=_money(loan.amount),
                amount_label=_money_label(loan.amount),
                funding_stage=funding,
                normalized_stage=normalized,
                stage_tone=_stage_tone(normalized, health),
                health=health,
                health_tone=health_tone,  # type: ignore[arg-type]
                program_tags=_program_tags_for_loan(loan),
                source_deal_id=loan.source_deal_id,
                updated_at=loan.updated_at,
            )
        )
    return rows


def _scope_intake_stmt(user: User, stmt):
    if user.role in INTERNAL_ROLES:
        return stmt
    if user.role == Role.CLIENT:
        if user.client is None:
            return stmt.where(sql_false())
        return stmt.where(PublicUnderwritingIntake.client_id == user.client.id)
    if user.role == Role.BROKER:
        if user.broker is None:
            return stmt.where(PublicUnderwritingIntake.broker_id == user.id)
        visible_clients = _visible_client_stmt(user)
        return stmt.where(
            or_(
                PublicUnderwritingIntake.broker_id == user.id,
                PublicUnderwritingIntake.client_id.in_(visible_clients),
            )
        )
    if user.role == Role.REGIONAL_MANAGER:
        return stmt.join(Client, Client.id == PublicUnderwritingIntake.client_id, isouter=True).where(
            Client.broker_id.in_(regional_manager_broker_ids_subquery(user))
        )
    if user.role == Role.DEALER_PARTNER:
        return stmt.where(PublicUnderwritingIntake.broker_id == user.id)
    return stmt.where(sql_false())


async def _intake_rows(user: User, db: AsyncSession) -> list[UnifiedFileRow]:
    stmt = (
        select(PublicUnderwritingIntake, Bucket, Client, User)
        .join(Bucket, Bucket.id == PublicUnderwritingIntake.bucket_id)
        .outerjoin(Client, Client.id == PublicUnderwritingIntake.client_id)
        .outerjoin(User, User.id == PublicUnderwritingIntake.broker_id)
        .where(Bucket.archived_at.is_(None))
        .options(
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.requested_documents),
            selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.files),
        )
        .order_by(PublicUnderwritingIntake.updated_at.desc())
    )
    stmt = _scope_intake_stmt(user, stmt)
    rows: list[UnifiedFileRow] = []
    for intake, bucket, client, partner in list((await db.execute(stmt)).all()):
        vertical = _variant_vertical(intake.variant)
        origin = "dealer" if intake.broker_id else "ai_intake"
        working_key = INTAKE_STATUS_TO_WORKING.get(intake.status, "applicant_intake")
        working = _working_stage(vertical, working_key)
        normalized = working.label
        health, health_tone = _health(normalized, intake.outcome_status)
        linked_bucket_ids, linked_intake_ids = _bucket_links(bucket)
        if intake.id not in linked_intake_ids:
            linked_intake_ids.append(intake.id)
        if bucket.id not in linked_bucket_ids:
            linked_bucket_ids.append(bucket.id)
        title = intake.business_name or intake.full_name
        rows.append(
            UnifiedFileRow(
                id=f"intake:{intake.id}",
                source_kind="intake",
                source_id=intake.id,
                ref=_source_ref("QC-I", intake.id),
                title=title,
                subtitle=intake.loan_purpose,
                principal=intake.full_name,
                phone=intake.phone,
                client_id=intake.client_id,
                client_name=client.name if client else intake.full_name,
                intake_id=intake.id,
                bucket_id=bucket.id,
                vertical=vertical,  # type: ignore[arg-type]
                vertical_label=VERTICAL_LABELS[vertical],
                origin=origin,  # type: ignore[arg-type]
                origin_label=ORIGIN_LABELS[origin],
                source_label=partner.name if partner else "Public AI intake",
                amount=_money(intake.requested_loan_amount),
                amount_label=_money_label(intake.requested_loan_amount),
                working_stage=working,
                normalized_stage=normalized,
                stage_tone=_stage_tone(normalized, health),
                health=health,
                health_tone=health_tone,  # type: ignore[arg-type]
                document_progress=_document_progress(bucket),
                program_tags=_program_tags_for_intake(intake),
                owner_name=partner.name if partner else None,
                dealer_name=partner.name if intake.broker_id and partner else None,
                linked_bucket_ids=linked_bucket_ids,
                linked_intake_ids=linked_intake_ids,
                updated_at=intake.updated_at,
            )
        )
    return rows


async def _bucket_rows(user: User, db: AsyncSession) -> list[UnifiedFileRow]:
    if user.role == Role.VENDOR:
        stmt = (
            select(Bucket)
            .join(BucketVendorAccess, BucketVendorAccess.bucket_id == Bucket.id)
            .where(BucketVendorAccess.vendor_user_id == user.id, Bucket.archived_at.is_(None))
            .order_by(Bucket.updated_at.desc())
        )
    elif user.role in INTERNAL_ROLES:
        stmt = select(Bucket).where(Bucket.archived_at.is_(None)).order_by(Bucket.updated_at.desc())
    else:
        return []
    buckets = list((await db.execute(_with_bucket_relationships(stmt))).scalars().all())
    intake_by_bucket = await _intake_by_bucket_map(db, {bucket.id for bucket in buckets})
    rows: list[UnifiedFileRow] = []
    for bucket in buckets:
        if bucket.id in intake_by_bucket:
            continue
        vertical = _bucket_vertical(bucket)
        linked_bucket_ids, linked_intake_ids = _bucket_links(bucket)
        if bucket.id not in linked_bucket_ids:
            linked_bucket_ids.append(bucket.id)
        working = _working_stage(vertical, "applicant_intake" if vertical != "real_estate" else "lead")
        health, health_tone = _health(bucket.status)
        rows.append(
            UnifiedFileRow(
                id=f"bucket:{bucket.id}",
                source_kind="bucket",
                source_id=bucket.id,
                ref=_source_ref("QC-B", bucket.id),
                title=bucket.name,
                subtitle=bucket.purpose or bucket.description,
                principal=bucket.client_name,
                client_name=bucket.client_name,
                bucket_id=bucket.id,
                vertical=vertical,  # type: ignore[arg-type]
                vertical_label=VERTICAL_LABELS[vertical],
                origin="console",
                origin_label=ORIGIN_LABELS["console"],
                source_label="Document room",
                working_stage=working,
                normalized_stage=_titleize(bucket.status),
                stage_tone=_stage_tone(bucket.status, None),
                health=health,
                health_tone=health_tone,  # type: ignore[arg-type]
                document_progress=_document_progress(bucket),
                program_tags=[_titleize(bucket.bucket_type)],
                linked_bucket_ids=linked_bucket_ids,
                linked_intake_ids=linked_intake_ids,
                updated_at=bucket.updated_at,
            )
        )
    return rows


async def _dealer_rows(user: User, db: AsyncSession) -> list[UnifiedFileRow]:
    if user.role not in INTERNAL_ROLES and user.role != Role.FIELD_REP and user.role != Role.DEALER:
        return []
    stmt = (
        select(DealerBusiness, DealerRepLead, User)
        .outerjoin(DealerRepLead, DealerRepLead.dealer_id == DealerBusiness.id)
        .outerjoin(User, User.id == DealerBusiness.owner_user_id)
        .order_by(DealerBusiness.updated_at.desc())
    )
    if user.role == Role.FIELD_REP:
        stmt = stmt.where(DealerBusiness.owner_user_id == user.id)
    if user.role == Role.DEALER:
        stmt = stmt.where(DealerBusiness.dealer_user_id == user.id)
    pairs = list((await db.execute(stmt)).all())
    bucket_ids = {dealer.bucket_id for dealer, _rep, _owner in pairs if dealer.bucket_id}
    buckets = await _bucket_map(db, bucket_ids)
    rows: list[UnifiedFileRow] = []
    for dealer, rep, owner in pairs:
        vertical = _dealer_vertical(dealer)
        bucket = buckets.get(dealer.bucket_id)
        working_key = REP_STATUS_TO_WORKING.get(rep.status if rep else dealer.status, "applicant_intake")
        working = _working_stage(vertical, working_key)
        normalized = working.label
        health, health_tone = _health(normalized, rep.status if rep else dealer.status)
        linked_bucket_ids, linked_intake_ids = _bucket_links(bucket)
        if bucket and bucket.id not in linked_bucket_ids:
            linked_bucket_ids.append(bucket.id)
        if dealer.handoff_intake_id and dealer.handoff_intake_id not in linked_intake_ids:
            linked_intake_ids.append(dealer.handoff_intake_id)
        rows.append(
            UnifiedFileRow(
                id=f"dealer:{dealer.id}",
                source_kind="dealer",
                source_id=dealer.id,
                ref=dealer.case_ref or _source_ref("QC-R", dealer.id),
                title=dealer.name,
                subtitle=dealer.funding_purpose or dealer.industry,
                principal=dealer.legal_name or dealer.name,
                phone=dealer.phone,
                bucket_id=dealer.bucket_id,
                dealer_id=dealer.id,
                intake_id=dealer.handoff_intake_id,
                vertical=vertical,  # type: ignore[arg-type]
                vertical_label=VERTICAL_LABELS[vertical],
                origin="rep" if rep else "console",
                origin_label=ORIGIN_LABELS["rep" if rep else "console"],
                source_label="Rep desk" if rep else "Dealer Capital OS",
                amount=_money(dealer.funding_goal),
                amount_label=_money_label(dealer.funding_goal),
                working_stage=working,
                normalized_stage=normalized,
                stage_tone=_stage_tone(normalized, health),
                health=health,
                health_tone=health_tone,  # type: ignore[arg-type]
                document_progress=_document_progress(bucket),
                program_tags=[_titleize(dealer.industry), _titleize(dealer.funding_purpose)],
                owner_name=owner.name if owner else None,
                rep_name=owner.name if rep and owner else None,
                case_ref=dealer.case_ref,
                linked_bucket_ids=linked_bucket_ids,
                linked_intake_ids=linked_intake_ids,
                updated_at=dealer.updated_at,
            )
        )
    return rows


async def _all_rows(user: User, db: AsyncSession) -> list[UnifiedFileRow]:
    rows: list[UnifiedFileRow] = []
    rows.extend(await _deal_rows(user, db))
    rows.extend(await _loan_rows(user, db))
    rows.extend(await _intake_rows(user, db))
    rows.extend(await _bucket_rows(user, db))
    rows.extend(await _dealer_rows(user, db))
    rows.sort(key=lambda row: row.updated_at, reverse=True)
    return rows


def _apply_filters(
    rows: list[UnifiedFileRow],
    *,
    vertical: str,
    origin: str,
    q: str | None,
) -> list[UnifiedFileRow]:
    filtered = rows
    if vertical != "all":
        filtered = [row for row in filtered if row.vertical == vertical]
    if origin != "all":
        filtered = [row for row in filtered if row.origin == origin]
    if q:
        needle = q.lower().strip()
        filtered = [
            row
            for row in filtered
            if needle in " ".join(
                [
                    row.ref,
                    row.title,
                    row.subtitle or "",
                    row.principal or "",
                    row.client_name or "",
                    row.source_label,
                    " ".join(row.program_tags),
                ]
            ).lower()
        ]
    return filtered


def _rollup(rows: list[UnifiedFileRow]) -> UnifiedRollup:
    by_vertical = Counter(row.vertical for row in rows)
    by_origin = Counter(row.origin for row in rows)
    by_stage = Counter(row.normalized_stage for row in rows)
    return UnifiedRollup(
        total=len(rows),
        by_vertical=dict(by_vertical),
        by_origin=dict(by_origin),
        by_stage=dict(by_stage),
        needs_attention=len([row for row in rows if row.health_tone in {"bad", "warn"}]),
        promoted=len([row for row in rows if row.funding_stage is not None or row.promoted_loan_id is not None]),
    )


@router.get("", response_model=UnifiedFilePage)
async def list_operator_files(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    vertical: str = Query("all"),
    origin: str = Query("all"),
    q: str | None = Query(None),
    limit: int = Query(250, ge=1, le=500),
) -> UnifiedFilePage:
    rows = await _all_rows(user, db)
    rows = _apply_filters(rows, vertical=vertical, origin=origin, q=q)
    return UnifiedFilePage(
        items=rows[:limit],
        rollup=_rollup(rows),
        limit=limit,
        filters={"vertical": vertical, "origin": origin, "q": q},
    )


@router.get("/{source_kind}/{source_id}", response_model=UnifiedFileDetail)
async def get_operator_file(
    source_kind: str,
    source_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UnifiedFileDetail:
    rows = await _all_rows(user, db)
    row = next((item for item in rows if item.source_kind == source_kind and item.source_id == source_id), None)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unified file not found")
    audit: list[UnifiedAuditItem] = []
    if row.bucket_id is not None:
        logs = (
            await db.execute(
                select(BucketActivityLog)
                .where(BucketActivityLog.bucket_id == row.bucket_id)
                .order_by(BucketActivityLog.created_at.desc())
                .limit(50)
            )
        ).scalars().all()
        audit = [
            UnifiedAuditItem(
                id=log.id,
                action=log.action,
                actor_name=log.actor_name,
                actor_role=log.actor_role,
                detail=log.detail,
                created_at=log.created_at,
            )
            for log in logs
        ]
    return UnifiedFileDetail(file=row, audit=audit)


def _client_ip(request: Request) -> str | None:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()[:80] or None
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()[:80] or None
    return request.client.host[:80] if request.client else None


def _user_agent(request: Request) -> str | None:
    value = request.headers.get("user-agent")
    return value[:500] if value else None


@router.post("/bucket-intake-links", response_model=BucketIntakeLinkResult)
async def link_bucket_to_intake(
    payload: BucketIntakeLinkRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketIntakeLinkResult:
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator role required")
    bucket = (
        await db.execute(
            _with_bucket_relationships(
                select(Bucket).where(Bucket.id == payload.bucket_id, Bucket.archived_at.is_(None))
            )
        )
    ).scalar_one_or_none()
    if bucket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bucket not found")
    intake = await db.get(PublicUnderwritingIntake, payload.intake_id)
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI intake not found")

    active_file_ids = {file.id for file in bucket.files if file.deleted_at is None}
    selected_file_ids = [file_id for file_id in payload.file_ids if file_id in active_file_ids]

    ctx = dict(bucket.ai_context or {})
    links = dict(ctx.get("unified_links") or {})
    intake_ids = [str(value) for value in links.get("intake_ids") or []]
    bucket_ids = [str(value) for value in links.get("bucket_ids") or []]
    if str(intake.id) not in intake_ids:
        intake_ids.append(str(intake.id))
    if str(bucket.id) not in bucket_ids:
        bucket_ids.append(str(bucket.id))
    linked_files_by_intake = dict(links.get("file_ids_by_intake") or {})
    current_files = [str(value) for value in linked_files_by_intake.get(str(intake.id)) or []]
    for file_id in selected_file_ids:
        if str(file_id) not in current_files:
            current_files.append(str(file_id))
    linked_files_by_intake[str(intake.id)] = current_files
    links.update(
        {
            "bucket_ids": bucket_ids,
            "intake_ids": intake_ids,
            "primary_intake_id": str(intake.id),
            "relationship_by_intake": {
                **dict(links.get("relationship_by_intake") or {}),
                str(intake.id): payload.relationship,
            },
            "file_ids_by_intake": linked_files_by_intake,
            "updated_at": datetime.now(UTC).isoformat(),
            "updated_by_user_id": str(user.id),
        }
    )
    if payload.note:
        links["note"] = payload.note
    ctx["unified_links"] = links
    bucket.ai_context = ctx
    audit_ids: list[UUID] = []

    bucket_audit_id = uuid4()
    audit_ids.append(bucket_audit_id)
    db.add(
        BucketActivityLog(
            id=bucket_audit_id,
            bucket_id=bucket.id,
            actor_user_id=user.id,
            actor_name=user.name,
            actor_email=user.email,
            actor_role=_role_value(user),
            action="unified_bucket_intake_linked",
            target_type="public_underwriting_intake",
            target_id=str(intake.id),
            detail=payload.note or f"Linked {len(selected_file_ids)} file(s) to unified AI intake as {payload.relationship}",
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
            created_at=datetime.now(UTC),
        )
    )
    if intake.bucket_id != bucket.id:
        intake_audit_id = uuid4()
        audit_ids.append(intake_audit_id)
        db.add(
            BucketActivityLog(
                id=intake_audit_id,
                bucket_id=intake.bucket_id,
                actor_user_id=user.id,
                actor_name=user.name,
                actor_email=user.email,
                actor_role=_role_value(user),
                action="unified_intake_linked_to_bucket",
                target_type="bucket",
                target_id=str(bucket.id),
                detail=payload.note or f"Linked from unified Operator Console drawer as {payload.relationship}",
                ip_address=_client_ip(request),
                user_agent=_user_agent(request),
                created_at=datetime.now(UTC),
            )
        )
    await db.flush()
    return BucketIntakeLinkResult(
        bucket_id=bucket.id,
        intake_id=intake.id,
        relationship=payload.relationship,
        linked_file_ids=selected_file_ids,
        audit_ids=audit_ids,
        audit_action="unified_bucket_intake_linked",
        bucket_context=ctx,
    )
