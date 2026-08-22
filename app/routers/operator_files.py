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
from sqlalchemy import false as sql_false
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.dealer_os.models import DealerAuditLog, DealerBusiness, DealerRepLead
from app.deps import CurrentUser
from app.enums import LoanPurpose, LoanStage, LoanType, PropertyType, Role
from app.models.activity import Activity
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
from app.models.operator_file import BucketIntakeLink, BucketIntakeLinkFile
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.schemas.operator_file import (
    BucketIntakeLinkOption,
    BucketIntakeLinkOptions,
    BucketIntakeLinkRead,
    BucketIntakeLinkRequest,
    BucketIntakeLinkResult,
    BucketIntakeLinkUpdate,
    IntakePromotionRequest,
    IntakePromotionResult,
    UnifiedActionDefinition,
    UnifiedActivity,
    UnifiedAuditItem,
    UnifiedDocumentPack,
    UnifiedDocumentProgress,
    UnifiedDocumentRequirement,
    UnifiedFileDetail,
    UnifiedFilePage,
    UnifiedFileRow,
    UnifiedGate,
    UnifiedParticipant,
    UnifiedProfile,
    UnifiedRollup,
    UnifiedSource,
    UnifiedStage,
)
from app.scoping import regional_manager_broker_ids_subquery, scope_client_query, scope_loan_query
from app.services.activity_log import log_activity
from app.services.operator_file_links import (
    active_links_for_sources,
    queue_link_change_review,
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

DOCUMENT_PACKS: dict[str, tuple[list[str], list[str]]] = {
    "real_estate": (
        [
            "Government ID",
            "Entity documents",
            "Purchase contract",
            "Property insurance",
            "Bank statements",
        ],
        ["Credit authorization", "Borrower certification"],
    ),
    "main_street": (
        [
            "Government ID",
            "Business formation",
            "Bank statements",
            "Profit and loss",
            "Business tax returns",
            "Debt schedule",
        ],
        ["Credit authorization", "Business application", "Owner certification"],
    ),
    "dealer": (
        [
            "Government ID",
            "Dealer license",
            "Bank statements",
            "Inventory report",
            "Business tax returns",
            "Debt schedule",
        ],
        ["Credit authorization", "Dealer application", "Owner certification"],
    ),
    "mca": (
        [
            "Government ID",
            "MCA statements",
            "Bank statements",
            "Merchant processing",
            "Payoff letters",
            "Business tax returns",
        ],
        ["Credit authorization", "Debt payoff authorization", "Owner certification"],
    ),
}

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
    return _dedupe(
        [
            _titleize(str(loan.type) if loan.type else None),
            _titleize(str(loan.purpose) if loan.purpose else None),
            _titleize(loan.funding_file_kind),
        ]
    )


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
        (
            await db.execute(
                _with_bucket_relationships(
                    select(Bucket).where(Bucket.id.in_(bucket_ids), Bucket.archived_at.is_(None))
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.id: row for row in rows}


async def _intake_by_bucket_map(
    db: AsyncSession, bucket_ids: set[UUID]
) -> dict[UUID, PublicUnderwritingIntake]:
    if not bucket_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(PublicUnderwritingIntake).where(
                    PublicUnderwritingIntake.bucket_id.in_(bucket_ids)
                )
            )
        )
        .scalars()
        .all()
    )
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
                amount=_money(
                    getattr(loan, "amount", None) or deal.target_price or deal.list_price
                ),
                amount_label=_money_label(
                    getattr(loan, "amount", None) or deal.target_price or deal.list_price
                ),
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
        .where(Loan.source_deal_id.is_(None), Loan.source_intake_id.is_(None))
        .order_by(Loan.updated_at.desc())
    )
    stmt = scope_loan_query(user, stmt)
    pairs = list((await db.execute(stmt)).all())
    rows: list[UnifiedFileRow] = []
    for loan, client in pairs:
        vertical = _loan_vertical(loan)
        funding = _funding_stage(str(loan.stage))
        normalized = funding.label if funding else "Prequalified"
        health, health_tone = _health(
            normalized, str(loan.deal_health) if loan.deal_health else None
        )
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
        return stmt.join(
            Client, Client.id == PublicUnderwritingIntake.client_id, isouter=True
        ).where(Client.broker_id.in_(regional_manager_broker_ids_subquery(user)))
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
    pairs = list((await db.execute(stmt)).all())
    loan_ids = {
        intake.promoted_loan_id
        for intake, _bucket, _client, _partner in pairs
        if intake.promoted_loan_id
    }
    loan_stmt = select(Loan).where(Loan.id.in_(loan_ids)) if loan_ids else None
    visible_loans = (
        list((await db.execute(scope_loan_query(user, loan_stmt))).scalars().all())
        if loan_stmt is not None
        else []
    )
    loan_map = {loan.id: loan for loan in visible_loans}
    rows: list[UnifiedFileRow] = []
    for intake, bucket, client, partner in pairs:
        vertical = _variant_vertical(intake.variant)
        origin = "dealer" if intake.broker_id else "ai_intake"
        working_key = INTAKE_STATUS_TO_WORKING.get(intake.status, "applicant_intake")
        working = _working_stage(vertical, working_key)
        loan = loan_map.get(intake.promoted_loan_id)
        funding = _funding_stage(str(loan.stage)) if loan else None
        normalized = funding.label if funding else working.label
        health, health_tone = _health(normalized, intake.outcome_status)
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
                loan_id=loan.id if loan else None,
                vertical=vertical,  # type: ignore[arg-type]
                vertical_label=VERTICAL_LABELS[vertical],
                origin=origin,  # type: ignore[arg-type]
                origin_label=ORIGIN_LABELS[origin],
                source_label=partner.name if partner else "Public AI intake",
                amount=_money(intake.requested_loan_amount),
                amount_label=_money_label(intake.requested_loan_amount),
                working_stage=working,
                funding_stage=funding,
                normalized_stage=normalized,
                stage_tone=_stage_tone(normalized, health),
                health=health,
                health_tone=health_tone,  # type: ignore[arg-type]
                document_progress=_document_progress(bucket),
                program_tags=_dedupe(
                    [
                        *_program_tags_for_intake(intake),
                        *(_program_tags_for_loan(loan) if loan else []),
                    ]
                ),
                owner_name=partner.name if partner else None,
                dealer_name=partner.name if intake.broker_id and partner else None,
                linked_bucket_ids=[bucket.id],
                linked_intake_ids=[intake.id],
                promoted_loan_id=intake.promoted_loan_id,
                updated_at=max(intake.updated_at, loan.updated_at) if loan else intake.updated_at,
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
        working = _working_stage(
            vertical, "applicant_intake" if vertical != "real_estate" else "lead"
        )
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
                linked_bucket_ids=[bucket.id],
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
        working_key = REP_STATUS_TO_WORKING.get(
            rep.status if rep else dealer.status, "applicant_intake"
        )
        working = _working_stage(vertical, working_key)
        normalized = working.label
        health, health_tone = _health(normalized, rep.status if rep else dealer.status)
        linked_bucket_ids = [bucket.id] if bucket else []
        linked_intake_ids = [dealer.handoff_intake_id] if dealer.handoff_intake_id else []
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


def _merge_explicit_lineage(root: UnifiedFileRow, linked: UnifiedFileRow) -> None:
    """Fold an explicitly linked physical source into its logical root."""

    if linked.loan_id and not root.loan_id:
        root.loan_id = linked.loan_id
    if linked.funding_stage:
        root.funding_stage = linked.funding_stage
        root.normalized_stage = linked.funding_stage.label
        root.stage_tone = linked.stage_tone
    if linked.client_id and not root.client_id:
        root.client_id = linked.client_id
    if linked.client_name and not root.client_name:
        root.client_name = linked.client_name
    if linked.amount is not None and root.amount is None:
        root.amount = linked.amount
        root.amount_label = linked.amount_label
    root.program_tags = _dedupe([*root.program_tags, *linked.program_tags])
    root.linked_bucket_ids = list(
        dict.fromkeys([*root.linked_bucket_ids, *linked.linked_bucket_ids])
    )
    root.linked_intake_ids = list(
        dict.fromkeys([*root.linked_intake_ids, *linked.linked_intake_ids])
    )
    linked_total = linked.document_progress.docs_total + linked.document_progress.signatures_total
    root_total = root.document_progress.docs_total + root.document_progress.signatures_total
    if linked_total > root_total:
        root.document_progress = linked.document_progress
    root.updated_at = max(root.updated_at, linked.updated_at)


def _collapse_logical_rows(rows: list[UnifiedFileRow]) -> list[UnifiedFileRow]:
    """Collapse only durable lineage; names and email addresses are ignored."""

    intake_roots = {row.source_id: row for row in rows if row.source_kind == "intake"}
    suppressed: set[str] = set()
    for dealer in [row for row in rows if row.source_kind == "dealer"]:
        if dealer.intake_id and dealer.intake_id in intake_roots:
            intake = intake_roots[dealer.intake_id]
            _merge_explicit_lineage(dealer, intake)
            suppressed.add(intake.id)
    return [row for row in rows if row.id not in suppressed]


async def _decorate_durable_links(rows: list[UnifiedFileRow], user: User, db: AsyncSession) -> None:
    visible_bucket_ids = {bucket_id for row in rows for bucket_id in row.linked_bucket_ids}
    visible_intake_ids = {intake_id for row in rows for intake_id in row.linked_intake_ids}
    links = await active_links_for_sources(
        db, bucket_ids=visible_bucket_ids, intake_ids=visible_intake_ids
    )
    for link in links:
        # Supporting links never widen a scoped user's book. Both endpoints
        # must already be represented by a visible source before IDs are added.
        if user.role not in INTERNAL_ROLES and not (
            link.bucket_id in visible_bucket_ids and link.intake_id in visible_intake_ids
        ):
            continue
        for row in rows:
            if link.bucket_id in row.linked_bucket_ids or link.intake_id in row.linked_intake_ids:
                if link.bucket_id not in row.linked_bucket_ids:
                    row.linked_bucket_ids.append(link.bucket_id)
                if link.intake_id not in row.linked_intake_ids:
                    row.linked_intake_ids.append(link.intake_id)


async def _all_rows(user: User, db: AsyncSession) -> list[UnifiedFileRow]:
    rows: list[UnifiedFileRow] = []
    rows.extend(await _deal_rows(user, db))
    rows.extend(await _loan_rows(user, db))
    rows.extend(await _intake_rows(user, db))
    rows.extend(await _bucket_rows(user, db))
    rows.extend(await _dealer_rows(user, db))
    await _decorate_durable_links(rows, user, db)
    rows = _collapse_logical_rows(rows)
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
            if needle
            in " ".join(
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
        promoted=len(
            [
                row
                for row in rows
                if row.funding_stage is not None or row.promoted_loan_id is not None
            ]
        ),
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


def _row_matches_source(row: UnifiedFileRow, source_kind: str, source_id: UUID) -> bool:
    source_ids = {
        "deal": row.deal_id,
        "loan": row.loan_id,
        "intake": row.intake_id,
        "bucket": row.bucket_id,
        "dealer": row.dealer_id,
    }
    return (row.source_kind == source_kind and row.source_id == source_id) or source_ids.get(
        source_kind
    ) == source_id


def _ladder_for(row: UnifiedFileRow) -> list[UnifiedStage]:
    working = REAL_ESTATE_LADDER if row.vertical == "real_estate" else APPLICATION_LADDER
    return [
        *[_stage(working, key, "working") for key, _label in working],
        *[_stage(FUNDING_LADDER, key, "funding") for key, _label in FUNDING_LADDER],
    ]


def _blockers_for(row: UnifiedFileRow) -> list[str]:
    progress = row.document_progress
    blockers: list[str] = []
    missing_docs = max(0, progress.docs_total - progress.docs_uploaded)
    missing_signatures = max(0, progress.signatures_total - progress.signatures_uploaded)
    if missing_docs:
        blockers.append(f"{missing_docs} required document(s) outstanding")
    if missing_signatures:
        blockers.append(f"{missing_signatures} required signature(s) outstanding")
    if row.health_tone == "bad":
        blockers.append("File has an unresolved attention flag")
    return blockers


def _gate_for(row: UnifiedFileRow, blockers: list[str]) -> UnifiedGate:
    passed = row.funding_stage is not None
    ready = passed or (
        not blockers
        and row.working_stage is not None
        and row.working_stage.index == row.working_stage.total
    )
    state = "passed" if passed else "ready" if ready else "locked"
    return UnifiedGate(
        key="funding_handoff",
        label="Ready for lending",
        state=state,
        ready=ready,
        blockers=[] if passed else blockers,
    )


def _requirement_status(name: str, bucket: Bucket | None) -> str:
    if bucket is None:
        return "missing"
    match = next(
        (doc for doc in bucket.requested_documents if doc.name.casefold() == name.casefold()),
        None,
    )
    if match is None:
        return "missing"
    if match.status == "uploaded":
        return "complete"
    return "requested"


def _document_pack_for(row: UnifiedFileRow, bucket: Bucket | None) -> UnifiedDocumentPack:
    documents, signatures = DOCUMENT_PACKS[row.vertical]
    return UnifiedDocumentPack(
        vertical=row.vertical,
        documents=[
            UnifiedDocumentRequirement(
                key=name.lower().replace(" ", "_"),
                label=name,
                status=_requirement_status(name, bucket),  # type: ignore[arg-type]
            )
            for name in documents
        ],
        signatures=[
            UnifiedDocumentRequirement(
                key=name.lower().replace(" ", "_"),
                label=name,
                kind="signature",
                status=_requirement_status(name, bucket),  # type: ignore[arg-type]
            )
            for name in signatures
        ],
    )


def _linked_sources(row: UnifiedFileRow) -> list[UnifiedSource]:
    sources: list[UnifiedSource] = []
    values = [
        ("deal", row.deal_id, "Realtor deal", "/deals/{}"),
        ("loan", row.loan_id, "Funding file", "/loans/{}"),
        ("intake", row.intake_id, "AI intake", "/admin/ai-underwriter-leads?lead={}"),
        ("bucket", row.bucket_id, "Primary document room", "/admin/buckets?bucket={}"),
        ("dealer", row.dealer_id, "Rep / dealer file", None),
    ]
    for kind, source_id, label, route in values:
        if source_id is None:
            continue
        sources.append(
            UnifiedSource(
                kind=kind,  # type: ignore[arg-type]
                id=source_id,
                ref=_source_ref(f"QC-{kind[0].upper()}", source_id),
                label=label,
                relationship="canonical" if kind == row.source_kind else "lineage",
                route=route.format(source_id) if route else None,
            )
        )
    for bucket_id in row.linked_bucket_ids:
        if bucket_id != row.bucket_id:
            sources.append(
                UnifiedSource(
                    kind="bucket",
                    id=bucket_id,
                    ref=_source_ref("QC-B", bucket_id),
                    label="Linked document room",
                    relationship="supporting",
                    route=f"/admin/buckets?bucket={bucket_id}",
                )
            )
    return sources


def _actions_for(row: UnifiedFileRow, user: User) -> list[UnifiedActionDefinition]:
    actions: list[UnifiedActionDefinition] = []
    if row.deal_id and row.client_id and not row.loan_id and user.role in OPERATOR_ROLES:
        actions.append(
            UnifiedActionDefinition(
                key="promote_deal",
                label="Send to funding",
                method="POST",
                path=f"/clients/{row.client_id}/deals/{row.deal_id}/mark-ready-for-lending",
                effects=[
                    "Creates the canonical funding file",
                    "Preserves the realtor deal lineage",
                ],
                reversible=False,
                confirmation_label="Send to funding",
            )
        )
    if row.intake_id and not row.loan_id and user.role in INTERNAL_ROLES:
        actions.append(
            UnifiedActionDefinition(
                key="promote_intake",
                label="Create funding file",
                method="POST",
                path=f"/operator-files/intakes/{row.intake_id}/promote",
                effects=[
                    "Creates the canonical funding file",
                    "Preserves the AI intake and evidence lineage",
                ],
                reversible=False,
                confirmation_label="Create funding file",
            )
        )
    if row.loan_id and user.role in OPERATOR_ROLES:
        actions.append(
            UnifiedActionDefinition(
                key="change_funding_stage",
                label="Update stage",
                method="POST",
                path=f"/loans/{row.loan_id}/stage",
                effects=["Updates the shared funding ladder", "Writes a funding activity entry"],
                reversible=True,
                confirmation_label="Update stage",
            )
        )
    if (row.bucket_id or row.intake_id) and user.role in INTERNAL_ROLES:
        actions.append(
            UnifiedActionDefinition(
                key="manage_bucket_intake_link",
                label="Manage linked evidence",
                method="POST",
                path="/operator-files/bucket-intake-links",
                effects=[
                    "Changes which selected files Elara may review",
                    "Queues a fresh intake review",
                ],
                reversible=True,
                confirmation_label="Confirm link",
            )
        )
    return actions


async def _activities_for(row: UnifiedFileRow, db: AsyncSession) -> list[UnifiedActivity]:
    activities: list[UnifiedActivity] = []
    predicates = []
    if row.loan_id:
        predicates.append(Activity.loan_id == row.loan_id)
    if row.client_id:
        predicates.append(Activity.client_id == row.client_id)
    if predicates:
        loan_rows = list(
            (
                await db.execute(
                    select(Activity)
                    .where(or_(*predicates))
                    .order_by(Activity.occurred_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        actor_ids = {item.actor_id for item in loan_rows if item.actor_id}
        actors = {
            actor.id: actor
            for actor in (
                (await db.execute(select(User).where(User.id.in_(actor_ids)))).scalars().all()
                if actor_ids
                else []
            )
        }
        for item in loan_rows:
            actor = actors.get(item.actor_id)
            activities.append(
                UnifiedActivity(
                    id=item.id,
                    source="funding" if item.loan_id else "client",
                    action=item.kind,
                    actor_name=actor.name if actor else item.actor_label,
                    actor_role=item.actor_label,
                    detail=item.summary,
                    metadata=item.payload or {},
                    created_at=item.occurred_at,
                )
            )
    if row.linked_bucket_ids:
        bucket_logs = list(
            (
                await db.execute(
                    select(BucketActivityLog)
                    .where(BucketActivityLog.bucket_id.in_(row.linked_bucket_ids))
                    .order_by(BucketActivityLog.created_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        activities.extend(
            UnifiedActivity(
                id=log.id,
                source="intake" if row.intake_id else "bucket",
                action=log.action,
                actor_name=log.actor_name,
                actor_role=log.actor_role,
                detail=log.detail,
                metadata={"target_type": log.target_type, "target_id": log.target_id},
                created_at=log.created_at,
            )
            for log in bucket_logs
        )
    if row.dealer_id:
        dealer_logs = list(
            (
                await db.execute(
                    select(DealerAuditLog)
                    .where(DealerAuditLog.dealer_id == row.dealer_id)
                    .order_by(DealerAuditLog.created_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        activities.extend(
            UnifiedActivity(
                id=log.id,
                source="dealer",
                action=log.action,
                actor_name=log.actor_name,
                detail=log.entity_kind,
                metadata={"before": log.before, "after": log.after},
                created_at=log.created_at,
            )
            for log in dealer_logs
        )
    activities.sort(key=lambda item: item.created_at, reverse=True)
    return activities[:150]


@router.get("/{source_kind}/{source_id}", response_model=UnifiedFileDetail)
async def get_operator_file(
    source_kind: str,
    source_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UnifiedFileDetail:
    rows = await _all_rows(user, db)
    row = next((item for item in rows if _row_matches_source(item, source_kind, source_id)), None)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unified file not found")
    bucket = (
        (
            await db.execute(
                _with_bucket_relationships(select(Bucket).where(Bucket.id == row.bucket_id))
            )
        ).scalar_one_or_none()
        if row.bucket_id
        else None
    )
    intake = await db.get(PublicUnderwritingIntake, row.intake_id) if row.intake_id else None
    client = await db.get(Client, row.client_id) if row.client_id else None
    activities = await _activities_for(row, db)
    audit = [
        UnifiedAuditItem(
            id=item.id,
            action=item.action,
            actor_name=item.actor_name,
            actor_role=item.actor_role,
            detail=item.detail,
            created_at=item.created_at,
        )
        for item in activities[:50]
    ]
    participants = []
    if row.principal:
        participants.append(
            UnifiedParticipant(
                name=row.principal,
                role="Applicant",
                email=intake.email if intake else client.email if client else None,
                phone=row.phone,
            )
        )
    if row.owner_name:
        participants.append(UnifiedParticipant(name=row.owner_name, role="Owner / rep"))
    blockers = _blockers_for(row)
    return UnifiedFileDetail(
        file=row,
        audit=audit,
        ladder=_ladder_for(row),
        gate=_gate_for(row, blockers),
        blockers=blockers,
        document_pack=_document_pack_for(row, bucket),
        linked_sources=_linked_sources(row),
        participants=participants,
        profile=UnifiedProfile(
            shape="person_and_business" if row.business_name else "person",
            person={
                "name": row.principal,
                "email": intake.email if intake else client.email if client else None,
                "phone": row.phone,
                "credit_score": intake.estimated_credit_score
                if intake
                else client.fico
                if client
                else None,
            },
            business={
                "name": row.business_name,
                "requested_amount": row.amount,
                "purpose": row.subtitle,
                "vertical": row.vertical_label,
            }
            if row.business_name
            else {},
        ),
        activities=activities,
        actions=_actions_for(row, user),
    )


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
    bucket, intake = await _load_link_sources(payload.bucket_id, payload.intake_id, user, db)
    selected_file_ids = _validated_file_ids(bucket, payload.file_ids)
    link = (
        await db.execute(
            select(BucketIntakeLink)
            .where(
                BucketIntakeLink.bucket_id == bucket.id,
                BucketIntakeLink.intake_id == intake.id,
            )
            .options(selectinload(BucketIntakeLink.files))
        )
    ).scalar_one_or_none()
    if link is None:
        link = BucketIntakeLink(
            bucket_id=bucket.id,
            intake_id=intake.id,
            linked_by_user_id=user.id,
            files=[],
        )
        db.add(link)
        await db.flush()
    link.relationship = payload.relationship
    link.note = payload.note
    link.updated_by_user_id = user.id
    link.unlinked_at = None
    link.unlinked_by_user_id = None
    _reconcile_link_files(link, selected_file_ids, user.id)
    review = await queue_link_change_review(db, intake=intake, requested_by_user_id=user.id)
    audit_ids = _write_link_audits(
        db,
        bucket=bucket,
        intake=intake,
        user=user,
        request=request,
        action="bucket_intake_linked",
        detail=payload.note
        or f"Linked {len(selected_file_ids)} selected file(s) as {payload.relationship}",
    )
    await db.flush()
    return _link_result(link, audit_ids, review.id, "bucket_intake_linked")


@router.get("/bucket-intake-links", response_model=list[BucketIntakeLinkRead])
async def list_bucket_intake_links(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    bucket_id: UUID | None = Query(None),
    intake_id: UUID | None = Query(None),
    include_unlinked: bool = Query(False),
) -> list[BucketIntakeLinkRead]:
    _require_internal(user)
    stmt = select(BucketIntakeLink).options(selectinload(BucketIntakeLink.files))
    if bucket_id:
        stmt = stmt.where(BucketIntakeLink.bucket_id == bucket_id)
    if intake_id:
        stmt = stmt.where(BucketIntakeLink.intake_id == intake_id)
    if not include_unlinked:
        stmt = stmt.where(BucketIntakeLink.unlinked_at.is_(None))
    links = list(
        (await db.execute(stmt.order_by(BucketIntakeLink.updated_at.desc()))).scalars().all()
    )
    return [
        BucketIntakeLinkRead(
            link_id=link.id,
            bucket_id=link.bucket_id,
            intake_id=link.intake_id,
            relationship=link.relationship,  # type: ignore[arg-type]
            linked_file_ids=_selected_file_ids(link),
            note=link.note,
            status="unlinked" if link.unlinked_at else "active",
            created_at=link.created_at,
            updated_at=link.updated_at,
        )
        for link in links
    ]


@router.patch("/bucket-intake-links/{link_id}", response_model=BucketIntakeLinkResult)
async def update_bucket_intake_link(
    link_id: UUID,
    payload: BucketIntakeLinkUpdate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketIntakeLinkResult:
    _require_internal(user)
    link = await _load_link(link_id, db)
    bucket, intake = await _load_link_sources(link.bucket_id, link.intake_id, user, db)
    if payload.relationship is not None:
        link.relationship = payload.relationship
    if payload.note is not None:
        link.note = payload.note
    if payload.file_ids is not None:
        _reconcile_link_files(link, _validated_file_ids(bucket, payload.file_ids), user.id)
    link.updated_by_user_id = user.id
    link.unlinked_at = None
    link.unlinked_by_user_id = None
    review = await queue_link_change_review(db, intake=intake, requested_by_user_id=user.id)
    selected = _selected_file_ids(link)
    audit_ids = _write_link_audits(
        db,
        bucket=bucket,
        intake=intake,
        user=user,
        request=request,
        action="bucket_intake_link_updated",
        detail=payload.note or f"Updated relationship with {len(selected)} selected file(s)",
    )
    await db.flush()
    return _link_result(link, audit_ids, review.id, "bucket_intake_link_updated")


@router.delete("/bucket-intake-links/{link_id}", response_model=BucketIntakeLinkResult)
async def unlink_bucket_from_intake(
    link_id: UUID,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketIntakeLinkResult:
    _require_internal(user)
    link = await _load_link(link_id, db)
    bucket, intake = await _load_link_sources(link.bucket_id, link.intake_id, user, db)
    now = datetime.now(UTC)
    link.unlinked_at = now
    link.unlinked_by_user_id = user.id
    link.updated_by_user_id = user.id
    for file_ref in link.files:
        if file_ref.removed_at is None:
            file_ref.removed_at = now
            file_ref.removed_by_user_id = user.id
    review = await queue_link_change_review(db, intake=intake, requested_by_user_id=user.id)
    audit_ids = _write_link_audits(
        db,
        bucket=bucket,
        intake=intake,
        user=user,
        request=request,
        action="bucket_intake_unlinked",
        detail="Removed linked evidence access; source files were not deleted",
    )
    await db.flush()
    return _link_result(
        link, audit_ids, review.id, "bucket_intake_unlinked", link_status="unlinked"
    )


@router.get("/link-options", response_model=BucketIntakeLinkOptions)
async def bucket_intake_link_options(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketIntakeLinkOptions:
    _require_internal(user)
    buckets = list(
        (
            await db.execute(
                _with_bucket_relationships(
                    select(Bucket)
                    .where(Bucket.archived_at.is_(None))
                    .order_by(Bucket.updated_at.desc())
                )
            )
        )
        .scalars()
        .all()
    )
    intakes = list(
        (
            await db.execute(
                select(PublicUnderwritingIntake).order_by(
                    PublicUnderwritingIntake.updated_at.desc()
                )
            )
        )
        .scalars()
        .all()
    )
    active_pairs = {
        (link.bucket_id, link.intake_id)
        for link in (
            await db.execute(select(BucketIntakeLink).where(BucketIntakeLink.unlinked_at.is_(None)))
        )
        .scalars()
        .all()
    }
    linked_bucket_ids = {bucket_id for bucket_id, _intake_id in active_pairs}
    linked_intake_ids = {intake_id for _bucket_id, intake_id in active_pairs}
    return BucketIntakeLinkOptions(
        buckets=[
            BucketIntakeLinkOption(
                id=bucket.id,
                label=bucket.name,
                subtitle=bucket.client_name or bucket.purpose,
                file_count=len([file for file in bucket.files if file.deleted_at is None]),
                linked=bucket.id in linked_bucket_ids,
            )
            for bucket in buckets
        ],
        intakes=[
            BucketIntakeLinkOption(
                id=intake.id,
                label=intake.business_name or intake.full_name,
                subtitle=f"{_titleize(_variant_vertical(intake.variant))} · {intake.email}",
                linked=intake.id in linked_intake_ids,
            )
            for intake in intakes
        ],
    )


@router.post("/intakes/{intake_id}/promote", response_model=IntakePromotionResult)
async def promote_intake_to_funding(
    intake_id: UUID,
    payload: IntakePromotionRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> IntakePromotionResult:
    _require_internal(user)
    intake = await db.get(PublicUnderwritingIntake, intake_id)
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI intake not found")
    if intake.promoted_loan_id:
        existing = await db.get(Loan, intake.promoted_loan_id)
        if existing:
            existing_audit = await log_activity(
                db,
                loan_id=existing.id,
                actor_id=user.id,
                actor_label=_role_value(user),
                kind="intake.promotion_reopened",
                summary="Opened the existing funding file from its AI intake",
                mark_dirty=False,
            )
            return IntakePromotionResult(
                intake_id=intake.id,
                loan_id=existing.id,
                client_id=existing.client_id,
                created=False,
                audit_id=existing_audit.id,
            )

    client = await db.get(Client, intake.client_id) if intake.client_id else None
    if client is None:
        client = Client(
            name=intake.full_name,
            email=intake.email,
            phone=intake.phone,
            referral_source=intake.referral_source,
            source_channel="ai_intake",
            client_experience_mode="guided",
            client_experience_mode_reason="ai_intake_promoted",
            client_experience_mode_locked_by="firm",
        )
        db.add(client)
        await db.flush()
        intake.client_id = client.id

    state = intake.intake_state or {}
    vertical = _variant_vertical(intake.variant)
    address = str(
        state.get("property_address")
        or state.get("business_address")
        or client.address
        or "Address pending"
    )
    funding_kind = (
        payload.funding_file_kind
        or {
            "real_estate": "dscr_purchase",
            "main_street": "business",
            "dealer": "dealer",
            "mca": "mca_refinance",
        }[vertical]
    )
    loan = Loan(
        id=uuid4(),
        deal_id=f"I-{str(intake.id)[:8].upper()}",
        client_id=client.id,
        address=address,
        property_type=PropertyType.COMMERCIAL,
        type=LoanType.DSCR if vertical == "real_estate" else LoanType.BRIDGE,
        purpose=LoanPurpose.PURCHASE if vertical == "real_estate" else LoanPurpose.CASH_OUT_REFI,
        stage=LoanStage.PREQUALIFIED,
        amount=float(intake.requested_loan_amount or 0),
        source_intake_id=intake.id,
        funding_file_kind=funding_kind,
        entity_name=intake.business_name,
        baseline_profile_snapshot={
            "source": "public_underwriting_intake",
            "intake_id": str(intake.id),
            "variant": intake.variant,
            "intake_state": state,
            "result_snapshot": intake.result_snapshot,
        },
        handoff_summary=payload.notes
        or f"Promoted from AI intake for {intake.business_name or intake.full_name}.",
        source_attribution="website",
        assigned_owner_id=user.id,
    )
    db.add(loan)
    await db.flush()
    intake.promoted_loan_id = loan.id
    audit = await log_activity(
        db,
        loan_id=loan.id,
        actor_id=user.id,
        actor_label=_role_value(user),
        kind="intake.promoted_to_funding",
        summary=f"AI intake promoted to funding file {loan.deal_id}",
        payload={"intake_id": str(intake.id), "vertical": vertical},
    )
    _write_link_audits(
        db,
        bucket=await db.get(Bucket, intake.bucket_id),
        intake=intake,
        user=user,
        request=request,
        action="intake_promoted_to_funding",
        detail=f"Created funding file {loan.deal_id}",
    )
    await db.flush()
    return IntakePromotionResult(
        intake_id=intake.id,
        loan_id=loan.id,
        client_id=client.id,
        created=True,
        audit_id=audit.id,
    )


def _require_internal(user: User) -> None:
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator role required")


async def _load_link_sources(
    bucket_id: UUID,
    intake_id: UUID,
    user: User,
    db: AsyncSession,
) -> tuple[Bucket, PublicUnderwritingIntake]:
    _require_internal(user)
    bucket = (
        await db.execute(
            _with_bucket_relationships(
                select(Bucket).where(Bucket.id == bucket_id, Bucket.archived_at.is_(None))
            )
        )
    ).scalar_one_or_none()
    if bucket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bucket not found")
    intake = await db.get(PublicUnderwritingIntake, intake_id)
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI intake not found")
    return bucket, intake


async def _load_link(link_id: UUID, db: AsyncSession) -> BucketIntakeLink:
    link = (
        await db.execute(
            select(BucketIntakeLink)
            .where(BucketIntakeLink.id == link_id)
            .options(selectinload(BucketIntakeLink.files))
        )
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bucket/intake link not found")
    return link


def _validated_file_ids(bucket: Bucket, file_ids: list[UUID]) -> list[UUID]:
    active_file_ids = {
        file.id for file in bucket.files if file.deleted_at is None and file.status == "uploaded"
    }
    requested = list(dict.fromkeys(file_ids))
    invalid = [file_id for file_id in requested if file_id not in active_file_ids]
    if invalid:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{len(invalid)} selected file(s) are not active files in this bucket",
        )
    return requested


def _reconcile_link_files(
    link: BucketIntakeLink, selected_file_ids: list[UUID], actor_id: UUID
) -> None:
    now = datetime.now(UTC)
    selected = set(selected_file_ids)
    existing = {file_ref.bucket_file_id: file_ref for file_ref in link.files}
    for file_id in selected:
        file_ref = existing.get(file_id)
        if file_ref is None:
            link.files.append(
                BucketIntakeLinkFile(
                    bucket_file_id=file_id,
                    selected_by_user_id=actor_id,
                )
            )
        else:
            file_ref.removed_at = None
            file_ref.removed_by_user_id = None
            file_ref.selected_by_user_id = actor_id
    for file_id, file_ref in existing.items():
        if file_id not in selected and file_ref.removed_at is None:
            file_ref.removed_at = now
            file_ref.removed_by_user_id = actor_id


def _selected_file_ids(link: BucketIntakeLink) -> list[UUID]:
    return [file_ref.bucket_file_id for file_ref in link.files if file_ref.removed_at is None]


def _write_link_audits(
    db: AsyncSession,
    *,
    bucket: Bucket | None,
    intake: PublicUnderwritingIntake,
    user: User,
    request: Request,
    action: str,
    detail: str,
) -> list[UUID]:
    bucket_ids = list(
        dict.fromkeys(
            [value for value in [bucket.id if bucket else None, intake.bucket_id] if value]
        )
    )
    audit_ids: list[UUID] = []
    for bucket_id in bucket_ids:
        audit_id = uuid4()
        audit_ids.append(audit_id)
        db.add(
            BucketActivityLog(
                id=audit_id,
                bucket_id=bucket_id,
                actor_user_id=user.id,
                actor_name=user.name,
                actor_email=user.email,
                actor_role=_role_value(user),
                action=action,
                target_type="public_underwriting_intake",
                target_id=str(intake.id),
                detail=detail,
                ip_address=_client_ip(request),
                user_agent=_user_agent(request),
                created_at=datetime.now(UTC),
            )
        )
    return audit_ids


def _link_result(
    link: BucketIntakeLink,
    audit_ids: list[UUID],
    review_id: UUID,
    action: str,
    *,
    link_status: str = "active",
) -> BucketIntakeLinkResult:
    return BucketIntakeLinkResult(
        link_id=link.id,
        bucket_id=link.bucket_id,
        intake_id=link.intake_id,
        relationship=link.relationship,  # type: ignore[arg-type]
        linked_file_ids=_selected_file_ids(link),
        audit_ids=audit_ids,
        audit_action=action,
        review_id=review_id,
        status=link_status,  # type: ignore[arg-type]
    )
