"""Dealer OS API — everything under /api/v1/dealer-os/* (isolation contract).

Stream 1 surface: team console CRUD + the per-dealer Targets & Settings
endpoints (AI propose / admin override, override-always-wins). Engines,
ledger, plan, forecast, messaging land in Streams 2-5 on this same router.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.db import get_db
from app.deps import CurrentUser
from app.models.user import User
from app.services import clerk as clerk_service
from app.enums import Role

# READ-ONLY reuse: bucket models are queried/appended, never altered; the
# analysis-version constant keeps cache lookups aligned with the bucket AI.
from app.models.bucket import Bucket, BucketFile, BucketFileAnalysis
from app.services.bucket_ai import CURRENT_FILE_ANALYSIS_VERSION

from .deps import load_dealer, require_team, require_team_or_dealer, resolve_dealer_scope
from .models import (
    DealerAccount,
    DealerAddback,
    DealerAlert,
    DealerAuditLog,
    DealerBusiness,
    DealerCashEvent,
    DealerCategoryRule,
    DealerCreditProfile,
    DealerDocRequest,
    DealerDocument,
    DealerFinancialPeriod,
    DealerMessage,
    DealerMetricLineage,
    DealerMetricSnapshot,
    DealerMetricTarget,
    DealerPlanAction,
    DealerSession,
    DealerSourceConnection,
    DealerTaxFiling,
)
from .schemas import (
    AccountPatch,
    AccountRead,
    AddbackPatch,
    AuditRead,
    BucketSearchItem,
    EventFeedsRead,
    LineageEdgeRead,
    LineageRead,
    RuleCreate,
    RuleCreateResult,
    RuleRead,
    AIInsightsAccept,
    AIInsightsRead,
    BucketFileItem,
    CreditRead,
    CreditUpsert,
    DealerInvite,
    DealerInviteResult,
    TaxFilingUpsert,
    TaxYearRead,
    AddbackRead,
    AlertRead,
    CashEventPatch,
    CashEventRead,
    CashImport,
    CashImportResult,
    DealerCreate,
    DealerListItem,
    DealerRead,
    DealerUpdate,
    DocRequestCreate,
    DocRequestPatch,
    DocRequestRead,
    DocumentCoverageRead,
    DocumentRead,
    DocumentReject,
    ForecastRead,
    HandoffRead,
    ProgressRead,
    GlobalAlertRead,
    HealthRead,
    LenderPackageRead,
    MessageCreate,
    MessageRead,
    PathsRead,
    PeriodRead,
    PeriodUpsert,
    PlanActionCreate,
    PlanActionRead,
    PlanActionUpdate,
    IrregularEventRead,
    RecurringGroupRead,
    RecurringRead,
    SessionCreate,
    SessionRead,
    SnapshotRead,
    TargetOverride,
    TargetRead,
)
from .services import analyst, archive, buckets_link, handoff as handoff_service, recurrence, report_pdf, rollups, storage
from .services.audit import log_action
from .services.progress import compute_progress
from .services.engines import recompute_snapshot
from .services.extract import _persist_plan, apply_extraction, extract_document
from .services.forecast import compute_forecast
from .services.normalize import (
    classify_with_rules,
    flags_for,
    load_active_rules,
    period_of,
    rebuild_periods,
)
from .services.paths import compute_ladder, compute_paths
from .services.targets import propose_targets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dealer-os", tags=["dealer-os"])


@router.get("/dealers", response_model=list[DealerListItem])
async def list_dealers(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[DealerListItem]:
    # Team sees the whole book; a DEALER login sees only businesses linked to it
    # (dealer_user_id) — this is what powers the self-serve "My business" view.
    require_team_or_dealer(user)
    stmt = select(DealerBusiness).order_by(DealerBusiness.created_at.desc())
    if user.role == Role.DEALER:
        stmt = stmt.where(DealerBusiness.dealer_user_id == user.id)
    dealers = (await db.execute(stmt)).scalars().all()
    if not dealers:
        return []
    ids = [d.id for d in dealers]
    # latest snapshot per dealer + open alert counts, batched
    snaps = (
        await db.execute(
            select(DealerMetricSnapshot)
            .where(DealerMetricSnapshot.dealer_id.in_(ids))
            .order_by(DealerMetricSnapshot.dealer_id, DealerMetricSnapshot.as_of.desc())
        )
    ).scalars().all()
    latest: dict[UUID, DealerMetricSnapshot] = {}
    for s in snaps:
        latest.setdefault(s.dealer_id, s)
    alerts = dict(
        (
            await db.execute(
                select(DealerAlert.dealer_id, func.count())
                .where(DealerAlert.dealer_id.in_(ids), DealerAlert.resolved_at.is_(None))
                .group_by(DealerAlert.dealer_id)
            )
        ).all()
    )
    # Phase 3 Wave 2 attention rollups — all batched, never per-dealer queries.
    today = date.today()
    last_month = rollups.last_calendar_month(today)
    has_last_month_period = set(
        (
            await db.execute(
                select(DealerFinancialPeriod.dealer_id)
                .where(
                    DealerFinancialPeriod.dealer_id.in_(ids),
                    DealerFinancialPeriod.period == last_month,
                )
                .distinct()
            )
        ).scalars().all()
    )
    overdue = dict(
        (
            await db.execute(
                select(DealerPlanAction.dealer_id, func.count())
                .where(
                    DealerPlanAction.dealer_id.in_(ids),
                    DealerPlanAction.status != "done",
                    DealerPlanAction.due_on.is_not(None),
                    DealerPlanAction.due_on < today,
                )
                .group_by(DealerPlanAction.dealer_id)
            )
        ).all()
    )
    fundable = dict(
        (
            await db.execute(
                select(DealerAlert.dealer_id, func.count())
                .where(
                    DealerAlert.dealer_id.in_(ids),
                    DealerAlert.resolved_at.is_(None),
                    DealerAlert.kind.like("fundability_%"),
                )
                .group_by(DealerAlert.dealer_id)
            )
        ).all()
    )
    out: list[DealerListItem] = []
    for d in dealers:
        item = DealerListItem.model_validate(d)
        snap = latest.get(d.id)
        if snap is not None:
            item.score = float(snap.score) if snap.score is not None else None
            item.tier = snap.tier
        item.open_alerts = int(alerts.get(d.id, 0))
        item.missing_statement = d.id not in has_last_month_period
        item.overdue_actions = int(overdue.get(d.id, 0))
        item.fundable_paths = int(fundable.get(d.id, 0))
        item.attention_score = rollups.attention_score(
            item.open_alerts, item.missing_statement, item.overdue_actions, item.fundable_paths
        )
        out.append(item)
    return out


@router.post("/dealers", response_model=DealerRead, status_code=status.HTTP_201_CREATED)
async def create_dealer(payload: DealerCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> DealerBusiness:
    require_team(user)
    dealer = DealerBusiness(**payload.model_dump(), owner_user_id=user.id)
    db.add(dealer)
    await db.flush()
    # Every dealer starts with the uploads source active and a full set of
    # AI-proposed targets, so the cockpit is never empty.
    db.add(DealerSourceConnection(dealer_id=dealer.id, kind="uploads", status="active"))
    await propose_targets(db, dealer)
    await db.commit()
    await db.refresh(dealer)
    return dealer



async def _dealer_read(db: AsyncSession, dealer: DealerBusiness) -> DealerRead:
    r = DealerRead.model_validate(dealer)
    if dealer.bucket_id is not None:
        r.bucket_name = (
            await db.execute(select(Bucket.name).where(Bucket.id == dealer.bucket_id))
        ).scalar_one_or_none()
    return r

@router.get("/buckets/search", response_model=list[BucketSearchItem])
async def search_buckets(user: CurrentUser, db: AsyncSession = Depends(get_db), q: str = "") -> list[Bucket]:
    """Team-only bucket picker for manual dealer<->bucket linking."""
    require_team(user)
    # Empty q = browse: the picker lists everything up front (newest first)
    # and search only narrows it.
    stmt = select(Bucket).order_by(Bucket.created_at.desc()).limit(200)
    needle = q.strip()
    if needle:
        like = f"%{needle.lower()}%"
        stmt = stmt.where(func.lower(Bucket.name).like(like) | func.lower(Bucket.client_name).like(like))
    return (await db.execute(stmt)).scalars().all()


@router.post("/dealers/{dealer_id}/bucket/match", response_model=DealerRead)
async def match_bucket_by_email(dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> DealerRead:
    """Explicitly find this dealer's intake bucket by email (no bucket creation —
    manual linking or ensure_bucket handle the rest)."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    if not dealer.email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Dealer has no email on file — add one, or link a bucket manually.")
    from app.models.public_underwriting_intake import PublicUnderwritingIntake

    intake = (
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(func.lower(PublicUnderwritingIntake.email) == dealer.email.strip().lower())
            .order_by(PublicUnderwritingIntake.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if intake is None or intake.bucket_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No intake bucket found for this email — link one manually.")
    dealer.bucket_id = intake.bucket_id
    await db.commit()
    await db.refresh(dealer)
    return await _dealer_read(db, dealer)


async def _remirror_documents(db: AsyncSession, dealer: DealerBusiness) -> int:
    """Re-mirror every archived document into the CURRENTLY linked bucket —
    the active connection owns the file set. Old bucket rows are left behind
    untouched (same S3 objects). Best-effort per document; flushes only."""
    docs = (
        (
            await db.execute(
                select(DealerDocument).where(
                    DealerDocument.dealer_id == dealer.id,
                    DealerDocument.s3_key.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    moved = 0
    for doc in docs:
        try:
            doc.bucket_file_id = None  # force a fresh mirror in the active bucket
            if await buckets_link.push_document(db, dealer, doc, doc.size_bytes or 0):
                moved += 1
        except Exception:
            logger.exception("dealer-os: bucket re-mirror failed for document %s", doc.id)
    return moved


@router.post("/dealers/{dealer_id}/bucket/create", response_model=DealerRead)
async def create_dealer_bucket(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerRead:
    """Create-and-link a dedicated bucket for this dealer (team only).

    No bucket linked -> exactly the standard ensure_bucket resolution (adopt
    the intake bucket matched by email, else create a fresh audit bucket).
    Already linked -> a BRAND-NEW "Audit — {name}" bucket is created (same row
    shape ensure_bucket uses) and the dealer is repointed at it; the previous
    bucket row is left untouched — never deleted. Manual linking to an
    arbitrary existing bucket stays where it already lives: PATCH /dealers
    with DealerUpdate.bucket_id."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    before_bucket_id = dealer.bucket_id
    if dealer.bucket_id is None:
        bucket = await buckets_link.ensure_bucket(db, dealer)
    else:
        # Idempotency: a double-click on an already-dedicated EMPTY audit
        # bucket should not mint junk buckets — keep the current link.
        current = await db.get(Bucket, dealer.bucket_id)
        dedicated_name = buckets_link.audit_bucket_name(dealer)
        if current is not None and (
            (current.name or "").startswith("Audit — ") or current.name == dedicated_name
        ):
            file_count = (
                await db.execute(
                    select(func.count()).select_from(BucketFile).where(
                        BucketFile.bucket_id == current.id
                    )
                )
            ).scalar_one()
            if file_count == 0:
                await db.commit()
                await db.refresh(dealer)
                return await _dealer_read(db, dealer)
        # Mirror ensure_bucket's fresh-audit-bucket row shape (name/client_name
        # only; bucket_type/purpose stay at their model defaults).
        bucket = Bucket(
            name=dedicated_name,
            client_name=(dealer.name or "")[:180] or None,
        )
        db.add(bucket)
        await db.flush()
        dealer.bucket_id = bucket.id
        await db.flush()
        # The active connection owns the file set — move the mirrors with it.
        await _remirror_documents(db, dealer)
    await log_action(
        db, dealer.id, user, "dealer.bucket_create", "dealer",
        entity_id=dealer.id,
        before={"bucket_id": before_bucket_id},
        after={"bucket_id": bucket.id},
    )
    await db.commit()
    await db.refresh(dealer)
    return await _dealer_read(db, dealer)


@router.get("/dealers/{dealer_id}", response_model=DealerRead)
async def get_dealer(dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> DealerBusiness:
    require_team_or_dealer(user)
    return await resolve_dealer_scope(db, user, dealer_id)


@router.patch("/dealers/{dealer_id}", response_model=DealerRead)
async def update_dealer(
    dealer_id: UUID, payload: DealerUpdate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerBusiness:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    changes = payload.model_dump(exclude_unset=True)
    before = {k: getattr(dealer, k) for k in changes}
    bucket_changed = (
        "bucket_id" in changes and changes["bucket_id"] != dealer.bucket_id
    )
    if bucket_changed and changes["bucket_id"] is not None:
        target = await db.get(Bucket, changes["bucket_id"])
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Bucket not found")
    for k, v in changes.items():
        setattr(dealer, k, v)
    if bucket_changed and dealer.bucket_id is not None:
        # Whichever bucket is the active connection receives the documents —
        # re-mirror the file set into the newly linked bucket.
        await _remirror_documents(db, dealer)
    if changes:
        await log_action(
            db, dealer.id, user, "dealer.update", "dealer",
            entity_id=dealer.id, before=before, after=changes,
        )
    await db.commit()
    await db.refresh(dealer)
    return await _dealer_read(db, dealer)


@router.post("/dealers/{dealer_id}/invite", response_model=DealerInviteResult, status_code=status.HTTP_201_CREATED)
async def invite_dealer_login(
    dealer_id: UUID, payload: DealerInvite, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerInviteResult:
    """Invite (or link) the dealer's self-serve login. Creates the local User
    row with Role.DEALER (clerk_id JIT-bound on first sign-in, same pattern as
    the operator invite flow), links it via dealer_user_id, and best-effort
    sends a Clerk invitation email that lands on audit.qualifiedcommercial.com."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    email = payload.email.strip().lower()
    existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    clerk_sent = False
    if existing is not None and existing.deleted_at is None:
        if existing.role != Role.DEALER:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"That email belongs to an existing {existing.role.value} account — use a different email.",
            )
        target, result_status = existing, "linked"
    else:
        if existing is not None:  # soft-deleted: resurrect as dealer
            existing.deleted_at = None
            existing.name = payload.name or existing.name
            existing.role = Role.DEALER
            existing.clerk_id = None
            target = existing
        else:
            target = User(
                email=email,
                name=payload.name or f"{dealer.name} owner",
                role=Role.DEALER,
                clerk_id=None,
            )
            db.add(target)
        await db.flush()
        result_status = "invited"
        sent = await clerk_service.invite_user(
            email=email,
            name=target.name or dealer.name,
            role=Role.DEALER,
            redirect_url="https://audit.qualifiedcommercial.com/sign-in",
        )
        clerk_sent = sent is not None
    dealer.dealer_user_id = target.id
    await db.commit()
    return DealerInviteResult(status=result_status, email=email, user_id=target.id, clerk_sent=clerk_sent)


def _target_read(t: DealerMetricTarget) -> TargetRead:
    r = TargetRead.model_validate(t)
    r.effective_value = float(t.effective_value) if t.effective_value is not None else None
    return r


@router.get("/dealers/{dealer_id}/targets", response_model=list[TargetRead])
async def list_targets(dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[TargetRead]:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    rows = (
        await db.execute(
            select(DealerMetricTarget)
            .where(DealerMetricTarget.dealer_id == dealer.id)
            .order_by(DealerMetricTarget.metric_key)
        )
    ).scalars().all()
    return [_target_read(t) for t in rows]


@router.post("/dealers/{dealer_id}/targets/propose", response_model=list[TargetRead])
async def repropose_targets(dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[TargetRead]:
    """Refresh AI proposals. Never touches admin overrides — an overridden row
    keeps its admin_value and simply carries the newer suggestion beside it."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    rows = await propose_targets(db, dealer)
    await db.commit()
    return [_target_read(t) for t in rows]


@router.put("/dealers/{dealer_id}/targets", response_model=TargetRead)
async def override_target(
    dealer_id: UUID, payload: TargetOverride, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> TargetRead:
    """Set (or clear, with admin_value=null) the admin override. Override wins."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    row = (
        await db.execute(
            select(DealerMetricTarget).where(
                DealerMetricTarget.dealer_id == dealer.id,
                DealerMetricTarget.metric_key == payload.metric_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown metric for this dealer — propose targets first")
    before = {"metric_key": row.metric_key, "admin_value": row.admin_value, "status": row.status}
    row.admin_value = payload.admin_value
    row.admin_set_by_user_id = user.id
    row.admin_set_at = datetime.now(timezone.utc)
    row.status = "overridden" if payload.admin_value is not None else "ai_proposed"
    await log_action(
        db, dealer.id, user, "target.override", "target",
        entity_id=row.id,
        before=before,
        after={"metric_key": row.metric_key, "admin_value": row.admin_value, "status": row.status},
    )
    await db.commit()
    await db.refresh(row)
    return _target_read(row)


# --- Stream 2: ingestion & normalization -----------------------------------


@router.post(
    "/dealers/{dealer_id}/cash-events/import",
    response_model=CashImportResult,
    status_code=status.HTTP_201_CREATED,
)
async def import_cash_events(
    dealer_id: UUID, payload: CashImport, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> CashImportResult:
    """Bulk-import statement/CSV lines. Each row is AI-classified via the
    normalization rules, then the affected monthly periods are rebuilt."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    rules = await load_active_rules(db, dealer.id)  # loaded once per request
    periods: set[date] = set()
    for row in payload.rows:
        category, flags, rule_matched = classify_with_rules(rules, row.description, row.amount)
        period = period_of(row.occurred_on)
        periods.add(period)
        db.add(
            DealerCashEvent(
                dealer_id=dealer.id,
                period=period,
                occurred_on=row.occurred_on,
                description=row.description,
                amount=row.amount,
                category=category,
                flags=flags,
                invoice_date=row.invoice_date,
                due_date=row.due_date,
                categorized_by="rule" if rule_matched else "ai",
                source="upload",
            )
        )
    await db.flush()
    touched = await rebuild_periods(db, dealer.id, periods)
    # Best-effort engine refresh — never fail the ingest on engine errors.
    try:
        await recompute_snapshot(db, dealer.id)
    except Exception:
        logger.exception("dealer-os: snapshot recompute failed after cash import for %s", dealer.id)
    try:
        await recurrence.stamp_recurrence(db, dealer.id)
    except Exception:
        logger.exception("dealer-os: recurrence stamp failed after cash import for %s", dealer.id)
    await db.commit()
    return CashImportResult(imported=len(payload.rows), periods=touched)


@router.get("/dealers/{dealer_id}/cash-events", response_model=list[CashEventRead])
async def list_cash_events(
    dealer_id: UUID,
    user: CurrentUser,
    period: date | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
) -> list[DealerCashEvent]:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    q = select(DealerCashEvent).where(DealerCashEvent.dealer_id == dealer.id)
    if period is not None:
        q = q.where(DealerCashEvent.period == period)
    q = q.order_by(DealerCashEvent.occurred_on.asc()).limit(limit)
    return (await db.execute(q)).scalars().all()


@router.patch("/dealers/{dealer_id}/cash-events/{event_id}", response_model=CashEventRead)
async def patch_cash_event(
    dealer_id: UUID,
    event_id: UUID,
    payload: CashEventPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerCashEvent:
    """Admin recategorization. Moving a line to owner_personal/one_time also
    seeds a candidate add-back (once per source event)."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    event = (
        await db.execute(
            select(DealerCashEvent).where(
                DealerCashEvent.id == event_id, DealerCashEvent.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cash event not found for this dealer")
    before = {"category": event.category, "flags": event.flags, "categorized_by": event.categorized_by}
    if payload.category is not None:
        event.category = payload.category
    if payload.flags is not None:
        event.flags = payload.flags
    event.categorized_by = "admin"  # human correction wins — retro rules skip these
    await log_action(
        db, dealer.id, user, "cash_event.recategorize", "cash_event",
        entity_id=event.id,
        before=before,
        after={"category": event.category, "flags": event.flags, "categorized_by": "admin"},
    )

    if payload.category in ("owner_personal", "one_time"):
        existing = (
            await db.execute(select(DealerAddback).where(DealerAddback.source_event_id == event.id))
        ).scalar_one_or_none()
        if existing is None:
            amt = abs(float(event.amount))
            is_owner = payload.category == "owner_personal"
            addback = DealerAddback(
                dealer_id=dealer.id,
                title=event.description[:200],
                monthly_amount=amt if is_owner else None,
                annual_amount=amt * 12 if is_owner else amt,
                status="candidate",
                evidence=f"Flagged from statement line {event.occurred_on}",
                source_event_id=event.id,
            )
            db.add(addback)
            await db.flush()
            await log_action(
                db, dealer.id, user, "addback.create", "addback",
                entity_id=addback.id,
                after={
                    "title": addback.title,
                    "status": addback.status,
                    "annual_amount": addback.annual_amount,
                    "source_event_id": event.id,
                },
            )
    # Rebuild only the (dealer, account) scope the event lives in.
    await rebuild_periods(db, dealer.id, {event.period}, account_id=event.account_id)
    await db.commit()
    await db.refresh(event)
    return event


@router.get("/dealers/{dealer_id}/recurring", response_model=RecurringRead)
async def recurring_view(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> RecurringRead:
    """Deterministic recurring-payment view: groups detected live from the
    event ledger via the pure recurrence core (stale stamps are never trusted
    for this response), plus large irregular one-off outflows. Groups are
    sorted by |monthly_equivalent| desc; irregular is capped at 40, newest
    first."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    rows = (
        await db.execute(
            select(
                DealerCashEvent.id,
                DealerCashEvent.occurred_on,
                DealerCashEvent.description,
                DealerCashEvent.amount,
                DealerCashEvent.category,
            )
            .where(DealerCashEvent.dealer_id == dealer.id)
            # Newest window — over-cap ledgers drop the OLDEST rows, never the
            # newest (otherwise every group reads as frozen/overdue).
            .order_by(DealerCashEvent.occurred_on.desc())
            .limit(recurrence.MAX_EVENTS)
        )
    ).all()
    today = date.today()
    lite = [
        recurrence.EventLite(rid, occurred_on, description or "", float(amount or 0))
        for rid, occurred_on, description, amount, _category in rows
    ]
    category_by_id = {rid: category for rid, _o, _d, _a, category in rows}
    groups = recurrence.detect_groups(lite)  # already sorted by |monthly_equivalent| desc
    irregular = recurrence.classify_irregular(lite, groups)
    irregular.sort(key=lambda e: e.occurred_on, reverse=True)
    return RecurringRead(
        groups=[
            RecurringGroupRead(
                key=g.key,
                sample_description=g.sample_description,
                cadence=g.cadence,
                occurrences=g.occurrences,
                avg_amount=g.avg_amount,
                amount_stable=g.amount_stable,
                first_seen=g.first_seen,
                last_seen=g.last_seen,
                next_expected_on=g.next_expected_on,
                overdue=g.next_expected_on < today,
                monthly_equivalent=g.monthly_equivalent,
                direction=g.direction,
            )
            for g in groups
        ],
        irregular=[
            IrregularEventRead(
                event_id=e.id,
                occurred_on=e.occurred_on,
                description=e.description,
                amount=e.amount,
                category=category_by_id.get(e.id) or "uncategorized",
            )
            for e in irregular[:40]
        ],
        computed_at=today,
    )


@router.get("/dealers/{dealer_id}/periods", response_model=list[PeriodRead])
async def list_periods(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerFinancialPeriod]:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return (
        (
            await db.execute(
                select(DealerFinancialPeriod)
                .where(DealerFinancialPeriod.dealer_id == dealer.id)
                .order_by(DealerFinancialPeriod.period.asc())
            )
        )
        .scalars()
        .all()
    )


@router.put("/dealers/{dealer_id}/periods/{period}", response_model=PeriodRead)
async def upsert_period(
    dealer_id: UUID,
    period: date,
    payload: PeriodUpsert,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerFinancialPeriod:
    """Manual month upsert — source becomes 'manual' and manual wins: later
    event-driven rebuilds only recompute deposits/withdrawals, never the
    manually entered balance/EBITDA/revenue fields."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    fp = (
        await db.execute(
            select(DealerFinancialPeriod).where(
                DealerFinancialPeriod.dealer_id == dealer.id,
                DealerFinancialPeriod.period == period,
            )
        )
    ).scalar_one_or_none()
    if fp is None:
        fp = DealerFinancialPeriod(dealer_id=dealer.id, period=period)
        db.add(fp)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(fp, k, v)
    fp.source = "manual"
    # Best-effort engine refresh — never fail the ingest on engine errors.
    try:
        await recompute_snapshot(db, dealer.id)
    except Exception:
        logger.exception("dealer-os: snapshot recompute failed after period upsert for %s", dealer.id)
    await db.commit()
    await db.refresh(fp)
    return fp


# --- Stream 7: document ingestion --------------------------------------------

MAX_DOCUMENT_BYTES = 15 * 1024 * 1024  # 15MB
_DOCUMENT_KINDS = {"statement", "pl", "tax", "debt_schedule", "other", "archive"}


async def _auto_fulfill_doc_request(
    db: AsyncSession, dealer_id: UUID, doc: DealerDocument, user: User
) -> DealerDocRequest | None:
    """Phase 3 Wave 2: a successfully extracted document satisfies the FIRST
    (oldest) open request of the same kind — a request pinned to an account
    only matches a document resolved to that account. Audit-logged."""
    if doc.status != "extracted":
        return None
    open_requests = (
        (
            await db.execute(
                select(DealerDocRequest)
                .where(
                    DealerDocRequest.dealer_id == dealer_id,
                    DealerDocRequest.status == "open",
                    DealerDocRequest.kind == doc.kind,
                )
                .order_by(DealerDocRequest.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    match = next(
        (r for r in open_requests if r.account_id is None or r.account_id == doc.account_id),
        None,
    )
    if match is None:
        return None
    match.status = "fulfilled"
    match.fulfilled_document_id = doc.id
    await log_action(
        db, dealer_id, user, "doc_request.fulfill", "doc_request",
        entity_id=match.id,
        before={"status": "open"},
        after={"status": "fulfilled", "fulfilled_document_id": str(doc.id), "title": match.title},
    )
    return match


async def _ingest_zip_upload(
    db: AsyncSession,
    dealer: DealerBusiness,
    user: User,
    is_dealer: bool,
    kind: str,
    filename: str,
    content_type: str,
    raw: bytes,
) -> DealerDocument:
    """Expand a ZIP upload in memory into a PARENT DealerDocument (the archive
    itself, kind='archive', no account) plus one CHILD row per usable entry.

    Zip-bomb guards run on ZipInfo metadata BEFORE any entry bytes are read
    (services/archive.plan_zip_entries). TEAM children land status='uploaded'
    and are NOT extracted inline — a 40-file archive would blow the request
    timeout — EXCEPT children whose S3 archive failed (s3_key None): their
    bytes would be unrecoverable, so those extract inline immediately. DEALER
    children are quarantined as status='pending_review' (the existing approve
    flow extracts from S3). Returns the PARENT row; the frontend refetches the
    list to render the children."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, zipfile.LargeZipFile):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "The file is not a readable ZIP archive"
        ) from None
    with zf:
        try:
            usable, skipped = archive.plan_zip_entries(zf.infolist(), MAX_DOCUMENT_BYTES)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
        if not usable:
            reasons = "; ".join(f"{s['name']} ({s['reason']})" for s in skipped[:5])
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "The ZIP contains no usable documents"
                + (f" — skipped: {reasons}" if reasons else ""),
            )
        entries: list[tuple[zipfile.ZipInfo, bytes]] = []
        for info in usable:
            try:
                entry_raw = zf.read(info.filename)
            except Exception:
                skipped.append({"name": info.filename, "reason": "unreadable"})
                continue
            if not entry_raw or len(entry_raw) > MAX_DOCUMENT_BYTES:
                # The declared size lied — treat like any other bad entry.
                skipped.append({"name": info.filename, "reason": "declared_size_mismatch"})
                continue
            entries.append((info, entry_raw))
    if not entries:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "No entry in the ZIP could be read"
        )

    # PARENT: the archive row itself — original zip archived best-effort.
    parent_key = storage.build_key(dealer.id, filename)
    parent = DealerDocument(
        dealer_id=dealer.id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(raw),
        s3_key=parent_key if storage.put_bytes(parent_key, raw, content_type) else None,
        kind="archive",
        status="extracted",
        detected_kind="archive",
        extracted={
            "entries": [info.filename for info, _ in entries],
            "skipped": skipped,
        },
    )
    db.add(parent)
    await db.flush()

    children: list[tuple[DealerDocument, bytes]] = []
    for info, entry_raw in entries:
        child_name = storage.safe_filename(info.filename.rsplit("/", 1)[-1])
        child_ct = archive.content_type_for(info.filename)
        child_key = storage.build_key(dealer.id, child_name)
        child = DealerDocument(
            dealer_id=dealer.id,
            filename=child_name,
            content_type=child_ct,
            size_bytes=len(entry_raw),
            s3_key=child_key if storage.put_bytes(child_key, entry_raw, child_ct) else None,
            # The uploader-declared kind carries to children ('archive' itself
            # would be meaningless on a child — those fall back to 'other').
            kind=kind if kind != "archive" else "other",
            status="pending_review" if is_dealer else "uploaded",
            parent_document_id=parent.id,
        )
        db.add(child)
        children.append((child, entry_raw))
    await db.flush()

    if not is_dealer:
        for child, entry_raw in children:
            if child.s3_key is None:
                # Bytes would be unrecoverable after this request — extract
                # inline now (the exception to the no-inline-extract rule).
                # SAVEPOINT: extract_document's failure recovery can roll the
                # session back, which without a savepoint would discard the
                # parent + sibling rows this request already flushed.
                try:
                    async with db.begin_nested():
                        await extract_document(db, child, entry_raw)
                except Exception:
                    logger.exception(
                        "dealer-os: inline archive extraction failed for %s", child.id
                    )
                    child.status = "failed"
                    child.error = "Extraction failed inside the archive — upload this file individually."
                    await db.flush()
                if child.status == "extracted":
                    await _auto_fulfill_doc_request(db, dealer.id, child, user)
        # Best-effort bucket mirror while the bytes are in hand — mirrors the
        # single-file upload path (push_document no-ops without an s3_key).
        for child, entry_raw in children:
            try:
                await buckets_link.push_document(db, dealer, child, len(entry_raw))
            except Exception:
                logger.exception("dealer-os: bucket push failed for document %s", child.id)

    await log_action(
        db, dealer.id, user, "document.zip_upload", "document",
        entity_id=parent.id,
        after={
            "filename": parent.filename,
            "size_bytes": parent.size_bytes,
            "children": len(children),
            "skipped": len(skipped),
            "dealer_upload": is_dealer,
        },
    )
    await db.commit()
    await db.refresh(parent)
    return parent


@router.post(
    "/dealers/{dealer_id}/documents",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    dealer_id: UUID,
    user: CurrentUser,
    file: UploadFile = File(...),
    kind: str = Form(default="statement"),
    account_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
) -> DealerDocument:
    """Upload one financial document, archive it to S3 best-effort.

    TEAM uploads run extraction inline — the response carries the final status
    (extracted or failed with a human-readable error) — then auto-fulfill any
    matching open doc request and mirror into the linked bucket best-effort.

    DEALER self-uploads (Phase 3 Wave 2) are quarantined: the file is stored
    and the row lands as status='pending_review' with NO extraction — nothing
    a dealer uploads touches the ledger or metrics until a team member
    approves it (POST .../documents/{doc_id}/approve). account_id pinning is a
    team affordance and is ignored for dealer uploads.

    account_id (optional form field, team only): '' = AI-detect the bank
    account from the statement; a UUID pins the document to that account and
    skips detection (admin choice wins)."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    is_dealer = user.role == Role.DEALER
    if kind not in _DOCUMENT_KINDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"kind must be one of: {', '.join(sorted(_DOCUMENT_KINDS))}",
        )
    pinned_account_id: UUID | None = None
    if account_id.strip() and not is_dealer:
        try:
            pinned_account_id = UUID(account_id.strip())
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "account_id must be a UUID (or empty for AI detection)",
            ) from None
        account = (
            await db.execute(
                select(DealerAccount).where(
                    DealerAccount.id == pinned_account_id, DealerAccount.dealer_id == dealer.id
                )
            )
        ).scalar_one_or_none()
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found for this dealer")
    raw = await file.read()
    if not raw:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The uploaded file is empty")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File exceeds the 15MB document limit"
        )
    filename = storage.safe_filename(file.filename)
    content_type = (file.content_type or "application/octet-stream")[:120]

    # Doc hub (0114): a ZIP expands into a parent 'archive' row + one child
    # row per usable entry (returned row = the PARENT; the list endpoint
    # carries the children). account pinning is a single-statement affordance
    # and does not apply to archives — children go through AI detection.
    if archive.is_zip_upload(content_type, filename):
        return await _ingest_zip_upload(
            db, dealer, user, is_dealer, kind, filename, content_type, raw
        )

    key = storage.build_key(dealer.id, filename)
    s3_key = key if storage.put_bytes(key, raw, content_type) else None

    doc = DealerDocument(
        dealer_id=dealer.id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(raw),
        s3_key=s3_key,
        kind=kind,
        status="pending_review" if is_dealer else "uploaded",
    )
    db.add(doc)
    await db.flush()

    if is_dealer:
        # No extraction, no bucket push — quarantined until team review.
        await log_action(
            db, dealer.id, user, "dealer_upload", "document",
            entity_id=doc.id,
            after={"filename": doc.filename, "kind": doc.kind, "status": doc.status,
                   "size_bytes": doc.size_bytes},
        )
        await db.commit()
        await db.refresh(doc)
        return doc

    await extract_document(db, doc, raw, account_id=pinned_account_id)
    await _auto_fulfill_doc_request(db, dealer.id, doc, user)
    # Best-effort mirror into the dealer's linked bucket — never fail the
    # upload because the bucket bridge hiccuped.
    try:
        await buckets_link.push_document(db, dealer, doc, len(raw))
    except Exception:
        logger.exception("dealer-os: bucket push failed for document %s", doc.id)
    await db.commit()
    await db.refresh(doc)
    return doc


@router.get("/dealers/{dealer_id}/documents", response_model=list[DocumentRead])
async def list_documents(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerDocument]:
    """Team sees every row; a DEALER login sees all non-failed rows (failed
    extractions are an internal operational detail, not dealer-facing).
    Rejected self-uploads STAY visible to the dealer — status='rejected' with
    the reviewer's note in `error` is the dealer-facing outcome."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    q = select(DealerDocument).where(DealerDocument.dealer_id == dealer.id)
    if user.role == Role.DEALER:
        q = q.where(DealerDocument.status != "failed")
    return (
        (await db.execute(q.order_by(DealerDocument.created_at.desc()))).scalars().all()
    )


_COVERAGE_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
# Legacy declared kinds mapped onto the classifier vocabulary — used when a
# document predates detected_kind (0114) and only `kind` is available.
_KIND_TO_DETECTED = {
    "statement": "bank_statement",
    "pl": "profit_and_loss",
    "tax": "tax_return",
    "debt_schedule": "debt_schedule",
}


@router.get("/dealers/{dealer_id}/documents/coverage", response_model=DocumentCoverageRead)
async def document_coverage(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DocumentCoverageRead:
    """Intake completeness for the Documents tab: which statement months are
    covered (extracted statement docs OR period rows with statement flow
    data), which tax years have a filing row, whether a P&L / debt schedule
    has landed, and how many doc requests are still open."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)

    months: set[str] = set()
    period_rows = (
        await db.execute(
            select(
                DealerFinancialPeriod.period,
                DealerFinancialPeriod.deposits,
                DealerFinancialPeriod.withdrawals,
            ).where(DealerFinancialPeriod.dealer_id == dealer.id)
        )
    ).all()
    for period, deposits, withdrawals in period_rows:
        if deposits is not None or withdrawals is not None:
            months.add(f"{period.year:04d}-{period.month:02d}")

    has_pl = False
    has_debt_schedule = False
    doc_rows = (
        await db.execute(
            select(
                DealerDocument.kind, DealerDocument.detected_kind, DealerDocument.extracted
            ).where(
                DealerDocument.dealer_id == dealer.id, DealerDocument.status == "extracted"
            )
        )
    ).all()
    for kind, detected_kind, extracted in doc_rows:
        effective = detected_kind or _KIND_TO_DETECTED.get(kind)
        if effective == "bank_statement":
            for m in (extracted or {}).get("months") or []:
                key = str(m.get("month") or "") if isinstance(m, dict) else ""
                if _COVERAGE_MONTH_RE.match(key):
                    months.add(key)
        elif effective == "profit_and_loss":
            has_pl = True
        elif effective == "debt_schedule":
            has_debt_schedule = True

    tax_years = sorted(
        (
            await db.execute(
                select(DealerTaxFiling.year).where(DealerTaxFiling.dealer_id == dealer.id)
            )
        )
        .scalars()
        .all()
    )
    open_doc_requests = (
        await db.execute(
            select(func.count())
            .select_from(DealerDocRequest)
            .where(DealerDocRequest.dealer_id == dealer.id, DealerDocRequest.status == "open")
        )
    ).scalar_one()

    return DocumentCoverageRead(
        statement_months=sorted(months),
        tax_years=tax_years,
        has_pl=has_pl,
        has_debt_schedule=has_debt_schedule,
        open_doc_requests=int(open_doc_requests or 0),
        # Deterministic freshness vs. today (pure helper — services.recurrence).
        **recurrence.compute_freshness(months, date.today()),
    )


@router.post("/dealers/{dealer_id}/documents/{doc_id}/extract", response_model=DocumentRead)
async def reextract_document(
    dealer_id: UUID, doc_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerDocument:
    """Re-run extraction from the S3 archive. 409 when the original bytes were
    never archived (S3 unconfigured at upload time) — re-upload instead."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    doc = (
        await db.execute(
            select(DealerDocument).where(
                DealerDocument.id == doc_id, DealerDocument.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found for this dealer")
    if not doc.s3_key:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The original file was not archived to S3 — upload the document again to re-extract it",
        )
    await extract_document(db, doc)
    await db.commit()
    await db.refresh(doc)
    return doc


# --- Phase 3 Wave 2: dealer self-upload review -------------------------------


async def _load_document(db: AsyncSession, dealer_id: UUID, doc_id: UUID) -> DealerDocument:
    doc = (
        await db.execute(
            select(DealerDocument).where(
                DealerDocument.id == doc_id, DealerDocument.dealer_id == dealer_id
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found for this dealer")
    return doc


@router.post("/dealers/{dealer_id}/documents/{doc_id}/approve", response_model=DocumentRead)
async def approve_dealer_document(
    dealer_id: UUID, doc_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerDocument:
    """Team approves a dealer self-upload: runs the SAME extraction pipeline
    as team uploads (incl. bank-account detection, classify -> rebuild_periods
    -> recompute_snapshot) from the S3 archive. The response carries the final
    status (extracted, or failed with a human-readable error). A previously
    rejected document may be re-approved. Auto-fulfills a matching open doc
    request and mirrors into the linked bucket best-effort."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    doc = await _load_document(db, dealer.id, doc_id)
    if doc.status not in ("pending_review", "rejected"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Document is not awaiting review (status: {doc.status})",
        )
    if not doc.s3_key:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The original file was not archived to S3 — ask the dealer to upload it again",
        )
    before_status = doc.status
    doc.error = None
    await extract_document(db, doc)  # fetches bytes from S3; account detect included
    await log_action(
        db, dealer.id, user, "dealer_doc.approve", "document",
        entity_id=doc.id,
        before={"status": before_status},
        after={"status": doc.status, "account_id": str(doc.account_id) if doc.account_id else None},
    )
    await _auto_fulfill_doc_request(db, dealer.id, doc, user)
    try:
        await buckets_link.push_document(db, dealer, doc, doc.size_bytes)
    except Exception:
        logger.exception("dealer-os: bucket push failed for document %s", doc.id)
    await db.commit()
    await db.refresh(doc)
    return doc


@router.post("/dealers/{dealer_id}/documents/{doc_id}/reject", response_model=DocumentRead)
async def reject_dealer_document(
    dealer_id: UUID,
    doc_id: UUID,
    payload: DocumentReject,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerDocument:
    """Team rejects a dealer self-upload with a required note. The row stays
    visible to the dealer (status='rejected', error=note) so the outcome and
    the reason are self-serve — nothing ever reached the ledger."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    doc = await _load_document(db, dealer.id, doc_id)
    if doc.status != "pending_review":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Only documents awaiting review can be rejected (status: {doc.status})",
        )
    doc.status = "rejected"
    doc.error = payload.note.strip()[:2000]
    await log_action(
        db, dealer.id, user, "dealer_doc.reject", "document",
        entity_id=doc.id,
        before={"status": "pending_review"},
        after={"status": "rejected", "note": doc.error},
    )
    await db.commit()
    await db.refresh(doc)
    return doc


# --- Phase 2: linked-bucket pull (bucket file -> Dealer OS ingest) -----------


@router.get("/dealers/{dealer_id}/bucket-files", response_model=list[BucketFileItem])
async def list_bucket_files(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[BucketFileItem]:
    """Files in the dealer's linked bucket (ensure_bucket resolves/creates the
    link first). has_analysis marks files whose cached BucketFileAnalysis can
    be ingested without a model call; already_ingested marks files a
    DealerDocument already references."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    bucket = await buckets_link.ensure_bucket(db, dealer)
    await db.commit()  # persist the adoption/creation before the read
    files = (
        await db.execute(
            select(BucketFile)
            .where(BucketFile.bucket_id == bucket.id, BucketFile.deleted_at.is_(None))
            .order_by(BucketFile.created_at.desc())
        )
    ).scalars().all()
    if not files:
        return []
    ids = [f.id for f in files]
    analysis_rows = (
        await db.execute(
            select(BucketFileAnalysis.bucket_file_id, BucketFileAnalysis.content_hash).where(
                BucketFileAnalysis.bucket_file_id.in_(ids),
                BucketFileAnalysis.analysis_version == CURRENT_FILE_ANALYSIS_VERSION,
                BucketFileAnalysis.status == "completed",
            )
        )
    ).all()
    analyzed_pairs = {(fid, ch) for fid, ch in analysis_rows}
    analyzed_ids = {fid for fid, _ in analysis_rows}
    ingested_ids = set(
        (
            await db.execute(
                select(DealerDocument.bucket_file_id).where(
                    DealerDocument.dealer_id == dealer.id,
                    DealerDocument.bucket_file_id.in_(ids),
                )
            )
        ).scalars().all()
    )
    return [
        BucketFileItem(
            id=f.id,
            file_name=f.file_name,
            content_type=f.content_type,
            size_bytes=f.size_bytes,
            created_at=f.created_at,
            has_analysis=(
                (f.id, f.content_hash) in analyzed_pairs
                if f.content_hash
                else f.id in analyzed_ids
            ),
            already_ingested=f.id in ingested_ids,
        )
        for f in files
    ]


@router.post(
    "/dealers/{dealer_id}/bucket-files/{file_id}/ingest",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
)
async def ingest_bucket_file(
    dealer_id: UUID, file_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerDocument:
    """Pull one linked-bucket file into Dealer OS. When a cached
    BucketFileAnalysis exists for the file's content_hash at the current
    analysis version, its JSON is adapted into the canonical extraction shape
    and persisted through the SAME plan path — zero model tokens. Otherwise
    the raw bytes are fetched from S3 and run through the normal extract
    pipeline (model call for PDF/image, pure parse for CSV/XLSX)."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    bucket = await buckets_link.ensure_bucket(db, dealer)
    bucket_file = (
        await db.execute(
            select(BucketFile).where(
                BucketFile.id == file_id,
                BucketFile.bucket_id == bucket.id,
                BucketFile.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if bucket_file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found in this dealer's linked bucket")

    analysis_q = select(BucketFileAnalysis).where(
        BucketFileAnalysis.bucket_file_id == bucket_file.id,
        BucketFileAnalysis.analysis_version == CURRENT_FILE_ANALYSIS_VERSION,
        BucketFileAnalysis.status == "completed",
    )
    if bucket_file.content_hash:
        analysis_q = analysis_q.where(BucketFileAnalysis.content_hash == bucket_file.content_hash)
    analysis_row = (
        await db.execute(analysis_q.order_by(BucketFileAnalysis.created_at.desc()).limit(1))
    ).scalar_one_or_none()

    doc = DealerDocument(
        dealer_id=dealer.id,
        filename=bucket_file.file_name[:260],
        content_type=(bucket_file.content_type or "application/octet-stream")[:120],
        size_bytes=int(bucket_file.size_bytes or 0),
        # Same S3 object — kept only when it fits our column so re-extract works.
        s3_key=bucket_file.s3_key if len(bucket_file.s3_key) <= 400 else None,
        kind=buckets_link.guess_document_kind(bucket_file.file_name),
        status="uploaded",
        bucket_file_id=bucket_file.id,
    )
    db.add(doc)
    await db.flush()

    source_note = f"Ingested from linked bucket file '{bucket_file.file_name}'"
    if analysis_row is not None and isinstance(analysis_row.analysis, dict):
        # Cache path — asserts no model call: adapt the stored analysis JSON
        # into the canonical extraction dict and persist via the same plan.
        extraction = buckets_link.adapt_analysis_to_extraction(analysis_row.analysis)
        rules = await load_active_rules(db, dealer.id)
        plan = apply_extraction(extraction, rules=rules)
        cache_note = f"{source_note} via cached analysis (no model call)"
        if plan["events"] or plan["period_upserts"]:
            await _persist_plan(db, dealer.id, plan)
            doc.extracted = {
                "months": plan["months"],
                "transactions_count": len(plan["events"]),
                "notes": ([cache_note] + plan["notes"])[:50],
                "parser": "bucket_analysis_cache",
            }
            doc.status = "extracted"
            doc.error = None
        else:
            doc.status = "failed"
            doc.error = "Cached analysis holds no usable monthly financial data"
            doc.extracted = {
                "months": [],
                "transactions_count": 0,
                "notes": ([cache_note] + plan["notes"])[:50],
                "parser": "bucket_analysis_cache",
            }
    else:
        raw = storage.get_bytes(bucket_file.s3_key)
        if raw is None:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "Could not fetch the bucket file from S3 — try again or re-upload it to Dealer OS directly",
            )
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "Bucket file exceeds the 15MB document limit",
            )
        await extract_document(db, doc, raw)
        if doc.extracted is not None:
            notes = list(doc.extracted.get("notes") or [])
            doc.extracted = {**doc.extracted, "notes": ([source_note] + notes)[:50]}
        elif doc.status == "failed":
            doc.extracted = {"months": [], "transactions_count": 0, "notes": [source_note]}

    await db.commit()
    await db.refresh(doc)
    return doc


# --- Stream 3: engines, lineage & alerts -----------------------------------


@router.post("/dealers/{dealer_id}/recompute", response_model=SnapshotRead)
async def recompute_dealer(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerMetricSnapshot:
    """Force a fresh metric snapshot (with lineage + alerts) for the dealer."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    snapshot = await recompute_snapshot(db, dealer.id)
    await db.commit()
    await db.refresh(snapshot)
    return snapshot


@router.get("/dealers/{dealer_id}/health", response_model=HealthRead)
async def dealer_health(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> HealthRead:
    """Cockpit read: latest snapshot + targets + unresolved alerts + lineage size."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    snapshot = (
        await db.execute(
            select(DealerMetricSnapshot)
            .where(DealerMetricSnapshot.dealer_id == dealer.id)
            .order_by(DealerMetricSnapshot.as_of.desc(), DealerMetricSnapshot.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    targets = (
        await db.execute(
            select(DealerMetricTarget)
            .where(DealerMetricTarget.dealer_id == dealer.id)
            .order_by(DealerMetricTarget.metric_key)
        )
    ).scalars().all()
    alerts = (
        await db.execute(
            select(DealerAlert)
            .where(DealerAlert.dealer_id == dealer.id, DealerAlert.resolved_at.is_(None))
            .order_by(DealerAlert.created_at.desc())
        )
    ).scalars().all()
    lineage_count = 0
    if snapshot is not None:
        lineage_count = (
            await db.execute(
                select(func.count())
                .select_from(DealerMetricLineage)
                .where(DealerMetricLineage.snapshot_id == snapshot.id)
            )
        ).scalar_one()
    return HealthRead(
        snapshot=SnapshotRead.model_validate(snapshot) if snapshot is not None else None,
        targets=[_target_read(t) for t in targets],
        alerts=[AlertRead.model_validate(a) for a in alerts],
        lineage_count=int(lineage_count),
    )


@router.post("/dealers/{dealer_id}/alerts/{alert_id}/resolve", response_model=AlertRead)
async def resolve_alert(
    dealer_id: UUID, alert_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerAlert:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    alert = (
        await db.execute(
            select(DealerAlert).where(DealerAlert.id == alert_id, DealerAlert.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert not found for this dealer")
    if alert.resolved_at is None:
        alert.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(alert)
    return alert


# --- Stream 4: plan, forecast & funding paths --------------------------------


async def _load_plan_action(db: AsyncSession, dealer_id: UUID, action_id: UUID) -> DealerPlanAction:
    action = (
        await db.execute(
            select(DealerPlanAction).where(
                DealerPlanAction.id == action_id, DealerPlanAction.dealer_id == dealer_id
            )
        )
    ).scalar_one_or_none()
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan action not found for this dealer")
    return action


async def _latest_snapshot_metrics(db: AsyncSession, dealer_id: UUID) -> dict:
    """Latest snapshot's metrics dict, or 400 with a clear next step."""
    snapshot = (
        await db.execute(
            select(DealerMetricSnapshot)
            .where(DealerMetricSnapshot.dealer_id == dealer_id)
            .order_by(DealerMetricSnapshot.as_of.desc(), DealerMetricSnapshot.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if snapshot is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No metric snapshot exists for this dealer yet — import financials or "
            "POST /dealers/{id}/recompute first",
        )
    return snapshot.metrics or {}


@router.get("/dealers/{dealer_id}/plan", response_model=list[PlanActionRead])
async def list_plan_actions(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerPlanAction]:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return (
        (
            await db.execute(
                select(DealerPlanAction)
                .where(DealerPlanAction.dealer_id == dealer.id)
                .order_by(DealerPlanAction.sort.asc(), DealerPlanAction.created_at.asc())
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/dealers/{dealer_id}/plan", response_model=PlanActionRead, status_code=status.HTTP_201_CREATED
)
async def create_plan_action(
    dealer_id: UUID, payload: PlanActionCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerPlanAction:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    action = DealerPlanAction(dealer_id=dealer.id, **payload.model_dump())
    db.add(action)
    await db.commit()
    await db.refresh(action)
    return action


@router.patch("/dealers/{dealer_id}/plan/{action_id}", response_model=PlanActionRead)
async def update_plan_action(
    dealer_id: UUID,
    action_id: UUID,
    payload: PlanActionUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerPlanAction:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    action = await _load_plan_action(db, dealer.id, action_id)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(action, k, v)
    await db.commit()
    await db.refresh(action)
    return action


@router.delete("/dealers/{dealer_id}/plan/{action_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan_action(
    dealer_id: UUID, action_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> None:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    action = await _load_plan_action(db, dealer.id, action_id)
    await db.delete(action)
    await db.commit()


@router.post("/dealers/{dealer_id}/plan/publish", response_model=list[PlanActionRead])
async def publish_plan(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerPlanAction]:
    """Publish the whole plan to the dealer portal (sets published=true on all)."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    actions = (
        (
            await db.execute(
                select(DealerPlanAction)
                .where(DealerPlanAction.dealer_id == dealer.id)
                .order_by(DealerPlanAction.sort.asc(), DealerPlanAction.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    for a in actions:
        a.published = True
    await db.commit()
    return actions


@router.get("/dealers/{dealer_id}/forecast", response_model=ForecastRead)
async def dealer_forecast(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ForecastRead:
    """12-month baseline vs plan-adjusted projection from the latest snapshot."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    metrics = await _latest_snapshot_metrics(db, dealer.id)
    open_actions = (
        (
            await db.execute(
                select(DealerPlanAction).where(
                    DealerPlanAction.dealer_id == dealer.id, DealerPlanAction.status != "done"
                )
            )
        )
        .scalars()
        .all()
    )
    plan_actions = [
        {"category": a.category, "status": a.status, "due_on": a.due_on} for a in open_actions
    ]
    return ForecastRead(**compute_forecast(metrics, plan_actions))


@router.get("/dealers/{dealer_id}/paths", response_model=PathsRead)
async def dealer_paths(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> PathsRead:
    """Funding-path readiness + credit-ladder position from the latest snapshot."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    metrics = await _latest_snapshot_metrics(db, dealer.id)
    target_rows = (
        (
            await db.execute(
                select(DealerMetricTarget).where(DealerMetricTarget.dealer_id == dealer.id)
            )
        )
        .scalars()
        .all()
    )
    targets = {
        t.metric_key: (float(t.effective_value) if t.effective_value is not None else None)
        for t in target_rows
    }
    return PathsRead(
        paths=compute_paths(metrics, targets), ladder=compute_ladder(metrics, targets)
    )


# --- Stream 5: messaging, sessions, global alerts & lender package -----------


@router.get("/dealers/{dealer_id}/messages", response_model=list[MessageRead])
async def list_messages(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerMessage]:
    """Full thread, oldest first. Team sees internal notes; a DEALER login
    only ever gets internal=false rows."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    q = select(DealerMessage).where(DealerMessage.dealer_id == dealer.id)
    if user.role == Role.DEALER:
        q = q.where(DealerMessage.internal.is_(False))
    return (
        (await db.execute(q.order_by(DealerMessage.created_at.asc()))).scalars().all()
    )


@router.post(
    "/dealers/{dealer_id}/messages", response_model=MessageRead, status_code=status.HTTP_201_CREATED
)
async def create_message(
    dealer_id: UUID, payload: MessageCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerMessage:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    # A dealer can never author an internal note, whatever the payload says.
    internal = False if user.role == Role.DEALER else payload.internal
    message = DealerMessage(
        dealer_id=dealer.id,
        author_user_id=user.id,
        author_name=user.name,
        body=payload.body,
        internal=internal,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message


@router.get("/dealers/{dealer_id}/sessions", response_model=list[SessionRead])
async def list_sessions(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerSession]:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return (
        (
            await db.execute(
                select(DealerSession)
                .where(DealerSession.dealer_id == dealer.id)
                .order_by(DealerSession.starts_at.asc())
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/dealers/{dealer_id}/sessions", response_model=SessionRead, status_code=status.HTTP_201_CREATED
)
async def create_session(
    dealer_id: UUID, payload: SessionCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerSession:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    session = DealerSession(dealer_id=dealer.id, created_by_user_id=user.id, **payload.model_dump())
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


@router.delete("/dealers/{dealer_id}/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    dealer_id: UUID, session_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> None:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    session = (
        await db.execute(
            select(DealerSession).where(
                DealerSession.id == session_id, DealerSession.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found for this dealer")
    await db.delete(session)
    await db.commit()


@router.get("/alerts", response_model=list[GlobalAlertRead])
async def list_global_alerts(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[GlobalAlertRead]:
    """Global team view: every unresolved alert across all dealers, newest first."""
    require_team(user)
    rows = (
        await db.execute(
            select(DealerAlert, DealerBusiness.name)
            .join(DealerBusiness, DealerBusiness.id == DealerAlert.dealer_id)
            .where(DealerAlert.resolved_at.is_(None))
            .order_by(DealerAlert.created_at.desc())
        )
    ).all()
    out: list[GlobalAlertRead] = []
    for alert, dealer_name in rows:
        item = GlobalAlertRead(
            **AlertRead.model_validate(alert).model_dump(),
            dealer_id=alert.dealer_id,
            dealer_name=dealer_name,
        )
        out.append(item)
    return out


# --- Phase 2: credit profile & IRS/tax alignment -----------------------------


@router.get("/dealers/{dealer_id}/credit", response_model=CreditRead)
async def get_credit_profile(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> CreditRead:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    row = (
        await db.execute(
            select(DealerCreditProfile).where(DealerCreditProfile.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    if row is None:
        return CreditRead()
    return CreditRead(
        business_history=row.business_history or [],
        personal_score=row.personal_score,
        personal_tier=row.personal_tier,
        updated_at=row.updated_at,
    )


@router.put("/dealers/{dealer_id}/credit", response_model=CreditRead)
async def upsert_credit_profile(
    dealer_id: UUID, payload: CreditUpsert, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> CreditRead:
    """Upsert the one dos_credit_profiles row. Only fields present in the
    payload change; business_history items are free-form (extras preserved)."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    row = (
        await db.execute(
            select(DealerCreditProfile).where(DealerCreditProfile.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = DealerCreditProfile(dealer_id=dealer.id)
        db.add(row)
    data = payload.model_dump(exclude_unset=True)
    if "business_history" in data:
        row.business_history = data["business_history"]
    if "personal_score" in data:
        row.personal_score = data["personal_score"]
    if "personal_tier" in data:
        row.personal_tier = data["personal_tier"]
    await db.commit()
    await db.refresh(row)
    return CreditRead(
        business_history=row.business_history or [],
        personal_score=row.personal_score,
        personal_tier=row.personal_tier,
        updated_at=row.updated_at,
    )


async def _deposits_by_year(db: AsyncSession, dealer_id: UUID) -> dict[int, float]:
    """Observed deposits per calendar year = sum of period deposits."""
    periods = (
        await db.execute(
            select(DealerFinancialPeriod.period, DealerFinancialPeriod.deposits).where(
                DealerFinancialPeriod.dealer_id == dealer_id,
                DealerFinancialPeriod.deposits.is_not(None),
            )
        )
    ).all()
    totals: dict[int, float] = {}
    for period, deposits in periods:
        totals[period.year] = totals.get(period.year, 0.0) + float(deposits)
    return {y: round(v, 2) for y, v in totals.items()}


def _tax_year_read(
    year: int, filing: DealerTaxFiling | None, observed: float | None
) -> TaxYearRead:
    reported = (
        float(filing.revenue_reported)
        if filing is not None and filing.revenue_reported is not None
        else None
    )
    discrepancy_pct = (
        round((observed - reported) / reported * 100.0, 1)
        if observed is not None and reported
        else None
    )
    return TaxYearRead(
        year=year,
        filed=bool(filing.filed) if filing is not None else False,
        revenue_reported=reported,
        deposits_observed=observed,
        discrepancy_pct=discrepancy_pct,
        filing_id=filing.id if filing is not None else None,
    )


@router.get("/dealers/{dealer_id}/tax", response_model=list[TaxYearRead])
async def list_tax_years(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[TaxYearRead]:
    """Filed years joined with observed deposits per calendar year (sum of the
    dealer's period deposits). Years that have observed deposits but no filing
    row still appear (filed=false, filing_id=null) so gaps are visible."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    filings = (
        await db.execute(
            select(DealerTaxFiling)
            .where(DealerTaxFiling.dealer_id == dealer.id)
            .order_by(DealerTaxFiling.year.asc())
        )
    ).scalars().all()
    observed = await _deposits_by_year(db, dealer.id)
    by_year = {f.year: f for f in filings}
    years = sorted(set(by_year) | set(observed))
    return [_tax_year_read(y, by_year.get(y), observed.get(y)) for y in years]


@router.put("/dealers/{dealer_id}/tax/{year}", response_model=TaxYearRead)
async def upsert_tax_filing(
    dealer_id: UUID,
    year: int,
    payload: TaxFilingUpsert,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TaxYearRead:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    if not 2000 <= year <= 2100:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "year must be between 2000 and 2100")
    filing = (
        await db.execute(
            select(DealerTaxFiling).where(
                DealerTaxFiling.dealer_id == dealer.id, DealerTaxFiling.year == year
            )
        )
    ).scalar_one_or_none()
    if filing is None:
        filing = DealerTaxFiling(dealer_id=dealer.id, year=year)
        db.add(filing)
    data = payload.model_dump(exclude_unset=True)
    if "filed" in data and data["filed"] is not None:
        filing.filed = data["filed"]
    if "revenue_reported" in data:
        filing.revenue_reported = data["revenue_reported"]
    await db.commit()
    await db.refresh(filing)
    observed = (await _deposits_by_year(db, dealer.id)).get(year)
    return _tax_year_read(year, filing, observed)


@router.get("/dealers/{dealer_id}/lender-package", response_model=LenderPackageRead)
async def lender_package(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> LenderPackageRead:
    """One-call JSON bundle powering the print-ready lender report. Sections
    that need a snapshot (forecast, paths) come back None — never 400 — so a
    brand-new dealer still renders a partial package."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return await _build_lender_package(db, dealer)


async def _build_lender_package(db: AsyncSession, dealer: DealerBusiness) -> LenderPackageRead:
    """Assemble the full lender bundle for one dealer — shared by the
    lender-package endpoint and the AI analyst (same facts, one code path)."""
    snapshot = (
        await db.execute(
            select(DealerMetricSnapshot)
            .where(DealerMetricSnapshot.dealer_id == dealer.id)
            .order_by(DealerMetricSnapshot.as_of.desc(), DealerMetricSnapshot.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    target_rows = (
        (
            await db.execute(
                select(DealerMetricTarget)
                .where(DealerMetricTarget.dealer_id == dealer.id)
                .order_by(DealerMetricTarget.metric_key)
            )
        )
        .scalars()
        .all()
    )
    periods = (
        (
            await db.execute(
                select(DealerFinancialPeriod)
                .where(DealerFinancialPeriod.dealer_id == dealer.id)
                .order_by(DealerFinancialPeriod.period.asc())
            )
        )
        .scalars()
        .all()
    )
    addbacks = (
        (
            await db.execute(
                select(DealerAddback)
                .where(DealerAddback.dealer_id == dealer.id)
                .order_by(DealerAddback.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    plan = (
        (
            await db.execute(
                select(DealerPlanAction)
                .where(DealerPlanAction.dealer_id == dealer.id)
                .order_by(DealerPlanAction.sort.asc(), DealerPlanAction.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    forecast: ForecastRead | None = None
    paths: PathsRead | None = None
    if snapshot is not None:
        metrics = snapshot.metrics or {}
        open_actions = [
            {"category": a.category, "status": a.status, "due_on": a.due_on}
            for a in plan
            if a.status != "done"
        ]
        forecast = ForecastRead(**compute_forecast(metrics, open_actions))
        targets_map = {
            t.metric_key: (float(t.effective_value) if t.effective_value is not None else None)
            for t in target_rows
        }
        paths = PathsRead(
            paths=compute_paths(metrics, targets_map), ladder=compute_ladder(metrics, targets_map)
        )

    return LenderPackageRead(
        dealer=DealerRead.model_validate(dealer),
        snapshot=SnapshotRead.model_validate(snapshot) if snapshot is not None else None,
        targets=[_target_read(t) for t in target_rows],
        periods=[PeriodRead.model_validate(p) for p in periods],
        addbacks=[AddbackRead.model_validate(a) for a in addbacks],
        plan=[PlanActionRead.model_validate(a) for a in plan],
        forecast=forecast,
        paths=paths,
    )


# --- Phase 2: AI Analyst ------------------------------------------------------


@router.post("/dealers/{dealer_id}/ai/insights", response_model=AIInsightsRead)
async def ai_insights(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> AIInsightsRead:
    """Run the AI analyst over the SAME bundle the lender package renders.
    Guardrailed to legitimate treasury/structuring advice only (never
    statement window-dressing). Nothing is persisted except tracked AI usage —
    accepting suggestions is a separate explicit call."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    bundle = (await _build_lender_package(db, dealer)).model_dump(mode="json")
    try:
        insights = await analyst.generate_insights(db, dealer, bundle)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"AI analyst returned unusable output: {exc}"
        ) from exc
    await db.commit()  # persist the tracked usage row
    return AIInsightsRead(**insights)


@router.post(
    "/dealers/{dealer_id}/ai/insights/accept",
    response_model=list[PlanActionRead],
    status_code=status.HTTP_201_CREATED,
)
async def accept_ai_insights(
    dealer_id: UUID, payload: AIInsightsAccept, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerPlanAction]:
    """Materialize accepted analyst suggestions as plan actions (status=todo,
    sort appended after the current plan, unpublished until plan publish)."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    max_sort = (
        await db.execute(
            select(func.max(DealerPlanAction.sort)).where(DealerPlanAction.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    base = int(max_sort) if max_sort is not None else 0
    created: list[DealerPlanAction] = []
    for offset, action in enumerate(payload.actions, start=1):
        row = DealerPlanAction(
            dealer_id=dealer.id,
            sort=base + offset,
            title=action.title,
            detail=action.rationale,
            category=action.category,
            owner=action.owner,
            timeline=action.timeline,
            expected_effect=action.expected_effect,
            status="todo",
        )
        db.add(row)
        created.append(row)
    await db.commit()
    for row in created:
        await db.refresh(row)
    return created


# --- Phase 3 Wave 1: accounts, rules, audit, lineage, add-back evidence ------


@router.get("/dealers/{dealer_id}/accounts", response_model=list[AccountRead])
async def list_accounts(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerAccount]:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return (
        (
            await db.execute(
                select(DealerAccount)
                .where(DealerAccount.dealer_id == dealer.id)
                .order_by(DealerAccount.created_at.asc())
            )
        )
        .scalars()
        .all()
    )


@router.patch("/dealers/{dealer_id}/accounts/{account_id}", response_model=AccountRead)
async def patch_account(
    dealer_id: UUID,
    account_id: UUID,
    payload: AccountPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerAccount:
    """Admin account edit. A role change flips role_set_by='admin' — from then
    on AI rematches never touch the role again (proposals only land in
    ai_proposed_role). Every change lands in the audit log."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    account = (
        await db.execute(
            select(DealerAccount).where(
                DealerAccount.id == account_id, DealerAccount.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found for this dealer")
    changes = payload.model_dump(exclude_unset=True)
    changes = {k: v for k, v in changes.items() if v is not None}
    if not changes:
        return account
    before = {k: getattr(account, k) for k in changes}
    before["role_set_by"] = account.role_set_by
    for k, v in changes.items():
        setattr(account, k, v)
    if "role" in changes:
        account.role_set_by = "admin"  # human correction wins, permanently
    await log_action(
        db, dealer.id, user, "account.update", "account",
        entity_id=account.id,
        before=before,
        after={**changes, "role_set_by": account.role_set_by},
    )
    await db.commit()
    await db.refresh(account)
    return account


@router.get("/dealers/{dealer_id}/rules", response_model=list[RuleRead])
async def list_rules(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerCategoryRule]:
    """The dealer's effective rule set: dealer-scoped rows plus global
    (dealer_id NULL) rows, active first, newest first."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    return (
        (
            await db.execute(
                select(DealerCategoryRule)
                .where(
                    (DealerCategoryRule.dealer_id == dealer.id)
                    | (DealerCategoryRule.dealer_id.is_(None))
                )
                .order_by(
                    DealerCategoryRule.active.desc(),
                    DealerCategoryRule.dealer_id.is_(None).asc(),
                    DealerCategoryRule.created_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/dealers/{dealer_id}/rules", response_model=RuleCreateResult, status_code=status.HTTP_201_CREATED
)
async def create_rule(
    dealer_id: UUID, payload: RuleCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> RuleCreateResult:
    """Create a dealer-scoped category rule (lowercase substring -> category).
    apply_retroactive=true also re-categorizes the dealer's existing matching
    events — EXCEPT admin-corrected ones (categorized_by='admin' is a human
    decision and is never overwritten) — and rebuilds the affected periods."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    pattern = payload.pattern.strip().lower()
    if not pattern:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "pattern must not be blank")
    rule = DealerCategoryRule(
        dealer_id=dealer.id,
        pattern=pattern[:160],
        category=payload.category.strip()[:48],
        created_by_user_id=user.id,
        active=True,
    )
    db.add(rule)
    await db.flush()
    await log_action(
        db, dealer.id, user, "rule.create", "rule",
        entity_id=rule.id,
        after={"pattern": rule.pattern, "category": rule.category, "retroactive": payload.apply_retroactive},
    )

    retro_applied = 0
    if payload.apply_retroactive:
        # Human correction wins: admin-corrected events are excluded.
        events = (
            await db.execute(
                select(DealerCashEvent).where(
                    DealerCashEvent.dealer_id == dealer.id,
                    func.lower(DealerCashEvent.description).contains(pattern),
                    (DealerCashEvent.categorized_by != "admin")
                    | (DealerCashEvent.categorized_by.is_(None)),
                )
            )
        ).scalars().all()
        scopes: dict[UUID | None, set[date]] = {}
        for event in events:
            if event.category == rule.category:
                continue
            event.category = rule.category
            base = event.flags if isinstance(event.flags, dict) else {}
            kept = {
                k: base[k]
                for k in ("recurring", "cadence", "recurrence_key", "irregular")
                if k in base
            }
            event.flags = {**flags_for(rule.category), **kept}
            event.categorized_by = "rule"
            scopes.setdefault(event.account_id, set()).add(event.period)
            retro_applied += 1
        for scope_account_id, scope_periods in scopes.items():
            await rebuild_periods(db, dealer.id, scope_periods, account_id=scope_account_id)
        if retro_applied:
            # Best-effort engine refresh — never fail the request on engine errors.
            try:
                await recompute_snapshot(db, dealer.id)
            except Exception:
                logger.exception(
                    "dealer-os: snapshot recompute failed after retroactive rule for %s", dealer.id
                )
    await db.commit()
    await db.refresh(rule)
    return RuleCreateResult(rule=RuleRead.model_validate(rule), retro_applied=retro_applied)


@router.delete("/dealers/{dealer_id}/rules/{rule_id}", response_model=RuleRead)
async def deactivate_rule(
    dealer_id: UUID, rule_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerCategoryRule:
    """Soft-delete: sets active=false (rules are never hard-deleted so the
    audit trail keeps pointing at real rows). Only dealer-scoped rules can be
    deactivated here — global rules are shared and managed elsewhere."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    rule = (
        await db.execute(
            select(DealerCategoryRule).where(
                DealerCategoryRule.id == rule_id, DealerCategoryRule.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if rule is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Rule not found for this dealer (global rules cannot be deactivated here)"
        )
    if rule.active:
        rule.active = False
        await log_action(
            db, dealer.id, user, "rule.deactivate", "rule",
            entity_id=rule.id,
            before={"active": True},
            after={"active": False},
        )
    await db.commit()
    await db.refresh(rule)
    return rule


@router.get("/dealers/{dealer_id}/audit", response_model=list[AuditRead])
async def list_audit_log(
    dealer_id: UUID,
    user: CurrentUser,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[DealerAuditLog]:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    return (
        (
            await db.execute(
                select(DealerAuditLog)
                .where(DealerAuditLog.dealer_id == dealer.id)
                .order_by(DealerAuditLog.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def _latest_snapshot_row(db: AsyncSession, dealer_id: UUID) -> DealerMetricSnapshot | None:
    return (
        await db.execute(
            select(DealerMetricSnapshot)
            .where(DealerMetricSnapshot.dealer_id == dealer_id)
            .order_by(DealerMetricSnapshot.as_of.desc(), DealerMetricSnapshot.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@router.get("/dealers/{dealer_id}/lineage", response_model=LineageRead)
async def read_lineage(
    dealer_id: UUID,
    user: CurrentUser,
    metric_key: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> LineageRead:
    """The latest snapshot's lineage edges (optionally one metric family),
    with cash_event refs resolved to description/amount so 'why is this number
    what it is' is answerable without extra round-trips."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    snapshot = await _latest_snapshot_row(db, dealer.id)
    if snapshot is None:
        return LineageRead()
    q = select(DealerMetricLineage).where(DealerMetricLineage.snapshot_id == snapshot.id)
    if metric_key:
        q = q.where(DealerMetricLineage.metric_key == metric_key)
    edges = (
        (await db.execute(q.order_by(DealerMetricLineage.metric_key.asc()).limit(limit)))
        .scalars()
        .all()
    )
    event_ids = [e.ref_id for e in edges if e.ref_kind == "cash_event" and e.ref_id is not None]
    events: dict[UUID, DealerCashEvent] = {}
    if event_ids:
        events = {
            ev.id: ev
            for ev in (
                await db.execute(select(DealerCashEvent).where(DealerCashEvent.id.in_(event_ids)))
            ).scalars()
        }
    out: list[LineageEdgeRead] = []
    for e in edges:
        item = LineageEdgeRead(
            metric_key=e.metric_key, ref_kind=e.ref_kind, ref_id=e.ref_id, period=e.period
        )
        ev = events.get(e.ref_id) if e.ref_kind == "cash_event" and e.ref_id is not None else None
        if ev is not None:
            item.description = ev.description
            item.amount = float(ev.amount)
            item.period = item.period or ev.period
        out.append(item)
    return LineageRead(snapshot_id=snapshot.id, as_of=snapshot.as_of, edges=out)


@router.get("/dealers/{dealer_id}/cash-events/{event_id}/feeds", response_model=EventFeedsRead)
async def cash_event_feeds(
    dealer_id: UUID, event_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> EventFeedsRead:
    """Reverse lineage for one statement line: which metrics reference it in
    the latest snapshot — directly (ref_kind='cash_event') and indirectly via
    add-backs sourced from it."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    event = (
        await db.execute(
            select(DealerCashEvent).where(
                DealerCashEvent.id == event_id, DealerCashEvent.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cash event not found for this dealer")
    snapshot = await _latest_snapshot_row(db, dealer.id)
    if snapshot is None:
        return EventFeedsRead(event_id=event.id)
    direct = (
        await db.execute(
            select(DealerMetricLineage.metric_key)
            .where(
                DealerMetricLineage.snapshot_id == snapshot.id,
                DealerMetricLineage.ref_kind == "cash_event",
                DealerMetricLineage.ref_id == event.id,
            )
            .distinct()
        )
    ).scalars().all()
    addback_ids = (
        await db.execute(
            select(DealerAddback.id).where(
                DealerAddback.dealer_id == dealer.id, DealerAddback.source_event_id == event.id
            )
        )
    ).scalars().all()
    via_addbacks: list[str] = []
    if addback_ids:
        via_addbacks = (
            await db.execute(
                select(DealerMetricLineage.metric_key)
                .where(
                    DealerMetricLineage.snapshot_id == snapshot.id,
                    DealerMetricLineage.ref_kind == "addback",
                    DealerMetricLineage.ref_id.in_(addback_ids),
                )
                .distinct()
            )
        ).scalars().all()
    return EventFeedsRead(
        event_id=event.id,
        snapshot_id=snapshot.id,
        metric_keys=sorted(direct),
        via_addbacks=sorted(via_addbacks),
    )


@router.get("/dealers/{dealer_id}/addbacks", response_model=list[AddbackRead])
async def list_addbacks(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerAddback]:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return (
        (
            await db.execute(
                select(DealerAddback)
                .where(DealerAddback.dealer_id == dealer.id)
                .order_by(DealerAddback.created_at.asc())
            )
        )
        .scalars()
        .all()
    )


@router.patch("/dealers/{dealer_id}/addbacks/{addback_id}", response_model=AddbackRead)
async def patch_addback(
    dealer_id: UUID,
    addback_id: UUID,
    payload: AddbackPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerAddback:
    """Admin add-back decision: status (verified/candidate/review/excluded)
    and/or evidence document link. Status changes stamp decided_by_user_id and
    a verified/excluded flip changes bankable EBITDA, so the snapshot is
    recomputed best-effort. Every change lands in the audit log."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    addback = (
        await db.execute(
            select(DealerAddback).where(
                DealerAddback.id == addback_id, DealerAddback.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if addback is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Add-back not found for this dealer")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return addback
    before = {k: getattr(addback, k) for k in changes}
    status_changed = False
    if "status" in changes and changes["status"] is not None:
        status_changed = changes["status"] != addback.status
        addback.status = changes["status"]
        addback.decided_by_user_id = user.id
    if "document_id" in changes:
        document_id = changes["document_id"]
        if document_id is not None:
            doc = (
                await db.execute(
                    select(DealerDocument).where(
                        DealerDocument.id == document_id, DealerDocument.dealer_id == dealer.id
                    )
                )
            ).scalar_one_or_none()
            if doc is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence document not found for this dealer")
        addback.document_id = document_id
    await log_action(
        db, dealer.id, user, "addback.decide", "addback",
        entity_id=addback.id, before=before, after=changes,
    )
    if status_changed:
        # Verified add-backs feed bankable EBITDA — refresh best-effort.
        try:
            await recompute_snapshot(db, dealer.id)
        except Exception:
            logger.exception(
                "dealer-os: snapshot recompute failed after addback decision for %s", dealer.id
            )
    await db.commit()
    await db.refresh(addback)
    return addback


# --- Phase 3 Wave 2: handoff, doc requests, progress, PDF --------------------


@router.post("/dealers/{dealer_id}/handoff", response_model=HandoffRead)
async def start_dealer_handoff(
    dealer_id: UUID, request: Request, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> HandoffRead:
    """Start (or return the existing) AI-underwriter funding file for this
    dealer. Idempotent: while dos_dealers.handoff_intake_id points at a live
    intake, that intake is returned; otherwise a new one is created through
    the same path the admin dealer-variant lead creation uses."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    intake_id = await handoff_service.start_handoff(db, dealer, user, request)
    await db.commit()
    return HandoffRead(intake_id=intake_id, url=handoff_service.handoff_url(intake_id))


@router.get("/dealers/{dealer_id}/handoff", response_model=HandoffRead)
async def get_dealer_handoff(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> HandoffRead:
    """The dealer's existing funding file, 404 when none was started (or the
    intake it pointed at has since been deleted)."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    intake_id = await handoff_service.find_existing_handoff(db, dealer)
    if intake_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No funding file has been started for this dealer")
    return HandoffRead(intake_id=intake_id, url=handoff_service.handoff_url(intake_id))


async def _load_doc_request(db: AsyncSession, dealer_id: UUID, req_id: UUID) -> DealerDocRequest:
    req = (
        await db.execute(
            select(DealerDocRequest).where(
                DealerDocRequest.id == req_id, DealerDocRequest.dealer_id == dealer_id
            )
        )
    ).scalar_one_or_none()
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document request not found for this dealer")
    return req


@router.get("/dealers/{dealer_id}/doc-requests", response_model=list[DocRequestRead])
async def list_doc_requests(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerDocRequest]:
    """All document requests for the dealer, open ones first (then newest).
    Dealer-visible by design — this IS the dealer's to-do list."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return (
        (
            await db.execute(
                select(DealerDocRequest)
                .where(DealerDocRequest.dealer_id == dealer.id)
                .order_by(
                    (DealerDocRequest.status == "open").desc(),
                    DealerDocRequest.created_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/dealers/{dealer_id}/doc-requests",
    response_model=DocRequestRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_doc_request(
    dealer_id: UUID, payload: DocRequestCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerDocRequest:
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    if payload.account_id is not None:
        account = (
            await db.execute(
                select(DealerAccount).where(
                    DealerAccount.id == payload.account_id, DealerAccount.dealer_id == dealer.id
                )
            )
        ).scalar_one_or_none()
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found for this dealer")
    req = DealerDocRequest(
        dealer_id=dealer.id,
        title=payload.title.strip(),
        kind=payload.kind,
        account_id=payload.account_id,
        due_on=payload.due_on,
        note=payload.note,
        status="open",
    )
    db.add(req)
    await db.flush()
    await log_action(
        db, dealer.id, user, "doc_request.create", "doc_request",
        entity_id=req.id,
        after={"title": req.title, "kind": req.kind, "due_on": req.due_on,
               "account_id": str(req.account_id) if req.account_id else None},
    )
    await db.commit()
    await db.refresh(req)
    return req


@router.patch("/dealers/{dealer_id}/doc-requests/{req_id}", response_model=DocRequestRead)
async def update_doc_request(
    dealer_id: UUID,
    req_id: UUID,
    payload: DocRequestPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerDocRequest:
    """Team edit: status / note / due_on, or manually pin the fulfilling
    document (which must belong to this dealer). Setting fulfilled_document_id
    without a status also flips the request to fulfilled."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    req = await _load_doc_request(db, dealer.id, req_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return req
    before = {k: getattr(req, k) for k in changes}
    if "fulfilled_document_id" in changes and changes["fulfilled_document_id"] is not None:
        await _load_document(db, dealer.id, changes["fulfilled_document_id"])  # 404 unless owned
        if "status" not in changes:
            changes["status"] = "fulfilled"
            before.setdefault("status", req.status)
    for k, v in changes.items():
        setattr(req, k, v)
    await log_action(
        db, dealer.id, user, "doc_request.update", "doc_request",
        entity_id=req.id, before=before, after=changes,
    )
    await db.commit()
    await db.refresh(req)
    return req


@router.delete("/dealers/{dealer_id}/doc-requests/{req_id}", response_model=DocRequestRead)
async def cancel_doc_request(
    dealer_id: UUID, req_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerDocRequest:
    """Soft-cancel: requests are never hard-deleted (audit trail keeps pointing
    at real rows) — DELETE flips status to cancelled."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    req = await _load_doc_request(db, dealer.id, req_id)
    if req.status != "cancelled":
        before = {"status": req.status}
        req.status = "cancelled"
        await log_action(
            db, dealer.id, user, "doc_request.cancel", "doc_request",
            entity_id=req.id, before=before, after={"status": "cancelled"},
        )
    await db.commit()
    await db.refresh(req)
    return req


@router.get("/dealers/{dealer_id}/progress", response_model=ProgressRead)
async def dealer_progress(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ProgressRead:
    """Month-over-month progress: the latest snapshot vs the closest snapshot
    at least 21 days older (fallback: the two latest; a single snapshot
    compares with itself, all deltas zero). Deterministic strings via the pure
    services.progress engine; actions_completed lists plan actions marked done
    between the two snapshots."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    snaps = (
        (
            await db.execute(
                select(DealerMetricSnapshot)
                .where(DealerMetricSnapshot.dealer_id == dealer.id)
                .order_by(DealerMetricSnapshot.as_of.desc(), DealerMetricSnapshot.created_at.desc())
                .limit(60)
            )
        )
        .scalars()
        .all()
    )
    if not snaps:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No metric snapshot exists for this dealer yet — import financials or "
            "POST /dealers/{id}/recompute first",
        )
    latest = snaps[0]
    cutoff = latest.as_of - timedelta(days=21)
    baseline = next((s for s in snaps[1:] if s.as_of <= cutoff), None)
    if baseline is None:
        baseline = snaps[1] if len(snaps) > 1 else latest
    data = compute_progress(
        baseline.as_of,
        latest.as_of,
        baseline.metrics or {},
        latest.metrics or {},
        float(baseline.score) if baseline.score is not None else None,
        float(latest.score) if latest.score is not None else None,
    )
    actions_completed = (
        (
            await db.execute(
                select(DealerPlanAction.title)
                .where(
                    DealerPlanAction.dealer_id == dealer.id,
                    DealerPlanAction.status == "done",
                    DealerPlanAction.updated_at >= baseline.created_at,
                    DealerPlanAction.updated_at <= latest.created_at,
                )
                .order_by(DealerPlanAction.updated_at.asc())
            )
        )
        .scalars()
        .all()
        if baseline.id != latest.id
        else []
    )
    return ProgressRead(**data, actions_completed=list(actions_completed))


@router.get("/dealers/{dealer_id}/lender-package.pdf")
async def lender_package_pdf(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> StreamingResponse:
    """Print-ready PDF of the SAME _build_lender_package bundle the JSON
    endpoint serves (one code path for the facts). 501 when the runtime lacks
    weasyprint's native stack — the JSON endpoint remains the fallback."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    bundle = (await _build_lender_package(db, dealer)).model_dump(mode="json")
    html_doc = report_pdf.build_html(bundle)
    try:
        pdf_bytes = await run_in_threadpool(report_pdf.render_pdf, html_doc)
    except report_pdf.PDFUnavailableError as exc:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "PDF rendering is unavailable in this runtime — use GET "
            "/dealers/{id}/lender-package (JSON) instead",
        ) from exc
    filename = storage.safe_filename(f"lender-package-{dealer.name}.pdf") or "lender-package.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
