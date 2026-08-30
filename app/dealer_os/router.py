"""Dealer OS API — everything under /api/v1/dealer-os/* (isolation contract).

Stream 1 surface: team console CRUD + the per-dealer Targets & Settings
endpoints (AI propose / admin override, override-always-wins). Engines,
ledger, plan, forecast, messaging land in Streams 2-5 on this same router.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import secrets
import zipfile
from dataclasses import asdict
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, exists, func, not_, select, or_
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.db import get_db
from app.deps import CurrentUser
from app.services.provider_secrets import provider_settings_status
from app.config import get_settings
from app.models.user import User
from app.models.application_profile import ApplicationTaxonomyEntry, PlaidAssetReport
from app.models.booking_settings import BookingSettings
from app.models.booking_notification import BookingNotification, BookingNotificationReminder
from app.models.event import CalendarEvent
from app.models.notification import Notification
from app.services import booking_notify, booking_reminders
from app.services.notifications import notify_users
from app.services.team_calendar import lock_calendar_owner, team_booking_settings
from app.services import plaid_lifecycle
from app.services.email import ses_client
from app.services.google import calendar_sync
from app.services import clerk as clerk_service
from app.enums import CalendarEventKind, CalendarEventSource, CalendarEventStatus, Role

# READ-ONLY reuse: bucket models are queried/appended, never altered; the
# analysis-version constant keeps cache lookups aligned with the bucket AI.
from app.models.bucket import Bucket, BucketFile, BucketFileAnalysis, BucketRequestedDocument, BucketUploadLink
from app.services.bucket_ai import CURRENT_FILE_ANALYSIS_VERSION

from .deps import (
    load_dealer,
    require_dealer,
    require_super_admin,
    require_team,
    require_team_or_dealer,
    require_team_or_dealer_or_rep,
    require_team_or_rep,
    resolve_dealer_scope,
)
from .models import (
    DealerRepLead,
    DealerApplicationProfile,
    ContractTemplate,
    ContractTemplateVersion,
    ContractDocument,
    ContractPackage,
    ContractPackageItem,
    ContractEnvelope,
    ContractEnvelopeDocument,
    DealerSmsConsent,
    DealerAIMessage,
    DealerRepAppointment,
    DealerRepContact,
    DealerRepContactAssignment,
    DealerApplicationContact,
    DealerFieldDeskProfile,
    DealerRepContactShare,
    DealerRepInboxMessage,
    DealerRepInboxThread,
    DealerUnderwritingReviewPreference,
    MESSAGE_CHANNELS,
    CLIENT_VISIBLE_CHANNELS,
    REP_LEAD_TERMINAL,
    DealerPlaidItem,
    DealerAccount,
    DealerAddback,
    DealerAlert,
    DealerAuditLog,
    DealerBusiness,
    DealerApplicationPreScreen,
    DealerApplicationRecommendation,
    DealerProgramRuleResolution,
    DealerCashEvent,
    DealerCategoryRule,
    DealerCreditProfile,
    DealerDebt,
    DealerDocRequest,
    DealerDocument,
    DealerFinancialPeriod,
    DealerGroup,
    DealerMessage,
    DealerMessageSeen,
    DealerMetricLineage,
    DealerMetricSnapshot,
    DealerMetricTarget,
    DealerOwner,
    DealerPaymentShift,
    DealerPlanAction,
    DealerPlanComment,
    DealerProductCatalog,
    DealerProgramSetting,
    DealerSession,
    DealerSourceConnection,
    DealerTaxFiling,
)
from .schemas import (
    AccountPatch,
    AccountRead,
    AddbackPatch,
    AddbackRead,
    AIInsightsAccept,
    AIInsightsRead,
    AlertRead,
    ApplicationPreScreenPatch,
    ApplicationPreScreenRead,
    ApplicationRecommendationRead,
    ApplicationRecommendationResponse,
    ProgramExceptionRequest,
    ProgramExceptionDecision,
    ProgramRuleResolutionRead,
    UnderwritingResolutionRead,
    AuditRead,
    BucketFileItem,
    BucketSearchItem,
    BusinessCreditRead,
    CashEventPatch,
    CashEventRead,
    CashEventSearchRead,
    CashEventSearchRow,
    CashImport,
    CashImportResult,
    RepProductionRead,
    RepProduction,
    RepProductionInsights,
    RepAmountMetric,
    RepCategoryMetric,
    RepLocationMetric,
    RepFileRow,
    CreditInviteRequest,
    CreditInviteResult,
    BulkCreditInviteRequest,
    BulkCreditInviteResult,
    OwnerCreditInviteResult,
    CreditRead,
    CreditUpsert,
    DealerCreate,
    AIThreadAsk,
    RoomFeaturesRead,
    RoomSignableRead,
    RoomSignRequest,
    RoomSignResult,
    ContractDocRead,
    ConvertToAuditRequest,
    ConvertToAuditResult,
    RoomContractRead,
    RoomContractSignRequest,
    ContractGenerateResult,
    ContractTemplateMapPatch,
    ContractTemplateRead,
    ContractTemplateVersionRead,
    ContractTemplateVersionCatalogRead,
    ContractPackageRead,
    ContractPackageItemRead,
    ContractPackageWrite,
    ContractEnvelopeRead,
    ContractEnvelopeDocumentRead,
    ContractEnvelopeGenerateRequest,
    ContractEnvelopeVoidRequest,
    ContractEnvelopeAcknowledgeRequest,
    ContractEnvelopeSignRequest,
    DeliveryRowRead,
    DecisionRead,
    UnreadSummary,
    PublicPlaidResult,
    PublicPlaidItemRead,
    RoomPasscode,
    RoomPlaidExchange,
    RoomPlaidUpdateLink,
    ClientRequestResult,
    ClientRequestSend,
    BankEvidenceRead,
    BankUploadRequestResult,
    SignatureRequestSend,
    AIThreadMessage,
    MessageEdit,
    SmsConsentIn,
    SmsConsentOut,
    SmsDisclosureOut,
    DealerInvite,
    DealerInviteResult,
    DealerIntegrationStatus,
    DealerListItem,
    DealerPortfolioItem,
    DealerPortfolioPage,
    FieldDeskGlobalSearchItem,
    FieldDeskGlobalSearchRead,
    DealerRead,
    DealerUpdate,
    DealerWorkflowSettingsPatch,
    DebtCreate,
    DebtDraftResult,
    PlaidExchange,
    PlaidAssetReportCreate,
    PlaidAssetReportRead,
    PlaidItemPatch,
    PlaidItemRead,
    PlaidLinkTokenRead,
    PlaidRefreshResult,
    PlaidStateRead,
    PlaidUpdateLinkRequest,
    DscrAddbackRead,
    DscrComponentAction,
    DscrComponentRead,
    DscrCompositionRead,
    DscrImprovementRead,
    DscrNetPoint,
    DscrNumeratorRead,
    DscrResultsRead,
    DscrSuggestionRead,
    McaReadinessRead,
    RefiDebtRead,
    RefinanceRead,
    RefinanceScenarioRead,
    RefinanceSimulateRead,
    RefinanceSimulateRequest,
    RefiObservedRead,
    RefiProgramRead,
    DebtPatch,
    DebtRead,
    DocRequestCreate,
    DocRequestPatch,
    DocRequestRead,
    DocumentCoverageRead,
    DocumentRead,
    DocumentReject,
    DocumentUrlRead,
    EventFeedsRead,
    ForecastRead,
    FundingPlanRead,
    FundingRangeRead,
    GlobalAlertRead,
    GroupCreate,
    GroupPatch,
    GroupRead,
    HandoffRead,
    HealthRead,
    IrregularEventRead,
    LenderPackageRead,
    LineageEdgeRead,
    LineageRead,
    MessageCreate,
    MessageRead,
    OwnerCreate,
    OwnerPatch,
    OwnerRead,
    PortfolioOwnerSummary,
    PathFundingRead,
    PathsRead,
    PaymentShiftCreate,
    PaymentShiftPatch,
    PaymentShiftRead,
    PaymentTimingRead,
    PeriodRead,
    PeriodUpsert,
    PipelineStatusRead,
    PlanActionCreate,
    PlanActionRead,
    PlanActionUpdate,
    PlanCommentCreate,
    PlanCommentRead,
    PlanRespond,
    RecurrenceMark,
    ProgramSettingRead,
    ProgramSettingsRead,
    ProgramSettingUpdate,
    ProgressRead,
    PublicConsentResult,
    PublicConsentSubmit,
    PublicConsentView,
    RecurringGroupRead,
    RecurringRead,
    RuleCreate,
    RuleCreateResult,
    RuleRead,
    SessionCreate,
    SessionRead,
    SimulateApplied,
    SimulateCurveRead,
    SimulateMetrics,
    SimulatePathRead,
    SimulateRead,
    SimulateRequest,
    SnapshotRead,
    SoftPullRequest,
    SoftPullResult,
    TargetOverride,
    TargetRead,
    TaxFilingUpsert,
    TaxYearRead,
    TimingOptimizeRead,
    TradelineRead,
    VendorAccountRead,
    VendorCategoryPatch,
    VendorDetailRead,
    VendorReportRead,
    VendorRowRead,
    BankConsentState,
    BankConsentGrant,
    RoomBankConsentGrant,
    ApplicationProfilePatch,
    ApplicationProfileRead,
    ApplicationHumanReviewPatch,
    ApplicationFinalizationPatch,
    SubmissionReadinessRead,
    BookingAvailabilityRead,
    BookingAvailabilitySlot,
    ContactCardRead,
    ContactCardProgramPdfRead,
    ContactShareCreate,
    ContactShareRead,
    ProgramPdfAttachmentRead,
    RepAppointmentCreate,
    RepAppointmentCancel,
    RepAppointmentOutcomePatch,
    RepAppointmentPatch,
    RepAppointmentRead,
    RepInboxComposeResult,
    RepInboxMessageCreate,
    RepInboxMessageRead,
    RepInboxThreadCreate,
    RepInboxThreadRead,
    UnderwritingReviewPreferenceCreate,
    UnderwritingReviewPreferenceBook,
    UnderwritingReviewPreferenceRead,
)
from .services import analyst, application_prescreen, application_taxonomy, archive, bucket_ingest, buckets_link, business_credit as business_credit_svc, vendors, handoff as handoff_service, recurrence, report_pdf, rollups, storage
from .services.audit import log_action
from .services.progress import compute_progress
from .services.engines import compute_metrics, load_metric_inputs, recompute_snapshot
from .services.extract import _persist_plan, _route_tax_years, apply_extraction, extract_document
from .services.forecast import compute_forecast
from .services.normalize import (
    classify_with_rules,
    flags_for,
    load_active_rules,
    period_of,
    rebuild_periods,
)
from .services.paths import (
    DEFAULT_REQUIREMENTS,
    DEFAULT_SIZING,
    PATH_KEYS,
    fundability_verdict as paths_service_fundability,
    PATH_LABELS,
    compute_ladder,
    compute_paths,
    merged_settings,
    path_model,
    requirements_for_amount,
    size_program,
    validate_requirements,
    validate_sizing,
)
from .services import bank_consent, balance_health, client_room, consent_delivery, contract_fill, contract_packages, contract_registry, contract_sign, decision, delivery_log, file_chat, qc_master_application, rep_workflows, routing_resolution, sms_consent as sms_consent_svc, mca_readiness as mca_svc, payment_timing, plaid_client, plaid_sync, refinance as refinance_svc, simulate, timing_optimizer
from .services.targets import propose_targets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dealer-os", tags=["dealer-os"])

_TRAINING_LIVE_ACTION_HEADER = "x-qc-training-live-action"


async def _require_training_live_action(
    db: AsyncSession,
    *,
    dealer: DealerBusiness,
    user: User,
    request: Request,
    action: str,
    provider: str,
    recipient: str | None,
    effect: str,
) -> None:
    """Require an explicit, per-request confirmation for training side effects.

    Navigation and internal file work stay frictionless. Calls that can contact
    a person, create a third-party record, or incur provider cost must be
    acknowledged by the super-admin and are recorded in the existing audit
    stream. The endpoint's own transaction decides whether the audit row and
    side effect commit together.
    """
    if not dealer.is_training:
        return
    require_super_admin(user)
    if request.headers.get(_TRAINING_LIVE_ACTION_HEADER, "").strip().lower() != "confirmed":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "training_live_action_confirmation_required",
                "action": action,
                "provider": provider,
                "recipient": recipient,
                "effect": effect,
            },
        )
    await log_action(
        db,
        dealer.id,
        user,
        "training.live_action_confirmed",
        "dealer",
        entity_id=dealer.id,
        after={
            "action": action,
            "provider": provider,
            "recipient": recipient,
            "effect": effect,
        },
    )


async def _load_visible_dealer(
    db: AsyncSession,
    dealer_id: UUID,
    user: User,
) -> DealerBusiness:
    """Load a file while keeping Training records super-admin-only."""
    dealer = await load_dealer(db=db, dealer_id=dealer_id)
    if dealer.is_training and user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    return dealer


@router.get("/dealers", response_model=list[DealerListItem])
async def list_dealers(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[DealerListItem]:
    # Team sees the whole book; a DEALER login sees only businesses linked to it
    # (dealer_user_id) — this is what powers the self-serve "My business" view;
    # a FIELD_REP sees only the files they own. This is the ONLY place a
    # collection is role-filtered, so a missed branch here is a book-wide leak.
    require_team_or_dealer_or_rep(user)
    # 0120: one outerjoin carries the client-file (group) name onto each row.
    stmt = (
        select(DealerBusiness, DealerGroup.name)
        .outerjoin(DealerGroup, DealerBusiness.group_id == DealerGroup.id)
        .where(
            DealerBusiness.archived_at.is_(None),
            DealerBusiness.is_training.is_(False),
        )
        .order_by(DealerBusiness.created_at.desc())
    )
    if user.role == Role.DEALER:
        stmt = stmt.where(DealerBusiness.dealer_user_id == user.id)
    elif user.role == Role.FIELD_REP:
        stmt = stmt.where(DealerBusiness.owner_user_id == user.id)
    pairs = (await db.execute(stmt)).all()
    dealers = [d for d, _ in pairs]
    group_names = {d.id: name for d, name in pairs}
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
    # Verification state per row, for the portfolio's Bank and Credit chips.
    # Batched with the rest: the list already avoids per-dealer queries and a
    # rep with forty files must not turn one screen into eighty round trips.
    ids = [d.id for d, _ in pairs]
    linked_ids: set = set()
    pulled_ids: set = set()
    if ids:
        linked_ids = {
            did
            for (did,) in (
                await db.execute(
                    select(DealerPlaidItem.dealer_id)
                    .where(
                        DealerPlaidItem.dealer_id.in_(ids),
                        DealerPlaidItem.status == "active",
                        DealerPlaidItem.environment == plaid_client.environment(),
                    )
                    .distinct()
                )
            ).all()
        }
        pulled_ids = {
            did
            for (did,) in (
                await db.execute(
                    select(DealerOwner.dealer_id)
                    .where(
                        DealerOwner.dealer_id.in_(ids),
                        DealerOwner.credit_pulled_at.is_not(None),
                    )
                    .distinct()
                )
            ).all()
        }

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
        item.group_name = group_names.get(d.id)
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
        item.bank_linked = d.id in linked_ids
        item.credit_returned = d.id in pulled_ids
        item.verified = item.bank_linked and item.credit_returned
        out.append(item)
    return out


@router.get("/portfolio", response_model=DealerPortfolioPage)
async def dealer_portfolio(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    q: str = Query(default="", max_length=160),
    stage: str = Query(default="all", pattern="^(all|awaiting|verified|contract)$"),
    bank: str = Query(default="all", pattern="^(all|linked|awaiting)$"),
    credit: str = Query(default="all", pattern="^(all|returned|awaiting)$"),
    archive: str = Query(default="active", pattern="^(active|archived|all)$"),
    lifecycle: str = Query(default="active", pattern="^(active|draft|all)$"),
    training: str = Query(default="exclude", pattern="^(exclude|only|all)$"),
    sort_by: str = Query(default="updated_at", pattern="^(created_at|updated_at)$"),
    sort_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=10, ge=1, le=10),
    offset: int = Query(default=0, ge=0),
) -> DealerPortfolioPage:
    """Rep portfolio with filtering and pagination applied before rows leave Postgres."""
    require_team_or_rep(user)
    bank_linked = exists(
        select(DealerPlaidItem.id).where(
            DealerPlaidItem.dealer_id == DealerBusiness.id,
            DealerPlaidItem.status == "active",
            DealerPlaidItem.environment == plaid_client.environment(),
        )
    )
    required_credit_owner = exists(
        select(DealerOwner.id).where(
            DealerOwner.dealer_id == DealerBusiness.id,
            DealerOwner.ownership_pct >= Decimal("20.00"),
        )
    )
    pending_credit_owner = exists(
        select(DealerOwner.id).where(
            DealerOwner.dealer_id == DealerBusiness.id,
            DealerOwner.ownership_pct >= Decimal("20.00"),
            DealerOwner.credit_pulled_at.is_(None),
        )
    )
    credit_returned = and_(required_credit_owner, not_(pending_credit_owner))
    filters = []
    if training != "exclude" and user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")
    if training == "exclude":
        filters.append(DealerBusiness.is_training.is_(False))
    elif training == "only":
        filters.append(DealerBusiness.is_training.is_(True))
    if user.role == Role.FIELD_REP:
        filters.extend(
            [DealerBusiness.owner_user_id == user.id, DealerBusiness.archived_at.is_(None)]
        )
    elif archive == "archived":
        if user.role != Role.SUPER_ADMIN:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")
        filters.append(DealerBusiness.archived_at.is_not(None))
    elif archive == "all":
        if user.role != Role.SUPER_ADMIN:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")
    else:
        filters.append(DealerBusiness.archived_at.is_(None))
    if lifecycle != "all":
        filters.append(DealerBusiness.application_lifecycle == lifecycle)

    needle = q.strip().lower()
    if needle:
        like = f"%{needle}%"
        owner_match = exists(
            select(DealerOwner.id).where(
                DealerOwner.dealer_id == DealerBusiness.id,
                or_(
                    func.lower(func.concat(DealerOwner.first_name, " ", DealerOwner.last_name)).like(like),
                    func.lower(func.coalesce(DealerOwner.email, "")).like(like),
                ),
            )
        )
        filters.append(
            or_(
                func.lower(DealerBusiness.name).like(like),
                func.lower(func.coalesce(DealerBusiness.address, "")).like(like),
                func.lower(func.coalesce(DealerBusiness.city, "")).like(like),
                func.lower(func.coalesce(DealerBusiness.state, "")).like(like),
                owner_match,
            )
        )
    if stage == "awaiting":
        filters.append(not_(and_(bank_linked, credit_returned)))
    elif stage == "verified":
        filters.extend([bank_linked, credit_returned])
    elif stage == "contract":
        filters.append(DealerBusiness.status == "complete")
    if bank == "linked":
        filters.append(bank_linked)
    elif bank == "awaiting":
        filters.append(not_(bank_linked))
    if credit == "returned":
        filters.append(credit_returned)
    elif credit == "awaiting":
        filters.append(not_(credit_returned))

    total = int(
        (await db.execute(select(func.count()).select_from(DealerBusiness).where(*filters))).scalar_one()
    )
    order_col = DealerBusiness.created_at if sort_by == "created_at" else DealerBusiness.updated_at
    order = order_col.asc() if sort_dir == "asc" else order_col.desc()
    dealers = (
        await db.execute(
            select(DealerBusiness).where(*filters).order_by(order, DealerBusiness.id).limit(limit).offset(offset)
        )
    ).scalars().all()
    ids = [row.id for row in dealers]
    owner_rows = (
        await db.execute(
            select(DealerOwner).where(DealerOwner.dealer_id.in_(ids)).order_by(DealerOwner.created_at)
        )
    ).scalars().all() if ids else []
    owners_by_dealer: dict[UUID, list[PortfolioOwnerSummary]] = {dealer_id: [] for dealer_id in ids}
    for owner in owner_rows:
        owners_by_dealer[owner.dealer_id].append(
            PortfolioOwnerSummary(
                id=owner.id,
                name=owner.full_name,
                email=owner.email,
                ownership_pct=float(owner.ownership_pct) if owner.ownership_pct is not None else None,
            )
        )
    linked_ids = {
        row[0]
        for row in (
            await db.execute(
                select(DealerPlaidItem.dealer_id)
                .where(
                    DealerPlaidItem.dealer_id.in_(ids),
                    DealerPlaidItem.status == "active",
                    DealerPlaidItem.environment == plaid_client.environment(),
                )
                .distinct()
            )
        ).all()
    } if ids else set()
    returned_ids: set[UUID] = set()
    if ids:
        required_status = (
            await db.execute(
                select(
                    DealerOwner.dealer_id,
                    func.count(DealerOwner.id),
                    func.count(DealerOwner.credit_pulled_at),
                )
                .where(
                    DealerOwner.dealer_id.in_(ids),
                    DealerOwner.ownership_pct >= Decimal("20.00"),
                )
                .group_by(DealerOwner.dealer_id)
            )
        ).all()
        returned_ids = {
            dealer_id
            for dealer_id, required_count, completed_count in required_status
            if required_count > 0 and completed_count == required_count
        }
    items: list[DealerPortfolioItem] = []
    for dealer in dealers:
        item = DealerPortfolioItem.model_validate(dealer)
        item.owners = owners_by_dealer.get(dealer.id, [])
        item.bank_linked = dealer.id in linked_ids
        item.credit_returned = dealer.id in returned_ids
        item.verified = item.bank_linked and item.credit_returned
        items.append(item)
    return DealerPortfolioPage(items=items, total=total, limit=limit, offset=offset)


def _global_search_file_access_filter(user: User):
    if user.role == Role.FIELD_REP:
        return and_(
            DealerBusiness.owner_user_id == user.id,
            DealerBusiness.archived_at.is_(None),
            DealerBusiness.is_training.is_(False),
        )
    return and_(
        DealerBusiness.archived_at.is_(None),
        DealerBusiness.is_training.is_(False),
    )


def _global_search_contact_access_filter(user: User):
    if user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        return True
    return or_(
        DealerRepContact.owner_user_id == user.id,
        exists(
            select(DealerRepContactAssignment.id).where(
                DealerRepContactAssignment.contact_id == DealerRepContact.id,
                DealerRepContactAssignment.user_id == user.id,
            )
        ),
    )


def _global_search_appointment_access_filter(user: User):
    if user.role == Role.FIELD_REP:
        return DealerRepAppointment.booked_by_user_id == user.id
    return True


def _search_context(*values: str | None) -> str | None:
    parts = [str(value).strip() for value in values if value and str(value).strip()]
    return " · ".join(dict.fromkeys(parts)) or None


@router.get("/global-search", response_model=FieldDeskGlobalSearchRead)
async def field_desk_global_search(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    q: str = Query(min_length=2, max_length=160),
    limit: int = Query(default=24, ge=4, le=40),
) -> FieldDeskGlobalSearchRead:
    """Search every Field Desk surface without widening any access boundary."""
    require_team_or_rep(user)
    needle = q.strip().lower()
    if len(needle) < 2:
        return FieldDeskGlobalSearchRead(query=needle)

    like = f"%{needle}%"
    per_kind = max(3, min(10, (limit + 3) // 4))
    owner_match = exists(
        select(DealerOwner.id).where(
            DealerOwner.dealer_id == DealerBusiness.id,
            or_(
                func.lower(func.concat(DealerOwner.first_name, " ", DealerOwner.last_name)).like(like),
                func.lower(func.coalesce(DealerOwner.email, "")).like(like),
                func.lower(func.coalesce(DealerOwner.phone, "")).like(like),
            ),
        )
    )
    file_rows = list(
        (
            await db.execute(
                select(DealerBusiness)
                .where(
                    _global_search_file_access_filter(user),
                    or_(
                        func.lower(DealerBusiness.name).like(like),
                        func.lower(func.coalesce(DealerBusiness.legal_name, "")).like(like),
                        func.lower(func.coalesce(DealerBusiness.case_ref, "")).like(like),
                        func.lower(func.coalesce(DealerBusiness.email, "")).like(like),
                        func.lower(func.coalesce(DealerBusiness.phone, "")).like(like),
                        func.lower(func.coalesce(DealerBusiness.address, "")).like(like),
                        func.lower(func.coalesce(DealerBusiness.city, "")).like(like),
                        func.lower(func.coalesce(DealerBusiness.state, "")).like(like),
                        func.lower(func.coalesce(DealerBusiness.zip, "")).like(like),
                        owner_match,
                    ),
                )
                .order_by(DealerBusiness.updated_at.desc())
                .limit(per_kind)
            )
        ).scalars().all()
    )

    contact_rows = (
        await db.execute(
            select(DealerRepContact, DealerBusiness)
            .outerjoin(DealerBusiness, DealerBusiness.id == DealerRepContact.dealer_id)
            .where(
                _global_search_contact_access_filter(user),
                or_(DealerBusiness.id.is_(None), DealerBusiness.is_training.is_(False)),
                or_(
                    func.lower(DealerRepContact.full_name).like(like),
                    func.lower(func.coalesce(DealerRepContact.company, "")).like(like),
                    func.lower(func.coalesce(DealerRepContact.email, "")).like(like),
                    func.lower(func.coalesce(DealerRepContact.phone_e164, "")).like(like),
                    func.lower(func.coalesce(DealerBusiness.name, "")).like(like),
                    func.lower(func.coalesce(DealerBusiness.address, "")).like(like),
                ),
            )
            .order_by(DealerRepContact.updated_at.desc())
            .limit(per_kind)
        )
    ).all()

    thread_rows = (
        await db.execute(
            select(DealerRepInboxThread, DealerRepContact, DealerBusiness)
            .outerjoin(DealerRepContact, DealerRepContact.id == DealerRepInboxThread.contact_id)
            .outerjoin(DealerBusiness, DealerBusiness.id == DealerRepInboxThread.dealer_id)
            .where(
                DealerRepInboxThread.owner_user_id == user.id,
                or_(DealerBusiness.id.is_(None), DealerBusiness.is_training.is_(False)),
                or_(
                    func.lower(DealerRepInboxThread.subject).like(like),
                    func.lower(func.coalesce(DealerRepContact.full_name, "")).like(like),
                    func.lower(func.coalesce(DealerRepContact.company, "")).like(like),
                    func.lower(func.coalesce(DealerRepContact.email, "")).like(like),
                    func.lower(func.coalesce(DealerRepContact.phone_e164, "")).like(like),
                    func.lower(func.coalesce(DealerBusiness.name, "")).like(like),
                    func.lower(func.coalesce(DealerBusiness.address, "")).like(like),
                ),
            )
            .order_by(
                DealerRepInboxThread.last_message_at.desc().nullslast(),
                DealerRepInboxThread.updated_at.desc(),
            )
            .limit(per_kind)
        )
    ).all()

    appointment_rows = (
        await db.execute(
            select(DealerRepAppointment, DealerBusiness)
            .outerjoin(DealerBusiness, DealerBusiness.id == DealerRepAppointment.dealer_id)
            .where(
                _global_search_appointment_access_filter(user),
                or_(DealerBusiness.id.is_(None), DealerBusiness.is_training.is_(False)),
                or_(
                    func.lower(DealerRepAppointment.invitee_name).like(like),
                    func.lower(func.coalesce(DealerRepAppointment.invitee_email, "")).like(like),
                    func.lower(func.coalesce(DealerRepAppointment.invitee_phone, "")).like(like),
                    func.lower(func.coalesce(DealerRepAppointment.company, "")).like(like),
                    func.lower(func.coalesce(DealerRepAppointment.full_address, "")).like(like),
                    func.lower(DealerRepAppointment.title).like(like),
                    func.lower(func.coalesce(DealerRepAppointment.program_name, "")).like(like),
                    func.lower(func.coalesce(DealerBusiness.name, "")).like(like),
                    func.lower(func.coalesce(DealerBusiness.address, "")).like(like),
                ),
            )
            .order_by(DealerRepAppointment.updated_at.desc())
            .limit(per_kind)
        )
    ).all()

    items: list[FieldDeskGlobalSearchItem] = []
    for dealer in file_rows:
        address = _search_context(dealer.address, dealer.city, dealer.state, dealer.zip)
        items.append(
            FieldDeskGlobalSearchItem(
                id=dealer.id,
                kind="file",
                title=dealer.name,
                subtitle=_search_context(dealer.case_ref, address),
                context=_search_context(dealer.email, dealer.phone),
                href=f"/applications/{dealer.id}",
                dealer_id=dealer.id,
                occurred_at=dealer.updated_at,
            )
        )
    for contact, dealer in contact_rows:
        items.append(
            FieldDeskGlobalSearchItem(
                id=contact.id,
                kind="contact",
                title=contact.full_name,
                subtitle=_search_context(contact.company, dealer.name if dealer else None),
                context=_search_context(contact.email, contact.phone_e164),
                href=f"/contacts/{contact.id}",
                dealer_id=contact.dealer_id,
                occurred_at=contact.last_activity_at or contact.updated_at,
            )
        )
    for thread, contact, dealer in thread_rows:
        channel_label = "SMS conversation" if thread.channel == "sms" else "Email conversation"
        items.append(
            FieldDeskGlobalSearchItem(
                id=thread.id,
                kind="sms" if thread.channel == "sms" else "email",
                title=contact.full_name if contact else thread.subject,
                subtitle=_search_context(channel_label, contact.company if contact else None),
                context=_search_context(thread.subject, dealer.name if dealer else None),
                href=f"/inbox?thread={thread.id}",
                dealer_id=thread.dealer_id,
                occurred_at=thread.last_message_at or thread.updated_at,
            )
        )
    for appointment, dealer in appointment_rows:
        try:
            appointment_date = appointment.starts_at.astimezone(
                ZoneInfo(appointment.timezone)
            ).date()
        except (KeyError, ValueError):
            appointment_date = appointment.starts_at.date()
        items.append(
            FieldDeskGlobalSearchItem(
                id=appointment.id,
                kind="booking",
                title=appointment.invitee_name,
                subtitle=_search_context(appointment.program_name, appointment.company, dealer.name if dealer else None),
                context=_search_context(appointment.full_address, appointment.title),
                href=f"/calendar?appointment={appointment.id}&date={appointment_date.isoformat()}",
                dealer_id=appointment.dealer_id,
                occurred_at=appointment.starts_at,
            )
        )
    return FieldDeskGlobalSearchRead(query=needle, items=items[:limit])


@router.post("/dealers/{dealer_id}/archive", response_model=DealerRead)
async def archive_dealer(
    dealer_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerRead:
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    if dealer.archived_at is None:
        dealer.archived_at = datetime.now(timezone.utc)
        dealer.archived_by_user_id = user.id
        await log_action(
            db, dealer.id, user, "dealer.archived", "dealer", entity_id=dealer.id,
            after={"archived_at": dealer.archived_at.isoformat()},
        )
        await db.commit()
        await db.refresh(dealer)
    return await _dealer_read(db, dealer)


@router.post("/dealers/{dealer_id}/restore", response_model=DealerRead)
async def restore_dealer(
    dealer_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerRead:
    require_super_admin(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    if dealer.archived_at is not None:
        before = {"archived_at": dealer.archived_at.isoformat()}
        dealer.archived_at = None
        dealer.archived_by_user_id = None
        await log_action(
            db, dealer.id, user, "dealer.restored", "dealer", entity_id=dealer.id,
            before=before, after={"archived_at": None},
        )
        await db.commit()
        await db.refresh(dealer)
    return await _dealer_read(db, dealer)


@router.get("/integrations/status", response_model=DealerIntegrationStatus)
async def dealer_integration_status(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntegrationStatus:
    """Credential-presence diagnostics only; secrets and bureau payloads never leave the server."""
    require_super_admin(user)
    settings = get_settings()
    private_key = settings.isoftpull_private_key or settings.isoftpull_api_key
    isoftpull_ready = bool(private_key and settings.isoftpull_public_key)
    plaid_ready = plaid_client.enabled()
    try:
        plaid_env = plaid_client.environment()
        plaid_env_error = None
    except plaid_client.PlaidUnavailable as exc:
        plaid_env = "invalid"
        plaid_env_error = str(exc)
    sms_status = consent_delivery.sms_provider_status()
    latest_sms_failure = (
        await db.execute(
            select(DealerRepInboxMessage.provider_error)
            .where(
                DealerRepInboxMessage.channel == "sms",
                DealerRepInboxMessage.provider == str(sms_status["provider"]),
                DealerRepInboxMessage.delivery_status.in_(["failed", "undelivered"]),
                DealerRepInboxMessage.provider_error.is_not(None),
            )
            .order_by(DealerRepInboxMessage.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    address_status = await provider_settings_status(db)
    address_provider = str(address_status["address_provider"])
    return DealerIntegrationStatus(
        isoftpull={
            "configured": isoftpull_ready,
            "environment": "production",
            "endpoint": settings.isoftpull_api_url,
            "detail": "Ready" if isoftpull_ready else "Public/private credentials are not configured",
        },
        plaid={
            "configured": plaid_ready,
            "environment": plaid_env,
            "endpoint": os.getenv("DEALER_OS_PLAID_WEBHOOK_URL") or None,
            "detail": plaid_env_error or (
                "Ready for production"
                if plaid_ready and plaid_env == "production"
                else "Configured outside production" if plaid_ready else "Client ID/secret are not configured"
            ),
        },
        sms={
            "configured": bool(sms_status["configured"] and sms_status["production"]),
            "environment": str(sms_status["provider"]),
            "endpoint": f"{settings.public_api_url.rstrip('/')}/api/v1/webhooks/{'twilio/sms/inbound' if sms_status['provider'] == 'twilio' else 'aws-sms'}",
            "detail": (
                f"{sms_status['detail']}. Latest delivery failure: {latest_sms_failure}"
                if latest_sms_failure
                else str(sms_status["detail"])
            ),
        },
        address={
            "configured": bool(address_status["address_provider_ready"]),
            "environment": address_provider,
            "endpoint": "/api/v1/property-intelligence/address/autocomplete",
            "detail": "Ready" if address_status["address_provider_ready"] else f"{address_provider.title()} key is not configured",
        },
    )


async def _require_group(db: AsyncSession, group_id: UUID) -> DealerGroup:
    group = await db.get(DealerGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    return group


async def _next_case_ref(db: AsyncSession) -> str:
    """QC-{year}-{5 digits}, counting within the calendar year.

    Derived from the highest existing reference for the year rather than from
    a row count, so deleting a file never causes the next one to collide with
    a reference already printed on a contract.

    The unique constraint is the real guarantee: two reps opening a file in the
    same second would both read the same maximum, and the loser's insert fails
    rather than duplicating. Retry once on that, which is enough for a
    two-person race and honest about not being a distributed sequence.
    """
    from datetime import datetime, timezone

    year = datetime.now(timezone.utc).year
    prefix = f"QC-{year}-"
    top = (
        await db.execute(
            select(func.max(DealerBusiness.case_ref)).where(
                DealerBusiness.case_ref.like(f"{prefix}%")
            )
        )
    ).scalar_one_or_none()
    n = 0
    if top:
        try:
            n = int(top.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            n = 0
    return f"{prefix}{n + 1:05d}"


def _client_ip(request: Request) -> str | None:
    """The consenting person's IP, as best we can see it.

    The app sits behind Caddy, so `request.client.host` is the proxy. Caddy
    sets X-Forwarded-For and, being the only hop we control, its LEFTMOST entry
    is the real client. Trusting the leftmost entry is normally unsafe because
    a client can forge the header, but here Caddy appends rather than replaces
    and nothing else terminates TLS, so the first value is what Caddy saw.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first[:64]
    return request.client.host[:64] if request.client else None


async def _record_sms_consent(
    db: AsyncSession,
    dealer: DealerBusiness,
    payload: "SmsConsentIn | None",
    user,
    request: Request,
) -> list[DealerSmsConsent]:
    """Turn ticked boxes into evidence rows. One row per kind, never bundled.

    Silently does nothing when nothing was agreed to, which is the normal case:
    consent is optional and a file opens without it.

    The legal checkbox is a precondition, not a row of its own. Someone who
    ticked "text me" but not "I agree to the terms" has not given usable
    consent, because the terms are where the SMS program is described, so we
    record neither rather than recording something we could not defend.
    """
    if payload is None:
        return []
    if not (payload.transactional or payload.marketing):
        return []
    if not payload.accepted_legal:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The Terms and Privacy Policy box has to be ticked before texts can be agreed to.",
        )
    phone = consent_delivery.normalize_phone(payload.phone)
    if not phone:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That phone number does not look complete, so the opt-in cannot be recorded.",
        )
    kinds = [k for k, on in (("transactional", payload.transactional), ("marketing", payload.marketing)) if on]
    rows = []
    for kind in kinds:
        rows.append(
            await sms_consent_svc.record_consent(
                db,
                dealer_id=dealer.id,
                phone_e164=phone,
                kind=kind,
                method=payload.method,
                captured_by_user_id=user.id,
                captured_by_name=user.name,
                consenter_name=payload.consenter_name,
                ip_address=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
        )
    await log_action(
        db, dealer.id, user, "sms_consent.granted", "dealer",
        entity_id=dealer.id,
        after={"kinds": kinds, "method": payload.method, "version": sms_consent_svc.SMS_DISCLOSURE_VERSION},
    )
    return rows


async def _notify_client_request(
    db: AsyncSession,
    dealer: DealerBusiness,
    user,
    *,
    purpose: str,
    path: str,
    channel: str,
    action: str,
    recipient_email: str | None = None,
    recipient_phone: str | None = None,
    strict_recipient: bool = False,
) -> "consent_delivery.DeliveryResult":
    """Tell the client that something is being asked of them.

    One seam for every request type, so signature, credit and bank-connect all
    reach the owner the same way and none of them can quietly send nothing.
    Email always goes; a text is added on top when asked for and consented to.

    Recipient comes from the owner record first and the business second. The
    owner is the person who actually signs and authorises, and on plenty of
    files the business email is a shared inbox nobody watches.
    """
    owner = (
        await db.execute(
            select(DealerOwner)
            .where(DealerOwner.dealer_id == dealer.id)
            .order_by(DealerOwner.is_primary.desc(), DealerOwner.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if strict_recipient:
        to_email = recipient_email
        to_phone = recipient_phone
    else:
        to_email = recipient_email or (owner.email if owner and owner.email else None) or dealer.email
        to_phone = recipient_phone or (owner.phone if owner and owner.phone else None) or dealer.phone
    delivery = await consent_delivery.deliver_link_checked(
        db,
        channel=channel,
        to_email=to_email,
        to_phone=to_phone,
        business_name=dealer.name,
        purpose=purpose,
        path=path,
        rep_name=user.name,
    )
    await log_action(
        db, dealer.id, user, action, "dealer",
        entity_id=dealer.id,
        after={
            "delivered": delivery.ok,
            "email": delivery.email_ok,
            "sms": delivery.sms_ok,
            "purpose": purpose,
            # Recorded so the delivery log can name who was reached without
            # re-deriving it from the owner row, which may have changed since.
            "recipient": to_email or to_phone or "",
            "channel": channel,
        },
    )
    return delivery


@router.post(
    "/dealers/{dealer_id}/bank-connect-invite",
    response_model=ClientRequestResult,
    status_code=status.HTTP_201_CREATED,
)
async def send_bank_connect_invite(
    dealer_id: UUID,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    payload: ClientRequestSend | None = None,
) -> ClientRequestResult:
    """Send the owner their secure room to authorize and connect business banks."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    req = payload or ClientRequestSend()
    await _require_training_live_action(
        db,
        dealer=dealer,
        user=user,
        request=request,
        action="Send bank connection request",
        provider="SES / SMS / Plaid",
        recipient=dealer.email or dealer.phone,
        effect="Send the client a secure room link for a live Plaid connection.",
    )
    room = await client_room.ensure_room(db, dealer)
    delivery = await _notify_client_request(
        db, dealer, user,
        purpose="connect your business bank account with Plaid",
        path=room.url,
        channel=req.channel,
        action="client_request.bank_connect",
    )
    await db.commit()
    return ClientRequestResult(
        url=room.url,
        passcode=room.passcode,
        delivered=delivery.ok,
        emailed=delivery.email_ok,
        texted=delivery.sms_ok,
        detail=delivery.detail,
    )


@router.post(
    "/dealers/{dealer_id}/bank-upload-request",
    response_model=BankUploadRequestResult,
    status_code=status.HTTP_201_CREATED,
)
async def send_bank_upload_request(
    dealer_id: UUID,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    payload: ClientRequestSend | None = None,
) -> BankUploadRequestResult:
    """Ask the owner to upload statements instead of connecting Plaid."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    req = payload or ClientRequestSend()
    await _require_training_live_action(
        db,
        dealer=dealer,
        user=user,
        request=request,
        action="Request bank statements",
        provider="SES / SMS",
        recipient=dealer.email or dealer.phone,
        effect="Send a live secure-room document request to the client.",
    )
    requested = await client_room.request_document(
        db,
        dealer,
        name="Six current months of business bank statements",
        description=(
            "Upload the six most recent completed months of bank-produced business "
            "statements. PDF statements are required; CSVs and screenshots are supplemental."
        ),
        category="financials",
        required=True,
    )
    room = await client_room.ensure_room(db, dealer)
    delivery = await _notify_client_request(
        db,
        dealer,
        user,
        purpose="upload six current months of bank-produced business statements",
        path=room.url,
        channel=req.channel,
        action="client_request.bank_upload",
    )
    await db.commit()
    return BankUploadRequestResult(
        url=room.url,
        passcode=room.passcode,
        delivered=delivery.ok,
        emailed=delivery.email_ok,
        texted=delivery.sms_ok,
        detail=delivery.detail,
        bucket_id=dealer.bucket_id,
        upload_link_id=room.link.id,
        requested_document_id=requested.id,
    )


@router.get("/dealers/{dealer_id}/bank-evidence", response_model=BankEvidenceRead)
async def bank_evidence(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> BankEvidenceRead:
    """Current bank evidence source and statement-month coverage for Step 2."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    ver = await _assess_verification(db, dealer)
    room_url: str | None = None
    passcode: str | None = None
    if dealer.bucket_id is not None:
        link = (
            await db.execute(
                select(BucketUploadLink)
                .where(
                    BucketUploadLink.bucket_id == dealer.bucket_id,
                    BucketUploadLink.status == "active",
                )
                .order_by(BucketUploadLink.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if link is not None:
            room_url = client_room.room_url(link.token)
    return BankEvidenceRead(
        bank_linked=ver.bank_linked,
        bank_source=ver.bank_source if ver.bank_source in {"plaid", "upload"} else "none",
        statement_months=ver.statement_months,
        missing_statement_months=ver.missing_statement_months,
        bucket_id=dealer.bucket_id,
        upload_url=room_url,
        passcode=passcode,
    )


@router.post(
    "/dealers/{dealer_id}/signature-request",
    response_model=ClientRequestResult,
    status_code=status.HTTP_201_CREATED,
)
async def send_signature_request(
    dealer_id: UUID,
    payload: SignatureRequestSend,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClientRequestResult:
    """Ask the owner to sign something.

    Adds it to the same checklist their documents are on, so there is one list
    with everything outstanding on it rather than a separate signing inbox they
    have to be told about separately."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    await _require_training_live_action(
        db,
        dealer=dealer,
        user=user,
        request=request,
        action="Send signature request",
        provider="SES / SMS",
        recipient=dealer.email or dealer.phone,
        effect=f"Send the client a live request to sign {payload.title}.",
    )
    room = await client_room.ensure_room(db, dealer)
    # The signable text IS the document. Composed from what the rep wrote so
    # the client signs words a person chose, and refused when there are none:
    # a signature over an empty page is not evidence of anything.
    text = "\n\n".join(x for x in (payload.title.strip(), (payload.note or "").strip()) if x)
    await client_room.request_document(
        db, dealer,
        name=payload.title,
        description=payload.note,
        category="signatures",
        requires_signature=True,
        signature_kind=payload.signature_kind or "custom",
        signature_document_text=text,
    )
    delivery = await _notify_client_request(
        db, dealer, user,
        purpose=f"sign {payload.title}",
        path=room.url,
        channel=payload.channel,
        action="client_request.signature",
    )
    await db.commit()
    return ClientRequestResult(
        url=room.url,
        passcode=room.passcode,
        delivered=delivery.ok,
        emailed=delivery.email_ok,
        texted=delivery.sms_ok,
        detail=delivery.detail,
    )


@router.get("/dealers/{dealer_id}/contracts", response_model=list[ContractDocRead])
async def list_case_contracts(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[ContractDocRead]:
    """The case's copies of the package: what has been generated, what is
    still draft, what is out for signature or executed."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    rows = (
        (
            await db.execute(
                select(ContractDocument)
                .where(ContractDocument.dealer_id == dealer.id)
                .order_by(ContractDocument.template_key)
            )
        )
        .scalars()
        .all()
    )
    return [ContractDocRead.model_validate(r) for r in rows]


async def _contract_envelope_read(
    db: AsyncSession,
    envelope: ContractEnvelope,
    *,
    public: bool = False,
) -> ContractEnvelopeRead:
    from app.services.payment_authorization import presign_private_s3_object

    rows = list(
        (
            await db.execute(
                select(ContractEnvelopeDocument, ContractDocument)
                .join(
                    ContractDocument,
                    ContractDocument.id == ContractEnvelopeDocument.contract_document_id,
                )
                .where(ContractEnvelopeDocument.envelope_id == envelope.id)
                .order_by(ContractEnvelopeDocument.sort_order)
            )
        ).all()
    )
    documents = []
    for link, document in rows:
        source_key = (
            document.executed_s3_key
            if document.status == "executed" and document.executed_s3_key
            else document.filled_s3_key
        )
        preview_url = None
        download_url = None
        if source_key:
            preview_url = presign_private_s3_object(source_key, ttl_seconds=3600)
            if document.status == "executed":
                download_url = presign_private_s3_object(
                    source_key,
                    ttl_seconds=3600,
                    download_filename=f"{document.template_key}-executed.pdf",
                )
        values = document.field_values or {}
        documents.append(
            ContractEnvelopeDocumentRead(
                id=link.id,
                contract_document_id=document.id,
                template_key=document.template_key,
                title=link.title_snapshot,
                sort_order=link.sort_order,
                required=link.required,
                status=document.status,
                missing_data=list(values.get("_missing_data") or []),
                filled_sha256=document.filled_sha256,
                executed_sha256=document.executed_sha256,
                reviewed_at=link.reviewed_at,
                acknowledged_at=link.acknowledged_at,
                preview_url=preview_url,
                download_url=download_url,
            )
        )
    bundle_url = None
    if envelope.bundle_s3_key:
        bundle_url = presign_private_s3_object(
            envelope.bundle_s3_key,
            ttl_seconds=3600,
            download_filename=f"{envelope.package_key}-executed-package.pdf",
        )
    return ContractEnvelopeRead(
        id=envelope.id,
        dealer_id=envelope.dealer_id,
        package_key=envelope.package_key,
        package_version=envelope.package_version,
        program_key=envelope.program_key,
        title=envelope.title,
        status=envelope.status,
        signer_name=envelope.signer_name,
        signer_title=envelope.signer_title,
        sent_at=envelope.sent_at,
        opened_at=envelope.opened_at,
        completed_at=envelope.completed_at,
        voided_at=envelope.voided_at,
        bundle_sha256=envelope.bundle_sha256,
        bundle_download_url=bundle_url,
        delivery_history=list(envelope.delivery_history or []),
        documents=documents,
    )


@router.get("/contract-packages", response_model=list[ContractPackageRead])
async def list_contract_packages(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[ContractPackageRead]:
    require_team_or_rep(user)
    rows = await contract_packages.packages(db)
    await db.commit()
    return [
        ContractPackageRead(
            id=package.id,
            key=package.key,
            program_key=package.program_key,
            title=package.title,
            version=package.version,
            active=package.active,
            items=[
                ContractPackageItemRead(
                    id=item.id,
                    template_key=item.template_key,
                    template_version_id=item.template_version_id,
                    title=item.title_snapshot,
                    sort_order=item.sort_order,
                    required=item.required,
                )
                for item in items
            ],
        )
        for package, items in rows
    ]


@router.put("/contract-packages/{program_key}", response_model=ContractPackageRead)
async def publish_contract_package(
    program_key: str,
    payload: ContractPackageWrite,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContractPackageRead:
    require_super_admin(user)
    if program_key not in contract_packages.SUPPORTED_PROGRAMS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unsupported direct program.")
    template_keys = [item.template_key for item in payload.items]
    if len(template_keys) != len(set(template_keys)):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "A forms package cannot contain the same document template more than once.",
        )
    if contract_packages.PROGRAM_APPLICATION_KEY not in template_keys:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The Business Loan Application is required in every direct-program package.",
        )
    existing = list(
        (
            await db.execute(
                select(ContractPackage)
                .where(ContractPackage.program_key == program_key)
                .order_by(ContractPackage.version.desc())
            )
        ).scalars().all()
    )
    version = (existing[0].version if existing else 0) + 1
    for row in existing:
        row.active = False
    package = ContractPackage(
        key=f"{program_key}_v{version}",
        program_key=program_key,
        title=payload.title,
        version=version,
        active=payload.active,
        created_by_user_id=user.id,
    )
    db.add(package)
    await db.flush()
    for item in sorted(payload.items, key=lambda value: value.sort_order):
        template = (
            await db.execute(select(ContractTemplate).where(ContractTemplate.key == item.template_key))
        ).scalar_one_or_none()
        template_version = await db.get(ContractTemplateVersion, item.template_version_id) if item.template_version_id else None
        if template is None or template_version is None or template_version.template_id != template.id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"No valid template version for {item.title}.")
        db.add(ContractPackageItem(
            package_id=package.id,
            template_key=item.template_key,
            template_version_id=template_version.id,
            title_snapshot=item.title,
            sort_order=item.sort_order,
            required=item.required,
            conditions={},
        ))
    await db.commit()
    rows = await contract_packages.packages(db)
    selected = next((row for row in rows if row[0].id == package.id), None)
    if selected is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Package publish failed.")
    package, items = selected
    return ContractPackageRead(
        id=package.id,
        key=package.key,
        program_key=package.program_key,
        title=package.title,
        version=package.version,
        active=package.active,
        items=[ContractPackageItemRead(
            id=item.id,
            template_key=item.template_key,
            template_version_id=item.template_version_id,
            title=item.title_snapshot,
            sort_order=item.sort_order,
            required=item.required,
        ) for item in items],
    )


@router.get(
    "/contract-templates/{key}/versions",
    response_model=list[ContractTemplateVersionRead],
)
async def list_contract_template_versions(
    key: str, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[ContractTemplateVersion]:
    require_team_or_rep(user)
    await contract_packages.ensure_defaults(db, user.id)
    template = (
        await db.execute(select(ContractTemplate).where(ContractTemplate.key == key))
    ).scalar_one_or_none()
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found.")
    rows = list((await db.execute(
        select(ContractTemplateVersion)
        .where(ContractTemplateVersion.template_id == template.id)
        .order_by(ContractTemplateVersion.revision.desc())
    )).scalars().all())
    await db.commit()
    return rows


@router.get(
    "/contract-template-versions",
    response_model=list[ContractTemplateVersionCatalogRead],
)
async def list_contract_template_version_catalog(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[ContractTemplateVersionCatalogRead]:
    """Version catalog used by the super-admin Forms and Packages workspace."""
    require_super_admin(user)
    await contract_packages.ensure_defaults(db, user.id)
    from app.services.payment_authorization import presign_private_s3_object

    rows = list((await db.execute(
        select(ContractTemplateVersion, ContractTemplate)
        .join(ContractTemplate, ContractTemplate.id == ContractTemplateVersion.template_id)
        .order_by(ContractTemplate.title, ContractTemplateVersion.revision.desc())
    )).all())
    await db.commit()
    return [
        ContractTemplateVersionCatalogRead(
            id=version.id,
            template_id=version.template_id,
            revision=version.revision,
            sha256=version.sha256,
            page_count=version.page_count,
            has_acroform=version.has_acroform,
            field_names=version.field_names,
            overlay_map=version.overlay_map,
            active=version.active,
            created_at=version.created_at,
            template_key=template.key,
            title=template.title,
            preview_url=presign_private_s3_object(version.s3_key, ttl_seconds=3600),
        )
        for version, template in rows
    ]


@router.post(
    "/contract-templates/{key}/versions",
    response_model=ContractTemplateVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_contract_template_version(
    key: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    signature_x: float | None = Form(default=None),
    signature_y: float | None = Form(default=None),
    signature_date_x: float | None = Form(default=None),
    signature_date_y: float | None = Form(default=None),
) -> ContractTemplateVersion:
    require_super_admin(user)
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,47}", key):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Template keys use lowercase letters, numbers, and underscores.",
        )
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "PDF templates are limited to 20 MB.")
    anchor_values = (signature_x, signature_y, signature_date_x, signature_date_y)
    if any(value is not None for value in anchor_values) and not all(
        value is not None for value in anchor_values
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Provide all four signature and date coordinates, or leave all four blank.",
        )
    overlay_map = None
    if all(value is not None for value in anchor_values):
        overlay_map = {
            "coordinate_space": "pdf_points_top_left",
            "signature": [signature_x, signature_y],
            "signature_date": [signature_date_x, signature_date_y],
            "static_supporting_document": key != contract_packages.PROGRAM_APPLICATION_KEY,
        }
    try:
        version = await contract_packages.create_template_version(
            db,
            template_key=key,
            title=(
                title
                or (
                    contract_packages.PROGRAM_APPLICATION_TITLE
                    if key == contract_packages.PROGRAM_APPLICATION_KEY
                    else file.filename or "Supporting agreement"
                )
            ).strip()[:180],
            raw=raw,
            actor_user_id=user.id,
            overlay_map=overlay_map,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await db.commit()
    await db.refresh(version)
    return version


@router.get(
    "/dealers/{dealer_id}/contract-envelopes",
    response_model=list[ContractEnvelopeRead],
)
async def list_case_contract_envelopes(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[ContractEnvelopeRead]:
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    rows = list((await db.execute(
        select(ContractEnvelope)
        .where(ContractEnvelope.dealer_id == dealer.id)
        .order_by(ContractEnvelope.created_at.desc())
    )).scalars().all())
    return [await _contract_envelope_read(db, row) for row in rows]


@router.post(
    "/dealers/{dealer_id}/contract-envelopes/generate",
    response_model=ContractEnvelopeRead,
)
async def generate_case_contract_envelope(
    dealer_id: UUID,
    payload: ContractEnvelopeGenerateRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContractEnvelopeRead:
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    try:
        envelope = await contract_packages.generate_envelope(
            db,
            dealer,
            program_key=payload.program_key,
            actor=user,
            override_reason=payload.override_reason,
        )
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await log_action(
        db,
        dealer.id,
        user,
        "contract_envelope.generated",
        "contract_envelope",
        entity_id=envelope.id,
        after={
            "program_key": payload.program_key,
            "package_version": envelope.package_version,
            "status": envelope.status,
            "override_reason": payload.override_reason,
        },
    )
    await db.commit()
    await db.refresh(envelope)
    return await _contract_envelope_read(db, envelope)


@router.post(
    "/dealers/{dealer_id}/contract-envelopes/{envelope_id}/send",
    response_model=ClientRequestResult,
)
async def send_case_contract_envelope(
    dealer_id: UUID,
    envelope_id: UUID,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    payload: ClientRequestSend | None = None,
) -> ClientRequestResult:
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    envelope = await db.get(ContractEnvelope, envelope_id)
    if envelope is None or envelope.dealer_id != dealer.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Package not found.")
    if envelope.status not in {"ready", "out_for_signature"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Complete every required source field before sending.")
    owner = await db.get(DealerOwner, envelope.recipient_owner_id) if envelope.recipient_owner_id else None
    if owner is None or not owner.email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The primary signer needs a personal email address.")
    await _require_training_live_action(
        db,
        dealer=dealer,
        user=user,
        request=request,
        action="Send contract package",
        provider="SES / SMS / secure signing room",
        recipient=owner.email,
        effect=f"Deliver the live {envelope.title} signing package to the client.",
    )
    docs = list((await db.execute(
        select(ContractDocument).where(ContractDocument.envelope_id == envelope.id)
    )).scalars().all())
    if not docs or any(document.status not in {"ready", "out_for_signature"} for document in docs):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "One or more package documents are not ready.")
    now = datetime.now(timezone.utc)
    for document in docs:
        document.status = "out_for_signature"
    envelope.status = "out_for_signature"
    envelope.sent_at = envelope.sent_at or now
    room = await client_room.ensure_room(db, dealer)
    req = payload or ClientRequestSend(channel="email")
    delivery = await _notify_client_request(
        db,
        dealer,
        user,
        purpose=f"review and sign the {envelope.title}",
        path=room.url,
        channel=req.channel,
        action="contract_envelope.sent_for_signature",
        recipient_email=owner.email,
        recipient_phone=owner.phone,
        strict_recipient=True,
    )
    envelope.delivery_history = [
        *(envelope.delivery_history or []),
        {
            "at": now.isoformat(),
            "channel": req.channel,
            "email": owner.email,
            "ok": delivery.ok,
            "email_ok": delivery.email_ok,
            "sms_ok": delivery.sms_ok,
            "detail": delivery.detail,
        },
    ]
    await db.commit()
    return ClientRequestResult(
        url=room.url,
        passcode=room.passcode,
        delivered=delivery.ok,
        emailed=delivery.email_ok,
        texted=delivery.sms_ok,
        detail=delivery.detail,
    )


@router.post(
    "/dealers/{dealer_id}/contract-envelopes/{envelope_id}/void",
    response_model=ContractEnvelopeRead,
)
async def void_case_contract_envelope(
    dealer_id: UUID,
    envelope_id: UUID,
    payload: ContractEnvelopeVoidRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContractEnvelopeRead:
    require_super_admin(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    envelope = await db.get(ContractEnvelope, envelope_id)
    if envelope is None or envelope.dealer_id != dealer.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Package not found.")
    if envelope.status == "executed":
        raise HTTPException(status.HTTP_409_CONFLICT, "Executed packages are immutable.")
    now = datetime.now(timezone.utc)
    envelope.status = "void"
    envelope.voided_at = now
    envelope.voided_by_user_id = user.id
    envelope.void_reason = payload.reason.strip()
    await db.execute(sa_update(ContractDocument).where(
        ContractDocument.envelope_id == envelope.id
    ).values(status="void"))
    await db.commit()
    await db.refresh(envelope)
    return await _contract_envelope_read(db, envelope)


@router.post(
    "/dealers/{dealer_id}/contracts/{key}/generate",
    response_model=ContractGenerateResult,
)
async def generate_case_contract(
    dealer_id: UUID, key: str, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ContractGenerateResult:
    """Prepopulate one agreement from what the case has collected.

    Values come from Steps 1-4. The primary owner or authorized representative
    signs the master application in the secure client room. Anything the case
    does not yet know is named as missing rather than defaulted: a blank on a
    legal document must be a decision, not an accident."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    if key == qc_master_application.MASTER_TEMPLATE_KEY:
        readiness = qc_master_application.build_readiness(
            await qc_master_application.build_context(db, dealer)
        )
        if not readiness["package_ready"]:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Complete the Step 4 underwriting package before generating the QC application.",
            )
    try:
        doc, result, missing = await contract_fill.generate(db, dealer, key)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await log_action(
        db, dealer.id, user, "contract.generate", "contract_document",
        entity_id=doc.id,
        after={"template": key, "sha256": result.sha256[:16],
               "placed": len(result.placed), "missing": missing},
    )
    await db.commit()
    from app.services.payment_authorization import presign_private_s3_object

    return ContractGenerateResult(
        status=doc.status,
        placed=result.placed,
        missing_data=missing,
        overlay_problems=result.missing,
        sha256=result.sha256,
        download_url=presign_private_s3_object(doc.filled_s3_key, ttl_seconds=900),
    )


@router.post(
    "/dealers/{dealer_id}/underwriting-summary",
    response_model=ContractGenerateResult,
)
async def generate_underwriting_summary(
    dealer_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContractGenerateResult:
    """Generate a review-only QC application and evidence summary.

    This may be rendered while the file is incomplete. It names every missing
    condition and is not eligible for signature through this endpoint.
    """

    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    try:
        doc, result, missing = await contract_fill.generate(
            db,
            dealer,
            qc_master_application.MASTER_TEMPLATE_KEY,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await log_action(
        db,
        dealer.id,
        user,
        "underwriting_summary.generated",
        "contract_document",
        entity_id=doc.id,
        after={"sha256": result.sha256[:16], "missing": missing},
    )
    await db.commit()
    from app.services.payment_authorization import presign_private_s3_object

    return ContractGenerateResult(
        status=doc.status,
        placed=result.placed,
        missing_data=missing,
        overlay_problems=result.missing,
        sha256=result.sha256,
        download_url=presign_private_s3_object(
            doc.filled_s3_key,
            ttl_seconds=900,
            download_filename=f"{dealer.case_ref or 'QC'}-underwriting-summary.pdf",
        ),
    )


@router.post(
    "/dealers/{dealer_id}/contracts/{key}/send-signature",
    response_model=ClientRequestResult,
)
async def send_contract_for_signature(
    dealer_id: UUID,
    key: str,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    payload: ClientRequestSend | None = None,
) -> ClientRequestResult:
    """Freeze the prepopulated agreement and put it in front of the client.

    From this moment the paper cannot be regenerated: the signer and the desk
    must be looking at the same document. The client signs in their own room,
    on their own device — the rep's session has no signing surface at all."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    await _require_training_live_action(
        db,
        dealer=dealer,
        user=user,
        request=request,
        action="Send contract for signature",
        provider="SES / SMS / secure signing room",
        recipient=dealer.email or dealer.phone,
        effect=f"Deliver the live {key} agreement to the client for signature.",
    )
    doc = (
        await db.execute(
            select(ContractDocument).where(
                ContractDocument.dealer_id == dealer.id,
                ContractDocument.template_key == key,
            )
        )
    ).scalar_one_or_none()
    if doc is None or not doc.filled_s3_key:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Generate the prepopulated agreement first.",
        )
    if doc.status == "executed":
        raise HTTPException(status.HTTP_409_CONFLICT, "This document is already signed.")
    if key == qc_master_application.MASTER_TEMPLATE_KEY:
        readiness = qc_master_application.build_readiness(
            await qc_master_application.build_context(db, dealer)
        )
        if not readiness["package_ready"]:
            open_items = [
                row["requirement"]
                for row in readiness["items"]
                if row["requirement"] != "Human-reviewed fundable path"
                and row["status"] in {"missing", "supplemental"}
            ]
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "The QC application cannot be sent yet: " + "; ".join(open_items[:5]),
            )
    doc.status = "out_for_signature"
    await db.flush()

    tpl = (
        await db.execute(select(ContractTemplate).where(ContractTemplate.key == key))
    ).scalar_one_or_none()
    title = tpl.title if tpl else key
    room = await client_room.ensure_room(db, dealer)
    req = payload or ClientRequestSend()
    delivery = await _notify_client_request(
        db, dealer, user,
        purpose=f"review and sign the {title}",
        path=room.url,
        channel=req.channel,
        action="contract.sent_for_signature",
    )
    await db.commit()
    return ClientRequestResult(
        url=room.url,
        passcode=room.passcode,
        delivered=delivery.ok,
        emailed=delivery.email_ok,
        texted=delivery.sms_ok,
        detail=delivery.detail,
    )


@router.get("/dealers/{dealer_id}/contracts/{key}/url")
async def case_contract_url(
    dealer_id: UUID, key: str, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> dict:
    """A short-lived link to the latest filled copy, for preview."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    doc = (
        await db.execute(
            select(ContractDocument).where(
                ContractDocument.dealer_id == dealer.id,
                ContractDocument.template_key == key,
            )
        )
    ).scalar_one_or_none()
    if doc is None or not doc.filled_s3_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No generated copy yet.")
    from app.services.payment_authorization import presign_private_s3_object

    source_key = (
        doc.executed_s3_key
        if doc.status == "executed" and doc.executed_s3_key
        else doc.filled_s3_key
    )
    filename = (
        f"{dealer.case_ref or 'QC'}-business-financing-application.pdf"
        if key == qc_master_application.MASTER_TEMPLATE_KEY
        else f"{dealer.case_ref or 'QC'}-{key}.pdf"
    )
    url = presign_private_s3_object(
        source_key,
        ttl_seconds=900,
        download_filename=filename if doc.status == "executed" else None,
    )
    if not url:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not sign a download link.")
    return {
        "url": url,
        "sha256": doc.executed_sha256 if doc.status == "executed" else doc.filled_sha256,
        "status": doc.status,
    }


@router.get("/contract-templates", response_model=list[ContractTemplateRead])
async def list_contract_templates(user: CurrentUser, db: AsyncSession = Depends(get_db)):
    """The package: which lender documents exist, which have paper, which are
    mapped. Open to reps so step 5 can draw the real state — a template holds
    no client data at all."""
    require_team_or_rep(user)
    rows = (
        (await db.execute(select(ContractTemplate).order_by(ContractTemplate.key)))
        .scalars()
        .all()
    )
    return [ContractTemplateRead.model_validate(t) for t in rows]


@router.post(
    "/contract-templates/{key}",
    response_model=ContractTemplateRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_contract_template(
    key: str,
    user: CurrentUser,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> ContractTemplateRead:
    """Upload (or replace) one lender document's blank PDF.

    Super-admin only: this is the paper every case will execute, and replacing
    it is a legal act, not a content edit. Field discovery runs on ingest;
    mapping stays a separate, deliberate step."""
    require_super_admin(user)
    raw = await file.read()
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "PDF larger than 25MB.")
    try:
        tpl = await contract_registry.ingest_template(
            db, key=key, pdf_bytes=raw, uploaded_by=user
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await db.commit()
    await db.refresh(tpl)
    return ContractTemplateRead.model_validate(tpl)


@router.patch("/contract-templates/{key}", response_model=ContractTemplateRead)
async def map_contract_template(
    key: str,
    payload: ContractTemplateMapPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContractTemplateRead:
    """Set the field mapping: discovered PDF field name -> case source path.

    Only names the PDF actually declares are accepted, so a typo surfaces here
    rather than as a silently unfilled field on a signed document."""
    require_super_admin(user)
    tpl = (
        await db.execute(select(ContractTemplate).where(ContractTemplate.key == key))
    ).scalar_one_or_none()
    if tpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such template.")
    known = set(tpl.field_names or [])
    unknown = sorted(set(payload.field_map) - known)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"These fields are not in the PDF: {', '.join(unknown[:8])}",
        )
    tpl.field_map = payload.field_map
    # No log_action here: the audit trail is per-dealer and a template is
    # desk-wide. The template row itself records uploader and revision.
    await db.commit()
    await db.refresh(tpl)
    return ContractTemplateRead.model_validate(tpl)


@router.get("/sms-disclosure", response_model=SmsDisclosureOut)
async def get_sms_disclosure(user: CurrentUser) -> SmsDisclosureOut:
    """The exact consent wording the form must show.

    Served rather than hardcoded in the client so the words on screen and the
    words stored as proof cannot drift apart. Editing the copy in one place
    changes both, and bumping SMS_DISCLOSURE_VERSION marks which records saw
    which wording."""
    require_team_or_rep(user)
    return SmsDisclosureOut(**asdict(sms_consent_svc.disclosure()))


@router.get("/dealers/{dealer_id}/sms-consent", response_model=list[SmsConsentOut])
async def list_sms_consent(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerSmsConsent]:
    """What this file's number has agreed to, and when. The audit trail a
    carrier complaint would be answered with."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return list(
        (
            await db.execute(
                select(DealerSmsConsent)
                .where(DealerSmsConsent.dealer_id == dealer.id)
                .order_by(DealerSmsConsent.created_at.desc())
            )
        ).scalars().all()
    )


@router.post(
    "/dealers/{dealer_id}/sms-consent",
    response_model=list[SmsConsentOut],
    status_code=status.HTTP_201_CREATED,
)
async def add_sms_consent(
    dealer_id: UUID,
    payload: SmsConsentIn,
    user: CurrentUser,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[DealerSmsConsent]:
    """Capture consent on an existing file, for the common case where the owner
    was not ready to opt in when the file was opened."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    rows = await _record_sms_consent(db, dealer, payload, user, request)
    await db.commit()
    for r in rows:
        await db.refresh(r)
    return rows


@router.delete("/dealers/{dealer_id}/sms-consent", status_code=status.HTTP_200_OK)
async def revoke_sms_consent(
    dealer_id: UUID,
    user: CurrentUser,
    phone: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Opt a number out by hand, for when someone says stop to the rep rather
    than to the shortcode. Revokes across every file the number appears on,
    because a person saying stop means stop."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    e164 = consent_delivery.normalize_phone(phone)
    if not e164:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That phone number does not look complete.")
    n = await sms_consent_svc.revoke(db, phone_e164=e164, reason="asked us to stop")
    await log_action(
        db, dealer.id, user, "sms_consent.revoked", "dealer",
        entity_id=dealer.id, after={"phone": e164, "rows": n},
    )
    await db.commit()
    return {"revoked": n, "phone": e164}


@router.post("/dealers", response_model=DealerRead, status_code=status.HTTP_201_CREATED)
async def create_dealer(
    payload: DealerCreate,
    user: CurrentUser,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerBusiness:
    """Open a client file. Team creates anywhere in the book; a FIELD_REP
    creates into their own, and owner_user_id is what makes it theirs.

    The bucket is created here rather than lazily. For a rep working on site
    the bucket IS the document room, so a file without one cannot receive
    anything, and "create the file, then remember to make a bucket" is a step
    that will be forgotten in a parking lot. ensure_bucket adopts the intake
    bucket matched by email when the business is already in the funding
    funnel, so this does not mint a second room for an existing client."""
    require_team_or_rep(user)
    if payload.is_training and user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")
    if payload.group_id is not None:
        await _require_group(db, payload.group_id)
    # sms_consent is captured alongside the file but is not a column on it: it
    # becomes its own evidence row below.
    fields = payload.model_dump(exclude={"sms_consent"})
    taxonomy = await application_taxonomy.canonicalize_selection(db, fields, required=False)
    fields.update({key: value for key, value in taxonomy.items() if key != "taxonomy_status"})
    dealer = DealerBusiness(**fields, owner_user_id=user.id, case_ref=await _next_case_ref(db))
    db.add(dealer)
    await db.flush()
    await _record_sms_consent(db, dealer, payload.sms_consent, user, request)
    # Every dealer starts with the uploads source active and a full set of
    # AI-proposed targets, so the cockpit is never empty.
    db.add(DealerSourceConnection(dealer_id=dealer.id, kind="uploads", status="active"))
    await propose_targets(db, dealer)
    # Best-effort: a bucket failure must not cost the rep the file they just
    # typed in front of a client. The file is still usable and
    # POST /dealers/{id}/bucket/create recovers it.
    try:
        await buckets_link.ensure_bucket(db, dealer)
    except Exception:
        logger.exception("dealer-os: bucket creation failed for new dealer %s", dealer.id)
    # A rep's file carries a pipeline row from the moment it exists, so it shows
    # up in production reporting immediately rather than only once it advances.
    # Team-created files deliberately get none: they are not field work and
    # counting them would inflate a rep's numbers with the desk's own.
    if user.role == Role.FIELD_REP:
        db.add(
            DealerRepLead(
                dealer_id=dealer.id,
                rep_user_id=user.id,
                status="draft",
                status_history=[
                    {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "from": None,
                        "to": "draft",
                        "by": str(user.id),
                        "by_name": user.name,
                    }
                ],
            )
        )
    if dealer.is_training:
        await log_action(
            db,
            dealer.id,
            user,
            "dealer.training_enabled",
            "dealer",
            entity_id=dealer.id,
            before={"is_training": False},
            after={"is_training": True},
        )
    await db.commit()
    await db.refresh(dealer)
    return dealer



async def _dealer_read(db: AsyncSession, dealer: DealerBusiness) -> DealerRead:
    r = DealerRead.model_validate(dealer)
    if dealer.owner_user_id is not None:
        submitting_agent = (
            await db.execute(
                select(User.name, User.email).where(User.id == dealer.owner_user_id)
            )
        ).one_or_none()
        if submitting_agent is not None:
            r.submitting_agent_name = submitting_agent.name or None
            r.submitting_agent_email = submitting_agent.email or None
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
async def match_bucket_by_email(
    dealer_id: UUID, background: BackgroundTasks, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerRead:
    """Explicitly find this dealer's intake bucket by email (no bucket creation —
    manual linking or ensure_bucket handle the rest)."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    if not dealer.email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Client has no email on file — add one, or link a bucket manually.")
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
    await _remirror_documents(db, dealer)
    await db.commit()
    await db.refresh(dealer)
    background.add_task(_background_ingest_bucket_files, dealer.id)
    return await _dealer_read(db, dealer)


@router.patch("/dealers/{dealer_id}/workflow-settings", response_model=DealerRead)
async def patch_dealer_workflow_settings(
    dealer_id: UUID,
    payload: DealerWorkflowSettingsPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerRead:
    """Change training classification or the persistent presentation gate.

    These controls are deliberately absent from DealerUpdate. They affect
    reporting visibility and every staff member's workflow, so only a
    super-admin may reach this explicit audited endpoint.
    """
    require_super_admin(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    requested = payload.model_dump(exclude_none=True)
    before = {key: bool(getattr(dealer, key)) for key in requested}
    changed = {
        key: bool(value)
        for key, value in requested.items()
        if bool(getattr(dealer, key)) != bool(value)
    }
    if not changed:
        return await _dealer_read(db, dealer)

    for key, value in changed.items():
        setattr(dealer, key, value)
    for key, value in changed.items():
        action = (
            "dealer.training_enabled"
            if key == "is_training" and value
            else "dealer.training_disabled"
            if key == "is_training"
            else "dealer.workflow_ungated"
            if value
            else "dealer.workflow_gated"
        )
        await log_action(
            db,
            dealer.id,
            user,
            action,
            "dealer",
            entity_id=dealer.id,
            before={key: before[key]},
            after={key: value},
        )
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
    dealer_id: UUID,
    background: BackgroundTasks,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
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
    background.add_task(_background_ingest_bucket_files, dealer.id)
    return await _dealer_read(db, dealer)


@router.get("/dealers/{dealer_id}/fundability")
async def dealer_fundability(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> dict:
    """THE verdict: is this deal fundable — one answer with the receipts.

    Composes the live program grid (readiness + sizing), the funding goal's
    reverse-engineered requirements, and the strongest program's unmet
    checklist into fundable | conditional | not_yet. 200 with verdict
    "no_data" when no snapshot exists yet (a brand-new dealer is not a 400)."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    try:
        metrics = await _latest_snapshot_metrics(db, dealer.id)
    except HTTPException:
        return {"verdict": "no_data", "best_path": None, "blocking": [], "goal_feasible": None,
                "goal": None, "goal_best_path": None}
    targets = await _effective_targets(db, dealer.id)
    settings = await _global_program_settings(db)
    deposits_avg = await _monthly_deposits_avg(db, dealer.id)
    tree = {**metrics, "deposits_monthly_avg": deposits_avg}
    paths = compute_paths(tree, targets, settings=settings)
    goal = float(dealer.funding_goal) if dealer.funding_goal is not None else None
    goal_paths = None
    if goal:
        goal_paths = []
        for key in PATH_KEYS:
            reqs = requirements_for_amount(key, goal, tree, settings=settings)
            goal_paths.append(
                {
                    "path_key": key,
                    "goal_feasible": (all(r["met"] for r in reqs) if reqs else None),
                    "requirements": reqs,
                }
            )
    verdict = paths_service_fundability(paths, goal, goal_paths)
    verdict["goal"] = goal
    return verdict


@router.get("/dealers/{dealer_id}/delivery-log", response_model=list[DeliveryRowRead])
async def dealer_delivery_log(
    dealer_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[DeliveryRowRead]:
    """What was asked of this applicant and what came back.

    A projection over the audit trail rather than a second store: every fact in
    it is already recorded when a link is sent, opened or completed. See
    services/delivery_log.py for why status is derived rather than stored."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    rows = await delivery_log.build(db, dealer.id, limit=limit)
    return [DeliveryRowRead(**asdict(r)) for r in rows]


@router.post("/dealers/{dealer_id}/convert-to-audit", response_model=ConvertToAuditResult)
async def convert_to_audit(
    dealer_id: UUID,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    payload: ConvertToAuditRequest | None = None,
) -> ConvertToAuditResult:
    """Graduate a rep application into a full audit client.

    Deliberately a FLAG on the same row, never a copy. The bank connection,
    credit pull, documents, consent records and executed contracts are all
    keyed to this dealer_id; a conversion that created a new file would strand
    every one of them. Setting a timestamp is what makes the transfer total.

    The rep keeps attribution: the pipeline row graduates to complete so the
    file counts as a conversion in production, and owner_user_id stays so the
    rep can still see the client they brought in."""
    require_super_admin(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    if dealer.audit_client_since is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This file is already an audit client.")
    dealer.audit_client_since = datetime.now(timezone.utc)
    lead = (
        await db.execute(
            select(DealerRepLead).where(DealerRepLead.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    if lead is not None and lead.status not in ("complete", "declined"):
        history = list(lead.status_history or [])
        history.append({
            "at": dealer.audit_client_since.isoformat(),
            "from": lead.status, "to": "complete",
            "by": str(user.id), "by_name": user.name,
            "note": "converted to audit client",
        })
        lead.status = "complete"
        lead.status_history = history
        lead.completed_at = dealer.audit_client_since
    # The client's Capital OS login, sent as part of the upgrade when asked.
    # An audit client without a login is a subscription nobody can use, so the
    # admin UI asks by default — but it stays optional, because sometimes the
    # desk converts first and onboards the client on a call later. The invite
    # can only ever mint Role.DEALER landing on audit., so this can never hand
    # a client the Field Desk.
    invite: DealerInviteResult | None = None
    invite_error: str | None = None
    req = payload or ConvertToAuditRequest()
    if req.send_login_invite:
        email = (req.login_email or "").strip() or dealer.email or ""
        if not email:
            owner_email = (
                await db.execute(
                    select(DealerOwner.email)
                    .where(DealerOwner.dealer_id == dealer.id, DealerOwner.email.is_not(None))
                    .order_by(DealerOwner.is_primary.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            email = owner_email or ""
        await _require_training_live_action(
            db,
            dealer=dealer,
            user=user,
            request=request,
            action="Convert file and invite client",
            provider="Clerk email",
            recipient=email or None,
            effect="Convert this file to an audit client and send a live login invitation.",
        )
        if email:
            try:
                invite = await _invite_dealer_login_core(db, dealer, email, None)
            except HTTPException as exc:
                # The conversion itself must not fail because the email clashes
                # with a staff account; report it and let the desk fix the email.
                invite_error = str(exc.detail)
        else:
            invite_error = "No email on the file to invite. Add one and invite from the file."

    await log_action(
        db, dealer.id, user, "dealer.converted_to_audit", "dealer",
        entity_id=dealer.id,
        after={"audit_client_since": dealer.audit_client_since.isoformat(),
               "login_invited": bool(invite and invite.status == "invited"),
               "login_linked": bool(invite and invite.status == "linked")},
    )
    await db.commit()
    await db.refresh(dealer)
    return ConvertToAuditResult(
        dealer=await _dealer_read(db, dealer),
        invite=invite,
        invite_error=invite_error,
    )


@router.post("/dealers/{dealer_id}/room/access-code", response_model=ClientRequestResult)
async def rotate_room_access_code(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ClientRequestResult:
    """A fresh access code, displayed to the rep to read out.

    Rotation rather than retrieval: the stored code is a hash and cannot be
    shown again, so "show me the code" always means "make a new one". The old
    code stops working the moment this returns, which is also the recovery
    path when a code has leaked."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    room = await client_room.rotate_passcode(db, dealer)
    await log_action(
        db, dealer.id, user, "room.passcode_rotated", "dealer", entity_id=dealer.id,
        after={"link_id": str(room.link.id)},
    )
    await db.commit()
    return ClientRequestResult(url=room.url, passcode=room.passcode, delivered=False)


async def _room_code_hash_by_token(db: AsyncSession, token: str) -> str | None:
    """Resolve the consent token to its file's room-code hash WITHOUT touching
    the token: the code check must run before consumption, or wrong guesses
    would burn the owner's single-use link."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    owner = (
        await db.execute(
            select(DealerOwner.dealer_id).where(DealerOwner.invite_token_hash == token_hash)
        )
    ).scalar_one_or_none()
    if owner is None:
        return None
    return await _room_code_hash(db, owner)


async def _room_code_hash(db: AsyncSession, dealer_id: UUID) -> str | None:
    """The active room link's passcode hash for this file, or None.

    None means either no room yet or a legacy link without a code — both are
    treated as "no code required" so links minted before this gate existed
    keep working. Every new credit invite ensures a room first, so new links
    always carry the gate."""
    from app.models.bucket import Bucket

    row = (
        await db.execute(
            select(BucketUploadLink.passcode_hash)
            .join(Bucket, Bucket.id == BucketUploadLink.bucket_id)
            .join(DealerBusiness, DealerBusiness.bucket_id == Bucket.id)
            .where(
                DealerBusiness.id == dealer_id,
                BucketUploadLink.status == "active",
            )
            .order_by(BucketUploadLink.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def _statement_month_coverage(
    db: AsyncSession, dealer_id: UUID
) -> tuple[list[str], list[str], str]:
    """Six current months proved by bank-produced statement artifacts.

    CSVs, screenshots, and financial periods derived from supplemental files
    are useful analysis inputs but cannot satisfy an official-statement gate.
    A Plaid connection also does not count until statement PDFs are imported.
    """
    months: set[str] = set()
    has_plaid_statement = False
    doc_rows = (
        await db.execute(
            select(
                DealerDocument.content_type,
                DealerDocument.plaid_item_id,
                DealerDocument.kind,
                DealerDocument.detected_kind,
                DealerDocument.extracted,
            ).where(
                DealerDocument.dealer_id == dealer_id,
                DealerDocument.status == "extracted",
            )
        )
    ).all()
    for content_type, plaid_item_id, kind, detected_kind, extracted in doc_rows:
        effective = detected_kind or _KIND_TO_DETECTED.get(kind)
        if effective != "bank_statement":
            continue
        is_plaid_statement = plaid_item_id is not None
        is_bank_pdf = str(content_type or "").lower() == "application/pdf"
        if not (is_plaid_statement or is_bank_pdf):
            continue
        has_plaid_statement = has_plaid_statement or is_plaid_statement
        for m in (extracted or {}).get("months") or []:
            key = str(m.get("month") or "") if isinstance(m, dict) else ""
            if _COVERAGE_MONTH_RE.match(key):
                months.add(key)

    freshness = recurrence.compute_freshness(months, date.today(), window=6)
    source = "plaid" if has_plaid_statement else ("upload" if months else "none")
    return sorted(months), list(freshness.get("missing_months") or []), source


async def _assess_verification(db: AsyncSession, dealer: DealerBusiness):
    """Read the two authorizations off the file.

    Bank readiness requires six current bank-produced statement months.
    Plaid is a transport, not evidence by itself; a connected item cannot
    unlock underwriting until its statement artifacts have been imported.

    Credit is complete only when ownership totals 100% and every disclosed
    owner at 20% or more has completed their own pull. Each pull remains tied
    to its owner row and consent link.
    """
    statement_months, missing_statement_months, statement_source = await _statement_month_coverage(
        db, dealer.id
    )
    bank_linked = len(statement_months) >= 6 and not missing_statement_months
    bank_source = statement_source if bank_linked else "none"
    owner_state = await _owner_requirement_state(db, dealer.id)
    pre_screen = await _application_pre_screen_state(db, dealer, owner_state)
    credit_returned = bool(
        owner_state["ownership_complete"]
        and owner_state["contact_complete"]
        and owner_state["required"]
        and len(owner_state["completed"]) == len(owner_state["required"])
    )
    return decision.assess_verification(
        bank_linked=bank_linked,
        bank_source=bank_source,
        statement_months=statement_months,
        missing_statement_months=missing_statement_months,
        credit_returned=credit_returned,
        ownership_total=owner_state["ownership_total"],
        ownership_complete=owner_state["ownership_complete"],
        owner_contact_complete=owner_state["contact_complete"],
        missing_credit_contact_owner_ids=[owner.id for owner in owner_state["missing_contact"]],
        required_credit_owner_count=len(owner_state["required"]),
        completed_credit_owner_count=len(owner_state["completed"]),
        pending_credit_owner_ids=[owner.id for owner in owner_state["pending"]],
        pre_screen_complete=pre_screen["complete"],
        pre_screen_blockers=pre_screen["blockers"],
        preliminary_program_fit=pre_screen["routing_result"],
    )


@router.get("/dealers/{dealer_id}/decision", response_model=DecisionRead)
async def dealer_decision(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DecisionRead:
    """THE answer for this file, with the balance rule actually applied.

    /fundability reads the program grid alone. That grid can say "fundable"
    about a business whose ending balances fall every month, which is the first
    thing a lender looks at and the first thing that gets the file declined. So
    this composes both and lets the balance rule cap the verdict, and every
    surface reads from here rather than deciding for itself.

    Open to the owning rep: the rep is who has to tell the owner where they
    stand, and sending them to another app to find out is how a visit ends
    without a next step."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    ver = await _assess_verification(db, dealer)
    pre_screen = await _application_pre_screen_state(db, dealer)
    direct_programs = [
        {
            "key": row.get("program_key"),
            "label": row.get("name"),
            "eligible": row.get("status") != "blocked",
            "needs": list(row.get("unresolved") or []),
            "blocked_by": list(row.get("borrower_safe_reasons") or []),
            "matched_rules": list(row.get("matched_rules") or []),
            "rules_version": (pre_screen.get("routing_result") or {}).get("rules_version"),
        }
        for row in ((pre_screen.get("routing_result") or {}).get("programs") or [])
    ]

    try:
        metrics = await _latest_snapshot_metrics(db, dealer.id)
    except HTTPException:
        d = decision.decide({"verdict": "no_data"}, None, ver)
        return DecisionRead(**asdict(d), programs=direct_programs)

    targets = await _effective_targets(db, dealer.id)
    settings = await _global_program_settings(db)
    tree = {**metrics, "deposits_monthly_avg": await _monthly_deposits_avg(db, dealer.id)}
    paths = compute_paths(tree, targets, settings=settings)

    goal = float(dealer.funding_goal) if dealer.funding_goal is not None else None
    goal_paths = None
    if goal:
        goal_paths = []
        for key in PATH_KEYS:
            reqs = requirements_for_amount(key, goal, tree, settings=settings)
            goal_paths.append(
                {
                    "path_key": key,
                    "goal_feasible": (all(r["met"] for r in reqs) if reqs else None),
                    "requirements": reqs,
                }
            )
    fundability = paths_service_fundability(paths, goal, goal_paths)

    # Most-recent-first, which is the order assess_balance_health expects.
    # Account-level rows are excluded: the rule is about the business, and a
    # single account dipping while the business holds steady is not the
    # failure the rule is describing.
    period_rows = (
        (
            await db.execute(
                select(DealerFinancialPeriod)
                .where(
                    DealerFinancialPeriod.dealer_id == dealer.id,
                    DealerFinancialPeriod.account_id.is_(None),
                )
                .order_by(DealerFinancialPeriod.period.desc())
                .limit(12)
            )
        )
        .scalars()
        .all()
    )
    health = balance_health.assess_balance_health(
        [{"period": r.period, "ending_balance": r.ending_balance} for r in period_rows]
    )

    out = decision.decide(fundability, health, ver)
    return DecisionRead(**asdict(out), programs=direct_programs)


@router.get("/dealers/{dealer_id}", response_model=DealerRead)
async def get_dealer(dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> DealerRead:
    """One file. resolve_dealer_scope is what confines each role: a DEALER to
    their own business, a FIELD_REP to files they own (404, never 403, so ids
    stay unprobeable), the team to everything."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    r = await _dealer_read(db, dealer)
    if user.role == Role.DEALER:
        r.notes = None  # internal advisor commentary is team-only
    return r


@router.patch("/dealers/{dealer_id}", response_model=DealerRead)
async def update_dealer(
    dealer_id: UUID,
    payload: DealerUpdate,
    background: BackgroundTasks,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerRead:
    require_team_or_dealer_or_rep(user)
    # Scope FIRST, once, for everyone.
    #
    # This handler used to resolve inside the DEALER branch and fall through to
    # an unscoped load_dealer for the team branch. That was correct while only
    # the team could reach it, and became a hole the moment a field rep could:
    # a rep would land in the team branch and be able to PATCH any file in the
    # book. resolve_dealer_scope returns anything for the team, so hoisting it
    # costs the desk nothing and closes it.
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    if user.role == Role.DEALER:
        # A client may complete the always-required business-profile fields
        # on their OWN file — nothing else.
        changes = payload.model_dump(exclude_unset=True)
        allowed = {
            "legal_name", "ein", "entity_type", "started_on",
            "industry", "industry_label", "subindustry", "subindustry_label",
            "industry_entry_id", "subindustry_entry_id", "activity_entry_id",
            "naics_code", "naics_label",
        }
        illegal = sorted(set(changes) - allowed)
        if illegal:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"These fields are maintained by your advisor: {', '.join(illegal)}",
            )
        taxonomy_fields = {
            "industry", "industry_label", "subindustry", "subindustry_label",
            "industry_entry_id", "subindustry_entry_id", "activity_entry_id",
            "naics_code", "naics_label",
        }
        if taxonomy_fields.intersection(changes):
            current = {
                "industry_entry_id": changes.get("industry_entry_id", dealer.industry_entry_id),
                "subindustry_entry_id": changes.get("subindustry_entry_id", dealer.subindustry_entry_id),
                "activity_entry_id": changes.get("activity_entry_id", dealer.activity_entry_id),
            }
            taxonomy = await application_taxonomy.canonicalize_selection(db, current, required=False)
            changes.update({key: value for key, value in taxonomy.items() if key != "taxonomy_status"})
        before = {k: getattr(dealer, k) for k in changes}
        for k, v in changes.items():
            setattr(dealer, k, v)
        if changes:
            await log_action(
                db, dealer.id, user, "dealer.profile_update", "dealer",
                entity_id=dealer.id, before=before, after=changes,
            )
        await db.commit()
        await db.refresh(dealer)
        r = await _dealer_read(db, dealer)
        r.notes = None
        return r
    changes = payload.model_dump(exclude_unset=True)
    if "status" in changes and user.role != Role.SUPER_ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only a super admin can change the file status.",
        )
    taxonomy_fields = {
        "industry", "industry_label", "subindustry", "subindustry_label",
        "industry_entry_id", "subindustry_entry_id", "activity_entry_id",
        "naics_code", "naics_label",
    }
    if taxonomy_fields.intersection(changes):
        current = {
            "industry_entry_id": changes.get("industry_entry_id", dealer.industry_entry_id),
            "subindustry_entry_id": changes.get("subindustry_entry_id", dealer.subindustry_entry_id),
            "activity_entry_id": changes.get("activity_entry_id", dealer.activity_entry_id),
        }
        taxonomy = await application_taxonomy.canonicalize_selection(db, current, required=False)
        changes.update({key: value for key, value in taxonomy.items() if key != "taxonomy_status"})
    before = {k: getattr(dealer, k) for k in changes}
    bucket_changed = (
        "bucket_id" in changes and changes["bucket_id"] != dealer.bucket_id
    )
    if bucket_changed and changes["bucket_id"] is not None:
        target = await db.get(Bucket, changes["bucket_id"])
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Bucket not found")
    if changes.get("group_id") is not None:
        await _require_group(db, changes["group_id"])
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
    if bucket_changed and dealer.bucket_id is not None:
        # Ingestion is not optional: a newly linked bucket's files flow into
        # the pipeline automatically (idempotent, background, capped).
        background.add_task(_background_ingest_bucket_files, dealer.id)
    return await _dealer_read(db, dealer)


async def _invite_dealer_login_core(
    db: AsyncSession, dealer: DealerBusiness, email: str, name: str | None
) -> DealerInviteResult:
    """Create-or-link the client's Capital OS login and send the Clerk invite.

    The invite ALWAYS lands on audit.qualifiedcommercial.com/sign-in and the
    account is ALWAYS Role.DEALER. That pair is what keeps a client out of the
    Field Desk: rep.qualifiedcommercial.com walls every non-rep, non-team role
    before a single page renders, so the only door this login opens is their
    own audit portal. Flushes; the caller commits."""
    email = email.strip().lower()
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
            existing.name = name or existing.name
            existing.role = Role.DEALER
            existing.clerk_id = None
            target = existing
        else:
            target = User(
                email=email,
                name=name or f"{dealer.name} owner",
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
    await db.flush()
    return DealerInviteResult(status=result_status, email=email, user_id=target.id, clerk_sent=clerk_sent)


@router.post("/dealers/{dealer_id}/invite", response_model=DealerInviteResult, status_code=status.HTTP_201_CREATED)
async def invite_dealer_login(
    dealer_id: UUID,
    payload: DealerInvite,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerInviteResult:
    """Invite (or link) the dealer's self-serve login. Creates the local User
    row with Role.DEALER (clerk_id JIT-bound on first sign-in, same pattern as
    the operator invite flow), links it via dealer_user_id, and best-effort
    sends a Clerk invitation email that lands on audit.qualifiedcommercial.com."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    if dealer.is_training and user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    await _require_training_live_action(
        db,
        dealer=dealer,
        user=user,
        request=request,
        action="Invite client login",
        provider="Clerk email",
        recipient=payload.email,
        effect="Create or link the client login and send a live account invitation.",
    )
    result = await _invite_dealer_login_core(db, dealer, payload.email, payload.name)
    await db.commit()
    return result


def _target_read(t: DealerMetricTarget) -> TargetRead:
    r = TargetRead.model_validate(t)
    r.effective_value = float(t.effective_value) if t.effective_value is not None else None
    return r


@router.get("/dealers/{dealer_id}/targets", response_model=list[TargetRead])
async def list_targets(dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[TargetRead]:
    require_team_or_dealer_or_rep(user)
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
    rows = await propose_targets(db, dealer)
    await db.commit()
    return [_target_read(t) for t in rows]


@router.put("/dealers/{dealer_id}/targets", response_model=TargetRead)
async def override_target(
    dealer_id: UUID, payload: TargetOverride, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> TargetRead:
    """Set (or clear, with admin_value=null) the admin override. Override wins."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    row = (
        await db.execute(
            select(DealerMetricTarget).where(
                DealerMetricTarget.dealer_id == dealer.id,
                DealerMetricTarget.metric_key == payload.metric_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown metric for this client — propose targets first")
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
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
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    q = select(DealerCashEvent).where(DealerCashEvent.dealer_id == dealer.id)
    if period is not None:
        q = q.where(DealerCashEvent.period == period)
    q = q.order_by(DealerCashEvent.occurred_on.asc()).limit(limit)
    return (await db.execute(q)).scalars().all()


_MONTH_PARAM_RE = re.compile(r"^(\d{4})-(\d{2})$")


def _search_row(event: DealerCashEvent, document_filename: str | None) -> CashEventSearchRow:
    row = CashEventSearchRow.model_validate(event)
    row.document_filename = document_filename
    return row


@router.get("/dealers/{dealer_id}/cash-events/search", response_model=CashEventSearchRead)
async def search_cash_events(
    dealer_id: UUID,
    user: CurrentUser,
    q: str = Query(default=""),
    month: str = Query(default=""),
    account_id: UUID | None = Query(default=None),
    unassigned: bool = Query(default=False),
    category: str = Query(default=""),
    direction: str = Query(default="", pattern="^(|in|out)$"),
    flag: str = Query(default=""),
    document_id: UUID | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=75, ge=1, le=75),
    db: AsyncSession = Depends(get_db),
) -> CashEventSearchRead:
    """Activity explorer (0119): paged, filterable ledger search with source-
    document provenance joined in. The old list endpoint stays untouched —
    this is the composable read the frontend explorer codes against.

    Ordered occurred_on DESC, id DESC. One count query + one page query."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)

    filters = [DealerCashEvent.dealer_id == dealer.id]
    needle = q.strip().lower()
    if needle:
        filters.append(func.lower(DealerCashEvent.description).contains(needle, autoescape=True))
    month_key = month.strip()
    if month_key:
        m = _MONTH_PARAM_RE.match(month_key)
        if m is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "month must be YYYY-MM")
        y, mo = int(m.group(1)), int(m.group(2))
        if not 1 <= mo <= 12:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "month must be YYYY-MM")
        try:
            filters.append(DealerCashEvent.period == date(y, mo, 1))
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "month must be a real YYYY-MM month"
            ) from None
    if unassigned:
        filters.append(DealerCashEvent.account_id.is_(None))
    elif account_id is not None:
        filters.append(DealerCashEvent.account_id == account_id)
    if category.strip():
        filters.append(DealerCashEvent.category == category.strip())
    if direction == "in":
        filters.append(DealerCashEvent.amount > 0)
    elif direction == "out":
        filters.append(DealerCashEvent.amount < 0)
    if flag.strip():
        # JSONB `?` (has_key) — presence of the flag key, whatever its value.
        filters.append(DealerCashEvent.flags.has_key(flag.strip()))
    if document_id is not None:
        filters.append(DealerCashEvent.document_id == document_id)

    total = int(
        (
            await db.execute(select(func.count()).select_from(DealerCashEvent).where(*filters))
        ).scalar_one()
        or 0
    )
    page = (
        await db.execute(
            select(DealerCashEvent, DealerDocument.filename)
            .outerjoin(DealerDocument, DealerDocument.id == DealerCashEvent.document_id)
            .where(*filters)
            .order_by(DealerCashEvent.occurred_on.desc(), DealerCashEvent.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return CashEventSearchRead(
        total=total,
        offset=offset,
        limit=limit,
        rows=[_search_row(ev, filename) for ev, filename in page],
    )


@router.post("/dealers/{dealer_id}/cash-events/{event_id}/recurrence")
async def mark_event_recurrence(
    dealer_id: UUID,
    event_id: UUID,
    payload: RecurrenceMark,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """ADMIN recurrence override: mark a transaction (and optionally every
    line from the same counterparty) as recurring or one-time when detection
    lacks the history to see it. Human correction is law — marked rows get
    categorized_by='admin', which the stamping engine skips, and the live
    recurring view overlays these marks on top of detection."""
    require_team(user)
    is_dealer_actor = user.role == Role.DEALER
    dealer = await _load_visible_dealer(db, dealer_id, user)
    event = (
        await db.execute(
            select(DealerCashEvent).where(
                DealerCashEvent.id == event_id, DealerCashEvent.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cash event not found for this client")

    def _team_locked(row) -> bool:
        return (
            row.categorized_by == "admin"
            and isinstance(row.flags, dict)
            and bool(row.flags.get("manual_recurrence"))
        )

    if is_dealer_actor and _team_locked(event):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "This line was marked by your advisor — ask them to change it.",
        )
    targets = [event]
    vendor_key = vendors.normalize_vendor(event.description or "")
    if payload.apply_similar and vendor_key:
        rows = (
            await db.execute(
                select(DealerCashEvent)
                .where(DealerCashEvent.dealer_id == dealer.id, DealerCashEvent.id != event.id)
                .order_by(DealerCashEvent.occurred_on.desc())
                .limit(2000)
            )
        ).scalars().all()
        similar = [r for r in rows if vendors.normalize_vendor(r.description or "") == vendor_key]
        if is_dealer_actor:
            similar = [r for r in similar if not _team_locked(r)]
        targets += similar[:499]

    for row in targets:
        flags = dict(row.flags) if isinstance(row.flags, dict) else {}
        if payload.mark == "recurring":
            flags["manual_recurrence"] = "recurring"
            flags["recurring"] = True
            flags.pop("one_time", None)
        elif payload.mark == "one_time":
            flags["manual_recurrence"] = "one_time"
            flags["recurring"] = False
            flags["one_time"] = True
            flags["irregular"] = True
            flags.pop("cadence", None)
            flags.pop("recurrence_key", None)
        elif payload.mark == "none":
            # Ordinary payment — neither recurring nor irregular. The remove
            # affordance on both panels lands here.
            flags["manual_recurrence"] = "none"
            flags["recurring"] = False
            flags["irregular"] = False
            flags.pop("one_time", None)
            flags.pop("cadence", None)
            flags.pop("recurrence_key", None)
        else:  # clear — hand the rows back to automatic detection
            flags.pop("manual_recurrence", None)
            flags.pop("one_time", None)
        row.flags = flags
        if payload.mark != "clear":
            row.categorized_by = "dealer" if is_dealer_actor else "admin"
    await log_action(
        db, dealer.id, user, "cash_event.recurrence_mark", "cash_event",
        entity_id=event.id,
        after={"mark": payload.mark, "apply_similar": payload.apply_similar,
               "updated": len(targets), "vendor_key": vendor_key or None},
    )
    await db.commit()
    return {"updated": len(targets), "mark": payload.mark, "vendor_key": vendor_key or None}


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
    dealer = await _load_visible_dealer(db, dealer_id, user)
    event = (
        await db.execute(
            select(DealerCashEvent).where(
                DealerCashEvent.id == event_id, DealerCashEvent.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cash event not found for this client")
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
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    rows = (
        await db.execute(
            select(
                DealerCashEvent.id,
                DealerCashEvent.occurred_on,
                DealerCashEvent.description,
                DealerCashEvent.amount,
                DealerCashEvent.category,
                DealerCashEvent.flags,
            )
            .where(DealerCashEvent.dealer_id == dealer.id)
            # Newest window — over-cap ledgers drop the OLDEST rows, never the
            # newest (otherwise every group reads as frozen/overdue).
            .order_by(DealerCashEvent.occurred_on.desc())
            .limit(recurrence.MAX_EVENTS)
        )
    ).all()
    today = date.today()
    manual = {}
    lite = [
        recurrence.EventLite(rid, occurred_on, description or "", float(amount or 0))
        for rid, occurred_on, description, amount, _category, _flags in rows
    ]
    category_by_id = {rid: category for rid, _o, _d, _a, category, _f in rows}
    for rid, _o, _d, _a, _c, flags in rows:
        mark = (flags or {}).get("manual_recurrence") if isinstance(flags, dict) else None
        if mark in ("recurring", "one_time", "none"):
            manual[rid] = mark
    groups = recurrence.detect_groups(lite)  # already sorted by |monthly_equivalent| desc
    if manual:
        groups, force_irregular = recurrence.apply_manual_marks(lite, manual, groups)
    else:
        force_irregular = set()
    irregular = recurrence.classify_irregular(lite, groups)
    suppressed = {rid for rid, mark in manual.items() if mark in ("none", "recurring")}
    if suppressed:
        irregular = [e for e in irregular if e.id not in suppressed]
    if force_irregular:
        by_id = {e.id: e for e in lite}
        have = {e.id for e in irregular}
        irregular = list(irregular) + [
            by_id[i] for i in force_irregular if i in by_id and i not in have
        ]
        irregular.sort(key=lambda e: e.occurred_on, reverse=True)
    irregular.sort(key=lambda e: e.occurred_on, reverse=True)
    return RecurringRead(
        groups=[
            RecurringGroupRead(
                key=g.key,
                sample_event_id=(g.event_ids[-1] if g.event_ids else None),
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
    require_team_or_dealer_or_rep(user)
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
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
_DOCUMENT_KINDS = {"statement", "pl", "tax", "debt_schedule", "loan_agreement", "other", "archive"}


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
    require_team_or_dealer_or_rep(user)
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
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found for this client")
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
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    q = select(DealerDocument).where(DealerDocument.dealer_id == dealer.id)
    if user.role == Role.DEALER:
        q = q.where(DealerDocument.status != "failed")
    rows = (
        (await db.execute(q.order_by(DealerDocument.created_at.desc()))).scalars().all()
    )
    if user.role == Role.DEALER:
        # Storage keys are an internal detail — previews go through /url.
        out = [DocumentRead.model_validate(r) for r in rows]
        for r in out:
            r.s3_key = None
        return out
    return rows


_DOCUMENT_URL_TTL = 900  # seconds — matches the buckets presign posture


@router.get("/dealers/{dealer_id}/documents/{doc_id}/url", response_model=DocumentUrlRead)
async def document_url(
    dealer_id: UUID,
    doc_id: UUID,
    user: CurrentUser,
    download: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
) -> DocumentUrlRead:
    """Short-lived presigned URL for the document's original bytes (0119) —
    the 'open the PDF' bridge for provenance links. Key resolution: the
    document's own archive first, else the mirrored bucket file's object.
    409 when neither holds bytes; 503 when presigning is unavailable."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    doc = await _load_document(db, dealer.id, doc_id)
    if user.role == Role.DEALER and doc.status == "failed":
        # Parity with list_documents: failed rows are not dealer-facing.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found for this client")
    key = doc.s3_key
    if not key and doc.bucket_file_id is not None:
        key = (
            await db.execute(
                select(BucketFile.s3_key).where(
                    BucketFile.id == doc.bucket_file_id, BucketFile.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
    if not key:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The original bytes were not archived — re-upload to enable preview",
        )
    url = storage.presign_get(
        key,
        ttl=_DOCUMENT_URL_TTL,
        disposition="attachment" if download else "inline",
        content_type=doc.content_type,
    )
    if url is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Document preview is unavailable — S3 presigning is not configured",
        )
    return DocumentUrlRead(
        url=url,
        expires_in=_DOCUMENT_URL_TTL,
        filename=doc.filename,
        content_type=doc.content_type,
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
    covered by official statement artifacts, which tax years have a filing
    row, whether a P&L / debt schedule has landed, and how many requests are
    still open. Supplemental CSV and screenshot data never satisfies the
    official statement count."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)

    covered_months, _, _ = await _statement_month_coverage(db, dealer.id)
    months: set[str] = set(covered_months)

    has_pl = False
    has_debt_schedule = bool(
        (
            await db.execute(
                select(func.count()).select_from(DealerDebt).where(
                    DealerDebt.dealer_id == dealer.id, DealerDebt.status == "active"
                )
            )
        ).scalar_one()
    )
    doc_rows = (
        await db.execute(
            select(
                DealerDocument.kind, DealerDocument.detected_kind, DealerDocument.extracted
            ).where(
                DealerDocument.dealer_id == dealer.id, DealerDocument.status == "extracted"
            )
        )
    ).all()
    for kind, detected_kind, _extracted in doc_rows:
        effective = detected_kind or _KIND_TO_DETECTED.get(kind)
        if effective == "profit_and_loss":
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
        **recurrence.compute_freshness(months, date.today(), window=6),
    )


@router.get("/dealers/{dealer_id}/pipeline", response_model=PipelineStatusRead)
async def pipeline_status(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> PipelineStatusRead:
    """Live ingestion state for the cockpit header.

    Deliberately cheap — the header polls this while work is moving. Five
    counting queries, no payload scans."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)

    by_status: dict[str, int] = {
        str(s): int(n)
        for s, n in (
            await db.execute(
                select(DealerDocument.status, func.count())
                .where(DealerDocument.dealer_id == dealer.id)
                .group_by(DealerDocument.status)
            )
        ).all()
    }

    # Linked-bucket files not yet pulled = queued work the browser can't see.
    bucket_pending = 0
    if dealer.bucket_id is not None:
        bucket_pending = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(BucketFile)
                    .where(
                        BucketFile.bucket_id == dealer.bucket_id,
                        BucketFile.deleted_at.is_(None),
                        ~select(DealerDocument.id)
                        .where(
                            DealerDocument.dealer_id == dealer.id,
                            DealerDocument.bucket_file_id == BucketFile.id,
                        )
                        .exists(),
                    )
                )
            ).scalar_one()
            or 0
        )

    last = (
        await db.execute(
            select(DealerDocument.filename, DealerDocument.updated_at)
            .where(DealerDocument.dealer_id == dealer.id, DealerDocument.status == "extracted")
            .order_by(DealerDocument.updated_at.desc())
            .limit(1)
        )
    ).first()

    months = int(
        (
            await db.execute(
                select(func.count(func.distinct(DealerFinancialPeriod.period))).where(
                    DealerFinancialPeriod.dealer_id == dealer.id
                )
            )
        ).scalar_one()
        or 0
    )
    tax_years = int(
        (
            await db.execute(
                select(func.count()).select_from(DealerTaxFiling).where(
                    DealerTaxFiling.dealer_id == dealer.id
                )
            )
        ).scalar_one()
        or 0
    )
    accounts = int(
        (
            await db.execute(
                select(func.count()).select_from(DealerAccount).where(
                    DealerAccount.dealer_id == dealer.id
                )
            )
        ).scalar_one()
        or 0
    )

    in_flight = by_status.get("uploaded", 0) + by_status.get("extracting", 0)
    return PipelineStatusRead(
        extracted=by_status.get("extracted", 0),
        failed=by_status.get("failed", 0),
        pending_review=by_status.get("pending_review", 0),
        in_flight=in_flight,
        bucket_pending=bucket_pending,
        active=bool(in_flight or bucket_pending),
        last_completed_at=last[1] if last else None,
        last_completed_name=last[0] if last else None,
        months_covered=months,
        tax_years_covered=tax_years,
        accounts=accounts,
    )


@router.post("/dealers/{dealer_id}/documents/{doc_id}/extract", response_model=DocumentRead)
async def reextract_document(
    dealer_id: UUID, doc_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerDocument:
    """Re-run extraction from the S3 archive. 409 when the original bytes were
    never archived (S3 unconfigured at upload time) — re-upload instead."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    doc = (
        await db.execute(
            select(DealerDocument).where(
                DealerDocument.id == doc_id, DealerDocument.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found for this client")
    if not doc.s3_key:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The original file was not archived to S3 — upload the document again to re-extract it",
        )
    # Replace, never append: drop this document's prior ledger lines first
    # (document_id makes the ownership exact), remember their months, and
    # rebuild any month the fresh extraction no longer covers.
    old_periods = set(
        (
            await db.execute(
                select(DealerCashEvent.period).where(
                    DealerCashEvent.dealer_id == dealer.id,
                    DealerCashEvent.document_id == doc.id,
                )
            )
        ).scalars().all()
    )
    if old_periods:
        from sqlalchemy import delete as sa_delete

        await db.execute(
            sa_delete(DealerCashEvent).where(
                DealerCashEvent.dealer_id == dealer.id,
                DealerCashEvent.document_id == doc.id,
            )
        )
        await db.flush()
    await extract_document(db, doc)
    if old_periods:
        try:
            await rebuild_periods(db, dealer.id, sorted(old_periods))
            await recompute_snapshot(db, dealer.id)
        except Exception:
            logger.exception("dealer-os: post-reextract rebuild failed for %s", doc.id)
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found for this client")
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
    doc = await _load_document(db, dealer.id, doc_id)
    if doc.status not in ("pending_review", "rejected"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Document is not awaiting review (status: {doc.status})",
        )
    if not doc.s3_key:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The original file was not archived to S3 — ask the client to upload it again",
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
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


@router.get("/dealers/{dealer_id}/bucket-files/{file_id}/url", response_model=DocumentUrlRead)
async def bucket_file_url(
    dealer_id: UUID,
    file_id: UUID,
    user: CurrentUser,
    download: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
) -> DocumentUrlRead:
    """Presigned URL for a file in the dealer's LINKED bucket (team only —
    bucket contents are not dealer-facing). The file must belong to the linked
    bucket and not be deleted. Same shape/TTL as the document URL."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    bucket_file = None
    if dealer.bucket_id is not None:
        bucket_file = (
            await db.execute(
                select(BucketFile).where(
                    BucketFile.id == file_id,
                    BucketFile.bucket_id == dealer.bucket_id,
                    BucketFile.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if bucket_file is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "File not found in this client's linked bucket"
        )
    if not bucket_file.s3_key:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The original bytes were not archived — re-upload to enable preview",
        )
    url = storage.presign_get(
        bucket_file.s3_key,
        ttl=_DOCUMENT_URL_TTL,
        disposition="attachment" if download else "inline",
        content_type=bucket_file.content_type,
    )
    if url is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "File preview is unavailable — S3 presigning is not configured",
        )
    return DocumentUrlRead(
        url=url,
        expires_in=_DOCUMENT_URL_TTL,
        filename=bucket_file.file_name,
        content_type=bucket_file.content_type or "application/octet-stream",
    )


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
    dealer = await _load_visible_dealer(db, dealer_id, user)
    doc = await _ingest_bucket_file_core(db, dealer, file_id)
    await db.commit()
    await db.refresh(doc)
    return doc


async def _ingest_bucket_file_core(
    db: AsyncSession, dealer: DealerBusiness, file_id: UUID
) -> DealerDocument:
    """Shared ingest core (endpoint + background auto-ingest). Idempotent: a
    bucket file already referenced by a DealerDocument is returned as-is,
    never double-counted. Flushes; the caller commits."""
    existing = (
        await db.execute(
            select(DealerDocument).where(
                DealerDocument.dealer_id == dealer.id,
                DealerDocument.bucket_file_id == file_id,
            )
        )
    ).scalars().first()
    if existing is not None:
        return existing
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found in this client's linked bucket")

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
    cached = analysis_row.analysis if analysis_row is not None else None
    # The cache only serves documents it can actually classify. Anything else
    # falls through to a full re-extract below — a cache miss is an extra model
    # call, never a failed document.
    cached_kind = (
        buckets_link.classify_cached_analysis(cached) if isinstance(cached, dict) else None
    )
    if cached_kind is not None:
        # Cache path — asserts no model call: adapt the stored analysis JSON
        # into the canonical extraction dict and persist via the same plan.
        #
        # Routing mirrors extract.extract_document exactly: a statement writes
        # ledger rows, a tax return upserts dos_tax_filings, and any other
        # classified document is a SUCCESS with its summary stored. Treating a
        # tax return as "failed" because it has no monthly bank data was the
        # old behaviour and it discarded fully-extracted filings.
        extraction = buckets_link.adapt_analysis_to_extraction(cached)
        rules = await load_active_rules(db, dealer.id)
        plan = apply_extraction(extraction, rules=rules)
        notes = [f"{source_note} via cached analysis (no model call)"] + plan["notes"]
        doc.detected_kind = cached_kind
        routed = True

        if cached_kind == "bank_statement" and (plan["events"] or plan["period_upserts"]):
            await _persist_plan(db, dealer.id, plan, document_id=doc.id)
        elif cached_kind == "tax_return":
            tax_years = buckets_link.adapt_analysis_to_tax_years(cached)
            if not tax_years:
                # Classified as a return but carrying no readable year — let
                # the model have a look rather than dropping it.
                routed = False
            elif buckets_link.is_business_return(cached):
                upserted = await _route_tax_years(db, dealer.id, tax_years, notes, document_id=doc.id)
                years = ", ".join(str(t["year"]) for t in tax_years)
                notes.append(f"Business tax return {years}: upserted {upserted} filing(s).")
                # Keep the return's own figures (0117) so EBITDA can be rebuilt
                # from the filing — bank statements carry no income statement,
                # and without this the whole EBITDA -> DSCR chain stays null.
                await _store_tax_detail(db, dealer.id, tax_years, cached, document_id=doc.id)
            else:
                # An owner's personal return: stored and classified, but it
                # never writes the business's filing row (dos_tax_filings is
                # one row per year and drives deposit reconciliation).
                years = ", ".join(str(t["year"]) for t in tax_years)
                notes.append(
                    f"Personal tax return {years} — stored for the owner's file; "
                    "no business filing row written."
                )
        elif cached_kind == "bank_statement":
            # Classified as a statement but the cache held no usable months.
            routed = False
        else:
            notes.append(
                f"Classified as {cached_kind} — summary stored, no ledger rows written."
            )

        if routed:
            doc.extracted = {
                "months": plan["months"],
                "transactions_count": len(plan["events"]),
                "notes": notes[:50],
                "parser": "bucket_analysis_cache",
                "doc_type": cached_kind,
            }
            doc.status = "extracted"
            doc.error = None
            await db.flush()
            return doc
        logger.info(
            "dealer-os: cached analysis for bucket file %s classified as %s but held no "
            "usable payload — falling back to full extract",
            bucket_file.id, cached_kind,
        )

    raw = storage.get_bytes(bucket_file.s3_key)
    if raw is None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Could not fetch the bucket file from S3 — try again or re-upload it to Capital OS directly",
        )
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "Bucket file exceeds the 15MB document limit",
        )

    # A ZIP in the bucket is an archive, not an unsupported file. Do NOT
    # expand it here: the intake pipeline already expands archives on the
    # bucket side into one BucketFile per entry (they arrive with their path
    # prefix, e.g. 'TAX RETURNS/2024 return.pdf') and those entries ingest on
    # their own. Expanding again would create a second copy of every statement
    # inside and double-count its deposits into ADB and EBITDA. Record it as a
    # catalogued archive so the row reads as handled rather than failed.
    if archive.is_zip_upload(doc.content_type, doc.filename):
        doc.kind = "archive"
        doc.detected_kind = "archive"
        doc.status = "extracted"
        doc.error = None
        doc.extracted = {
            "months": [],
            "transactions_count": 0,
            "notes": [
                source_note,
                "Archive catalogued — its contents were ingested individually from the "
                "bucket, so the archive itself writes no ledger rows.",
            ],
            "parser": "archive_catalogued",
            "doc_type": "archive",
        }
        await db.flush()
        return doc

    await extract_document(db, doc, raw)
    if doc.extracted is not None:
        notes = list(doc.extracted.get("notes") or [])
        doc.extracted = {**doc.extracted, "notes": ([source_note] + notes)[:50]}
    elif doc.status == "failed":
        doc.extracted = {"months": [], "transactions_count": 0, "notes": [source_note]}

    await db.flush()
    return doc


async def _background_ingest_bucket_files(dealer_id: UUID) -> None:
    """Compatibility wrapper for older background-task call sites."""
    await bucket_ingest.auto_ingest_dealer_bucket_files(dealer_id)


@router.post("/dealers/{dealer_id}/bucket-files/ingest-all")
async def ingest_all_bucket_files(
    dealer_id: UUID,
    background: BackgroundTasks,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Schedule background ingestion of every pending bucket file (idempotent
    — already-ingested files are skipped). Returns the pending count."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    if dealer.bucket_id is None:
        return {"pending": 0, "scheduled": False}
    ingested = set(
        (
            await db.execute(
                select(DealerDocument.bucket_file_id).where(
                    DealerDocument.dealer_id == dealer.id,
                    DealerDocument.bucket_file_id.is_not(None),
                )
            )
        ).scalars().all()
    )
    all_ids = (
        await db.execute(
            select(BucketFile.id).where(
                BucketFile.bucket_id == dealer.bucket_id, BucketFile.deleted_at.is_(None)
            )
        )
    ).scalars().all()
    pending = len([f for f in all_ids if f not in ingested])
    if pending:
        background.add_task(_background_ingest_bucket_files, dealer.id)
    return {"pending": pending, "scheduled": bool(pending)}


# --- Stream 3: engines, lineage & alerts -----------------------------------


@router.post("/dealers/{dealer_id}/recompute", response_model=SnapshotRead)
async def recompute_dealer(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerMetricSnapshot:
    """Force a fresh metric snapshot (with lineage + alerts) for the dealer."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    snapshot = await recompute_snapshot(db, dealer.id)
    await db.commit()
    await db.refresh(snapshot)
    return snapshot


@router.get("/dealers/{dealer_id}/health", response_model=HealthRead)
async def dealer_health(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> HealthRead:
    """Cockpit read: latest snapshot + targets + unresolved alerts + lineage size."""
    require_team_or_dealer_or_rep(user)
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
    alert = (
        await db.execute(
            select(DealerAlert).where(DealerAlert.id == alert_id, DealerAlert.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert not found for this client")
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan action not found for this client")
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
            "No metric snapshot exists for this client yet — import financials or "
            "POST /dealers/{id}/recompute first",
        )
    return snapshot.metrics or {}


@router.get("/dealers/{dealer_id}/plan", response_model=list[PlanActionRead])
async def list_plan_actions(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerPlanAction]:
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    q = select(DealerPlanAction).where(DealerPlanAction.dealer_id == dealer.id)
    if user.role == Role.DEALER:
        # Drafts and AI-accepted actions stay team-side until the advisor
        # publishes — publishing IS the share step.
        q = q.where(DealerPlanAction.published.is_(True))
    actions = (
        (
            await db.execute(
                q.order_by(DealerPlanAction.sort.asc(), DealerPlanAction.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    counts = dict(
        (
            await db.execute(
                select(DealerPlanComment.action_id, func.count()).where(
                    DealerPlanComment.dealer_id == dealer.id
                ).group_by(DealerPlanComment.action_id)
            )
        ).all()
    )
    out = []
    for a in actions:
        r = PlanActionRead.model_validate(a)
        r.comments_count = int(counts.get(a.id, 0))
        out.append(r)
    return out


@router.post(
    "/dealers/{dealer_id}/plan", response_model=PlanActionRead, status_code=status.HTTP_201_CREATED
)
async def create_plan_action(
    dealer_id: UUID, payload: PlanActionCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerPlanAction:
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
    action = await _load_plan_action(db, dealer.id, action_id)
    changes = payload.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(action, k, v)
    # Reverse sync: completing the plan action completes its payment shift —
    # the simulation keeps counting the lift until real statements absorb it.
    if changes.get("status") == "done":
        shift = (
            await db.execute(
                select(DealerPaymentShift).where(
                    DealerPaymentShift.dealer_id == dealer.id,
                    DealerPaymentShift.plan_action_id == action.id,
                    DealerPaymentShift.status == "proposed",
                )
            )
        ).scalar_one_or_none()
        if shift is not None:
            shift.status = "done"
    await db.commit()
    await db.refresh(action)
    return action


@router.delete("/dealers/{dealer_id}/plan/{action_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan_action(
    dealer_id: UUID, action_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> None:
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    action = await _load_plan_action(db, dealer.id, action_id)
    await db.delete(action)
    await db.commit()


@router.post("/dealers/{dealer_id}/plan/{action_id}/respond", response_model=PlanActionRead)
async def respond_plan_action(
    dealer_id: UUID,
    action_id: UUID,
    payload: PlanRespond,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanActionRead:
    """The client's answer to a published action (DEALER role; team may record
    it on the client's behalf). Declining feeds straight back into the
    simulation: the linked payment shift is dismissed, so its ADB lift leaves
    the optimized scenario on the next recompute. Re-responding is allowed —
    accepting again after a decline re-proposes a dismissed linked shift."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    action = await _load_plan_action(db, dealer.id, action_id)
    if not action.published:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
    action.client_response = payload.response
    action.client_response_at = datetime.now(timezone.utc)
    if payload.comment and payload.comment.strip():
        db.add(
            DealerPlanComment(
                dealer_id=dealer.id,
                action_id=action.id,
                author_user_id=user.id,
                author_role="dealer" if user.role == Role.DEALER else "team",
                author_name=(user.name or user.email or None),
                body=payload.comment.strip(),
            )
        )
    shift = (
        await db.execute(
            select(DealerPaymentShift).where(
                DealerPaymentShift.dealer_id == dealer.id,
                DealerPaymentShift.plan_action_id == action.id,
            )
        )
    ).scalar_one_or_none()
    if shift is not None:
        if payload.response == "declined" and shift.status == "proposed":
            shift.status = "dismissed"
        elif payload.response == "accepted" and shift.status == "dismissed":
            shift.status = "proposed"
    await log_action(
        db, dealer.id, user, "plan.client_response", "plan_action",
        entity_id=action.id,
        after={"response": payload.response, "title": action.title},
    )
    await db.commit()
    await db.refresh(action)
    r = PlanActionRead.model_validate(action)
    r.comments_count = int(
        (
            await db.execute(
                select(func.count()).select_from(DealerPlanComment).where(
                    DealerPlanComment.action_id == action.id
                )
            )
        ).scalar_one()
    )
    return r


@router.get("/dealers/{dealer_id}/plan/{action_id}/comments", response_model=list[PlanCommentRead])
async def list_plan_comments(
    dealer_id: UUID, action_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerPlanComment]:
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    action = await _load_plan_action(db, dealer.id, action_id)
    if user.role == Role.DEALER and not action.published:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
    return (
        (
            await db.execute(
                select(DealerPlanComment)
                .where(DealerPlanComment.action_id == action.id)
                .order_by(DealerPlanComment.created_at.asc())
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/dealers/{dealer_id}/plan/{action_id}/comments",
    response_model=PlanCommentRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_plan_comment(
    dealer_id: UUID,
    action_id: UUID,
    payload: PlanCommentCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerPlanComment:
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    action = await _load_plan_action(db, dealer.id, action_id)
    if user.role == Role.DEALER and not action.published:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
    comment = DealerPlanComment(
        dealer_id=dealer.id,
        action_id=action.id,
        author_user_id=user.id,
        author_role="dealer" if user.role == Role.DEALER else "team",
        author_name=(user.name or user.email or None),
        body=payload.body.strip(),
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    return comment


@router.post("/dealers/{dealer_id}/plan/publish", response_model=list[PlanActionRead])
async def publish_plan(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerPlanAction]:
    """Publish the whole plan to the dealer portal (sets published=true on all)."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
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
    require_team_or_dealer_or_rep(user)
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


async def _monthly_deposits_avg(db: AsyncSession, dealer_id: UUID) -> float | None:
    """Average monthly deposits over the trailing 6 observed months, collapsed
    per calendar month across accounts (same never-double-count posture as the
    snapshot engine). Feeds the deposit-multiple program sizing."""
    rows = (
        await db.execute(
            select(
                DealerFinancialPeriod.period,
                func.sum(DealerFinancialPeriod.deposits),
            )
            .where(
                DealerFinancialPeriod.dealer_id == dealer_id,
                DealerFinancialPeriod.deposits.is_not(None),
            )
            .group_by(DealerFinancialPeriod.period)
            .order_by(DealerFinancialPeriod.period.desc())
            .limit(6)
        )
    ).all()
    vals = [float(total) for _, total in rows if total is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


async def _effective_targets(db: AsyncSession, dealer_id: UUID) -> dict[str, float | None]:
    target_rows = (
        (
            await db.execute(
                select(DealerMetricTarget).where(DealerMetricTarget.dealer_id == dealer_id)
            )
        )
        .scalars()
        .all()
    )
    return {
        t.metric_key: (float(t.effective_value) if t.effective_value is not None else None)
        for t in target_rows
    }


async def _global_program_settings(db: AsyncSession) -> dict:
    """Desk-approved program overrides (0120) merged over the code defaults —
    the settings dict every paths.* computation takes."""
    rows = (await db.execute(select(DealerProgramSetting))).scalars().all()
    return merged_settings(rows)


@router.get("/dealers/{dealer_id}/paths", response_model=PathsRead)
async def dealer_paths(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> PathsRead:
    """Funding-path readiness + credit-ladder position from the latest snapshot."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    metrics = await _latest_snapshot_metrics(db, dealer.id)
    targets = await _effective_targets(db, dealer.id)
    settings = await _global_program_settings(db)
    # 0119: deposit history feeds the deposit-multiple sizing (additive fields
    # on each path row) — injected here, never stored on the snapshot.
    metrics = {**metrics, "deposits_monthly_avg": await _monthly_deposits_avg(db, dealer.id)}
    return PathsRead(
        paths=compute_paths(metrics, targets, settings=settings),
        ladder=compute_ladder(metrics, targets),
    )


@router.get("/dealers/{dealer_id}/funding-plan", response_model=FundingPlanRead)
async def funding_plan(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> FundingPlanRead:
    """The dealer's funding goal reverse-engineered per path (0119): what each
    program could fund today, and — when a goal is set — the metric levels the
    goal requires (typical-case assumptions, PROVISIONAL sizing constants).
    No goal => every path carries empty requirements and null feasibility."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    metrics = await _latest_snapshot_metrics(db, dealer.id)
    targets = await _effective_targets(db, dealer.id)
    settings = await _global_program_settings(db)
    metrics = {**metrics, "deposits_monthly_avg": await _monthly_deposits_avg(db, dealer.id)}
    goal = float(dealer.funding_goal) if dealer.funding_goal is not None else None

    paths_out: list[PathFundingRead] = []
    for key in PATH_KEYS:
        sized = size_program(key, metrics, targets, settings=settings)
        fundable = None
        if sized["funding_typical"] is not None:
            fundable = FundingRangeRead(
                min=sized["funding_min"],
                typical=sized["funding_typical"],
                max=sized["funding_max"],
            )
        requirements = (
            requirements_for_amount(key, goal, metrics, settings=settings)
            if goal is not None
            else []
        )
        feasible = all(r["met"] for r in requirements) if requirements else None
        paths_out.append(
            PathFundingRead(
                path_key=key,
                fundable_now=fundable,
                goal_feasible=feasible,
                requirements=requirements,
            )
        )
    return FundingPlanRead(goal=goal, purpose=dealer.funding_purpose, paths=paths_out)


@router.post("/dealers/{dealer_id}/simulate", response_model=SimulateRead)
async def simulate_dealer(
    dealer_id: UUID,
    payload: SimulateRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SimulateRead:
    """Read-only what-if: rerun the metric engine + program sizing under the
    requested levers and report baseline vs scenario side by side.

    Loads the SAME inputs recompute_snapshot does (shared loader) but
    persists nothing — no snapshot, no lineage, no alerts, no writes. The
    baseline is recomputed from the actual periods rather than read off the
    latest snapshot so baseline == scenario holds exactly at zero levers."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)

    inputs = await load_metric_inputs(db, dealer.id)
    settings = await _global_program_settings(db)
    deposits_avg = await _monthly_deposits_avg(db, dealer.id)
    goal = float(dealer.funding_goal) if dealer.funding_goal is not None else None

    # Resolve the flag-driven pools into their delta channels: proposed
    # payment shifts land in ADB; verifying open add-backs lifts annual
    # EBITDA through the add-back pool (the exact production channel).
    addbacks_added = (
        simulate.unverified_addbacks_annual(inputs.addback_rows)
        if payload.verify_all_addbacks
        else 0.0
    )
    shifts_added = 0.0
    shift_rows: list[DealerPaymentShift] = []
    if payload.apply_proposed_shifts:
        shift_rows = (
            (
                await db.execute(
                    select(DealerPaymentShift).where(
                        DealerPaymentShift.dealer_id == dealer.id,
                        DealerPaymentShift.status.in_(("proposed", "done")),
                    )
                )
            )
            .scalars()
            .all()
        )
        shifts_added = simulate.proposed_shifts_adb(shift_rows)
    if payload.shifts:
        # Staged (unsaved) shifts — same first-order ADB math the stored
        # rows use, folded into the same pool and drawn on the same curve.
        staged = [
            s_
            for s_ in payload.shifts
            if s_.from_day != s_.to_day
        ]
        shifts_added += round(
            sum(
                payment_timing.adb_impact(
                    s_.monthly_amount, s_.from_day, s_.to_day, direction=s_.direction
                )
                for s_ in staged
            ),
            2,
        )
        shift_rows = list(shift_rows) + staged

    adb_delta = float(payload.adb_delta) + shifts_added
    ebitda_delta = float(payload.ebitda_annual_delta) + addbacks_added

    baseline_metrics = compute_metrics(
        inputs.periods, inputs.addbacks_annual_verified, inputs.targets, fallbacks=inputs.fallbacks
    )
    scenario_metrics = compute_metrics(
        simulate.apply_levers(
            inputs.periods,
            adb_delta=adb_delta,
            debt_service_monthly_delta=float(payload.debt_service_monthly_delta),
            deposits_monthly_delta=float(payload.deposits_monthly_delta),
            nsf_zero=payload.nsf_zero,
        ),
        inputs.addbacks_annual_verified + ebitda_delta,
        inputs.targets,
        fallbacks=simulate.adjusted_fallbacks(
            inputs.fallbacks, float(payload.debt_service_monthly_delta)
        ),
    )

    # Paths comparison: deposits injected for the deposit-multiple sizing;
    # statement_months patches the history the SCENARIO grades against (the
    # requirements/readiness side only — never the metric engine above).
    scenario_deposits = deposits_avg
    if payload.deposits_monthly_delta:
        scenario_deposits = max(0.0, (deposits_avg or 0.0) + float(payload.deposits_monthly_delta))
    statement_months = (
        payload.statement_months if payload.statement_months is not None else len(inputs.periods)
    )
    baseline_tree = {**baseline_metrics, "deposits_monthly_avg": deposits_avg}
    scenario_tree = {
        **scenario_metrics,
        "deposits_monthly_avg": scenario_deposits,
        "periods_used": statement_months,
    }

    before = {p["key"]: p for p in compute_paths(baseline_tree, inputs.targets, settings=settings)}
    after = {p["key"]: p for p in compute_paths(scenario_tree, inputs.targets, settings=settings)}

    paths_out: list[SimulatePathRead] = []
    for key in PATH_KEYS:
        b, a = before[key], after[key]
        feasible_before = feasible_after = None
        if goal is not None:
            req_before = requirements_for_amount(key, goal, baseline_tree, settings=settings)
            req_after = requirements_for_amount(key, goal, scenario_tree, settings=settings)
            feasible_before = all(r["met"] for r in req_before) if req_before else None
            feasible_after = all(r["met"] for r in req_after) if req_after else None
        paths_out.append(
            SimulatePathRead(
                path_key=key,
                label=PATH_LABELS[key],
                readiness_before=b["readiness_pct"],
                readiness_after=a["readiness_pct"],
                funding_typical_before=b["funding_typical"],
                funding_typical_after=a["funding_typical"],
                goal_feasible_before=feasible_before,
                goal_feasible_after=feasible_after,
            )
        )

    # The ADB visual: intra-month balance curve from the actual ledger,
    # baseline vs scenario, each anchored to its engine ADB (picture ==
    # number, always). Reuses the vendor-input ledger load + cutoff markers.
    curve = None
    b_adb = (baseline_metrics.get("adb") or {}).get("current")
    s_adb = (scenario_metrics.get("adb") or {}).get("current")
    if b_adb is not None and s_adb is not None:
        ledger_rows, _ov, _sn = await _load_vendor_inputs(db, dealer)
        window_start = date.today() - timedelta(days=_TIMING_WINDOW_DAYS)
        windowed = [r for r in ledger_rows if r.occurred_on >= window_start]
        curve_data = simulate.daily_curve(windowed, b_adb, s_adb, shift_rows)
        if curve_data is not None:
            curve = SimulateCurveRead(
                **curve_data,
                adb_target=inputs.targets.get("adb_target"),
                cutoff_days=sorted(
                    {c["cutoff_day"] for c in payment_timing.cutoff_days(ledger_rows)}
                ),
            )

    return SimulateRead(
        daily_curve=curve,
        applied=SimulateApplied(
            adb_delta=round(adb_delta, 2),
            debt_service_monthly_delta=float(payload.debt_service_monthly_delta),
            deposits_monthly_delta=float(payload.deposits_monthly_delta),
            ebitda_annual_delta=round(ebitda_delta, 2),
            nsf_zero=payload.nsf_zero,
            addbacks_annual_added=addbacks_added,
            shifts_adb_added=shifts_added,
            statement_months=statement_months,
        ),
        baseline=SimulateMetrics(**simulate.summarize(baseline_metrics)),
        scenario=SimulateMetrics(**simulate.summarize(scenario_metrics)),
        paths=paths_out,
        goal=goal,
    )


def _to_utc_minute(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0)


def _parse_hhmm(value: str) -> int:
    hours, minutes = [int(part) for part in value.split(":")]
    return hours * 60 + minutes


def _js_weekday(day: date) -> int:
    return (day.weekday() + 1) % 7


def _round_up_to_step(value: datetime, step_min: int) -> datetime:
    value = value.replace(second=0, microsecond=0)
    remainder = value.minute % step_min
    if remainder:
        value += timedelta(minutes=step_min - remainder)
    return value


def _time_label(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _date_label(value: datetime) -> str:
    return f"{value.strftime('%a, %b')} {value.day}"


async def _rep_host_for(
    db: AsyncSession, dealer: DealerBusiness | None, user: User
) -> User:
    # Field Desk intentionally shares one authoritative calendar. The rep who
    # books is retained on DealerRepAppointment/booked_by_user_id and in the
    # event description; the event itself belongs to the primary super admin so
    # Google Calendar, Meet links and collision checks all use Franco's diary.
    host, _ = await team_booking_settings(db)
    return host


async def _booking_settings_for(db: AsyncSession, host: User) -> BookingSettings:
    row = (
        await db.execute(select(BookingSettings).where(BookingSettings.user_id == host.id))
    ).scalar_one_or_none()
    if row is not None:
        return row
    return BookingSettings(
        user_id=host.id,
        enabled=True,
        title=f"Book a meeting with {host.name or 'Qualified Commercial'}",
        intro="Choose a time that works for you.",
        duration_min=20,
        buffer_before_min=5,
        buffer_after_min=5,
        timezone="America/New_York",
        available_days=[1, 2, 3, 4, 5],
        start_time="09:00",
        end_time="17:00",
    )


async def _booking_slots(
    db: AsyncSession,
    host: User,
    booking: BookingSettings,
    *,
    duration_min: int | None = None,
    exclude_event_id: UUID | None = None,
) -> BookingAvailabilityRead:
    zone = rep_workflows.tz(booking.timezone)
    duration = duration_min or booking.duration_min or 30
    now_local = datetime.now(zone)
    earliest_local = _round_up_to_step(now_local + timedelta(hours=2), 5)
    window_end_local = (now_local + timedelta(days=15)).replace(hour=23, minute=59, second=0, microsecond=0)
    live_google = await calendar_sync.busy_periods(
        db,
        host.id,
        time_min=now_local.astimezone(timezone.utc),
        time_max=window_end_local.astimezone(timezone.utc),
    )
    # Field Desk books against the shared Franco calendar. Fail closed when the
    # live calendar cannot be consulted; otherwise a revoked token or Google
    # outage could expose a slot that is already occupied outside QC.
    if live_google.status != "connected":
        return BookingAvailabilityRead(
            timezone=booking.timezone,
            duration_min=duration,
            buffer_before_min=booking.buffer_before_min,
            buffer_after_min=booking.buffer_after_min,
            host_name=host.name,
            calendar_sync_status=live_google.status,
            slots=[],
        )
    busy_rows = (
        await db.execute(
            select(CalendarEvent)
            .where(
                CalendarEvent.owner_user_id == host.id,
                CalendarEvent.status != CalendarEventStatus.CANCELLED,
                CalendarEvent.starts_at >= now_local.astimezone(timezone.utc),
                CalendarEvent.starts_at <= window_end_local.astimezone(timezone.utc),
            )
            .order_by(CalendarEvent.starts_at)
        )
    ).scalars().all()
    excluded_event = next((ev for ev in busy_rows if ev.id == exclude_event_id), None)
    if exclude_event_id:
        busy_rows = [ev for ev in busy_rows if ev.id != exclude_event_id]
    busy = [
        (
            ev.starts_at.astimezone(zone) - timedelta(minutes=booking.buffer_before_min),
            ev.starts_at.astimezone(zone)
            + timedelta(minutes=max(15, ev.duration_min or duration) + booking.buffer_after_min),
        )
        for ev in busy_rows
    ]
    for start, end in live_google.intervals:
        # FreeBusy does not return event ids. Ignore only the exact mirrored
        # interval of the appointment being rescheduled; merged/overlapping
        # busy ranges remain blocking so another Google event cannot be hidden.
        if excluded_event is not None:
            expected_start = excluded_event.starts_at.astimezone(timezone.utc)
            expected_end = expected_start + timedelta(minutes=excluded_event.duration_min or duration)
            if abs((start - expected_start).total_seconds()) < 2 and abs((end - expected_end).total_seconds()) < 2:
                continue
        busy.append((
            start.astimezone(zone) - timedelta(minutes=booking.buffer_before_min),
            end.astimezone(zone) + timedelta(minutes=booking.buffer_after_min),
        ))
    start_min = _parse_hhmm(booking.start_time or "09:00")
    end_min = _parse_hhmm(booking.end_time or "17:00")
    slot_duration = timedelta(minutes=duration)
    slots: list[BookingAvailabilitySlot] = []
    for offset in range(15):
        day = now_local.date() + timedelta(days=offset)
        if _js_weekday(day) not in (booking.available_days or [1, 2, 3, 4, 5]):
            continue
        day_start = datetime.combine(day, dt_time(start_min // 60, start_min % 60), tzinfo=zone)
        day_end = datetime.combine(day, dt_time(end_min // 60, end_min % 60), tzinfo=zone)
        cursor = max(day_start, earliest_local if day == earliest_local.date() else day_start)
        cursor = _round_up_to_step(cursor, 5)
        while cursor + slot_duration <= day_end:
            slot_end = cursor + slot_duration
            if not any(cursor < busy_end and slot_end > busy_start for busy_start, busy_end in busy):
                starts_utc = cursor.astimezone(timezone.utc).replace(second=0, microsecond=0)
                slots.append(BookingAvailabilitySlot(
                    starts_at=starts_utc,
                    label=_time_label(cursor),
                    date_label=_date_label(cursor),
                ))
                if len(slots) >= 80:
                    return BookingAvailabilityRead(
                        timezone=booking.timezone,
                        duration_min=duration,
                        buffer_before_min=booking.buffer_before_min,
                        buffer_after_min=booking.buffer_after_min,
                        host_name=host.name,
                        calendar_sync_status=live_google.status,
                        slots=slots,
                    )
            cursor += timedelta(minutes=5)
    return BookingAvailabilityRead(
        timezone=booking.timezone,
        duration_min=duration,
        buffer_before_min=booking.buffer_before_min,
        buffer_after_min=booking.buffer_after_min,
        host_name=host.name,
        calendar_sync_status=live_google.status,
        slots=slots,
    )


def _appointment_title(kind: str, invitee_name: str, dealer: DealerBusiness | None) -> str:
    labels = {
        "callback": "Callback",
        "program_intro": "Program intro",
        "underwriting_review": "Underwriting review",
    }
    suffix = f" · {dealer.name}" if dealer else ""
    return f"{labels.get(kind, 'Appointment')}: {invitee_name}{suffix}"


GENERAL_PROGRAM_KEY = "general_funding_discussion"
GENERAL_PROGRAM_NAME = "General funding discussion / Not decided yet"


async def _resolve_appointment_program(
    db: AsyncSession,
    *,
    program_key: str | None,
    program_name: str | None,
    existing: DealerRepAppointment | None = None,
) -> tuple[str | None, str]:
    key = (program_key or "").strip()
    name = (program_name or "").strip()
    if key == GENERAL_PROGRAM_KEY or (not key and not name):
        return GENERAL_PROGRAM_KEY, GENERAL_PROGRAM_NAME
    if key:
        row = (
            await db.execute(
                select(DealerProductCatalog)
                .where(
                    DealerProductCatalog.program_key == key,
                    DealerProductCatalog.active.is_(True),
                )
                .order_by(DealerProductCatalog.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            if existing and existing.program_key == key:
                return key, existing.program_name or name or key.replace("_", " ").title()
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose an active funding program.")
        copy = (row.copy or {}).get("en") or {}
        return row.program_key, str(copy.get("name") or row.program_key.replace("_", " ").title())

    if existing and name == (existing.program_name or ""):
        return existing.program_key, name
    if name == GENERAL_PROGRAM_NAME:
        return GENERAL_PROGRAM_KEY, GENERAL_PROGRAM_NAME
    # Backward compatibility for older clients that sent a catalog label only.
    rows = list(
        (
            await db.execute(
                select(DealerProductCatalog)
                .where(DealerProductCatalog.active.is_(True))
                .order_by(DealerProductCatalog.version.desc())
            )
        ).scalars().all()
    )
    for row in rows:
        copy = (row.copy or {}).get("en") or {}
        catalog_name = str(copy.get("name") or row.program_key.replace("_", " ").title())
        if catalog_name.casefold() == name.casefold():
            return row.program_key, catalog_name
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose a program from the active catalog.")


def _booking_description(
    *,
    user: User,
    payload: RepAppointmentCreate,
    dealer: DealerBusiness | None,
) -> tuple[str, str | None, str | None, str | None]:
    program = (payload.program_name or (dealer.funding_purpose if dealer else None) or "").strip() or None
    amount = (payload.requested_amount or "").strip() or None
    if amount is None and dealer and dealer.funding_goal is not None:
        amount = "$" + format(float(dealer.funding_goal), ",.0f")
    address = (payload.full_address or "").strip() or None
    if address is None and dealer:
        address = ", ".join(
            part for part in [
                dealer.address,
                " ".join(part for part in [dealer.city, dealer.state, dealer.zip] if part).strip(),
            ]
            if part
        ) or None
    lines = [
        "Qualified Commercial Field Desk appointment.",
        f"Booked by: {user.name or user.email or 'Field Desk'}",
        f"Agent email: {user.email or '(not provided)'}",
        f"Meeting type: {payload.kind}",
        f"Client: {payload.invitee_name}",
        f"Company: {payload.company or (dealer.name if dealer else None) or '(not provided)'}",
        f"Client email: {payload.invitee_email or '(not provided)'}",
        f"Client phone: {payload.invitee_phone or '(not provided)'}",
        f"Program: {program or '(to be discussed)'}",
        f"Interested amount: {amount or '(not provided)'}",
        f"Address: {address or '(not provided)'}",
        "",
        "Agent notes:",
        payload.notes or "(none)",
    ]
    return "\n".join(lines), program, amount, address


async def _load_owned_appointment(
    db: AsyncSession, appointment_id: UUID, user: User
) -> DealerRepAppointment:
    require_team_or_rep(user)
    row = (
        await db.execute(
            select(DealerRepAppointment).where(DealerRepAppointment.id == appointment_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found.")
    if row.dealer_id is not None:
        dealer = await db.get(DealerBusiness, row.dealer_id)
        if dealer is not None and dealer.is_training and user.role != Role.SUPER_ADMIN:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found.")
    if user.role == Role.FIELD_REP and row.booked_by_user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found.")
    return row


async def _appointment_read_rows(
    db: AsyncSession, rows: list[DealerRepAppointment]
) -> list[dict]:
    event_ids = [row.calendar_event_id for row in rows if row.calendar_event_id]
    notices = {}
    events = {}
    rep_reminders: dict[UUID, list[BookingNotificationReminder]] = {}
    rep_notifications: dict[str, Notification] = {}
    if event_ids:
        notice_rows = (
            await db.execute(select(BookingNotification).where(BookingNotification.event_id.in_(event_ids)))
        ).scalars().all()
        notices = {row.event_id: row for row in notice_rows}
        notice_ids = [row.id for row in notice_rows]
        if notice_ids:
            reminder_rows = (
                await db.execute(
                    select(BookingNotificationReminder).where(
                        BookingNotificationReminder.booking_notification_id.in_(notice_ids),
                        BookingNotificationReminder.channel == "rep",
                    )
                )
            ).scalars().all()
            for reminder in reminder_rows:
                rep_reminders.setdefault(reminder.booking_notification_id, []).append(reminder)
        event_rows = (
            await db.execute(select(CalendarEvent).where(CalendarEvent.id.in_(event_ids)))
        ).scalars().all()
        events = {row.id: row for row in event_rows}
    appointment_ids = [str(row.id) for row in rows]
    if appointment_ids:
        notification_rows = (
            await db.execute(
                select(Notification)
                .where(
                    Notification.target_type == "dealer_rep_appointment",
                    Notification.target_id.in_(appointment_ids),
                )
                .order_by(Notification.created_at.desc())
            )
        ).scalars().all()
        for notification in notification_rows:
            if notification.target_id:
                rep_notifications.setdefault(notification.target_id, notification)
    payloads: list[dict] = []
    for row in rows:
        data = RepAppointmentRead.model_validate(row).model_dump()
        notice = notices.get(row.calendar_event_id)
        event = events.get(row.calendar_event_id)
        if notice:
            data.update({
                "confirmation_email_status": notice.confirmation_email_status,
                "confirmation_sms_status": notice.confirmation_sms_status,
                "email_reminder_status": notice.email_reminder_status,
                "sms_reminder_status": notice.sms_reminder_status,
                "delivery_error": notice.last_error,
            })
            staff_rows = rep_reminders.get(notice.id, [])
            staff_statuses = {item.status for item in staff_rows}
            data["rep_reminder_status"] = (
                "failed" if "failed" in staff_statuses
                else "pending" if "pending" in staff_statuses
                else "sent" if "sent" in staff_statuses
                else "cancelled" if staff_rows and staff_statuses == {"cancelled"}
                else "disabled"
            )
        notification = rep_notifications.get(str(row.id))
        data["rep_notification_status"] = (
            "emailed" if notification and notification.emailed_at
            else "in_app" if notification
            else "unavailable"
        )
        data["google_sync_status"] = (
            "connected" if event and event.google_event_id
            else "pending" if event and event.owner_user_id
            else "unavailable"
        )
        payloads.append(data)
    return payloads


def _appointment_payload(
    appt: DealerRepAppointment, *, transactional_sms_consent: bool = False
) -> RepAppointmentCreate:
    return RepAppointmentCreate(
        kind=appt.kind,
        title=appt.title,
        starts_at=appt.starts_at,
        duration_min=appt.duration_min,
        timezone=appt.timezone,
        invitee_name=appt.invitee_name,
        company=appt.company,
        invitee_email=appt.invitee_email,
        invitee_phone=appt.invitee_phone,
        join_url=appt.join_url,
        notes=appt.notes,
        program_key=appt.program_key,
        program_name=appt.program_name,
        requested_amount=appt.requested_amount,
        full_address=appt.full_address,
        transactional_sms_consent=transactional_sms_consent,
    )


def _appointment_google_color(outcome: str | None) -> str | None:
    # Google Calendar event palette: tomato, banana, basil.
    return {"not_converted": "11", "did_not_show": "5", "converted": "10"}.get(outcome or "")


def _appointment_local_time(starts_at: datetime, timezone_name: str | None) -> str:
    return starts_at.astimezone(rep_workflows.tz(timezone_name)).strftime("%b %d at %I:%M %p %Z")


async def _ensure_rep_contact(
    db: AsyncSession,
    *,
    owner_user_id: UUID,
    dealer_id: UUID | None,
    full_name: str,
    company: str | None,
    email: str | None,
    phone_e164: str | None,
    source: str,
) -> DealerRepContact:
    q = select(DealerRepContact).where(DealerRepContact.owner_user_id == owner_user_id)
    if email:
        q = q.where(DealerRepContact.email == email.strip().lower())
    elif phone_e164:
        q = q.where(DealerRepContact.phone_e164 == phone_e164)
    else:
        q = q.where(DealerRepContact.full_name == full_name.strip())
    row = (await db.execute(q.order_by(DealerRepContact.updated_at.desc()))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if row is None:
        row = DealerRepContact(
            owner_user_id=owner_user_id,
            dealer_id=dealer_id,
            full_name=full_name.strip(),
            company=company,
            email=email.strip().lower() if email else None,
            phone_e164=phone_e164,
            source=source,
            last_activity_at=now,
        )
        db.add(row)
        await db.flush()
        return row
    row.dealer_id = row.dealer_id or dealer_id
    row.full_name = full_name.strip() or row.full_name
    row.company = company or row.company
    row.email = email.strip().lower() if email else row.email
    row.phone_e164 = phone_e164 or row.phone_e164
    row.last_activity_at = now
    await db.flush()
    return row


async def _capture_rep_contact_sms_consent(
    db: AsyncSession,
    *,
    request: Request,
    user: User,
    contact: DealerRepContact,
    dealer: DealerBusiness | None,
    phone_e164: str | None,
    recipient_name: str,
    transactional: bool,
    marketing: bool,
    method: str,
) -> list[str]:
    if not phone_e164 or not (transactional or marketing):
        return []
    now = datetime.now(timezone.utc)
    meta = {
        "method": method,
        "captured_by": str(user.id),
        "captured_by_name": user.name,
        "captured_at": now.isoformat(),
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent", "")[:400],
    }
    contact.sms_consent_meta = meta
    consent_kinds: list[str] = []
    if transactional:
        contact.sms_transactional_consented_at = now
        consent_kinds.append("transactional")
    if marketing:
        contact.sms_marketing_consented_at = now
        consent_kinds.append("marketing")
    if dealer is not None:
        for kind in consent_kinds:
            await sms_consent_svc.record_consent(
                db,
                dealer_id=dealer.id,
                phone_e164=phone_e164,
                kind=kind,
                method=method,
                captured_by_user_id=user.id,
                captured_by_name=user.name,
                consenter_name=recipient_name,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
    return consent_kinds


async def _ensure_rep_thread(
    db: AsyncSession,
    *,
    owner_user_id: UUID,
    contact: DealerRepContact,
    dealer_id: UUID | None,
    channel: str,
    subject: str,
    source: str,
    dealer_scoped: bool = False,
) -> DealerRepInboxThread:
    subject_key = None
    if channel == "email":
        subject_key = subject.strip().lower()
        while re.match(r"^(re|fw|fwd)\s*:", subject_key):
            subject_key = re.sub(r"^(re|fw|fwd)\s*:\s*", "", subject_key)
        subject_key = " ".join(subject_key.split())[:200]
    filters = [
        DealerRepInboxThread.owner_user_id == owner_user_id,
        DealerRepInboxThread.contact_id == contact.id,
        DealerRepInboxThread.channel == channel,
        DealerRepInboxThread.status == "open",
    ]
    if channel == "email":
        filters.append(DealerRepInboxThread.subject_key == subject_key)
    if dealer_scoped:
        filters.append(
            DealerRepInboxThread.dealer_id == dealer_id
            if dealer_id is not None
            else DealerRepInboxThread.dealer_id.is_(None)
        )
    row = (
        await db.execute(
            select(DealerRepInboxThread)
            .where(*filters)
            .order_by(DealerRepInboxThread.updated_at.desc())
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if row is None:
        row = DealerRepInboxThread(
            owner_user_id=owner_user_id,
            contact_id=contact.id,
            dealer_id=dealer_id,
            subject=subject,
            subject_key=subject_key,
            channel=channel,
            source=source,
            last_message_at=now,
        )
        db.add(row)
        await db.flush()
    else:
        row.dealer_id = row.dealer_id or dealer_id
        row.last_message_at = now
    return row


async def _append_rep_inbox_message(
    db: AsyncSession,
    *,
    thread: DealerRepInboxThread,
    contact: DealerRepContact | None,
    direction: str,
    channel: str,
    body: str,
    subject: str | None = None,
    provider: str | None = None,
    provider_message_id: str | None = None,
    provider_error: str | None = None,
    delivery_status: str = "stored",
    sender: str | None = None,
    recipient: str | None = None,
) -> DealerRepInboxMessage:
    now = datetime.now(timezone.utc)
    msg = DealerRepInboxMessage(
        thread_id=thread.id,
        owner_user_id=thread.owner_user_id,
        contact_id=thread.contact_id,
        dealer_id=thread.dealer_id,
        direction=direction,
        channel=channel,
        subject=subject,
        body=body,
        provider=provider,
        provider_message_id=provider_message_id,
        provider_error=provider_error,
        delivery_status=delivery_status,
        sender=sender,
        recipient=recipient,
        read_at=now if direction == "outbound" else None,
    )
    db.add(msg)
    thread.last_message_at = now
    if direction == "inbound":
        thread.unread_count = int(thread.unread_count or 0) + 1
    if contact is not None:
        contact.last_activity_at = now
    await db.flush()
    return msg


def _thread_read(thread: DealerRepInboxThread, contact: DealerRepContact | None) -> RepInboxThreadRead:
    return RepInboxThreadRead(
        id=thread.id,
        owner_user_id=thread.owner_user_id,
        contact_id=thread.contact_id,
        dealer_id=thread.dealer_id,
        subject=thread.subject,
        channel=thread.channel,
        source=thread.source,
        last_message_at=thread.last_message_at,
        unread_count=thread.unread_count,
        status=thread.status,
        contact_name=contact.full_name if contact else None,
        contact_email=contact.email if contact else None,
        contact_phone=contact.phone_e164 if contact else None,
        company=contact.company if contact else None,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def _rep_inbox_access_filter(user: User):
    """Keep the Field Desk Inbox personal for every staff role."""
    return DealerRepInboxThread.owner_user_id == user.id


def _rep_inbox_live_file_filter():
    """Ordinary inbox views never surface a training-linked conversation."""
    return or_(
        DealerRepInboxThread.dealer_id.is_(None),
        DealerRepInboxThread.dealer_id.in_(
            select(DealerBusiness.id).where(DealerBusiness.is_training.is_(False))
        ),
    )


async def _mirror_file_message_to_rep_inbox(
    db: AsyncSession,
    *,
    dealer: DealerBusiness,
    user: User,
    message: DealerMessage,
) -> None:
    """Mirror client-facing file messages into the rep inbox.

    Desk notes stay only on the file. The inbox is the controlled outreach
    surface, so it should show the same client conversation history and unread
    client replies without making private underwriting notes look sendable.
    """
    if message.channel not in CLIENT_VISIBLE_CHANNELS:
        return
    owner_user_id = dealer.owner_user_id
    if owner_user_id is None:
        if user.role == Role.DEALER:
            return
        owner_user_id = user.id

    phone = consent_delivery.normalize_phone(dealer.phone)
    email = dealer.email.strip().lower() if dealer.email else None
    contact = await _ensure_rep_contact(
        db,
        owner_user_id=owner_user_id,
        dealer_id=dealer.id,
        full_name=dealer.name or "Business owner",
        company=dealer.name,
        email=email,
        phone_e164=phone,
        source="file_message",
    )
    thread_channel = "email" if email else "sms"
    thread = await _ensure_rep_thread(
        db,
        owner_user_id=owner_user_id,
        contact=contact,
        dealer_id=dealer.id,
        channel=thread_channel,
        subject=f"File messages: {dealer.name}",
        source="file_message",
        dealer_scoped=True,
    )
    direction = "inbound" if user.role == Role.DEALER else "outbound"
    await _append_rep_inbox_message(
        db,
        thread=thread,
        contact=contact,
        direction=direction,
        channel=thread_channel,
        subject=thread.subject,
        body=message.body,
        provider="file_message",
        provider_message_id=str(message.id),
        delivery_status="stored",
        sender=user.email,
        recipient=email or phone,
    )


@router.get("/dealers/{dealer_id}/application-profile", response_model=ApplicationProfileRead | None)
async def get_application_profile(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerApplicationProfile | None:
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return (
        await db.execute(
            select(DealerApplicationProfile).where(DealerApplicationProfile.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()


@router.patch("/dealers/{dealer_id}/application-profile", response_model=ApplicationProfileRead)
async def patch_application_profile(
    dealer_id: UUID,
    payload: ApplicationProfilePatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerApplicationProfile:
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    row = (
        await db.execute(
            select(DealerApplicationProfile).where(DealerApplicationProfile.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = DealerApplicationProfile(dealer_id=dealer.id, updated_by_user_id=user.id)
        db.add(row)
    patch = payload.model_dump(exclude_unset=True)
    confirm_fields = set(patch.pop("confirm_fields", None) or [])
    editable_fields = set(ApplicationProfilePatch.model_fields) - {"confirm_fields"}
    unknown_confirmations = sorted(confirm_fields - editable_fields)
    if unknown_confirmations:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unsupported confirmation fields: {', '.join(unknown_confirmations)}",
        )
    before = jsonable_encoder({key: getattr(row, key, None) for key in patch})
    provenance = dict(row.field_provenance or {})
    confirmations = dict(row.field_confirmations or {})
    now_iso = datetime.now(timezone.utc).isoformat()
    for key, value in patch.items():
        setattr(row, key, value)
        if key in confirm_fields:
            provenance[key] = {
                "source": "agent_confirmed",
                "label": "Agent confirmed",
                "confirmed_at": now_iso,
                "confirmed_by_user_id": str(user.id),
            }
            confirmations[key] = {
                "value": jsonable_encoder(value),
                "confirmed_at": now_iso,
                "confirmed_by_user_id": str(user.id),
            }
        elif key not in confirmations:
            provenance[key] = {
                "source": "agent_entered",
                "label": "Agent entered",
                "updated_at": now_iso,
                "updated_by_user_id": str(user.id),
            }
    for key in confirm_fields - set(patch):
        value = getattr(row, key, None)
        if value is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"{key.replace('_', ' ').title()} cannot be confirmed while empty.",
            )
        provenance[key] = {
            "source": "agent_confirmed",
            "label": "Agent confirmed",
            "confirmed_at": now_iso,
            "confirmed_by_user_id": str(user.id),
        }
        confirmations[key] = {
            "value": jsonable_encoder(value),
            "confirmed_at": now_iso,
            "confirmed_by_user_id": str(user.id),
        }
    row.field_provenance = provenance
    row.field_confirmations = confirmations
    row.updated_by_user_id = user.id
    await db.flush()
    lead = (
        await db.execute(select(DealerRepLead).where(DealerRepLead.dealer_id == dealer.id))
    ).scalar_one_or_none()
    ver = await _assess_verification(db, dealer)
    if lead is not None and ver.unlocked and lead.status in {"draft", "info_collected", "awaiting_docs", "analyzing"}:
        history = list(lead.status_history or [])
        history.append({
            "at": datetime.now(timezone.utc).isoformat(),
            "from": lead.status,
            "to": "decision_ready",
            "by": str(user.id),
            "by_name": user.name,
            "note": "step 4 profile saved",
        })
        lead.status = "decision_ready"
        lead.status_history = history[-50:]
        lead.submitted_at = lead.submitted_at or datetime.now(timezone.utc)
    await log_action(
        db,
        dealer.id,
        user,
        "application_profile.update",
        "application_profile",
        entity_id=row.id,
        before=before,
        after={**payload.model_dump(exclude_unset=True, mode="json"), "confirmed_fields": sorted(confirm_fields)},
    )
    await db.commit()
    await db.refresh(row)
    return row


@router.get(
    "/dealers/{dealer_id}/submission-readiness",
    response_model=SubmissionReadinessRead,
)
async def get_submission_readiness(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> SubmissionReadinessRead:
    """Source-by-source release gate for the QC master application."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    context = await qc_master_application.build_context(db, dealer)
    return SubmissionReadinessRead(**qc_master_application.build_readiness(context))


@router.patch(
    "/dealers/{dealer_id}/submission-readiness/human-review",
    response_model=SubmissionReadinessRead,
)
async def patch_submission_human_review(
    dealer_id: UUID,
    payload: ApplicationHumanReviewPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SubmissionReadinessRead:
    """Record the authorized desk decision that controls signature release."""
    require_super_admin(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    review_context = await qc_master_application.build_context(db, dealer)
    current_readiness = qc_master_application.build_readiness(review_context)
    if payload.status == "fundable" and not current_readiness["package_ready"]:
        blockers = [
            item["requirement"]
            for item in current_readiness["items"]
            if item["requirement"] != "Human-reviewed fundable path"
            and item["status"] in {"missing", "supplemental"}
        ]
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Complete the underwriting package before marking the file fundable: "
            + "; ".join(blockers[:5]),
        )
    row = (
        await db.execute(
            select(DealerApplicationProfile).where(
                DealerApplicationProfile.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = DealerApplicationProfile(dealer_id=dealer.id, updated_by_user_id=user.id)
        db.add(row)
        await db.flush()
    before = {
        "status": row.human_review_status,
        "note": row.human_review_note,
        "reviewed_at": row.human_reviewed_at.isoformat() if row.human_reviewed_at else None,
    }
    row.human_review_status = payload.status
    row.human_review_note = (payload.note or "").strip() or None
    row.human_reviewed_at = datetime.now(timezone.utc) if payload.status != "pending" else None
    row.human_reviewed_by_user_id = user.id if payload.status != "pending" else None
    lead = (
        await db.execute(
            select(DealerRepLead)
            .where(DealerRepLead.dealer_id == dealer.id)
            .order_by(DealerRepLead.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if lead is not None:
        lead.decision = (
            "fundable" if payload.status == "fundable"
            else "not_yet" if payload.status == "not_fundable"
            else None
        )
        lead.decision_at = datetime.now(timezone.utc) if payload.status != "pending" else None
    await log_action(
        db,
        dealer.id,
        user,
        "application.human_review_updated",
        "application_profile",
        entity_id=row.id,
        before=before,
        after={"status": row.human_review_status, "note": row.human_review_note},
    )
    await db.commit()
    context = await qc_master_application.build_context(db, dealer)
    return SubmissionReadinessRead(**qc_master_application.build_readiness(context))


@router.patch(
    "/dealers/{dealer_id}/finalization",
    response_model=DealerRead,
)
async def patch_application_finalization(
    dealer_id: UUID,
    payload: ApplicationFinalizationPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerRead:
    """Record desk-controlled closing status and the amount actually funded."""
    require_super_admin(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    target_status = payload.status or dealer.status
    if target_status == "complete" and payload.funded_amount is None and dealer.funded_amount is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Enter the amount funded before marking this file funded.",
        )
    if payload.funded_amount is not None and target_status != "complete":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Funded amount can only be recorded when the file status is Funded.",
        )

    before = {
        "status": dealer.status,
        "funded_amount": float(dealer.funded_amount) if dealer.funded_amount is not None else None,
    }
    contract = (
        await db.execute(
            select(ContractDocument).where(
                ContractDocument.dealer_id == dealer.id,
                ContractDocument.template_key == qc_master_application.MASTER_TEMPLATE_KEY,
            )
        )
    ).scalars().first()
    if target_status == "forms_out" and (contract is None or contract.status not in {"out_for_signature", "executed"}):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Send the QC application for signature before marking agreements sent.",
        )
    if target_status in {"signed", "complete"} and (contract is None or contract.status != "executed"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The QC application must be executed before using Signed or Funded status.",
        )

    if payload.status is not None:
        dealer.status = payload.status
        if payload.status != "complete":
            dealer.funded_amount = None
    if payload.funded_amount is not None:
        dealer.funded_amount = payload.funded_amount

    lead = (
        await db.execute(
            select(DealerRepLead)
            .where(DealerRepLead.dealer_id == dealer.id)
            .order_by(DealerRepLead.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    lead_status = "decision_ready" if payload.status == "active" else payload.status
    if lead is not None and lead_status is not None and lead.status != lead_status:
        changed_at = datetime.now(timezone.utc)
        history = list(lead.status_history or [])
        history.append(
            {
                "at": changed_at.isoformat(),
                "from": lead.status,
                "to": lead_status,
                "by": str(user.id),
                "by_name": user.name,
                "note": "Step 5 status updated",
            }
        )
        lead.status = lead_status
        lead.status_history = history[-50:]
        lead.completed_at = changed_at if lead_status in {"complete", "declined"} else None

    await log_action(
        db,
        dealer.id,
        user,
        "application.finalization_updated",
        "dealer",
        entity_id=dealer.id,
        before=before,
        after={
            "status": dealer.status,
            "funded_amount": float(dealer.funded_amount) if dealer.funded_amount is not None else None,
        },
    )
    await db.commit()
    await db.refresh(dealer)
    return await _dealer_read(db, dealer)


@router.get("/booking/availability", response_model=BookingAvailabilityRead)
async def booking_availability(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    dealer_id: UUID | None = None,
    duration_min: int | None = Query(default=None, ge=15, le=180),
) -> BookingAvailabilityRead:
    require_team_or_rep(user)
    dealer: DealerBusiness | None = None
    if dealer_id is not None:
        dealer = await resolve_dealer_scope(db, user, dealer_id)
    host = await _rep_host_for(db, dealer, user)
    booking = await _booking_settings_for(db, host)
    # Availability follows the shared host policy. The retained query argument
    # is ignored for API compatibility so callers cannot bypass meeting length.
    return await _booking_slots(db, host, booking, duration_min=booking.duration_min)


@router.get("/appointments", response_model=list[RepAppointmentRead])
async def list_all_rep_appointments(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    starts_from: datetime | None = Query(default=None, alias="from"),
    starts_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=250, ge=1, le=500),
    include_cancelled: bool = Query(default=False),
) -> list[dict]:
    require_team_or_rep(user)
    q = select(DealerRepAppointment).where(
        or_(
            DealerRepAppointment.dealer_id.is_(None),
            DealerRepAppointment.dealer_id.in_(
                select(DealerBusiness.id).where(DealerBusiness.is_training.is_(False))
            ),
        )
    )
    if user.role == Role.FIELD_REP:
        q = q.where(DealerRepAppointment.booked_by_user_id == user.id)
    if starts_from is not None:
        q = q.where(DealerRepAppointment.starts_at >= _to_utc_minute(starts_from))
    if starts_to is not None:
        q = q.where(DealerRepAppointment.starts_at <= _to_utc_minute(starts_to))
    if not include_cancelled:
        q = q.where(
            DealerRepAppointment.archived_at.is_(None),
            DealerRepAppointment.status != "cancelled",
        )
    rows = list((await db.execute(q.order_by(DealerRepAppointment.starts_at.asc()).limit(limit))).scalars().all())
    return await _appointment_read_rows(db, rows)


async def _prepare_underwriting_review_room(
    db: AsyncSession,
    *,
    dealer: DealerBusiness,
    user: User,
    recipient_email: str | None,
    recipient_phone: str | None,
    requested_document_keys: list[str] | None = None,
) -> dict[str, str]:
    """Create the post-booking checklist without risking the appointment.

    This runs only after an actual underwriting-review time is selected. The
    three proposed windows never call it, so they cannot create rooms, rotate
    codes, or send document requests. Checklist writes are idempotent.
    """

    results: dict[str, str] = {}
    try:
        room = await client_room.rotate_passcode(db, dealer)
        results["secure_room"] = "ready"
        results["access_code"] = room.passcode or "rotated"
        tax_years = (date.today().year - 1, date.today().year - 2)
        for year in tax_years:
            await client_room.request_document(
                db,
                dealer,
                name=f"Business federal tax return - {year}",
                description=(
                    f"Upload the complete filed federal business tax return for {year}, "
                    "including all schedules and statements."
                ),
                category="tax_returns",
                required=True,
            )
        owner_state = await _owner_requirement_state(db, dealer.id)
        for owner in owner_state["required"]:
            for year in tax_years:
                await client_room.request_document(
                    db,
                    dealer,
                    name=f"Personal federal tax return - {year} - {owner.full_name}",
                    description=(
                        f"Upload the complete filed federal personal tax return for "
                        f"{owner.full_name} for {year}, including all schedules."
                    ),
                    category="tax_returns",
                    required=True,
                )
        optional_requests = {
            "ytd_profit_and_loss": (
                "Current year-to-date profit and loss statement",
                "Upload the current year-to-date business profit and loss statement.",
                "financial_statements",
            ),
            "debt_schedule": (
                "Current business debt schedule",
                "Upload the current debt schedule showing lender, balance, and payment for each obligation.",
                "debt_schedule",
            ),
            "use_of_funds_support": (
                "Use-of-funds supporting documents",
                "Upload estimates, invoices, payoff letters, or other documents supporting the intended use of funds.",
                "use_of_funds",
            ),
            "entity_documents": (
                "Business entity documents",
                "Upload formation, ownership, or authority documents relevant to the financing request.",
                "entity_documents",
            ),
        }
        selected_optional = sorted(set(requested_document_keys or []))
        for key in selected_optional:
            configured = optional_requests.get(key)
            if configured is None:
                continue
            name, description, category = configured
            await client_room.request_document(
                db,
                dealer,
                name=name,
                description=description,
                category=category,
                required=True,
            )
        results["tax_return_checklist"] = "ready"
        results["additional_document_requests"] = str(len(selected_optional))

        purpose = "review the underwriting document request"
        if room.passcode:
            purpose += f" using access code {room.passcode}"
        delivery = await _notify_client_request(
            db,
            dealer,
            user,
            purpose=purpose,
            path=room.url,
            channel="email" if recipient_email else "sms",
            action="client_request.underwriting_review_documents",
            recipient_email=recipient_email,
            recipient_phone=recipient_phone,
            strict_recipient=True,
        )
        results["document_request_delivery"] = "sent" if delivery.ok else "failed"
        if not delivery.ok:
            results["document_request_error"] = (delivery.detail or "delivery_failed")[:240]
        await log_action(
            db,
            dealer.id,
            user,
            "underwriting_review.room_prepared",
            "appointment",
            entity_id=dealer.id,
            after={
                "room_link_id": str(room.link.id),
                "required_owner_count": len(owner_state["required"]),
                "tax_years": list(tax_years),
                "delivery": results["document_request_delivery"],
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("underwriting review room setup failed dealer=%s", dealer.id)
        results["secure_room"] = "failed"
        results["document_request_delivery"] = "failed"
        results["document_request_error"] = "secure_room_setup_failed"
    return results


@router.post("/appointments", response_model=RepAppointmentRead, status_code=status.HTTP_201_CREATED)
async def create_standalone_rep_appointment(
    payload: RepAppointmentCreate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerRepAppointment:
    require_team_or_rep(user)
    host = await _rep_host_for(db, None, user)
    booking = await _booking_settings_for(db, host)
    await lock_calendar_owner(db, host.id)
    starts_at = _to_utc_minute(payload.starts_at)
    duration = booking.duration_min or 20
    slots = await _booking_slots(db, host, booking, duration_min=duration)
    if not any(abs((slot.starts_at - starts_at).total_seconds()) < 1 for slot in slots.slots):
        raise HTTPException(status.HTTP_409_CONFLICT, "That time is no longer available.")
    program_key, program_name = await _resolve_appointment_program(
        db,
        program_key=payload.program_key,
        program_name=payload.program_name,
    )
    payload = payload.model_copy(update={"program_key": program_key, "program_name": program_name})
    title = payload.title or _appointment_title(payload.kind, payload.invitee_name, None)
    who = payload.invitee_name
    if payload.invitee_email:
        who = f"{payload.invitee_name} <{payload.invitee_email}>"
    description, program, requested_amount, full_address = _booking_description(
        user=user, payload=payload, dealer=None
    )
    ev = CalendarEvent(
        loan_id=None,
        kind=CalendarEventKind.CALL,
        title=title,
        description=description,
        who=who[:160],
        starts_at=starts_at,
        duration_min=duration,
        status=CalendarEventStatus.PENDING,
        source=CalendarEventSource.MANUAL,
        owner_user_id=host.id,
        external_ref_kind="dealer_rep_appointment",
        external_ref_id=secrets.token_urlsafe(12),
    )
    db.add(ev)
    await db.flush()
    appt = DealerRepAppointment(
        dealer_id=None,
        owner_user_id=host.id,
        calendar_event_id=ev.id,
        kind=payload.kind,
        title=title,
        starts_at=starts_at,
        duration_min=duration,
        timezone=payload.timezone or booking.timezone,
        invitee_name=payload.invitee_name.strip(),
        invitee_email=payload.invitee_email,
        invitee_phone=payload.invitee_phone,
        company=payload.company,
        program_key=program_key,
        program_name=program,
        requested_amount=requested_amount,
        full_address=full_address,
        join_url=payload.join_url,
        notes=payload.notes,
        status="pending",
        client_rsvp_status="needs_action" if payload.invitee_email else "unknown",
        booked_by_user_id=user.id,
    )
    db.add(appt)
    await db.flush()
    ev.external_ref_id = str(appt.id)
    notice = await booking_reminders.register_booking(
        db,
        event=ev,
        booking=booking,
        invitee_name=payload.invitee_name,
        invitee_email=payload.invitee_email,
        invitee_phone=payload.invitee_phone,
        sms_consent=payload.transactional_sms_consent,
        sms_consent_method="in_person_device" if payload.transactional_sms_consent else None,
        sms_consent_ip=request.client.host if request.client else None,
        sms_consent_user_agent=request.headers.get("user-agent"),
        booked_by_user_id=user.id,
        program_name=program,
        requested_amount=requested_amount,
        full_address=full_address,
    )

    phone = consent_delivery.normalize_phone(payload.invitee_phone)
    if not payload.invitee_email and not phone:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide a valid email or mobile number.")
    contact = await _ensure_rep_contact(
        db,
        owner_user_id=user.id,
        dealer_id=None,
        full_name=payload.invitee_name,
        company=payload.company,
        email=payload.invitee_email.strip().lower() if payload.invitee_email else None,
        phone_e164=phone,
        source="appointment",
    )
    appt.contact_id = contact.id
    await _capture_rep_contact_sms_consent(
        db,
        request=request,
        user=user,
        contact=contact,
        dealer=None,
        phone_e164=phone,
        recipient_name=payload.invitee_name,
        transactional=payload.transactional_sms_consent,
        marketing=False,
        method="in_person_device",
    )
    thread = await _ensure_rep_thread(
        db,
        owner_user_id=user.id,
        contact=contact,
        dealer_id=None,
        channel="email" if payload.invitee_email else "sms",
        subject=title,
        source="appointment",
    )
    await _append_rep_inbox_message(
        db,
        thread=thread,
        contact=contact,
        direction="outbound",
        channel=thread.channel,
        subject=title,
        body=f"Appointment booked for {payload.invitee_name}: {starts_at.isoformat()}",
        provider="calendar",
        delivery_status="stored",
        sender=user.email if thread.channel == "email" else consent_delivery.sms_sender(),
        recipient=payload.invitee_email or phone,
    )
    await notify_users(
        db,
        recipient_ids={user.id},
        event_type="appointment_created",
        category="calendar",
        priority="high",
        title=f"Appointment booked: {payload.invitee_name}",
        body=f"{title} is scheduled for {_appointment_local_time(starts_at, appt.timezone)}.",
        target_type="dealer_rep_appointment",
        target_id=str(appt.id),
        deep_link=f"/calendar?appointment={appt.id}",
        meta={"appointment_id": str(appt.id), "calendar_event_id": str(ev.id)},
        email=True,
        push=True,
    )
    await db.commit()
    await db.refresh(appt)
    join = await booking_notify.push_to_google(
        db,
        ev,
        invitee_email=payload.invitee_email,
        invitee_name=payload.invitee_name,
        rep_email=user.email,
        rep_name=user.name,
        want_meet=booking.google_meet_enabled,
    )
    if join and not appt.join_url:
        appt.join_url = join
        notice.join_url = join
        ev.description = f"{description}\n\nJoin: {join}"
        await db.commit()
        await db.refresh(appt)
    booking_notify.notify_host(
        host,
        booking,
        starts_at,
        invitee_name=payload.invitee_name,
        invitee_email=payload.invitee_email or "not provided",
        invitee_phone=payload.invitee_phone,
        notes=payload.notes,
        join_url=appt.join_url,
    )
    if payload.invitee_email:
        if booking.confirmation_email_enabled:
            email_result = booking_notify.send_invitee_invite(
                host,
                booking,
                ev,
                starts_at,
                invitee_name=payload.invitee_name,
                invitee_email=payload.invitee_email,
                join_url=appt.join_url,
            )
            notice.confirmation_email_status = (
                "sent" if email_result and email_result.ok else "failed"
            )
            if email_result and not email_result.ok:
                notice.last_error = email_result.detail[:1000]
        await db.commit()
    booking_notify.send_rep_invite(host, booking, ev, starts_at, rep=user, join_url=appt.join_url)
    await booking_reminders.send_confirmation_sms(
        db, notice, ev, timezone_name=booking.timezone
    )
    return (await _appointment_read_rows(db, [appt]))[0]


@router.get("/dealers/{dealer_id}/appointments", response_model=list[RepAppointmentRead])
async def list_rep_appointments(
    dealer_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    include_cancelled: bool = Query(default=False),
) -> list[dict]:
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    q = select(DealerRepAppointment).where(DealerRepAppointment.dealer_id == dealer.id)
    if not include_cancelled:
        q = q.where(DealerRepAppointment.archived_at.is_(None), DealerRepAppointment.status != "cancelled")
    rows = list((await db.execute(q.order_by(DealerRepAppointment.starts_at.asc()))).scalars().all())
    return await _appointment_read_rows(db, rows)


@router.post(
    "/dealers/{dealer_id}/appointments",
    response_model=RepAppointmentRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_rep_appointment(
    dealer_id: UUID,
    payload: RepAppointmentCreate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerRepAppointment:
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    await _require_training_live_action(
        db,
        dealer=dealer,
        user=user,
        request=request,
        action="Book appointment",
        provider="Google Calendar / Google Meet / SES",
        recipient=payload.invitee_email or payload.invitee_phone,
        effect="Create a live calendar event and send the client invitation.",
    )
    host = await _rep_host_for(db, dealer, user)
    booking = await _booking_settings_for(db, host)
    await lock_calendar_owner(db, host.id)
    starts_at = _to_utc_minute(payload.starts_at)
    duration = booking.duration_min or 20
    slots = await _booking_slots(db, host, booking, duration_min=duration)
    if not any(abs((slot.starts_at - starts_at).total_seconds()) < 1 for slot in slots.slots):
        raise HTTPException(status.HTTP_409_CONFLICT, "That time is no longer available.")
    program_key, program_name = await _resolve_appointment_program(
        db,
        program_key=payload.program_key,
        program_name=payload.program_name,
    )
    payload = payload.model_copy(update={"program_key": program_key, "program_name": program_name})
    title = payload.title or _appointment_title(payload.kind, payload.invitee_name, dealer)
    who = payload.invitee_name
    if payload.invitee_email:
        who = f"{payload.invitee_name} <{payload.invitee_email}>"
    description, program, requested_amount, full_address = _booking_description(
        user=user, payload=payload, dealer=dealer
    )
    ev = CalendarEvent(
        loan_id=None,
        kind=CalendarEventKind.CALL,
        title=title,
        description=description,
        who=who[:160],
        starts_at=starts_at,
        duration_min=duration,
        status=CalendarEventStatus.PENDING,
        source=CalendarEventSource.MANUAL,
        owner_user_id=host.id,
        external_ref_kind="dealer_rep_appointment",
        external_ref_id=secrets.token_urlsafe(12),
    )
    db.add(ev)
    await db.flush()
    appt = DealerRepAppointment(
        dealer_id=dealer.id,
        owner_user_id=host.id,
        calendar_event_id=ev.id,
        kind=payload.kind,
        title=title,
        starts_at=starts_at,
        duration_min=duration,
        timezone=payload.timezone or booking.timezone,
        invitee_name=payload.invitee_name.strip(),
        invitee_email=payload.invitee_email,
        invitee_phone=payload.invitee_phone,
        company=payload.company or dealer.name,
        program_key=program_key,
        program_name=program,
        requested_amount=requested_amount,
        full_address=full_address,
        join_url=payload.join_url,
        notes=payload.notes,
        status="pending",
        client_rsvp_status="needs_action" if payload.invitee_email else "unknown",
        booked_by_user_id=user.id,
    )
    db.add(appt)
    await db.flush()
    ev.external_ref_id = str(appt.id)
    notice = await booking_reminders.register_booking(
        db,
        event=ev,
        booking=booking,
        invitee_name=payload.invitee_name,
        invitee_email=payload.invitee_email,
        invitee_phone=payload.invitee_phone,
        sms_consent=payload.transactional_sms_consent,
        sms_consent_method="in_person_device" if payload.transactional_sms_consent else None,
        sms_consent_ip=request.client.host if request.client else None,
        sms_consent_user_agent=request.headers.get("user-agent"),
        booked_by_user_id=user.id,
        program_name=program,
        requested_amount=requested_amount,
        full_address=full_address,
    )
    if dealer.is_training:
        await booking_reminders.cancel_pending(db, notice)
        notice.last_error = "Training file: unattended reminders are suppressed."
    phone = consent_delivery.normalize_phone(payload.invitee_phone)
    contact = await _ensure_rep_contact(
        db,
        owner_user_id=user.id,
        dealer_id=dealer.id,
        full_name=payload.invitee_name,
        company=payload.company or dealer.name,
        email=payload.invitee_email.strip().lower() if payload.invitee_email else None,
        phone_e164=phone,
        source="appointment",
    )
    appt.contact_id = contact.id
    await _capture_rep_contact_sms_consent(
        db,
        request=request,
        user=user,
        contact=contact,
        dealer=dealer,
        phone_e164=phone,
        recipient_name=payload.invitee_name,
        transactional=payload.transactional_sms_consent,
        marketing=False,
        method="in_person_device",
    )
    await log_action(
        db,
        dealer.id,
        user,
        "appointment.create",
        "appointment",
        entity_id=appt.id,
        after={
            "kind": appt.kind,
            "starts_at": appt.starts_at.isoformat(),
            "invitee": appt.invitee_name,
            "calendar_event_id": str(ev.id),
        },
    )
    room_results: dict[str, str] = {}
    if payload.kind == "underwriting_review":
        room_results = await _prepare_underwriting_review_room(
            db,
            dealer=dealer,
            user=user,
            recipient_email=payload.invitee_email,
            recipient_phone=payload.invitee_phone,
            requested_document_keys=payload.requested_document_keys,
        )
    await notify_users(
        db,
        recipient_ids={user.id},
        event_type="appointment_created",
        category="calendar",
        priority="high",
        title=f"Appointment booked: {payload.invitee_name}",
        body=f"{title} is scheduled for {_appointment_local_time(starts_at, appt.timezone)}.",
        target_type="dealer_rep_appointment",
        target_id=str(appt.id),
        deep_link=f"/calendar?appointment={appt.id}",
        meta={"appointment_id": str(appt.id), "calendar_event_id": str(ev.id)},
        email=True,
        push=True,
    )
    await db.commit()
    await db.refresh(appt)
    join = await booking_notify.push_to_google(
        db,
        ev,
        invitee_email=payload.invitee_email,
        invitee_name=payload.invitee_name,
        rep_email=user.email,
        rep_name=user.name,
        want_meet=booking.google_meet_enabled,
    )
    if join and not appt.join_url:
        appt.join_url = join
        notice.join_url = join
        ev.description = f"{description}\n\nJoin: {join}"
        await db.commit()
        await db.refresh(appt)
    booking_notify.notify_host(
        host,
        booking,
        starts_at,
        invitee_name=payload.invitee_name,
        invitee_email=payload.invitee_email or "not provided",
        invitee_phone=payload.invitee_phone,
        notes=payload.notes,
        join_url=appt.join_url,
    )
    if payload.invitee_email:
        if booking.confirmation_email_enabled:
            email_result = booking_notify.send_invitee_invite(
                host,
                booking,
                ev,
                starts_at,
                invitee_name=payload.invitee_name,
                invitee_email=payload.invitee_email,
                join_url=appt.join_url,
            )
            notice.confirmation_email_status = (
                "sent" if email_result and email_result.ok else "failed"
            )
            if email_result and not email_result.ok:
                notice.last_error = email_result.detail[:1000]
        await db.commit()
    booking_notify.send_rep_invite(host, booking, ev, starts_at, rep=user, join_url=appt.join_url)
    await booking_reminders.send_confirmation_sms(
        db, notice, ev, timezone_name=booking.timezone
    )
    result = (await _appointment_read_rows(db, [appt]))[0]
    if room_results:
        result["notification_results"] = {
            **(result.get("notification_results") or {}),
            **room_results,
        }
    return result


async def _cancel_rep_appointment(
    db: AsyncSession,
    *,
    appt: DealerRepAppointment,
    user: User,
    reason: str | None,
) -> dict:
    now = datetime.now(timezone.utc)
    if appt.archived_at is not None and appt.status == "cancelled":
        return (await _appointment_read_rows(db, [appt]))[0]
    event = await db.get(CalendarEvent, appt.calendar_event_id) if appt.calendar_event_id else None
    host = await db.get(User, appt.owner_user_id) if appt.owner_user_id else user
    rep = await db.get(User, appt.booked_by_user_id) if appt.booked_by_user_id else user
    booking = await _booking_settings_for(db, host or user)
    notice = (
        await db.execute(select(BookingNotification).where(BookingNotification.event_id == appt.calendar_event_id))
    ).scalar_one_or_none() if appt.calendar_event_id else None

    appt.status = "cancelled"
    appt.archived_at = now
    appt.archived_by_user_id = user.id
    appt.cancellation_reason = (reason or "").strip() or None
    if event:
        event.status = CalendarEventStatus.CANCELLED
    if notice:
        await booking_reminders.cancel_pending(db, notice)
    if appt.dealer_id:
        await log_action(
            db, appt.dealer_id, user, "appointment.cancelled", "appointment",
            entity_id=appt.id,
            after={"reason": appt.cancellation_reason, "archived_at": now.isoformat()},
        )
    if rep:
        await notify_users(
            db,
            recipient_ids={rep.id},
            event_type="appointment_cancelled",
            category="calendar",
            priority="high",
            title=f"Appointment cancelled: {appt.invitee_name}",
            body=f"{appt.title} was cancelled and archived.",
            target_type="dealer_rep_appointment",
            target_id=str(appt.id),
            deep_link="/calendar?include_cancelled=1",
            email=True,
            push=True,
        )
    await db.commit()

    results: dict[str, str] = {"rep": "queued" if rep else "unavailable"}
    if event:
        await booking_notify.push_to_google(
            db,
            event,
            invitee_email=appt.invitee_email,
            invitee_name=appt.invitee_name,
            rep_email=rep.email if rep else None,
            rep_name=rep.name if rep else None,
            want_meet=False,
            color_id=_appointment_google_color(appt.outcome),
        )
        results["google"] = "sent" if event.google_event_id else "unavailable"
        rep_result = booking_notify.send_rep_invite(
            host or user,
            booking,
            event,
            appt.starts_at,
            rep=rep,
            join_url=appt.join_url,
            cancel=True,
            sequence=int(now.timestamp()),
        )
        results["rep_calendar"] = "sent" if rep_result and rep_result.ok else "unavailable" if rep_result is None else "failed"
    if event and appt.invitee_email:
        email_result = booking_notify.send_invitee_invite(
            host or user,
            booking,
            event,
            appt.starts_at,
            invitee_name=appt.invitee_name,
            invitee_email=appt.invitee_email,
            join_url=appt.join_url,
            cancel=True,
            sequence=int(now.timestamp()),
        )
        results["client_email"] = "sent" if email_result and email_result.ok else "failed"
        if notice and email_result and not email_result.ok:
            notice.last_error = email_result.detail[:1000]
    if notice and notice.invitee_phone and notice.sms_consent:
        sms_body = f"Qualified Commercial: your appointment on {_appointment_local_time(appt.starts_at, appt.timezone)} was cancelled."
        try:
            sms_result = await asyncio.to_thread(consent_delivery._send_sms, notice.invitee_phone, sms_body)  # noqa: SLF001
            results["client_sms"] = "sent" if sms_result.ok else "failed"
            if not sms_result.ok:
                notice.last_error = sms_result.detail[:1000]
        except Exception:  # noqa: BLE001
            logger.exception("appointment cancellation SMS raised appointment=%s", appt.id)
            results["client_sms"] = "failed"
            notice.last_error = "sms_provider_exception"
    elif appt.invitee_phone:
        results["client_sms"] = "blocked_no_consent"
    await db.commit()
    await db.refresh(appt)
    data = (await _appointment_read_rows(db, [appt]))[0]
    data["notification_results"] = results
    return data


@router.patch("/appointments/{appointment_id}", response_model=RepAppointmentRead)
async def patch_rep_appointment(
    appointment_id: UUID,
    payload: RepAppointmentPatch,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    appt = await _load_owned_appointment(db, appointment_id, user)
    dealer = await db.get(DealerBusiness, appt.dealer_id) if appt.dealer_id else None
    if dealer is not None:
        await _require_training_live_action(
            db,
            dealer=dealer,
            user=user,
            request=request,
            action="Update appointment",
            provider="Google Calendar / SES / SMS",
            recipient=payload.invitee_email or appt.invitee_email or appt.invitee_phone,
            effect="Update the live event and send revised invitations when applicable.",
        )
    if payload.status == "cancelled":
        return await _cancel_rep_appointment(db, appt=appt, user=user, reason=None)
    event = await db.get(CalendarEvent, appt.calendar_event_id) if appt.calendar_event_id else None
    host = await db.get(User, appt.owner_user_id) if appt.owner_user_id else user
    rep = await db.get(User, appt.booked_by_user_id) if appt.booked_by_user_id else user
    booking = await _booking_settings_for(db, host or user)
    notice = (
        await db.execute(select(BookingNotification).where(BookingNotification.event_id == appt.calendar_event_id))
    ).scalar_one_or_none() if appt.calendar_event_id else None
    before = RepAppointmentRead.model_validate(appt).model_dump(mode="json")
    old_starts_at = appt.starts_at
    old_email = appt.invitee_email
    old_phone = consent_delivery.normalize_phone(appt.invitee_phone)

    if "dealer_id" in payload.model_fields_set and payload.dealer_id is not None:
        dealer = await resolve_dealer_scope(db, user, payload.dealer_id)
        appt.dealer_id = dealer.id
    else:
        dealer = await db.get(DealerBusiness, appt.dealer_id) if appt.dealer_id else None

    proposed_start = _to_utc_minute(payload.starts_at) if payload.starts_at is not None else appt.starts_at
    proposed_duration = payload.duration_min or appt.duration_min
    rescheduled = proposed_start != appt.starts_at or proposed_duration != appt.duration_min
    if rescheduled:
        await lock_calendar_owner(db, (host or user).id)
        availability = await _booking_slots(
            db,
            host or user,
            booking,
            duration_min=proposed_duration,
            exclude_event_id=event.id if event else None,
        )
        if not any(abs((slot.starts_at - proposed_start).total_seconds()) < 1 for slot in availability.slots):
            raise HTTPException(status.HTTP_409_CONFLICT, "That time is no longer available on the shared calendar.")
        appt.starts_at = proposed_start
        appt.duration_min = proposed_duration
        if appt.outcome in {"not_converted", "did_not_show"} and not payload.reopen_outcome:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Confirm that rescheduling should reopen the recorded outcome.",
            )
        if appt.outcome in {"not_converted", "did_not_show"}:
            appt.outcome = None
            appt.outcome_note = None
            appt.outcome_at = None
            appt.outcome_by_user_id = None

    if "program_key" in payload.model_fields_set or "program_name" in payload.model_fields_set:
        program_key, program_name = await _resolve_appointment_program(
            db,
            program_key=(payload.program_key if "program_key" in payload.model_fields_set else appt.program_key),
            program_name=(payload.program_name if "program_name" in payload.model_fields_set else appt.program_name),
            existing=appt,
        )
        appt.program_key = program_key
        appt.program_name = program_name

    for field in (
        "kind", "title", "timezone", "invitee_name", "company",
        "requested_amount", "full_address", "join_url", "notes",
    ):
        value = getattr(payload, field)
        if field in payload.model_fields_set:
            setattr(appt, field, value.strip() if isinstance(value, str) and value.strip() else value or None)
    if payload.invitee_email is not None:
        appt.invitee_email = payload.invitee_email.strip().lower() or None
    if payload.invitee_phone is not None:
        appt.invitee_phone = consent_delivery.normalize_phone(payload.invitee_phone)
    if payload.status is not None:
        if payload.status == "confirmed" and appt.client_rsvp_status != "accepted":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Only the client's accepted Google invitation can confirm an appointment.",
            )
        appt.status = payload.status
    if not appt.invitee_email and not appt.invitee_phone:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide an invitee email or phone.")

    email_changed = old_email != appt.invitee_email
    if rescheduled or email_changed:
        appt.client_rsvp_status = "needs_action" if appt.invitee_email else "unknown"
        appt.client_rsvp_at = None
        appt.rsvp_checked_at = None
        if appt.status != "cancelled":
            appt.status = "pending"

    if event:
        event.title = appt.title
        event.starts_at = appt.starts_at
        event.duration_min = appt.duration_min
        event.who = (
            f"{appt.invitee_name} <{appt.invitee_email}>" if appt.invitee_email else appt.invitee_name
        )[:160]
        event.status = CalendarEventStatus.DONE if appt.status == "done" else CalendarEventStatus.PENDING
        description, _, _, _ = _booking_description(
            user=rep or user,
            payload=_appointment_payload(appt, transactional_sms_consent=bool(notice and notice.sms_consent)),
            dealer=dealer,
        )
        event.description = f"{description}\n\nJoin: {appt.join_url}" if appt.join_url else description

    if appt.contact_id:
        contact = await db.get(DealerRepContact, appt.contact_id)
        if contact:
            contact.full_name = appt.invitee_name
            contact.company = appt.company or contact.company
            contact.email = appt.invitee_email
            contact.phone_e164 = consent_delivery.normalize_phone(appt.invitee_phone)
            contact.last_activity_at = datetime.now(timezone.utc)
            if old_phone != contact.phone_e164:
                contact.sms_transactional_consented_at = None
                contact.sms_marketing_consented_at = None
                contact.sms_consent_meta = None

    if notice:
        notice.invitee_name = appt.invitee_name
        notice.invitee_email = appt.invitee_email
        notice.invitee_phone = consent_delivery.normalize_phone(appt.invitee_phone)
        notice.program_name = appt.program_name
        notice.requested_amount = appt.requested_amount
        notice.full_address = appt.full_address
        notice.join_url = appt.join_url
        if old_phone != notice.invitee_phone:
            notice.sms_consent = False
            notice.sms_consent_at = None
            notice.sms_consent_method = None
            notice.sms_reminder_status = "blocked_no_consent" if notice.invitee_phone else "disabled"
            notice.confirmation_sms_status = "blocked_no_consent" if notice.invitee_phone else "disabled"
        if rescheduled:
            await booking_reminders.reschedule_pending(db, notice, appt.starts_at)

    if appt.dealer_id:
        await log_action(
            db, appt.dealer_id, user,
            "appointment.rescheduled" if rescheduled else "appointment.updated",
            "appointment", entity_id=appt.id, before=before,
            after=payload.model_dump(exclude_unset=True, mode="json"),
        )
    if rep:
        await notify_users(
            db,
            recipient_ids={rep.id},
            event_type="appointment_rescheduled" if rescheduled else "appointment_updated",
            category="calendar",
            priority="high",
            title=f"Appointment {'rescheduled' if rescheduled else 'updated'}: {appt.invitee_name}",
            body=f"{appt.title} is scheduled for {_appointment_local_time(appt.starts_at, appt.timezone)}.",
            target_type="dealer_rep_appointment",
            target_id=str(appt.id),
            deep_link=f"/calendar?appointment={appt.id}",
            email=True,
            push=True,
        )
    await db.commit()

    results: dict[str, str] = {"rep": "queued" if rep else "unavailable"}
    if event:
        join = await booking_notify.push_to_google(
            db,
            event,
            invitee_email=appt.invitee_email,
            invitee_name=appt.invitee_name,
            rep_email=rep.email if rep else None,
            rep_name=rep.name if rep else None,
            want_meet=booking.google_meet_enabled,
            color_id=_appointment_google_color(appt.outcome),
        )
        if join and not appt.join_url:
            appt.join_url = join
            if notice:
                notice.join_url = join
        results["google"] = "sent" if event.google_event_id else "unavailable"
        sequence = int(datetime.now(timezone.utc).timestamp())
        if old_email and old_email != appt.invitee_email:
            booking_notify.send_invitee_invite(
                host or user, booking, event, old_starts_at,
                invitee_name=appt.invitee_name, invitee_email=old_email,
                join_url=appt.join_url, cancel=True, sequence=sequence,
            )
        if appt.invitee_email:
            email_result = booking_notify.send_invitee_invite(
                host or user, booking, event, appt.starts_at,
                invitee_name=appt.invitee_name, invitee_email=appt.invitee_email,
                join_url=appt.join_url, sequence=sequence,
            )
            results["client_email"] = "sent" if email_result and email_result.ok else "failed"
            if notice:
                notice.confirmation_email_status = results["client_email"]
                if email_result and not email_result.ok:
                    notice.last_error = email_result.detail[:1000]
        rep_result = booking_notify.send_rep_invite(
            host or user,
            booking,
            event,
            appt.starts_at,
            rep=rep,
            join_url=appt.join_url,
            sequence=sequence,
        )
        results["rep_calendar"] = "sent" if rep_result and rep_result.ok else "unavailable" if rep_result is None else "failed"
    await db.commit()
    await db.refresh(appt)
    data = (await _appointment_read_rows(db, [appt]))[0]
    data["notification_results"] = results
    return data


@router.post("/appointments/{appointment_id}/cancel", response_model=RepAppointmentRead)
async def cancel_rep_appointment(
    appointment_id: UUID,
    payload: RepAppointmentCancel,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    appt = await _load_owned_appointment(db, appointment_id, user)
    dealer = await db.get(DealerBusiness, appt.dealer_id) if appt.dealer_id else None
    if dealer is not None:
        await _require_training_live_action(
            db,
            dealer=dealer,
            user=user,
            request=request,
            action="Cancel appointment",
            provider="Google Calendar / SES / SMS",
            recipient=appt.invitee_email or appt.invitee_phone,
            effect="Cancel the live event and send cancellation notices.",
        )
    return await _cancel_rep_appointment(db, appt=appt, user=user, reason=payload.reason)


def _appointment_amount(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = re.sub(r"[^0-9.]", "", value)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


async def _convert_appointment_to_field_desk(
    db: AsyncSession, appt: DealerRepAppointment, user: User
) -> DealerBusiness:
    if appt.converted_dealer_id:
        existing = await db.get(DealerBusiness, appt.converted_dealer_id)
        if existing:
            return existing
    if appt.dealer_id:
        existing = await db.get(DealerBusiness, appt.dealer_id)
        if existing:
            return existing
    owner_id = appt.booked_by_user_id or user.id
    dealer = DealerBusiness(
        name=appt.company or appt.invitee_name,
        legal_name=appt.company or appt.invitee_name,
        email=appt.invitee_email,
        phone=appt.invitee_phone,
        address=appt.full_address,
        funding_goal=_appointment_amount(appt.requested_amount),
        client_requested_amount=_appointment_amount(appt.requested_amount),
        funding_purpose=(appt.program_name or "other")[:48],
        notes="\n\n".join(part for part in [appt.notes, "Created from converted appointment."] if part),
        application_lifecycle="draft",
        owner_user_id=owner_id,
        case_ref=await _next_case_ref(db),
    )
    db.add(dealer)
    await db.flush()
    db.add(DealerSourceConnection(dealer_id=dealer.id, kind="uploads", status="active"))
    await propose_targets(db, dealer)
    await buckets_link.ensure_bucket(db, dealer)
    db.add(
        DealerRepLead(
            dealer_id=dealer.id,
            rep_user_id=owner_id,
            status="draft",
            status_history=[{
                "at": datetime.now(timezone.utc).isoformat(),
                "from": None,
                "to": "draft",
                "by": str(user.id),
                "by_name": user.name,
                "source": "appointment_conversion",
            }],
        )
    )
    if appt.contact_id:
        db.add(
            DealerApplicationContact(
                dealer_id=dealer.id,
                contact_id=appt.contact_id,
                relationship="primary_contact",
                is_primary=True,
            )
        )
        contact = await db.get(DealerRepContact, appt.contact_id)
        if contact:
            contact.dealer_id = dealer.id
    await db.flush()
    return dealer


async def _convert_appointment_to_ai_intake(
    db: AsyncSession,
    *,
    appt: DealerRepAppointment,
    user: User,
    request: Request,
    variant: str,
    notify_client: bool,
) -> UUID:
    if appt.converted_intake_id:
        return appt.converted_intake_id
    # Reuse the production admin-intake path so bucket setup, access controls,
    # requested documents, client ownership, and notification behavior stay
    # identical to an intake created from the lead-management screen.
    from app.routers import dealer_ai_intake as intake_api

    variant_const = intake_api.FUNDING_VARIANT if variant == "real_estate" else intake_api.DEALER_VARIANT
    existing = await intake_api._latest_active_intake_by_email(
        db, appt.invitee_email or "", variant=variant_const
    ) if appt.invitee_email else None
    if existing:
        return existing.id
    if not appt.invitee_email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "An email is required to create an AI intake.")
    result = await intake_api.create_admin_ai_lead(
        intake_api.AdminLeadCreate(
            variant=variant,
            full_name=appt.invitee_name,
            email=appt.invitee_email,
            phone=appt.invitee_phone,
            business_name=appt.company,
            investor_name=appt.company if variant == "real_estate" else None,
            target_property_address=appt.full_address if variant == "real_estate" else None,
            transaction_type=appt.program_name if variant == "real_estate" else None,
            requested_amount=_appointment_amount(appt.requested_amount),
            notify_client=notify_client,
            secure_room_pin=f"{secrets.randbelow(1_000_000):06d}",
        ),
        request,
        user,
        db,
    )
    return result.intake.id


@router.patch("/appointments/{appointment_id}/outcome", response_model=RepAppointmentRead)
async def set_rep_appointment_outcome(
    appointment_id: UUID,
    payload: RepAppointmentOutcomePatch,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    require_super_admin(user)
    appt = (
        await db.execute(
            select(DealerRepAppointment)
            .where(DealerRepAppointment.id == appointment_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if appt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found.")
    if appt.status == "cancelled" or appt.archived_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A cancelled appointment cannot receive an outcome.")
    if appt.outcome == "converted" and appt.conversion_target:
        if payload.outcome != "converted" or payload.conversion_target != appt.conversion_target:
            raise HTTPException(status.HTTP_409_CONFLICT, "This appointment has already been converted.")
        return (await _appointment_read_rows(db, [appt]))[0]

    converted_dealer: DealerBusiness | None = None
    converted_intake_id: UUID | None = None
    if payload.outcome == "converted":
        if payload.conversion_target == "field_desk":
            converted_dealer = await _convert_appointment_to_field_desk(db, appt, user)
        else:
            converted_intake_id = await _convert_appointment_to_ai_intake(
                db,
                appt=appt,
                user=user,
                request=request,
                variant=payload.ai_variant or "dealer",
                notify_client=payload.notify_client,
            )
            # The admin-intake helper commits. Re-lock before finalizing the
            # appointment so a retried request cannot create another target.
            appt = (
                await db.execute(
                    select(DealerRepAppointment)
                    .where(DealerRepAppointment.id == appointment_id)
                    .with_for_update()
                )
            ).scalar_one()
            if appt.converted_intake_id:
                converted_intake_id = appt.converted_intake_id

    now = datetime.now(timezone.utc)
    appt.outcome = payload.outcome
    appt.outcome_note = (payload.note or "").strip() or None
    appt.outcome_at = now
    appt.outcome_by_user_id = user.id
    if payload.outcome == "converted":
        appt.conversion_target = payload.conversion_target
        appt.converted_dealer_id = converted_dealer.id if converted_dealer else appt.converted_dealer_id
        appt.converted_intake_id = converted_intake_id or appt.converted_intake_id
        if converted_dealer:
            appt.dealer_id = converted_dealer.id
    event = await db.get(CalendarEvent, appt.calendar_event_id) if appt.calendar_event_id else None
    if appt.dealer_id:
        await log_action(
            db, appt.dealer_id, user, "appointment.outcome_changed", "appointment",
            entity_id=appt.id,
            after={
                "outcome": payload.outcome,
                "note": appt.outcome_note,
                "conversion_target": appt.conversion_target,
                "converted_dealer_id": str(appt.converted_dealer_id) if appt.converted_dealer_id else None,
                "converted_intake_id": str(appt.converted_intake_id) if appt.converted_intake_id else None,
            },
        )
    rep = await db.get(User, appt.booked_by_user_id) if appt.booked_by_user_id else None
    if rep:
        await notify_users(
            db,
            recipient_ids={rep.id},
            event_type="appointment_outcome_changed",
            category="calendar",
            priority="medium",
            title=f"Appointment outcome: {appt.invitee_name}",
            body=payload.outcome.replace("_", " ").title(),
            target_type="dealer_rep_appointment",
            target_id=str(appt.id),
            deep_link=f"/calendar?appointment={appt.id}",
            email=False,
            push=True,
        )
    await db.commit()
    if event:
        await booking_notify.push_to_google(
            db,
            event,
            invitee_email=appt.invitee_email,
            invitee_name=appt.invitee_name,
            rep_email=rep.email if rep else None,
            rep_name=rep.name if rep else None,
            want_meet=False,
            color_id=_appointment_google_color(appt.outcome),
        )
        await db.commit()
    await db.refresh(appt)
    return (await _appointment_read_rows(db, [appt]))[0]


@router.get(
    "/dealers/{dealer_id}/underwriting-review-preferences",
    response_model=list[UnderwritingReviewPreferenceRead],
)
async def list_underwriting_review_preferences(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerUnderwritingReviewPreference]:
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return list(
        (
            await db.execute(
                select(DealerUnderwritingReviewPreference)
                .where(DealerUnderwritingReviewPreference.dealer_id == dealer.id)
                .order_by(DealerUnderwritingReviewPreference.submitted_at.desc())
            )
        ).scalars().all()
    )


@router.get(
    "/dealers/{dealer_id}/underwriting-review-preferences/availability",
    response_model=BookingAvailabilityRead,
)
async def underwriting_review_preference_availability(
    dealer_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BookingAvailabilityRead:
    """Return shared-calendar openings inside the required review window."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    host = await _rep_host_for(db, dealer, user)
    booking = await _booking_settings_for(db, host)
    availability = await _booking_slots(
        db,
        host,
        booking,
        duration_min=booking.duration_min,
    )
    window_end = rep_workflows.underwriting_window_end(
        timezone_name=booking.timezone,
    )
    availability.slots = [
        slot
        for slot in availability.slots
        if slot.starts_at <= window_end
        and slot.starts_at.astimezone(rep_workflows.tz(booking.timezone)).weekday() < 5
    ]
    return availability


@router.post(
    "/dealers/{dealer_id}/underwriting-review-preferences",
    response_model=UnderwritingReviewPreferenceRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_underwriting_review_preference(
    dealer_id: UUID,
    payload: UnderwritingReviewPreferenceCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerUnderwritingReviewPreference:
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    host = await _rep_host_for(db, dealer, user)
    booking = await _booking_settings_for(db, host)
    try:
        slots = rep_workflows.validate_underwriting_slots(
            payload.slots, timezone_name=payload.timezone
        )
    except rep_workflows.SlotValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    availability = await _booking_slots(
        db,
        host,
        booking,
        duration_min=booking.duration_min,
    )
    if availability.calendar_sync_status != "connected":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The shared calendar is unavailable. Reconnect it before choosing review windows.",
        )
    available_starts = {
        slot.starts_at.astimezone(timezone.utc).replace(second=0, microsecond=0)
        for slot in availability.slots
    }
    selected_starts = {
        datetime.fromisoformat(slot["starts_at"]).astimezone(timezone.utc).replace(second=0, microsecond=0)
        for slot in slots
    }
    if not selected_starts.issubset(available_starts):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "One or more review windows are no longer available. Choose three current openings.",
        )

    now = datetime.now(timezone.utc)
    for existing in (
        await db.execute(
            select(DealerUnderwritingReviewPreference).where(
                DealerUnderwritingReviewPreference.dealer_id == dealer.id,
                DealerUnderwritingReviewPreference.status == "pending",
            )
        )
    ).scalars().all():
        existing.status = "expired"
    slots = [
        {
            **slot,
            "duration_min": booking.duration_min,
            "buffer_before_min": booking.buffer_before_min,
            "buffer_after_min": booking.buffer_after_min,
        }
        for slot in slots
    ]
    row = DealerUnderwritingReviewPreference(
        dealer_id=dealer.id,
        rep_user_id=user.id,
        timezone=payload.timezone,
        slots=slots,
        status="pending",
        submitted_at=now,
    )
    db.add(row)
    await db.flush()
    await log_action(
        db,
        dealer.id,
        user,
        "underwriting_review_preferences.create",
        "underwriting_review_preference",
        entity_id=row.id,
        after={
            "timezone": row.timezone,
            "slots": slots,
            "calendar_host_user_id": str(host.id),
        },
    )
    await db.commit()
    await db.refresh(row)
    return row


@router.post(
    "/dealers/{dealer_id}/underwriting-review-preferences/{preference_id}/book",
    response_model=RepAppointmentRead,
    status_code=status.HTTP_201_CREATED,
)
async def book_underwriting_review_preference(
    dealer_id: UUID,
    preference_id: UUID,
    payload: UnderwritingReviewPreferenceBook,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Turn one proposed window into the single real calendar appointment."""
    require_super_admin(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    await _require_training_live_action(
        db,
        dealer=dealer,
        user=user,
        request=request,
        action="Book underwriting review",
        provider="Google Calendar / Google Meet / SES",
        recipient=dealer.email or dealer.phone,
        effect="Select a proposed window, create a live appointment, and send the client invitation.",
    )
    preference = (
        await db.execute(
            select(DealerUnderwritingReviewPreference)
            .where(
                DealerUnderwritingReviewPreference.id == preference_id,
                DealerUnderwritingReviewPreference.dealer_id == dealer.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if preference is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review-window proposal not found.")
    if preference.appointment_id:
        existing = await db.get(DealerRepAppointment, preference.appointment_id)
        if existing is not None:
            existing_start = _to_utc_minute(existing.starts_at)
            requested_start = _to_utc_minute(payload.starts_at)
            if existing_start == requested_start or existing.client_rsvp_status != "declined":
                return (await _appointment_read_rows(db, [existing]))[0]
            # A declined invitation may be replaced by one of the other stored
            # proposals. Keep the declined appointment as immutable history.

    starts_at = _to_utc_minute(payload.starts_at)
    proposed_starts = {
        _to_utc_minute(datetime.fromisoformat(str(slot.get("starts_at")).replace("Z", "+00:00")))
        for slot in (preference.slots or [])
        if slot.get("starts_at")
    }
    if starts_at not in proposed_starts:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Choose one of the three proposed review windows.",
        )

    booking_rep = await db.get(User, preference.rep_user_id) if preference.rep_user_id else None
    booking_rep = booking_rep or user
    host = await _rep_host_for(db, dealer, booking_rep)
    booking = await _booking_settings_for(db, host)
    await lock_calendar_owner(db, host.id)
    availability = await _booking_slots(
        db,
        host,
        booking,
        duration_min=booking.duration_min,
    )
    if availability.calendar_sync_status != "connected":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The shared calendar is unavailable. Reconnect it before sending the invitation.",
        )
    if not any(abs((slot.starts_at - starts_at).total_seconds()) < 1 for slot in availability.slots):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That proposed time is no longer available. Choose another window or request new options.",
        )

    program_key, program_name = await _resolve_appointment_program(
        db,
        program_key=payload.program_key,
        program_name=payload.program_name,
    )
    appointment_payload = RepAppointmentCreate(
        kind="underwriting_review",
        starts_at=starts_at,
        duration_min=booking.duration_min,
        timezone=preference.timezone or booking.timezone,
        invitee_name=payload.invitee_name,
        invitee_email=str(payload.invitee_email),
        invitee_phone=payload.invitee_phone,
        company=dealer.name,
        program_key=program_key,
        program_name=program_name,
        requested_amount=payload.requested_amount,
        full_address=payload.full_address,
        notes=payload.notes,
        transactional_sms_consent=payload.transactional_sms_consent,
        requested_document_keys=payload.requested_document_keys,
    )
    title = _appointment_title("underwriting_review", payload.invitee_name, dealer)
    description, program, requested_amount, full_address = _booking_description(
        user=booking_rep,
        payload=appointment_payload,
        dealer=dealer,
    )
    event = CalendarEvent(
        kind=CalendarEventKind.CALL,
        title=title,
        description=description,
        who=f"{payload.invitee_name} <{payload.invitee_email}>"[:160],
        starts_at=starts_at,
        duration_min=booking.duration_min,
        status=CalendarEventStatus.PENDING,
        source=CalendarEventSource.MANUAL,
        owner_user_id=host.id,
        external_ref_kind="dealer_rep_appointment",
        external_ref_id=secrets.token_urlsafe(12),
    )
    db.add(event)
    await db.flush()
    appointment = DealerRepAppointment(
        dealer_id=dealer.id,
        owner_user_id=host.id,
        calendar_event_id=event.id,
        kind="underwriting_review",
        title=title,
        starts_at=starts_at,
        duration_min=booking.duration_min,
        timezone=preference.timezone or booking.timezone,
        invitee_name=payload.invitee_name.strip(),
        invitee_email=str(payload.invitee_email).strip().lower(),
        invitee_phone=payload.invitee_phone,
        company=dealer.name,
        program_key=program_key,
        program_name=program,
        requested_amount=requested_amount,
        full_address=full_address,
        notes=payload.notes,
        status="pending",
        client_rsvp_status="needs_action",
        booked_by_user_id=booking_rep.id,
    )
    db.add(appointment)
    await db.flush()
    event.external_ref_id = str(appointment.id)
    preference.selected_slot_at = starts_at
    preference.selected_by_user_id = user.id
    preference.appointment_id = appointment.id
    preference.status = "selected"

    notice = await booking_reminders.register_booking(
        db,
        event=event,
        booking=booking,
        invitee_name=payload.invitee_name,
        invitee_email=str(payload.invitee_email),
        invitee_phone=payload.invitee_phone,
        sms_consent=payload.transactional_sms_consent,
        sms_consent_method="in_person_device" if payload.transactional_sms_consent else None,
        sms_consent_ip=request.client.host if request.client else None,
        sms_consent_user_agent=request.headers.get("user-agent"),
        booked_by_user_id=booking_rep.id,
        program_name=program,
        requested_amount=requested_amount,
        full_address=full_address,
    )
    if dealer.is_training:
        await booking_reminders.cancel_pending(db, notice)
        notice.last_error = "Training file: unattended reminders are suppressed."
    phone = consent_delivery.normalize_phone(payload.invitee_phone)
    contact = await _ensure_rep_contact(
        db,
        owner_user_id=booking_rep.id,
        dealer_id=dealer.id,
        full_name=payload.invitee_name,
        company=dealer.name,
        email=str(payload.invitee_email).strip().lower(),
        phone_e164=phone,
        source="underwriting_review_appointment",
    )
    appointment.contact_id = contact.id
    await _capture_rep_contact_sms_consent(
        db,
        request=request,
        user=user,
        contact=contact,
        dealer=dealer,
        phone_e164=phone,
        recipient_name=payload.invitee_name,
        transactional=payload.transactional_sms_consent,
        marketing=False,
        method="in_person_device",
    )
    await log_action(
        db,
        dealer.id,
        user,
        "underwriting_review_preference.booked",
        "underwriting_review_preference",
        entity_id=preference.id,
        after={
            "appointment_id": str(appointment.id),
            "starts_at": starts_at.isoformat(),
            "client_rsvp_status": "needs_action",
        },
    )
    room_results = await _prepare_underwriting_review_room(
        db,
        dealer=dealer,
        user=user,
        recipient_email=str(payload.invitee_email),
        recipient_phone=payload.invitee_phone,
        requested_document_keys=payload.requested_document_keys,
    )
    await notify_users(
        db,
        recipient_ids={booking_rep.id, user.id},
        event_type="underwriting_review_invitation_sent",
        category="calendar",
        priority="high",
        title=f"Invitation sent: {payload.invitee_name}",
        body=f"Client response is pending for {_appointment_local_time(starts_at, appointment.timezone)}.",
        target_type="dealer_rep_appointment",
        target_id=str(appointment.id),
        deep_link=f"/calendar?appointment={appointment.id}",
        meta={"appointment_id": str(appointment.id), "preference_id": str(preference.id)},
        email=True,
        push=True,
    )
    # Appointment, selected preference, and idempotency link commit together.
    await db.commit()
    await db.refresh(appointment)

    join = await booking_notify.push_to_google(
        db,
        event,
        invitee_email=str(payload.invitee_email),
        invitee_name=payload.invitee_name,
        rep_email=booking_rep.email,
        rep_name=booking_rep.name,
        want_meet=booking.google_meet_enabled,
    )
    if join:
        appointment.join_url = join
        notice.join_url = join
        event.description = f"{description}\n\nJoin: {join}"
    if booking.confirmation_email_enabled:
        email_result = booking_notify.send_invitee_invite(
            host,
            booking,
            event,
            starts_at,
            invitee_name=payload.invitee_name,
            invitee_email=str(payload.invitee_email),
            join_url=join,
        )
        notice.confirmation_email_status = "sent" if email_result and email_result.ok else "failed"
        if email_result and not email_result.ok:
            notice.last_error = email_result.detail[:1000]
    booking_notify.send_rep_invite(
        host,
        booking,
        event,
        starts_at,
        rep=booking_rep,
        join_url=join,
    )
    await booking_reminders.send_confirmation_sms(
        db,
        notice,
        event,
        timezone_name=booking.timezone,
    )
    await db.commit()
    await db.refresh(appointment)
    result = (await _appointment_read_rows(db, [appointment]))[0]
    if room_results:
        result["notification_results"] = {
            **(result.get("notification_results") or {}),
            **room_results,
        }
    return result


@router.get("/contact-shares/program-pdfs", response_model=list[ProgramPdfAttachmentRead])
async def list_contact_share_program_pdfs(user: CurrentUser) -> list[dict[str, str]]:
    require_team_or_rep(user)
    return rep_workflows.program_pdf_options()


@router.post("/contact-shares", response_model=ContactShareRead, status_code=status.HTTP_201_CREATED)
async def create_contact_share(
    payload: ContactShareCreate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerRepContactShare:
    require_team_or_rep(user)
    dealer: DealerBusiness | None = None
    if payload.dealer_id is not None:
        dealer = await resolve_dealer_scope(db, user, payload.dealer_id)
        await _require_training_live_action(
            db,
            dealer=dealer,
            user=user,
            request=request,
            action="Share business card",
            provider="SES / SMS",
            recipient=payload.recipient_email or payload.recipient_phone,
            effect="Send the live contact card and selected program documents.",
        )
    phone = consent_delivery.normalize_phone(payload.recipient_phone)
    email = payload.recipient_email.strip().lower() if payload.recipient_email else None
    try:
        selected_pdfs = rep_workflows.selected_program_pdfs(payload.program_pdf_keys)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    contact = await _ensure_rep_contact(
        db,
        owner_user_id=user.id,
        dealer_id=dealer.id if dealer else None,
        full_name=payload.recipient_name,
        company=payload.company or (dealer.name if dealer else None),
        email=email,
        phone_e164=phone,
        source="contact_share",
    )
    await _capture_rep_contact_sms_consent(
        db,
        request=request,
        user=user,
        contact=contact,
        dealer=dealer,
        phone_e164=phone,
        recipient_name=payload.recipient_name,
        transactional=payload.transactional_sms_consent,
        marketing=payload.marketing_sms_consent,
        method=payload.consent_method,
    )
    token = secrets.token_urlsafe(24)[:48]
    base = get_settings().rep_app_url.rstrip("/")
    card_url = f"{base}/card/{token}"
    booking_row = (
        await db.execute(
            select(BookingSettings).where(
                BookingSettings.user_id == user.id,
                BookingSettings.enabled.is_(True),
                BookingSettings.slug.is_not(None),
            )
        )
    ).scalar_one_or_none()
    booking_url = f"{base}/book/{booking_row.slug}" if booking_row and booking_row.slug else card_url
    application_url = f"{base}/?new=1"
    profile = (
        await db.execute(
            select(DealerFieldDeskProfile).where(
                DealerFieldDeskProfile.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    profile_snapshot = {
        "display_name": (profile.display_name if profile else None) or user.name,
        "title": profile.title if profile else None,
        "phone": profile.phone if profile else None,
        "display_email": (profile.display_email if profile else None) or user.email,
        "short_bio": profile.short_bio if profile else None,
        "preferred_locale": (profile.preferred_locale if profile else None) or "en",
        "headshot_s3_key": profile.headshot_s3_key if profile else None,
    }
    copy = rep_workflows.build_contact_share_copy(
        rep_name=profile_snapshot["display_name"] or "Qualified Commercial",
        rep_email=profile_snapshot["display_email"] or user.email,
        rep_phone=profile_snapshot["phone"],
        recipient_name=payload.recipient_name,
        card_url=card_url,
        booking_url=booking_url,
        application_url=application_url,
        notes=payload.notes,
    )
    share = DealerRepContactShare(
        owner_user_id=user.id,
        contact_id=contact.id,
        dealer_id=dealer.id if dealer else None,
        recipient_name=payload.recipient_name.strip(),
        recipient_email=email,
        recipient_phone_e164=phone,
        channel=payload.channel,
        card_token=token,
        subject=copy.subject,
        body=copy.email_body,
        email_status="not_requested",
        sms_status="not_requested",
        provider_refs={
            "program_pdf_keys": [pdf.key for pdf in selected_pdfs],
            "profile_snapshot": profile_snapshot,
            "personal_note": payload.notes.strip() if payload.notes else None,
        },
        created_by_user_id=user.id,
    )
    db.add(share)
    await db.flush()

    refs: dict[str, object] = {
        "program_pdf_keys": [pdf.key for pdf in selected_pdfs],
        "profile_snapshot": profile_snapshot,
        "personal_note": payload.notes.strip() if payload.notes else None,
    }
    if payload.channel in {"email", "email_sms"}:
        if email:
            attachments = [
                (pdf.filename, rep_workflows.render_program_pdf(pdf), "application/pdf")
                for pdf in selected_pdfs
            ]
            if attachments:
                email_res = await asyncio.to_thread(
                    ses_client.send_raw_email,
                    to_emails=[email],
                    subject=copy.subject,
                    body_text=copy.email_body,
                    attachments=attachments,
                )
            else:
                email_res = await asyncio.to_thread(
                    ses_client.send_email,
                    to_email=email,
                    subject=copy.subject,
                    body_text=copy.email_body,
                )
            share.email_status = "sent" if email_res.ok else "failed"
            if email_res.message_id:
                refs["email_message_id"] = email_res.message_id
        else:
            share.email_status = "missing_recipient"
        thread = await _ensure_rep_thread(
            db,
            owner_user_id=user.id,
            contact=contact,
            dealer_id=dealer.id if dealer else None,
            channel="email",
            subject=copy.subject,
            source="contact_share",
            dealer_scoped=dealer is not None,
        )
        await _append_rep_inbox_message(
            db,
            thread=thread,
            contact=contact,
            direction="outbound",
            channel="email",
            subject=copy.subject,
            body=copy.email_body,
            provider="ses",
            provider_message_id=refs.get("email_message_id") if isinstance(refs.get("email_message_id"), str) else None,
            delivery_status=share.email_status,
            sender=user.email,
            recipient=email,
        )
    if payload.channel in {"sms", "email_sms"}:
        sms_res = None
        sms_allowed = bool(
            phone
            and contact.sms_opted_out_at is None
            and (contact.sms_marketing_consented_at or contact.sms_transactional_consented_at)
        )
        if not phone:
            share.sms_status = "missing_recipient"
        elif not sms_allowed:
            share.sms_status = "blocked_no_consent"
        else:
            sms_res = await asyncio.to_thread(consent_delivery._send_sms, phone, copy.sms_body)  # noqa: SLF001
            share.sms_status = "sent" if sms_res.ok else "failed"
            if sms_res.ok:
                refs["sms_message_id"] = sms_res.provider_message_id
                refs["sms_provider"] = sms_res.provider
        thread = await _ensure_rep_thread(
            db,
            owner_user_id=user.id,
            contact=contact,
            dealer_id=dealer.id if dealer else None,
            channel="sms",
            subject=copy.subject,
            source="contact_share",
            dealer_scoped=dealer is not None,
        )
        await _append_rep_inbox_message(
            db,
            thread=thread,
            contact=contact,
            direction="outbound",
            channel="sms",
            subject=copy.subject,
            body=copy.sms_body,
            provider=sms_res.provider if sms_res else str(consent_delivery.sms_provider_status()["provider"]),
            provider_message_id=refs.get("sms_message_id"),
            provider_error=sms_res.detail if sms_res and not sms_res.ok else None,
            delivery_status=share.sms_status,
            sender=sms_res.sender if sms_res else consent_delivery.sms_sender(),
            recipient=phone,
        )
    share.provider_refs = refs
    if dealer is not None:
        await log_action(
            db,
            dealer.id,
            user,
            "contact_share.create",
            "contact_share",
            entity_id=share.id,
            after={
                "recipient": payload.recipient_name,
                "channel": payload.channel,
                "email_status": share.email_status,
                "sms_status": share.sms_status,
            },
        )
    await db.commit()
    await db.refresh(share)
    return share


@router.get("/contact-shares/card/{token}", response_model=ContactCardRead)
async def read_contact_card(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ContactCardRead:
    share_row = (
        await db.execute(
            select(DealerRepContactShare, DealerRepContact, User)
            .outerjoin(DealerRepContact, DealerRepContact.id == DealerRepContactShare.contact_id)
            .outerjoin(User, User.id == DealerRepContactShare.owner_user_id)
            .where(DealerRepContactShare.card_token == token)
        )
    ).first()
    if share_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact card not found.")
    share, contact, rep = share_row
    base = get_settings().rep_app_url.rstrip("/")
    booking_row = None
    if share.owner_user_id:
        booking_row = (
            await db.execute(
                select(BookingSettings).where(
                    BookingSettings.user_id == share.owner_user_id,
                    BookingSettings.enabled.is_(True),
                    BookingSettings.slug.is_not(None),
                )
            )
        ).scalar_one_or_none()
    booking_url = f"{base}/book/{booking_row.slug}" if booking_row and booking_row.slug else f"{base}/?new=1"
    refs = share.provider_refs or {}
    profile = None
    if share.owner_user_id:
        profile = (
            await db.execute(
                select(DealerFieldDeskProfile).where(
                    DealerFieldDeskProfile.user_id == share.owner_user_id
                )
            )
        ).scalar_one_or_none()
    if profile is not None and not profile.card_visible:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact card not found.")
    snapshot = refs.get("profile_snapshot") if isinstance(refs, dict) else None
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    use_profile = profile is not None
    rep_name = (
        profile.display_name if use_profile else snapshot.get("display_name")
    ) or (rep.name if rep else None) or "Qualified Commercial"
    rep_email = (
        profile.display_email if use_profile else snapshot.get("display_email")
    ) or (rep.email if rep else None)
    rep_title = profile.title if use_profile else snapshot.get("title")
    rep_phone = profile.phone if use_profile else snapshot.get("phone")
    rep_bio = profile.short_bio if use_profile else snapshot.get("short_bio")
    rep_locale = (
        profile.preferred_locale if use_profile else snapshot.get("preferred_locale")
    ) or "en"
    headshot_key = (
        profile.headshot_s3_key if use_profile else snapshot.get("headshot_s3_key")
    )
    pdf_keys = refs.get("program_pdf_keys") if isinstance(refs, dict) else None
    program_pdfs: list[ContactCardProgramPdfRead] = []
    if isinstance(pdf_keys, list):
        selected = []
        for raw_key in pdf_keys:
            pdf = rep_workflows.program_pdf(str(raw_key))
            if pdf is not None:
                selected.append(pdf)
        for pdf in selected:
            program_pdfs.append(
                ContactCardProgramPdfRead(
                    key=pdf.key,
                    title=pdf.title,
                    description=pdf.description,
                    filename=pdf.filename,
                    download_url=str(
                        request.url_for(
                            "read_contact_card_program_pdf",
                            token=token,
                            key=pdf.key,
                        )
                    ),
                )
            )
    return ContactCardRead(
        recipient_name=share.recipient_name,
        company=contact.company if contact else None,
        rep_name=rep_name,
        rep_email=rep_email,
        rep_title=rep_title,
        rep_phone=rep_phone,
        rep_bio=rep_bio,
        rep_locale="es" if rep_locale == "es" else "en",
        headshot_url=storage.presign_get(
            str(headshot_key), content_type="image/jpeg"
        )
        if headshot_key
        else None,
        subject=share.subject,
        body=share.body,
        message=_contact_card_message(share.body, refs),
        booking_url=booking_url,
        application_url=f"{base}/?new=1",
        vcard_url=str(request.url_for("read_contact_card_vcard", token=token)),
        program_pdfs=program_pdfs,
    )


def _contact_card_message(body: str, refs: dict) -> str:
    personal_note = refs.get("personal_note") if isinstance(refs, dict) else None
    if isinstance(personal_note, str) and personal_note.strip():
        return personal_note.strip()
    lines = [line.strip() for line in (body or "").splitlines()]
    excluded_prefixes = (
        "Contact card:",
        "Book a time:",
        "Open an application:",
        "Email:",
        "Phone:",
    )
    meaningful = [
        line
        for line in lines
        if line
        and not line.startswith(excluded_prefixes)
        and line != "Qualified Commercial"
        and not line.startswith("You can keep my contact card")
    ]
    if meaningful and meaningful[0].lower().startswith("hi "):
        meaningful = meaningful[1:]
    return "\n\n".join(meaningful).strip()


def _vcard_escape(value: str | None) -> str:
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


@router.get("/contact-shares/card/{token}/vcard", name="read_contact_card_vcard")
async def read_contact_card_vcard(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    share_row = (
        await db.execute(
            select(DealerRepContactShare, User)
            .outerjoin(User, User.id == DealerRepContactShare.owner_user_id)
            .where(DealerRepContactShare.card_token == token)
        )
    ).first()
    if share_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact card not found.")
    share, rep = share_row
    profile = None
    if share.owner_user_id:
        profile = (
            await db.execute(
                select(DealerFieldDeskProfile).where(
                    DealerFieldDeskProfile.user_id == share.owner_user_id
                )
            )
        ).scalar_one_or_none()
    if profile is not None and not profile.card_visible:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact card not found.")
    refs = share.provider_refs or {}
    snapshot = refs.get("profile_snapshot") if isinstance(refs, dict) else None
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    use_profile = profile is not None
    name = (
        (profile.display_name if use_profile else snapshot.get("display_name"))
        or (rep.name if rep else None)
        or "Qualified Commercial"
    )
    email = (
        (profile.display_email if use_profile else snapshot.get("display_email"))
        or (rep.email if rep else None)
    )
    phone = profile.phone if use_profile else snapshot.get("phone")
    title = profile.title if use_profile else snapshot.get("title")
    base = get_settings().rep_app_url.rstrip("/")
    booking = None
    if share.owner_user_id:
        booking = (
            await db.execute(
                select(BookingSettings).where(
                    BookingSettings.user_id == share.owner_user_id,
                    BookingSettings.enabled.is_(True),
                    BookingSettings.slug.is_not(None),
                )
            )
        ).scalar_one_or_none()
    name_parts = str(name).split(None, 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""
    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"N:{_vcard_escape(last_name)};{_vcard_escape(first_name)};;;",
        f"FN:{_vcard_escape(str(name))}",
        "ORG:Qualified Commercial LLC",
    ]
    if title:
        lines.append(f"TITLE:{_vcard_escape(str(title))}")
    if email:
        lines.append(f"EMAIL;TYPE=INTERNET,WORK:{_vcard_escape(str(email))}")
    if phone:
        lines.append(f"TEL;TYPE=CELL,VOICE:{_vcard_escape(str(phone))}")
    if booking and booking.slug:
        lines.append(f"URL;TYPE=WORK:{_vcard_escape(f'{base}/book/{booking.slug}')}")
    lines.extend([f"URL:{_vcard_escape(f'{base}/card/{token}')}", "END:VCARD", ""])
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip("-") or "qualified-commercial-contact"
    return StreamingResponse(
        io.BytesIO("\r\n".join(lines).encode("utf-8")),
        media_type="text/vcard; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}.vcf"'},
    )


@router.get("/contact-shares/card/{token}/program-pdfs/{key}")
async def read_contact_card_program_pdf(
    token: str,
    key: str,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    share = (
        await db.execute(
            select(DealerRepContactShare).where(DealerRepContactShare.card_token == token)
        )
    ).scalar_one_or_none()
    if share is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact card not found.")
    refs = share.provider_refs or {}
    pdf_keys = refs.get("program_pdf_keys") if isinstance(refs, dict) else None
    if not isinstance(pdf_keys, list) or key not in {str(value) for value in pdf_keys}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program PDF not found.")
    pdf = rep_workflows.program_pdf(key)
    if pdf is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program PDF not found.")
    return StreamingResponse(
        io.BytesIO(rep_workflows.render_program_pdf(pdf)),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{pdf.filename}"'},
    )


@router.post(
    "/inbox/threads",
    response_model=RepInboxComposeResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_rep_inbox_thread(
    payload: RepInboxThreadCreate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RepInboxComposeResult:
    require_team_or_rep(user)
    dealer: DealerBusiness | None = None
    if payload.dealer_id is not None:
        dealer = await resolve_dealer_scope(db, user, payload.dealer_id)
        await _require_training_live_action(
            db,
            dealer=dealer,
            user=user,
            request=request,
            action="Start inbox conversation",
            provider="SES / SMS",
            recipient=payload.recipient_email or payload.recipient_phone,
            effect="Send the first live message through the selected providers.",
        )
    phone = consent_delivery.normalize_phone(payload.recipient_phone)
    email = payload.recipient_email.strip().lower() if payload.recipient_email else None
    channels = list(dict.fromkeys(payload.channels))
    if "sms" in channels and not phone:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide a valid mobile number for SMS.")

    contact = await _ensure_rep_contact(
        db,
        owner_user_id=user.id,
        dealer_id=dealer.id if dealer else None,
        full_name=payload.recipient_name,
        company=payload.company or (dealer.name if dealer else None),
        email=email,
        phone_e164=phone,
        source="manual",
    )
    await _capture_rep_contact_sms_consent(
        db,
        request=request,
        user=user,
        contact=contact,
        dealer=dealer,
        phone_e164=phone,
        recipient_name=payload.recipient_name,
        transactional=payload.transactional_sms_consent,
        marketing=payload.marketing_sms_consent,
        method=payload.consent_method,
    )

    if "sms" in channels:
        if contact.sms_opted_out_at is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "This contact opted out of SMS.")
        allowed = bool(contact.sms_transactional_consented_at or contact.sms_marketing_consented_at)
        if not allowed:
            raise HTTPException(status.HTTP_409_CONFLICT, "This contact has not granted SMS consent.")

    result_threads: list[RepInboxThreadRead] = []
    result_messages: list[DealerRepInboxMessage] = []
    for channel in channels:
        thread = await _ensure_rep_thread(
            db,
            owner_user_id=user.id,
            contact=contact,
            dealer_id=dealer.id if dealer else None,
            channel=channel,
            subject=payload.subject,
            source="manual",
            dealer_scoped=dealer is not None,
        )
        provider = None
        provider_id = None
        delivery_status = "stored"
        sender = user.email
        recipient = email
        if channel == "email":
            if not email:
                raise HTTPException(status.HTTP_409_CONFLICT, "This contact has no email address.")
            res = await asyncio.to_thread(
                ses_client.send_email,
                to_email=email,
                subject=payload.subject,
                body_text=payload.body,
            )
            provider = "ses"
            provider_id = res.message_id
            delivery_status = "sent" if res.ok else "failed"
        else:
            recipient = phone
            res = await asyncio.to_thread(consent_delivery._send_sms, phone, payload.body)  # noqa: SLF001
            sender = res.sender
            provider = res.provider
            provider_id = res.provider_message_id if res.ok else None
            delivery_status = "sent" if res.ok else "failed"
        msg = await _append_rep_inbox_message(
            db,
            thread=thread,
            contact=contact,
            direction="outbound",
            channel=channel,
            subject=payload.subject,
            body=payload.body,
            provider=provider,
            provider_message_id=provider_id,
            delivery_status=delivery_status,
            sender=sender,
            recipient=recipient,
        )
        result_threads.append(_thread_read(thread, contact))
        result_messages.append(msg)

    if dealer is not None:
        db.add(
            DealerMessage(
                dealer_id=dealer.id,
                author_user_id=user.id,
                author_name=user.name,
                body=payload.body,
                internal=False,
                channel="client",
            )
        )
        await log_action(
            db,
            dealer.id,
            user,
            "inbox.thread.create",
            "inbox_thread",
            after={
                "recipient": payload.recipient_name,
                "channels": channels,
                "subject": payload.subject,
            },
        )
    await db.commit()
    for msg in result_messages:
        await db.refresh(msg)
    return RepInboxComposeResult(threads=result_threads, messages=result_messages)


@router.get("/inbox/threads", response_model=list[RepInboxThreadRead])
async def list_rep_inbox_threads(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    channel: str | None = None,
) -> list[RepInboxThreadRead]:
    require_team_or_rep(user)
    q = (
        select(DealerRepInboxThread, DealerRepContact)
        .outerjoin(DealerRepContact, DealerRepContact.id == DealerRepInboxThread.contact_id)
        .where(_rep_inbox_access_filter(user), _rep_inbox_live_file_filter())
        .order_by(DealerRepInboxThread.last_message_at.desc().nullslast(), DealerRepInboxThread.created_at.desc())
    )
    if channel:
        if channel not in {"email", "sms"}:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown inbox channel.")
        q = q.where(DealerRepInboxThread.channel == channel)
    rows = (await db.execute(q)).all()
    return [_thread_read(thread, contact) for thread, contact in rows]


@router.get("/inbox/threads/{thread_id}/messages", response_model=list[RepInboxMessageRead])
async def list_rep_inbox_messages(
    thread_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[DealerRepInboxMessage]:
    require_team_or_rep(user)
    thread = (
        await db.execute(
            select(DealerRepInboxThread).where(
                DealerRepInboxThread.id == thread_id,
                _rep_inbox_access_filter(user),
                _rep_inbox_live_file_filter(),
            )
        )
    ).scalar_one_or_none()
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found.")
    rows = list(
        (
            await db.execute(
                select(DealerRepInboxMessage)
                .where(DealerRepInboxMessage.thread_id == thread.id)
                .order_by(DealerRepInboxMessage.created_at.asc())
            )
        ).scalars().all()
    )
    now = datetime.now(timezone.utc)
    for row in rows:
        if row.direction == "inbound" and row.read_at is None:
            row.read_at = now
    thread.unread_count = 0
    await db.commit()
    return rows


@router.post(
    "/inbox/threads/{thread_id}/messages",
    response_model=RepInboxMessageRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_rep_inbox_message(
    thread_id: UUID,
    payload: RepInboxMessageCreate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerRepInboxMessage:
    require_team_or_rep(user)
    row = (
        await db.execute(
            select(DealerRepInboxThread, DealerRepContact)
            .outerjoin(DealerRepContact, DealerRepContact.id == DealerRepInboxThread.contact_id)
            .where(
                DealerRepInboxThread.id == thread_id,
                _rep_inbox_access_filter(user),
                _rep_inbox_live_file_filter(),
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found.")
    thread, contact = row
    if thread.dealer_id is not None:
        dealer = await load_dealer(db, thread.dealer_id)
        await _require_training_live_action(
            db,
            dealer=dealer,
            user=user,
            request=request,
            action="Reply from inbox",
            provider="SES / SMS",
            recipient=(contact.email or contact.phone_e164) if contact else None,
            effect="Send this reply through the live messaging provider.",
        )
    channel = payload.channel or thread.channel
    if channel not in {"email", "sms"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown inbox channel.")
    delivery_status = "stored"
    provider = None
    provider_id = None
    provider_error = None
    recipient = None
    sender = user.email
    if channel == "email":
        recipient = contact.email if contact else None
        if not recipient:
            raise HTTPException(status.HTTP_409_CONFLICT, "This contact has no email address.")
        res = await asyncio.to_thread(
            ses_client.send_email,
            to_email=recipient,
            subject=thread.subject,
            body_text=payload.body,
        )
        provider = "ses"
        provider_id = res.message_id
        delivery_status = "sent" if res.ok else "failed"
        provider_error = None if res.ok else res.detail
    else:
        recipient = contact.phone_e164 if contact else None
        if not recipient:
            raise HTTPException(status.HTTP_409_CONFLICT, "This contact has no mobile number.")
        if contact and contact.sms_opted_out_at is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "This contact opted out of SMS.")
        allowed = bool(contact and (contact.sms_transactional_consented_at or contact.sms_marketing_consented_at))
        if not allowed:
            raise HTTPException(status.HTTP_409_CONFLICT, "This contact has not granted SMS consent.")
        res = await asyncio.to_thread(consent_delivery._send_sms, recipient, payload.body)  # noqa: SLF001
        provider = res.provider
        provider_id = res.provider_message_id if res.ok else None
        delivery_status = "sent" if res.ok else "failed"
        provider_error = None if res.ok else res.detail
        sender = res.sender
    msg = await _append_rep_inbox_message(
        db,
        thread=thread,
        contact=contact,
        direction="outbound",
        channel=channel,
        subject=thread.subject,
        body=payload.body,
        provider=provider,
        provider_message_id=provider_id,
        provider_error=provider_error,
        delivery_status=delivery_status,
        sender=sender,
        recipient=recipient,
    )
    if thread.dealer_id is not None:
        # Thread ownership was checked above. The linked application remains a
        # case activity target; it does not broaden mailbox visibility.
        dealer = await load_dealer(db, thread.dealer_id)
        db.add(
            DealerMessage(
                dealer_id=dealer.id,
                author_user_id=user.id,
                author_name=user.name,
                body=payload.body,
                internal=False,
                channel="client",
            )
        )
    await db.commit()
    await db.refresh(msg)
    return msg


# --- Stream 5: messaging, sessions, global alerts & lender package -----------


@router.get("/dealers/{dealer_id}/messages/unread-count")
async def messages_unread_count(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> dict:
    """Messages this viewer hasn't seen: created after their seen marker, by
    someone else; dealers never count internal notes."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    seen = (
        await db.execute(
            select(DealerMessageSeen.seen_at).where(
                DealerMessageSeen.dealer_id == dealer.id, DealerMessageSeen.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    q = select(func.count()).select_from(DealerMessage).where(
        DealerMessage.dealer_id == dealer.id,
        DealerMessage.author_user_id != user.id,
    )
    if user.role == Role.DEALER:
        q = q.where(DealerMessage.internal.is_(False))
    if seen is not None:
        q = q.where(DealerMessage.created_at > seen)
    return {"unread": int((await db.execute(q)).scalar_one())}


@router.post("/dealers/{dealer_id}/messages/seen", status_code=status.HTTP_204_NO_CONTENT)
async def mark_messages_seen(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> None:
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    row = (
        await db.execute(
            select(DealerMessageSeen).where(
                DealerMessageSeen.dealer_id == dealer.id, DealerMessageSeen.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if row is None:
        db.add(DealerMessageSeen(dealer_id=dealer.id, user_id=user.id, seen_at=now))
    else:
        row.seen_at = now
    await db.commit()


@router.get("/dealers/{dealer_id}/messages", response_model=list[MessageRead])
async def list_messages(
    dealer_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    channel: str | None = None,
) -> list[DealerMessage]:
    """One channel of the file's conversation, oldest first.

    Omit `channel` and the desk gets everything, which is what the combined
    view wants. A DEALER login is confined to the client channel whatever they
    ask for."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    q = select(DealerMessage).where(DealerMessage.dealer_id == dealer.id)
    if user.role == Role.DEALER:
        # Belt and braces: filter on the channel allowlist AND on the legacy
        # boolean. Either alone would be enough today; together, a row that
        # somehow disagrees with itself stays hidden rather than leaking.
        q = q.where(
            DealerMessage.channel.in_(CLIENT_VISIBLE_CHANNELS),
            DealerMessage.internal.is_(False),
        )
    elif channel is not None:
        if channel not in MESSAGE_CHANNELS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown channel {channel!r}")
        q = q.where(DealerMessage.channel == channel)
    return (
        (await db.execute(q.order_by(DealerMessage.created_at.asc()))).scalars().all()
    )


@router.post(
    "/dealers/{dealer_id}/messages", response_model=MessageRead, status_code=status.HTTP_201_CREATED
)
async def create_message(
    dealer_id: UUID, payload: MessageCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerMessage:
    """Post to the file's thread.

    Two opposite defaults, both deliberate. A DEALER can never author an
    internal note whatever the payload says, because the client must not be
    able to write into the desk's private conversation. A FIELD_REP defaults
    the other way: their messages are internal unless they explicitly ask for
    a client-visible one, because a rep typing a candid note about a borrower
    and having it land in front of that borrower is the failure that matters
    most here."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)

    if user.role == Role.DEALER:
        # A client writes to the client thread and nowhere else, whatever the
        # payload claims. This is the one rule in the file that must not have
        # an escape hatch.
        channel = "client"
    elif payload.channel is not None:
        if payload.channel not in MESSAGE_CHANNELS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Unknown channel {payload.channel!r}"
            )
        channel = payload.channel
    elif payload.internal is not None:
        # Older callers that only know about the boolean still work.
        channel = "desk" if payload.internal else "client"
    elif user.role == Role.FIELD_REP:
        # A rep typing a candid remark about a borrower and having it land in
        # front of that borrower is the failure that matters most here, so
        # silence defaults inward. Reaching the client is a deliberate act.
        channel = "desk"
    else:
        channel = "client"

    internal = channel not in CLIENT_VISIBLE_CHANNELS
    message = DealerMessage(
        dealer_id=dealer.id,
        author_user_id=user.id,
        author_name=user.name,
        body=payload.body,
        internal=internal,
        channel=channel,
    )
    db.add(message)
    await db.flush()
    await _mirror_file_message_to_rep_inbox(db, dealer=dealer, user=user, message=message)
    await db.commit()
    await db.refresh(message)
    return message


@router.patch("/dealers/{dealer_id}/messages/{message_id}", response_model=MessageRead)
async def edit_message(
    dealer_id: UUID,
    message_id: UUID,
    payload: MessageEdit,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerMessage:
    """Edit your own note.

    Notes only, and only your own. A note is an annotation the desk maintains
    ("owner travels until the 14th") and it goes stale, so editing it is the
    point. A message in a conversation is a record of something that was said
    to someone, and quietly rewriting one after the fact would make the whole
    thread untrustworthy, so those are immutable."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    msg = (
        await db.execute(
            select(DealerMessage).where(
                DealerMessage.id == message_id, DealerMessage.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if msg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found on this file")
    if msg.channel != "note":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only notes can be edited. A message someone has already read stays as it was sent.",
        )
    if msg.author_user_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the person who wrote a note can edit it."
        )
    msg.body = payload.body
    msg.edited_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(msg)
    return msg


@router.get("/dealers/{dealer_id}/ai/thread", response_model=list[AIThreadMessage])
async def list_ai_thread(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerAIMessage]:
    """Your own AI thread on this file, oldest first.

    Filtered by user_id as well as dealer_id, and there is no parameter that
    widens it. A rep working out why coverage came out at 1.02 should not
    appear in the underwriter's view, and the reverse matters just as much."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return list(
        (
            await db.execute(
                select(DealerAIMessage)
                .where(
                    DealerAIMessage.dealer_id == dealer.id,
                    DealerAIMessage.user_id == user.id,
                )
                .order_by(DealerAIMessage.created_at.asc())
            )
        ).scalars().all()
    )


@router.post(
    "/dealers/{dealer_id}/ai/thread",
    response_model=AIThreadMessage,
    status_code=status.HTTP_201_CREATED,
)
async def ask_ai_thread(
    dealer_id: UUID,
    payload: AIThreadAsk,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerAIMessage:
    """Ask a question about this file and get an answer from its actual numbers.

    The question is persisted before the model runs, so a timeout leaves the
    thread showing what was asked rather than losing it. The answer is a second
    row; a failed call leaves the question standing and the caller can retry."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)

    history = list(
        (
            await db.execute(
                select(DealerAIMessage)
                .where(
                    DealerAIMessage.dealer_id == dealer.id,
                    DealerAIMessage.user_id == user.id,
                )
                .order_by(DealerAIMessage.created_at.asc())
            )
        ).scalars().all()
    )
    db.add(
        DealerAIMessage(
            dealer_id=dealer.id, user_id=user.id, role="user", body=payload.question
        )
    )
    await db.commit()

    bundle = (await _build_lender_package(db, dealer)).model_dump(mode="json")
    try:
        text = await file_chat.answer(db, dealer, bundle, history, payload.question)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"The analyst returned nothing usable: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("dealer-os: file chat failed for dealer %s", dealer.id)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "The analyst could not be reached. Try again."
        ) from exc

    reply = DealerAIMessage(
        dealer_id=dealer.id, user_id=user.id, role="assistant", body=text
    )
    db.add(reply)
    await db.commit()
    await db.refresh(reply)
    return reply


@router.get("/dealers/{dealer_id}/sessions", response_model=list[SessionRead])
async def list_sessions(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerSession]:
    require_team_or_dealer_or_rep(user)
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
    "/dealers/{dealer_id}/sessions",
    response_model=SessionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    dealer_id: UUID,
    payload: SessionCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerSession:
    """Book a meeting on this file.

    This replaces a team-only handler that sat on the identical path. Two
    routes with the same method and path is not an error in FastAPI: the first
    registered wins and the second is silently unreachable, which is how a
    rep-enabled version shipped and 403'd every rep anyway. If you add a route
    here, grep the path first and remember the decorator often spans two lines.

    Open to the owning rep, because the rep is who arranges the follow-up
    visit. The client sees these read-only with a Join button, so a join_url
    that is wrong is worse than one that is absent."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    session = DealerSession(
        dealer_id=dealer.id,
        title=payload.title.strip(),
        kind=payload.kind,
        starts_at=payload.starts_at,
        join_url=(payload.join_url or None),
        notes=payload.notes,
        created_by_user_id=user.id,
    )
    db.add(session)
    await db.flush()
    await log_action(
        db, dealer.id, user, "session.create", "session", entity_id=session.id,
        after={"title": session.title, "kind": session.kind,
               "starts_at": session.starts_at.isoformat()},
    )
    await db.commit()
    await db.refresh(session)
    return session


@router.get("/unread-summary", response_model=UnreadSummary)
async def unread_summary(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> UnreadSummary:
    """Unread across every file this viewer can see, for the nav badge.

    One query with a group-by rather than the per-file endpoint in a loop,
    because a rep with forty files would otherwise fire forty requests to draw
    one number.

    Scoping reuses the same rule as the file list: a rep sees their own book, a
    dealer their own business, the team everything. It has to, or the badge
    would count messages on files the viewer cannot open."""
    require_team_or_dealer_or_rep(user)

    visible = select(DealerBusiness.id).where(DealerBusiness.is_training.is_(False))
    if user.role == Role.FIELD_REP:
        visible = visible.where(DealerBusiness.owner_user_id == user.id)
    elif user.role == Role.DEALER:
        visible = visible.where(DealerBusiness.dealer_user_id == user.id)

    seen_at = select(
        DealerMessageSeen.dealer_id.label("dealer_id"),
        DealerMessageSeen.seen_at.label("seen_at"),
    ).where(DealerMessageSeen.user_id == user.id).subquery()

    q = (
        select(DealerMessage.dealer_id, func.count().label("n"))
        .outerjoin(seen_at, seen_at.c.dealer_id == DealerMessage.dealer_id)
        .where(
            DealerMessage.dealer_id.in_(visible),
            DealerMessage.author_user_id != user.id,
            or_(seen_at.c.seen_at.is_(None), DealerMessage.created_at > seen_at.c.seen_at),
        )
        .group_by(DealerMessage.dealer_id)
    )
    if user.role == Role.DEALER:
        q = q.where(DealerMessage.internal.is_(False))

    rows = (await db.execute(q)).all()
    per_file = {str(dealer_id): int(n) for dealer_id, n in rows}
    return UnreadSummary(total=sum(per_file.values()), per_file=per_file)


@router.delete("/dealers/{dealer_id}/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    dealer_id: UUID, session_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> None:
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    session = (
        await db.execute(
            select(DealerSession).where(
                DealerSession.id == session_id, DealerSession.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found for this client")
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
            .where(
                DealerAlert.resolved_at.is_(None),
                DealerBusiness.is_training.is_(False),
            )
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
    require_team_or_dealer_or_rep(user)
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
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
    year: int,
    filing: DealerTaxFiling | None,
    observed: float | None,
    document_filename: str | None = None,
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
        document_id=filing.document_id if filing is not None else None,
        document_filename=document_filename,
    )


async def _document_filenames(db: AsyncSession, doc_ids: list[UUID]) -> dict[UUID, str]:
    """Batch id -> filename lookup (0119 provenance joins — never N+1)."""
    if not doc_ids:
        return {}
    return dict(
        (
            await db.execute(
                select(DealerDocument.id, DealerDocument.filename).where(
                    DealerDocument.id.in_(doc_ids)
                )
            )
        ).all()
    )


@router.get("/dealers/{dealer_id}/tax", response_model=list[TaxYearRead])
async def list_tax_years(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[TaxYearRead]:
    """Filed years joined with observed deposits per calendar year (sum of the
    dealer's period deposits). Years that have observed deposits but no filing
    row still appear (filed=false, filing_id=null) so gaps are visible."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    filings = (
        await db.execute(
            select(DealerTaxFiling)
            .where(DealerTaxFiling.dealer_id == dealer.id)
            .order_by(DealerTaxFiling.year.asc())
        )
    ).scalars().all()
    observed = await _deposits_by_year(db, dealer.id)
    names = await _document_filenames(
        db, [f.document_id for f in filings if f.document_id is not None]
    )
    by_year = {f.year: f for f in filings}
    years = sorted(set(by_year) | set(observed))
    return [
        _tax_year_read(
            y,
            by_year.get(y),
            observed.get(y),
            document_filename=(
                names.get(by_year[y].document_id) if y in by_year else None
            ),
        )
        for y in years
    ]


@router.put("/dealers/{dealer_id}/tax/{year}", response_model=TaxYearRead)
async def upsert_tax_filing(
    dealer_id: UUID,
    year: int,
    payload: TaxFilingUpsert,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TaxYearRead:
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
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
    names = await _document_filenames(
        db, [filing.document_id] if filing.document_id is not None else []
    )
    return _tax_year_read(
        year, filing, observed, document_filename=names.get(filing.document_id)
    )


@router.get("/dealers/{dealer_id}/lender-package", response_model=LenderPackageRead)
async def lender_package(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> LenderPackageRead:
    """One-call JSON bundle powering the print-ready lender report. Sections
    that need a snapshot (forecast, paths) come back None — never 400 — so a
    brand-new dealer still renders a partial package."""
    require_team_or_dealer_or_rep(user)
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
            paths=compute_paths(metrics, targets_map, settings=await _global_program_settings(db)),
            ladder=compute_ladder(metrics, targets_map),
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
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
    require_team_or_dealer_or_rep(user)
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
    account = (
        await db.execute(
            select(DealerAccount).where(
                DealerAccount.id == account_id, DealerAccount.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found for this client")
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
    rule = (
        await db.execute(
            select(DealerCategoryRule).where(
                DealerCategoryRule.id == rule_id, DealerCategoryRule.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if rule is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Rule not found for this client (global rules cannot be deactivated here)"
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
    """The case's own history. Open to the owning rep, because the workflow
    puts an Audit trail tab on every case and a rep asked about a date needs
    to answer it without calling the desk.

    Scoped, not just guarded: this handler used load_dealer, so admitting a
    role without also scoping would have exposed every client's history."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
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
    require_team_or_dealer_or_rep(user)
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
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    event = (
        await db.execute(
            select(DealerCashEvent).where(
                DealerCashEvent.id == event_id, DealerCashEvent.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cash event not found for this client")
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
    require_team_or_dealer_or_rep(user)
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
    addback = (
        await db.execute(
            select(DealerAddback).where(
                DealerAddback.id == addback_id, DealerAddback.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if addback is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Add-back not found for this client")
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
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence document not found for this client")
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
    intake_id = await handoff_service.start_handoff(db, dealer, user, request)
    await db.commit()
    return HandoffRead(intake_id=intake_id, url=handoff_service.handoff_url(intake_id))


@router.get("/dealers/{dealer_id}/handoff", response_model=HandoffRead)
async def get_dealer_handoff(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> HandoffRead:
    """The dealer's existing funding file. intake_id is null when none was
    started (or the intake it pointed at has since been deleted) — a normal
    state, returned as 200 so browsers don't log console errors for it."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    intake_id = await handoff_service.find_existing_handoff(db, dealer)
    if intake_id is None:
        return HandoffRead(intake_id=None, url=None)
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document request not found for this client")
    return req


@router.get("/dealers/{dealer_id}/doc-requests", response_model=list[DocRequestRead])
async def list_doc_requests(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerDocRequest]:
    """All document requests for the dealer, open ones first (then newest).
    Dealer-visible by design — this IS the dealer's to-do list."""
    require_team_or_dealer_or_rep(user)
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
    dealer_id: UUID, payload: DocRequestCreate, request: Request, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerDocRequest:
    """Ask the client for a document, and tell them you have.

    Open to the owning rep as well as the desk: a rep standing in the business
    is exactly who knows which statement is missing."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    await _require_training_live_action(
        db,
        dealer=dealer,
        user=user,
        request=request,
        action="Request supporting document",
        provider="SES / SMS / secure room",
        recipient=dealer.email or dealer.phone,
        effect=f"Create and send a live request for {payload.title}.",
    )
    if payload.account_id is not None:
        account = (
            await db.execute(
                select(DealerAccount).where(
                    DealerAccount.id == payload.account_id, DealerAccount.dealer_id == dealer.id
                )
            )
        ).scalar_one_or_none()
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found for this client")
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
    # A request nobody is told about is a request that does not get answered.
    # This used to write a row and stop, which meant the item sat in the desk's
    # view waiting on a client who had never heard of it. Best-effort: a mail
    # failure must not lose the request itself, and the desk can resend.
    try:
        await client_room.request_document(
            db, dealer, name=req.title, description=req.note, category=req.kind,
        )
        room = await client_room.ensure_room(db, dealer)
        await _notify_client_request(
            db, dealer, user,
            purpose=f"send us {req.title}",
            path=room.url,
            channel=payload.notify,
            action="client_request.document",
        )
    except Exception:
        logger.exception("dealer-os: could not notify client of doc request %s", req.id)
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
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
    require_team_or_dealer_or_rep(user)
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
            "No metric snapshot exists for this client yet — import financials or "
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
    dealer = await _load_visible_dealer(db, dealer_id, user)
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


async def _store_tax_detail(
    db: AsyncSession,
    dealer_id: UUID,
    tax_years: list[dict],
    analysis: dict,
    document_id: UUID | None = None,
) -> None:
    """Persist a business return's key_facts onto its dos_tax_filings row.

    Fill-only-null on the identity columns, same precedence law as everywhere
    else; `detail` is refreshed because it is the AI's own reading of the
    document, never a human's entry. document_id (0119) is stamped where the
    source document is known and the column is still NULL."""
    facts = analysis.get("key_facts") if isinstance(analysis, dict) else None
    if not isinstance(facts, dict):
        return
    for item in tax_years:
        row = (
            await db.execute(
                select(DealerTaxFiling).where(
                    DealerTaxFiling.dealer_id == dealer_id,
                    DealerTaxFiling.year == item["year"],
                )
            )
        ).scalar_one_or_none()
        if row is None:
            continue
        row.detail = facts
        if row.entity_name is None:
            row.entity_name = str(facts.get("entity_name") or "")[:180] or None
        if row.form_type is None:
            row.form_type = str(facts.get("form_type") or facts.get("form") or "")[:32] or None
        if document_id is not None and row.document_id is None:
            row.document_id = document_id
    await db.flush()


# --- Vendor report & debt schedule (0116) ------------------------------------


async def _load_vendor_inputs(
    db: AsyncSession, dealer: DealerBusiness
) -> tuple[list[DealerCashEvent], dict[str, str], list[str]]:
    """Shared loader: the dealer's full event ledger + admin category rules +
    self names — one load feeding the rollup, the drill and the tradelines."""
    rows = (
        (
            await db.execute(
                select(DealerCashEvent).where(DealerCashEvent.dealer_id == dealer.id)
            )
        )
        .scalars()
        .all()
    )
    overrides = {
        r.pattern: r.category
        for r in (
            (
                await db.execute(
                    select(DealerCategoryRule).where(
                        or_(
                            DealerCategoryRule.dealer_id == dealer.id,
                            DealerCategoryRule.dealer_id.is_(None),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    self_names = [n for n in (dealer.name, dealer.legal_name) if n]
    return rows, overrides, self_names


def _rollup_from_inputs(
    rows: list[DealerCashEvent], overrides: dict[str, str], self_names: list[str]
):
    events = [
        vendors.VendorEvent(r.occurred_on, r.description or "", float(r.amount or 0))
        for r in rows
    ]
    return vendors.rollup_vendors(events, overrides=overrides, self_names=self_names), len(events)


async def _vendor_rollup(db: AsyncSession, dealer: DealerBusiness):
    """Shared: load the dealer's events + admin category rules and roll up."""
    rows, overrides, self_names = await _load_vendor_inputs(db, dealer)
    return _rollup_from_inputs(rows, overrides, self_names)


async def _account_names(db: AsyncSession, dealer_id: UUID, account_ids: list[UUID]) -> dict[UUID, str]:
    if not account_ids:
        return {}
    return dict(
        (
            await db.execute(
                select(DealerAccount.id, DealerAccount.name).where(
                    DealerAccount.dealer_id == dealer_id,
                    DealerAccount.id.in_(account_ids),
                )
            )
        ).all()
    )


@router.get("/dealers/{dealer_id}/vendors", response_model=VendorReportRead)
async def vendor_report(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> VendorReportRead:
    """Per-vendor activity rollup — who the money actually moves with.

    Grouped on vendor identity rather than a cadence band, because the old
    recurrence engine required a regular interval and therefore returned zero
    groups on a real dealer's 414 events."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    rolled, analyzed = await _vendor_rollup(db, dealer)
    return VendorReportRead(
        vendors=[VendorRowRead(**{k: v for k, v in vars(r).items() if not k.startswith("_")}) for r in rolled],
        categories=list(vendors.CATEGORIES),
        recurring_count=sum(1 for r in rolled if r.is_recurring),
        one_off_count=sum(1 for r in rolled if not r.is_recurring),
        events_analyzed=analyzed,
    )


@router.get("/dealers/{dealer_id}/vendors/{vendor_key}/events", response_model=VendorDetailRead)
async def vendor_events(
    dealer_id: UUID,
    vendor_key: str,
    user: CurrentUser,
    direction: str = Query(default="all", pattern="^(all|in|out)$"),
    db: AsyncSession = Depends(get_db),
) -> VendorDetailRead:
    """Vendor drill-down (0119): the ledger lines behind one rollup row, with
    per-account attribution (count desc) and source-document provenance.
    Membership uses the SAME normalize_vendor identity the rollup groups on,
    so the drill can never disagree with the report."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    rows, overrides, self_names = await _load_vendor_inputs(db, dealer)
    rolled, _ = _rollup_from_inputs(rows, overrides, self_names)

    key = vendor_key.strip()
    matching = [r for r in rows if vendors.normalize_vendor(r.description or "") == key]
    if direction == "in":
        matching = [r for r in matching if float(r.amount or 0) > 0]
    elif direction == "out":
        matching = [r for r in matching if float(r.amount or 0) < 0]
    matching.sort(key=lambda r: (r.occurred_on, str(r.id)), reverse=True)

    # The matching rollup row: honor the direction filter; on 'all' the rollup
    # order (abs total desc) picks the dominant side of the relationship.
    wanted = {"in": (1,), "out": (-1,)}.get(direction, (1, -1))
    vendor_row = next((v for v in rolled if v.key == key and v.direction in wanted), None)

    # Dominant-account attribution: count desc, then |total| desc.
    stats: dict[UUID | None, list[float]] = {}
    for r in matching:
        s = stats.setdefault(r.account_id, [0, 0.0])
        s[0] += 1
        s[1] += float(r.amount or 0)
    names = await _account_names(db, dealer.id, [a for a in stats if a is not None])
    accounts = [
        VendorAccountRead(
            account_id=acct_id,
            account_name=names.get(acct_id) if acct_id is not None else None,
            count=int(count),
            total=round(total, 2),
        )
        for acct_id, (count, total) in sorted(
            stats.items(), key=lambda kv: (-kv[1][0], -abs(kv[1][1]))
        )
    ]

    doc_names = await _document_filenames(
        db, list({r.document_id for r in matching if r.document_id is not None})
    )
    return VendorDetailRead(
        vendor=(
            VendorRowRead(**{k: v for k, v in vars(vendor_row).items() if not k.startswith("_")})
            if vendor_row is not None
            else None
        ),
        accounts=accounts,
        events=[_search_row(r, doc_names.get(r.document_id)) for r in matching],
    )


@router.post("/dealers/{dealer_id}/vendors/category", response_model=VendorReportRead)
async def set_vendor_category(
    dealer_id: UUID,
    body: VendorCategoryPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> VendorReportRead:
    """Correct a vendor's category. Stored as a dealer-scoped rule so it
    survives re-classification — the AI never overwrites it."""
    require_team(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    if body.category not in vendors.CATEGORIES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"category must be one of: {', '.join(vendors.CATEGORIES)}",
        )
    existing = (
        await db.execute(
            select(DealerCategoryRule).where(
                DealerCategoryRule.dealer_id == dealer.id,
                DealerCategoryRule.pattern == body.vendor_key,
            )
        )
    ).scalar_one_or_none()
    before = {"category": existing.category} if existing else None
    if existing is None:
        db.add(
            DealerCategoryRule(
                dealer_id=dealer.id, pattern=body.vendor_key, category=body.category
            )
        )
    else:
        existing.category = body.category
    await log_action(
        db, dealer.id, user, "vendor.categorize", "vendor",
        before=before, after={"vendor_key": body.vendor_key, "category": body.category},
    )
    await db.commit()
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    rolled, analyzed = await _vendor_rollup(db, dealer)
    return VendorReportRead(
        vendors=[VendorRowRead(**{k: v for k, v in vars(r).items() if not k.startswith("_")}) for r in rolled],
        categories=list(vendors.CATEGORIES),
        recurring_count=sum(1 for r in rolled if r.is_recurring),
        one_off_count=sum(1 for r in rolled if not r.is_recurring),
        events_analyzed=analyzed,
    )


@router.get("/dealers/{dealer_id}/debts", response_model=list[DebtRead])
async def list_debts(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerDebt]:
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return list(
        (
            await db.execute(
                select(DealerDebt)
                .where(DealerDebt.dealer_id == dealer.id, DealerDebt.status == "active")
                .order_by(DealerDebt.monthly_payment.desc().nullslast())
            )
        )
        .scalars()
        .all()
    )


@router.post("/dealers/{dealer_id}/debts/draft", response_model=DebtDraftResult)
async def draft_debt_schedule(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DebtDraftResult:
    """Draft the debt schedule from observed vendor activity.

    A baseline, not an answer: every row is editable and a row a human has
    touched (origin='admin') is never rewritten. Dismissed rows stay dismissed
    so a re-draft does not resurrect something the admin rejected."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    rolled, _ = await _vendor_rollup(db, dealer)
    proposals = vendors.draft_debt_rows(rolled)

    existing = {
        d.vendor_key: d
        for d in (
            (
                await db.execute(
                    select(DealerDebt).where(
                        DealerDebt.dealer_id == dealer.id, DealerDebt.vendor_key.is_not(None)
                    )
                )
            )
            .scalars()
            .all()
        )
    }

    created = updated = skipped = 0
    for p in proposals:
        evidence = {
            "observed_months": p["observed_months"],
            "observed_count": p["observed_count"],
            "cadence": p["cadence"],
            "amount_stable": p["amount_stable"],
            "rationale": p["rationale"],
            "last_seen": p["last_seen"].isoformat(),
        }
        row = existing.get(p["vendor_key"])
        if row is None:
            db.add(
                DealerDebt(
                    dealer_id=dealer.id,
                    lender=p["lender"],
                    category=p["category"],
                    monthly_payment=p["monthly_payment"],
                    origin="ai_draft",
                    status="active",
                    vendor_key=p["vendor_key"],
                    evidence=evidence,
                    count_in_dscr=(p["category"] != "credit_card"),
                )
            )
            created += 1
        elif row.origin == "admin" or row.status == "dismissed":
            skipped += 1
        else:
            row.monthly_payment = p["monthly_payment"]
            row.category = p["category"]
            row.evidence = evidence
            updated += 1

    await log_action(
        db, dealer.id, user, "debts.draft", "debt",
        after={"created": created, "updated": updated, "skipped_admin": skipped},
    )
    await db.commit()

    rows = list(
        (
            await db.execute(
                select(DealerDebt)
                .where(DealerDebt.dealer_id == dealer.id, DealerDebt.status == "active")
                .order_by(DealerDebt.monthly_payment.desc().nullslast())
            )
        )
        .scalars()
        .all()
    )
    return DebtDraftResult(
        created=created,
        updated=updated,
        skipped_admin=skipped,
        total_monthly=round(sum(refinance_svc.monthly_equivalent(r) for r in rows), 2),
        debts=[DebtRead.model_validate(r) for r in rows],
    )


@router.post("/dealers/{dealer_id}/debts", response_model=DebtRead, status_code=status.HTTP_201_CREATED)
async def create_debt(
    dealer_id: UUID, body: DebtCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerDebt:
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    fields = body.model_dump()
    if fields.get("monthly_payment") is None and fields.get("payment_amount") and fields.get("payment_frequency"):
        fields["monthly_payment"] = round(
            float(fields["payment_amount"])
            * refinance_svc.FREQUENCY_MONTHLY_MULT[fields["payment_frequency"]],
            2,
        )
    row = DealerDebt(dealer_id=dealer.id, origin="admin", status="active", **fields)
    db.add(row)
    await log_action(db, dealer.id, user, "debts.create", "debt", after=body.model_dump(mode="json"))
    await db.commit()
    await db.refresh(row)
    return row


@router.patch("/dealers/{dealer_id}/debts/{debt_id}", response_model=DebtRead)
async def patch_debt(
    dealer_id: UUID,
    debt_id: UUID,
    body: DebtPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerDebt:
    """Edit a debt row. Any human edit promotes origin to 'admin', which is
    what stops a later re-draft from overwriting the correction."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    row = (
        await db.execute(
            select(DealerDebt).where(DealerDebt.id == debt_id, DealerDebt.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Debt row not found for this client")
    patch = body.model_dump(exclude_unset=True)
    before = {k: getattr(row, k) for k in patch}
    for k, v in patch.items():
        setattr(row, k, v)
    if row.monthly_payment is None and row.payment_amount and row.payment_frequency:
        # keep the engine-facing monthly figure in step with the cadence
        row.monthly_payment = round(
            float(row.payment_amount)
            * refinance_svc.FREQUENCY_MONTHLY_MULT.get(row.payment_frequency, 1.0),
            2,
        )
    row.origin = "admin"
    await log_action(
        db, dealer.id, user, "debts.update", "debt",
        entity_id=row.id, before=jsonable_encoder(before), after=jsonable_encoder(patch),
    )
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/dealers/{dealer_id}/debts/{debt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_debt(
    dealer_id: UUID, debt_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> None:
    """Delete a hand-added row; a DRAFTED row is dismissed instead so the next
    draft does not resurrect it."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    row = (
        await db.execute(
            select(DealerDebt).where(DealerDebt.id == debt_id, DealerDebt.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Debt row not found for this client")
    if row.vendor_key:
        row.status = "dismissed"
    else:
        await db.delete(row)
    await log_action(db, dealer.id, user, "debts.delete", "debt", entity_id=debt_id)
    await db.commit()


# --- MCA-style statement-only readiness -----------------------------------------


@router.get("/dealers/{dealer_id}/mca-readiness", response_model=McaReadinessRead)
async def mca_readiness_read(
    dealer_id: UUID,
    user: CurrentUser,
    advance_pct: float | None = Query(default=None, gt=0.05, le=1.5),
    factor_rate: float | None = Query(default=None, gt=1.0, le=2.0),
    term_months: int | None = Query(default=None, ge=1, le=24),
    db: AsyncSession = Depends(get_db),
) -> McaReadinessRead:
    """The MCA-underwriter lens over the trailing statements: scrubbed AMR,
    the four health checks, and a backed-into offer with the daily-pull
    stress test. Desk-facing (we structure these as term loans)."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    inputs = await load_metric_inputs(db, dealer.id)
    metrics = compute_metrics(
        inputs.periods, inputs.addbacks_annual_verified, inputs.targets, fallbacks=inputs.fallbacks
    )
    rolled, _n = await _vendor_rollup(db, dealer)
    result = mca_svc.compute_mca_readiness(
        inputs.periods, rolled, (metrics.get("adb") or {}).get("current"),
        advance_pct=advance_pct, factor_rate=factor_rate,
        term_days=term_months * 21 if term_months else None,
    )
    return McaReadinessRead(**result)


# --- DSCR composition (0129) — the clickable DSCR container --------------------


def _addback_annualized(a) -> float:
    if a.status != "verified":
        return 0.0
    if a.annual_amount is not None:
        return float(a.annual_amount)
    if a.monthly_amount is not None:
        return round(float(a.monthly_amount) * 12.0, 2)
    return 0.0


@router.get("/dealers/{dealer_id}/dscr/composition", response_model=DscrCompositionRead)
async def dscr_composition(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DscrCompositionRead:
    """Everything the DSCR is built from, itemized: the numerator build-up
    (EBITDA source -> add-backs -> bankable), every denominator component
    (a debt-schedule row, observed-vs-stated monthly, include flag), and
    observed debt-like vendors not yet on the schedule as suggestions."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)

    inputs = await load_metric_inputs(db, dealer.id)
    metrics = compute_metrics(
        inputs.periods, inputs.addbacks_annual_verified, inputs.targets, fallbacks=inputs.fallbacks
    )
    dscr_m = metrics.get("dscr") or {}
    ebitda_m = metrics.get("ebitda") or {}

    debts = (
        (
            await db.execute(
                select(DealerDebt)
                .where(DealerDebt.dealer_id == dealer.id, DealerDebt.status == "active")
                .order_by(DealerDebt.monthly_payment.desc().nullslast())
            )
        )
        .scalars()
        .all()
    )
    events, _ov, _sn = await _load_vendor_inputs(db, dealer)
    observed = refinance_svc.observed_monthly(events, debts)
    months_covered = {p["period"].replace(day=1) for p in inputs.periods if p.get("period")}
    n_months = max(len(months_covered), 1)

    components: list[DscrComponentRead] = []
    for d in debts:
        by_m = observed.get(d.id) or {}
        obs = round(sum(by_m.get(m, 0.0) for m in months_covered) / n_months, 2) if by_m else None
        # Stated = the row's FULL monthly equivalent (payment_amount x cadence
        # counts too) — the same basis the engine's drafted pool uses, so the
        # composer's itemization always sums to the headline denominator.
        _stated_eq = refinance_svc.monthly_equivalent(d)
        stated = round(_stated_eq, 2) if _stated_eq > 0 else None
        effective = obs if (obs is not None and obs > 0) else (stated or 0.0)
        components.append(
            DscrComponentRead(
                debt_id=d.id,
                lender=d.lender,
                category=d.category,
                origin=d.origin,
                source="contract" if d.document_id else ("drafted" if d.origin == "ai_draft" else "manual"),
                stated_monthly=stated,
                observed_monthly=obs,
                effective_monthly=round(effective, 2),
                count_in_dscr=bool(d.count_in_dscr),
                vendor_key=d.vendor_key,
                document_id=d.document_id,
            )
        )

    # Observed debt-like vendors (non-card, recurring) with no schedule row —
    # including DISMISSED rows (a human killed them; never resurface).
    rolled, _n = _rollup_from_inputs(events, _ov, _sn)
    all_row_keys = (
        (
            await db.execute(
                select(DealerDebt.vendor_key).where(
                    DealerDebt.dealer_id == dealer.id, DealerDebt.vendor_key.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    covered_keys = list(all_row_keys)
    suggestions = [
        DscrSuggestionRead(
            vendor_key=v.key,
            label=(v.sample_description or v.key)[:80],
            monthly_avg=round(abs(v.monthly_average), 2),
            months=v.months,
            count=v.count,
            category=v.category,
        )
        for v in rolled
        if v.debt_like
        and v.category != "credit_card"
        and v.is_recurring
        and not any(refinance_svc.key_matches(k, v.key) for k in covered_keys)
    ]

    net_series = [
        DscrNetPoint(
            month=p["period"].strftime("%Y-%m"),
            net=(
                round(float(p["deposits"]) - float(p["withdrawals"]), 2)
                if p.get("deposits") is not None and p.get("withdrawals") is not None
                else None
            ),
        )
        for p in sorted(inputs.periods, key=lambda x: x.get("period"))
        if p.get("period")
    ]

    # Rule-based improvement suggestions with concrete impact numbers.
    improvements: list[DscrImprovementRead] = []
    bankable = ebitda_m.get("bankable")
    cur_ds = dscr_m.get("monthly_debt_service") or 0
    def _dscr_at(ds):
        return round(bankable / (ds * 12), 2) if bankable and ds and ds > 0 else None
    cur = _dscr_at(cur_ds)
    for comp_row in components:
        if comp_row.count_in_dscr and comp_row.effective_monthly > 0 and cur_ds > comp_row.effective_monthly:
            after = _dscr_at(cur_ds - comp_row.effective_monthly)
            if after and cur and after > cur:
                improvements.append(DscrImprovementRead(
                    title=f"Review whether {comp_row.lender[:40]} is really debt service",
                    detail=f"{comp_row.category} · ${comp_row.effective_monthly:,.0f}/mo counted — tax or trade payments are operating obligations, not debt.",
                    impact=f"DSCR {cur}x -> {after}x if excluded",
                ))
    pending = [a for a in inputs.addback_rows if a.status in ("candidate", "review")]
    if pending and bankable:
        gain = sum(_addback_annualized(type("x", (), {"status": "verified", "annual_amount": a.annual_amount, "monthly_amount": a.monthly_amount})()) for a in pending)
        if gain > 0 and cur_ds > 0:
            improvements.append(DscrImprovementRead(
                title=f"Verify {len(pending)} pending add-back(s)",
                detail=f"${gain:,.0f}/yr of candidate add-backs are not counted until verified.",
                impact=f"DSCR {cur}x -> {round((bankable + gain * 0.96) / (cur_ds * 12), 2)}x",
            ))
    if ebitda_m.get("source") == "tax_return":
        improvements.append(DscrImprovementRead(
            title="Upload P&L months",
            detail="EBITDA currently comes from the last tax return — observed P&L months are what banks underwrite and usually run higher.",
        ))
    if dealer.funding_goal is None:
        improvements.append(DscrImprovementRead(
            title="Set a funding goal",
            detail="A goal unlocks the DSCR-at-goal view and goal-aligned targets.",
        ))

    return DscrCompositionRead(
        improvements=improvements[:6],
        numerator=DscrNumeratorRead(
            ebitda_source=ebitda_m.get("source"),
            reported_ttm=ebitda_m.get("reported_ttm"),
            addbacks=[
                DscrAddbackRead(
                    id=a.id, title=a.title, status=a.status,
                    monthly_amount=a.monthly_amount, annual_amount=a.annual_amount,
                    annualized=_addback_annualized(a),
                    document_id=a.document_id, source_event_id=a.source_event_id,
                )
                for a in inputs.addback_rows
                if a.status != "excluded"
            ],
            adjusted=ebitda_m.get("adjusted"),
            bankable=ebitda_m.get("bankable"),
        ),
        components=components,
        suggestions=suggestions,
        results=DscrResultsRead(
            dscr_current=dscr_m.get("current"),
            dscr_draft=dscr_m.get("draft"),
            display=dscr_m.get("display"),
            at_goal=dscr_m.get("at_goal"),
            cash_flow=dscr_m.get("cash_flow"),
            net_cash_flow_monthly=dscr_m.get("net_cash_flow_monthly"),
            monthly_debt_service=dscr_m.get("monthly_debt_service"),
            ds_source=dscr_m.get("source"),
            funding_goal=float(dealer.funding_goal) if dealer.funding_goal is not None else None,
            goal_monthly_payment=(inputs.fallbacks or {}).get("goal_monthly_payment"),
        ),
        net_series=net_series,
    )


@router.post("/dealers/{dealer_id}/dscr/components", response_model=DscrComponentRead)
async def dscr_component_action(
    dealer_id: UUID,
    payload: DscrComponentAction,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DscrComponentRead:
    """Add/remove DSCR denominator components — always through the debt
    schedule (the single ledger). toggle flips count_in_dscr on a row;
    add_vendor materializes an observed lender into a schedule row."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)

    if payload.action == "toggle":
        if payload.debt_id is None or payload.count_in_dscr is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "toggle needs debt_id and count_in_dscr")
        row = (
            await db.execute(
                select(DealerDebt).where(DealerDebt.id == payload.debt_id, DealerDebt.dealer_id == dealer.id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Debt row not found for this client")
        before = {"count_in_dscr": row.count_in_dscr}
        row.count_in_dscr = payload.count_in_dscr
        row.origin = "admin"  # a human decided — re-drafts never override
        await log_action(
            db, dealer.id, user, "debts.dscr_toggle", "debt", entity_id=row.id,
            before=before, after={"count_in_dscr": payload.count_in_dscr},
        )
    elif payload.action == "add_vendor":
        if not payload.vendor_key:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "add_vendor needs vendor_key")
        events, _ov, _sn = await _load_vendor_inputs(db, dealer)
        rolled = vendors.rollup_vendors(events)
        v = next((x for x in rolled if x.key == payload.vendor_key), None)
        if v is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No observed vendor with that key")
        if v.direction >= 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Inflow vendors can't be debt service")
        existing = (
            await db.execute(
                select(DealerDebt).where(
                    DealerDebt.dealer_id == dealer.id, DealerDebt.vendor_key == v.key
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.status, existing.count_in_dscr, existing.origin = "active", True, "admin"
            row = existing
        else:
            row = DealerDebt(
                dealer_id=dealer.id,
                lender=(v.sample_description or v.key)[:180],
                category=v.category if v.category in ("floorplan", "loan", "credit_card") else "loan",
                monthly_payment=round(abs(v.monthly_average), 2),
                origin="admin",
                status="active",
                count_in_dscr=True,
                vendor_key=v.key,
                evidence={"source": "dscr_composer", "observed_months": v.months, "observed_count": v.count},
            )
            db.add(row)
        await db.flush()
        await log_action(
            db, dealer.id, user, "debts.create", "debt", entity_id=row.id,
            after={"lender": row.lender, "via": "dscr_composer"},
        )
    else:  # unreachable given the schema pattern
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown action")

    try:
        await recompute_snapshot(db, dealer.id)
    except Exception:
        logger.exception("dealer-os: snapshot recompute failed after dscr component action")
    await db.commit()
    await db.refresh(row)
    _row_eq = refinance_svc.monthly_equivalent(row)
    return DscrComponentRead(
        debt_id=row.id, lender=row.lender, category=row.category, origin=row.origin,
        source="contract" if row.document_id else ("drafted" if row.origin == "ai_draft" else "manual"),
        stated_monthly=round(_row_eq, 2) if _row_eq > 0 else None,
        observed_monthly=None,
        effective_monthly=round(_row_eq, 2),
        count_in_dscr=bool(row.count_in_dscr),
        vendor_key=row.vendor_key, document_id=row.document_id,
    )


# --- Plaid bank connections (0127, statements only) ----------------------------

# Per-dealer cooldowns on the Plaid surfaces the CLIENT can reach — these hit
# Plaid's API (metered) and, for refresh, spawn extraction work. In-memory is
# fine on the single-instance deploy (same assumption the scheduler documents).
_PLAID_LAST_CALL: dict[tuple[str, str], float] = {}


_NO_BANK_CONSENT = (
    "Bank connection consent is required before an account can be linked."
)


def _plaid_cooldown(action: str, dealer_id: UUID, seconds: float) -> None:
    import time

    key = (action, str(dealer_id))
    now = time.monotonic()
    last = _PLAID_LAST_CALL.get(key)
    if last is not None and now - last < seconds:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Please wait a moment before trying again ({int(seconds - (now - last)) + 1}s)",
        )
    _PLAID_LAST_CALL[key] = now
    if len(_PLAID_LAST_CALL) > 5000:  # bound the map
        _PLAID_LAST_CALL.clear()


async def _plaid_items(db: AsyncSession, dealer_id: UUID) -> list[DealerPlaidItem]:
    return (
        (
            await db.execute(
                select(DealerPlaidItem)
                .where(
                    DealerPlaidItem.dealer_id == dealer_id,
                    DealerPlaidItem.status != "removed",
                    DealerPlaidItem.environment == plaid_client.environment(),
                )
                .order_by(DealerPlaidItem.created_at)
            )
        )
        .scalars()
        .all()
    )


async def _dealer_plaid_item(
    db: AsyncSession, dealer_id: UUID, item_pk: UUID
) -> DealerPlaidItem:
    item = (
        await db.execute(
            select(DealerPlaidItem).where(
                DealerPlaidItem.id == item_pk,
                DealerPlaidItem.dealer_id == dealer_id,
                DealerPlaidItem.status != "removed",
                DealerPlaidItem.environment == plaid_client.environment(),
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bank connection not found")
    return item


@router.get("/dealers/{dealer_id}/plaid", response_model=PlaidStateRead)
async def plaid_state(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> PlaidStateRead:
    """Business-bank state for the client, team, or owning rep.

    This read contains status only. Consent and credential entry are separate
    client-owned mutations below.
    """
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    items = await _plaid_items(db, dealer.id)
    months = await _plaid_statement_months_by_item(db, dealer.id)
    cs = await bank_consent.state(db, dealer.id)
    current_consent = await bank_consent.has_consent(db, dealer.id)
    d = bank_consent.disclosure()
    return PlaidStateRead(
        enabled=plaid_client.enabled(),
        environment=plaid_client.environment(),
        consent=BankConsentState(
            granted=current_consent,
            version=cs.version,
            at=cs.at,
            consenter_name=cs.consenter_name,
            disclosure_version=d["version"],
            disclosure_text=d["text"],
        ),
        items=[
            PlaidItemRead(
                **PlaidItemRead.model_validate(item).model_dump(exclude={"statement_months"}),
                statement_months=months.get(item.id, []),
            )
            for item in items
        ],
        assets_enabled=plaid_client.assets_enabled(),
        asset_reports=[
            PlaidAssetReportRead.model_validate(report)
            for report in await plaid_lifecycle.owner_asset_reports(db, dealer_id=dealer.id)
        ],
    )


@router.post("/dealers/{dealer_id}/plaid/link-token", response_model=PlaidLinkTokenRead)
async def plaid_link_token(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> PlaidLinkTokenRead:
    """Start Plaid Link from the owning client's authenticated account."""
    require_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    # The gate sits HERE and not on exchange: consent has to precede credential
    # entry, and by exchange the user has already typed their bank password.
    if not await bank_consent.has_consent(db, dealer.id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _NO_BANK_CONSENT)
    _plaid_cooldown("link", dealer.id, 10)
    try:
        token = await plaid_client.create_link_token(
            dealer_id=str(dealer.id), dealer_name=dealer.legal_name or dealer.name
        )
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return PlaidLinkTokenRead(link_token=token)


@router.post(
    "/dealers/{dealer_id}/plaid/{item_pk}/update-link-token",
    response_model=PlaidLinkTokenRead,
)
async def plaid_update_link_token(
    dealer_id: UUID,
    item_pk: UUID,
    payload: PlaidUpdateLinkRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlaidLinkTokenRead:
    require_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    if not await bank_consent.has_consent(db, dealer.id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _NO_BANK_CONSENT)
    item = await _dealer_plaid_item(db, dealer.id, item_pk)
    try:
        token = await plaid_client.create_update_link_token(
            access_token=plaid_lifecycle.decrypted_access_token(item),
            client_user_id=str(dealer.id),
            display_name=dealer.legal_name or dealer.name,
            account_selection_enabled=(
                payload.account_selection_enabled
                or item.update_mode_account_selection
            ),
        )
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return PlaidLinkTokenRead(link_token=token)


@router.post(
    "/dealers/{dealer_id}/plaid/{item_pk}/update-complete",
    response_model=PlaidItemRead,
)
async def plaid_update_complete(
    dealer_id: UUID,
    item_pk: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerPlaidItem:
    require_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    item = await _dealer_plaid_item(db, dealer.id, item_pk)
    try:
        await plaid_lifecycle.complete_update(item)
    except plaid_client.PlaidUnavailable as exc:
        await db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await log_action(
        db,
        dealer.id,
        user,
        "plaid.update_mode.completed.client",
        "plaid_item",
        entity_id=item.id,
        after={"via": "authenticated_client"},
    )
    await db.commit()
    await db.refresh(item)
    return item


@router.post("/dealers/{dealer_id}/bank-consent", response_model=BankConsentState)
async def grant_bank_consent(
    dealer_id: UUID,
    payload: BankConsentGrant,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BankConsentState:
    """Record authorisation to connect a bank account.

    IP and user agent are taken from the REQUEST, never from the body — they
    are the part of the proof a client must not be able to author.
    """
    require_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    try:
        await bank_consent.record(
            db,
            dealer_id=dealer.id,
            method="self_web",
            consenter_name=payload.consenter_name,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            captured_by_user_id=user.id,
            captured_by_name=user.name,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await log_action(
        db,
        dealer.id,
        user,
        "plaid.consent.client",
        "dealer",
        entity_id=dealer.id,
        after={"via": "authenticated_client", "consenter_name": payload.consenter_name},
    )
    await db.commit()
    cs = await bank_consent.state(db, dealer.id)
    d = bank_consent.disclosure()
    return BankConsentState(
        granted=cs.granted, version=cs.version, at=cs.at,
        consenter_name=cs.consenter_name,
        disclosure_version=d["version"], disclosure_text=d["text"],
    )


@router.delete("/dealers/{dealer_id}/bank-consent", response_model=BankConsentState)
async def withdraw_bank_consent(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> BankConsentState:
    """Withdraw authorisation. The published privacy policy promises this is
    possible, so it is a real endpoint rather than a support-inbox convention."""
    require_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    try:
        disconnected = await plaid_lifecycle.purge_owner_connections(
            db, dealer_id=dealer.id
        )
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    await bank_consent.revoke(db, dealer_id=dealer.id, reason="withdrawn by request")
    await log_action(
        db,
        dealer.id,
        user,
        "plaid.consent.withdrawn.client",
        "dealer",
        entity_id=dealer.id,
        after={"via": "authenticated_client", "disconnected_items": disconnected},
    )
    await db.commit()
    d = bank_consent.disclosure()
    return BankConsentState(
        granted=False, disclosure_version=d["version"], disclosure_text=d["text"]
    )


@router.post(
    "/dealers/{dealer_id}/plaid/exchange",
    response_model=PlaidItemRead,
    status_code=status.HTTP_201_CREATED,
)
async def plaid_exchange(
    dealer_id: UUID,
    payload: PlaidExchange,
    background: BackgroundTasks,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerPlaidItem:
    """Finish Link: swap the public token, store the encrypted access token,
    and pull the first batch of statements in the background."""
    require_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    await _lock_dealer_related_writes(db, dealer.id)
    _plaid_cooldown("exchange", dealer.id, 5)
    try:
        access_token, item_id = await plaid_client.exchange_public_token(payload.public_token)
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    existing = (
        await db.execute(select(DealerPlaidItem).where(DealerPlaidItem.item_id == item_id))
    ).scalar_one_or_none()
    if existing is not None and existing.dealer_id != dealer.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This bank connection belongs to another file")
    if existing is not None:  # reconnect: refresh the token, revive the row
        existing.encrypted_access_token = plaid_client.encrypt_token(access_token)
        existing.environment = plaid_client.environment()
        existing.status, existing.error = "active", None
        existing.update_mode_reason = None
        existing.update_mode_account_selection = False
        existing.next_refresh_at = datetime.now(timezone.utc)
        item = existing
    else:
        item = DealerPlaidItem(
            dealer_id=dealer.id,
            item_id=item_id,
            institution_name=(payload.institution_name or "")[:160] or None,
            encrypted_access_token=plaid_client.encrypt_token(access_token),
            environment=plaid_client.environment(),
            status="active",
            # Safety net: the in-process background first sync is not durable
            # (a redeploy kills it) — a due next_refresh_at means the daily
            # scheduler sweep picks the item up regardless.
            next_refresh_at=datetime.now(timezone.utc),
        )
        db.add(item)
    await db.flush()
    if payload.is_primary_operating is True or not await _has_primary_operating_bank(db, dealer.id):
        await _make_primary_operating_bank(db, dealer.id, item)
    await log_action(
        db, dealer.id, user, "plaid.connect.client", "plaid_item", entity_id=item.id,
        after={
            "institution": item.institution_name,
            "is_primary_operating": item.is_primary_operating,
            "via": "authenticated_client",
        },
    )
    await db.commit()
    await db.refresh(item)
    background.add_task(_background_plaid_first_sync, item.id)
    return item


@router.post("/public/room/{token}/features", response_model=RoomFeaturesRead)
async def public_room_features(
    token: str, payload: RoomPasscode, db: AsyncSession = Depends(get_db)
) -> RoomFeaturesRead:
    """PUBLIC. What this room can do beyond uploading.

    The room page is generic; the capabilities are per-file. One call after the
    passcode gate tells it whether to draw a bank-connect section and which
    checklist items are signable, so the page never renders a button that will
    404. The signable rows carry their full document text because the signer
    must SEE what they sign; a sign action over hidden text is not a signature.
    """
    try:
        link, dealer = await client_room.resolve_room(db, token, payload.passcode)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    signable = (
        (
            await db.execute(
                select(BucketRequestedDocument).where(
                    BucketRequestedDocument.bucket_id == link.bucket_id,
                    BucketRequestedDocument.requires_signature.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    from app.services import document_signature as sig_service

    rows = []
    for r in signable:
        text = r.signature_document_text or (
            sig_service.credit_authorization_document_text()
            if r.signature_kind == "credit_authorization"
            else ""
        )
        rows.append(
            RoomSignableRead(
                id=r.id,
                name=r.name,
                kind=r.signature_kind,
                signed=r.status == "uploaded",
                document_text=text,
                signable=bool(text),
            )
        )
    # Agreements out for signature (and already-executed ones, so the room can
    # show Signed instead of silently dropping them). The FULL text rides
    # along: what is shown in Agreement mode is extracted from the exact PDF
    # that gets signed, never a summary.
    contract_rows: list[RoomContractRead] = []
    docs = (
        (
            await db.execute(
                select(ContractDocument).where(
                    ContractDocument.dealer_id == dealer.id,
                    ContractDocument.envelope_id.is_(None),
                    ContractDocument.status.in_(["out_for_signature", "executed"]),
                )
            )
        )
        .scalars()
        .all()
    )
    if docs:
        titles = {
            t.key: t.title
            for t in (
                await db.execute(select(ContractTemplate))
            ).scalars().all()
        }
        for d in docs:
            text_full = ""
            if d.status == "out_for_signature" and d.filled_s3_key:
                raw_pdf = storage.get_bytes(d.filled_s3_key)
                if raw_pdf is not None:
                    text_full = contract_sign.agreement_text(raw_pdf)
            executed_url = None
            if d.status == "executed" and d.executed_s3_key:
                from app.services.payment_authorization import presign_private_s3_object

                executed_url = presign_private_s3_object(
                    d.executed_s3_key,
                    ttl_seconds=3600,
                    download_filename=f"{dealer.case_ref or 'QC'}-{d.template_key}.pdf",
                )
            contract_rows.append(
                RoomContractRead(
                    id=d.id,
                    key=d.template_key,
                    title=titles.get(d.template_key, d.template_key),
                    status=d.status,
                    agreement_text=text_full,
                    commission_note=(
                        "3% of the total funded amount"
                        if d.template_key == "consulting_agreement"
                        else None
                    ),
                    download_url=executed_url,
                    pdf_sha256=d.executed_sha256,
                )
            )

    envelope_rows = list(
        (
            await db.execute(
                select(ContractEnvelope)
                .where(
                    ContractEnvelope.dealer_id == dealer.id,
                    ContractEnvelope.status.in_(["out_for_signature", "executed"]),
                )
                .order_by(ContractEnvelope.created_at.asc())
            )
        ).scalars().all()
    )
    if envelope_rows:
        now = datetime.now(timezone.utc)
        for envelope in envelope_rows:
            if envelope.status == "out_for_signature" and envelope.opened_at is None:
                envelope.opened_at = now
        await db.commit()

    return RoomFeaturesRead(
        business_name=dealer.name,
        bank_connect_available=plaid_client.enabled(),
        plaid_environment=plaid_client.environment(),
        bank_consent_granted=await bank_consent.has_consent(db, dealer.id),
        bank_consent_disclosure=bank_consent.disclosure()["text"],
        bank_connections=await _safe_plaid_items(db, dealer.id),
        plaid_assets_enabled=plaid_client.assets_enabled(),
        signable=rows,
        contracts=contract_rows,
        envelopes=[await _contract_envelope_read(db, envelope, public=True) for envelope in envelope_rows],
    )


@router.post(
    "/public/room/{token}/contract-envelopes/{envelope_id}/documents/{envelope_document_id}/acknowledge",
    response_model=ContractEnvelopeRead,
)
async def public_room_acknowledge_envelope_document(
    token: str,
    envelope_id: UUID,
    envelope_document_id: UUID,
    payload: ContractEnvelopeAcknowledgeRequest,
    db: AsyncSession = Depends(get_db),
) -> ContractEnvelopeRead:
    try:
        _link, dealer = await client_room.resolve_room(db, token, payload.passcode)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    envelope = await db.get(ContractEnvelope, envelope_id)
    if envelope is None or envelope.dealer_id != dealer.id or envelope.status != "out_for_signature":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such signing package in this room.")
    row = await db.get(ContractEnvelopeDocument, envelope_document_id)
    if row is None or row.envelope_id != envelope.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such package document.")
    now = datetime.now(timezone.utc)
    row.reviewed_at = row.reviewed_at or now
    row.acknowledged_at = now if payload.acknowledged else None
    envelope.opened_at = envelope.opened_at or now
    await db.commit()
    await db.refresh(envelope)
    return await _contract_envelope_read(db, envelope, public=True)


@router.post(
    "/public/room/{token}/contract-envelopes/{envelope_id}/sign",
    response_model=RoomSignResult,
)
async def public_room_sign_contract_envelope(
    token: str,
    envelope_id: UUID,
    payload: ContractEnvelopeSignRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> RoomSignResult:
    try:
        link, dealer = await client_room.resolve_room(db, token, payload.passcode)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if not payload.esign_consent or not payload.applies_to_all_documents:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "E-SIGN consent and the package-wide signature affirmation are required.",
        )
    # Serialize execution for this envelope so simultaneous retries cannot
    # produce duplicate executed artifacts or certificates.
    envelope = (
        await db.execute(
            select(ContractEnvelope)
            .where(ContractEnvelope.id == envelope_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if envelope is None or envelope.dealer_id != dealer.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such signing package in this room.")
    owner = await db.get(DealerOwner, envelope.recipient_owner_id) if envelope.recipient_owner_id else None
    expected = " ".join((owner.full_name if owner else "").lower().split())
    supplied = " ".join(payload.typed_name.lower().split())
    if not expected or supplied != expected:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The typed name must match the designated authorized representative.",
        )

    from app.services.payment_authorization import decode_signature_data_url, presign_private_s3_object

    sig_bytes, sig_sha, _ctype = decode_signature_data_url(payload.signature_data_url)
    already_completed = envelope.status == "executed"
    try:
        bundle, bundle_sha = await contract_packages.execute_envelope(
            db,
            dealer,
            envelope,
            typed_name=payload.typed_name.strip(),
            signature_png=sig_bytes,
            signature_sha256=sig_sha,
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    if not already_completed:
        await log_action(
            db,
            dealer.id,
            None,
            "contract_envelope.executed",
            "contract_envelope",
            entity_id=envelope.id,
            after={
                "program_key": envelope.program_key,
                "signer": payload.typed_name.strip(),
                "bundle_sha256": bundle_sha[:16],
                "via": "client_room",
            },
        )
    await db.commit()

    delivery_ok = True
    to = owner.email if owner and owner.email else link.recipient_email or dealer.email
    if not already_completed and to and "@" in to:
        from app.services.email.ses_client import send_raw_email

        try:
            sent = send_raw_email(
                to_emails=[to],
                subject=f"Your executed {envelope.title} — Qualified Commercial",
                body_text=(
                    f"Attached is your completed {envelope.title}. The package includes a "
                    "certificate and a separate fingerprint for every signed document. "
                    "Keep this copy for your records.\n\nQualified Commercial"
                ),
                attachments=[(
                    f"{envelope.package_key}-executed-package.pdf",
                    bundle,
                    "application/pdf",
                )],
            )
            delivery_ok = sent.ok
        except Exception:  # noqa: BLE001 - execution remains valid; delivery is retryable
            logger.exception("executed package email failed for envelope %s", envelope.id)
            delivery_ok = False
    download_url = presign_private_s3_object(
        envelope.bundle_s3_key,
        ttl_seconds=3600,
        download_filename=f"{envelope.package_key}-executed-package.pdf",
    )
    return RoomSignResult(
        signed=True,
        message=(
            "The package was already completed. Your signed copy is available below."
            if already_completed
            else "Every acknowledged document was signed. Your completed package is ready."
        ),
        execution_status="executed" if delivery_ok else "delivery_warning",
        pdf_sha256=bundle_sha,
        download_url=download_url,
    )


@router.post(
    "/public/room/{token}/contracts/{doc_id}/sign",
    response_model=RoomSignResult,
    status_code=status.HTTP_201_CREATED,
)
async def public_room_sign_contract(
    token: str,
    doc_id: UUID,
    payload: RoomContractSignRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> RoomSignResult:
    """PUBLIC. Execute one agreement from the client's own device.

    Typed-and-adopted or drawn, the evidence is identical; a typed adoption is
    stamped as a conformed signature (/s/ Name), the legal convention, rather
    than a script font pretending to be handwriting. The executed artifact is
    one PDF — agreement plus certificate page — emailed to the signer at once.

    This endpoint existing ONLY behind the room token is what enforces the
    desk's rule that a signature is never taken on the rep's device."""
    try:
        link, dealer = await client_room.resolve_room(db, token, payload.passcode)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if not payload.esign_consent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "E-SIGN consent is required.")

    doc = (
        await db.execute(
            select(ContractDocument).where(
                ContractDocument.id == doc_id,
                ContractDocument.dealer_id == dealer.id,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such agreement on this file.")

    from app.services.payment_authorization import decode_signature_data_url

    sig_bytes, sig_sha, _ctype = decode_signature_data_url(payload.signature_data_url)

    tpl = (
        await db.execute(
            select(ContractTemplate).where(ContractTemplate.key == doc.template_key)
        )
    ).scalar_one_or_none()
    title = tpl.title if tpl else doc.template_key
    primary_signer = None
    if doc.template_key == qc_master_application.MASTER_TEMPLATE_KEY:
        primary_signer = (
            await db.execute(
                select(DealerOwner)
                .where(DealerOwner.dealer_id == dealer.id)
                .order_by(DealerOwner.is_primary.desc(), DealerOwner.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        expected_name = " ".join((primary_signer.full_name if primary_signer else "").lower().split())
        supplied_name = " ".join(payload.typed_name.lower().split())
        if not expected_name or supplied_name != expected_name:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "The typed name must match the designated authorized representative.",
            )

    try:
        executed_pdf, executed_sha = await contract_sign.execute(
            db, dealer, doc,
            typed_name=payload.typed_name.strip(),
            signature_png=sig_bytes,
            signature_sha256=sig_sha,
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            title=title,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    await log_action(
        db, dealer.id, None, "contract.executed", "contract_document",
        entity_id=doc.id,
        after={"template": doc.template_key, "signer": payload.typed_name.strip(),
               "sha256": executed_sha[:16], "via": "client_room",
               "method": "drawn" if sig_bytes else "typed"},
    )
    await db.commit()

    # The signer's copy, immediately. Retention and delivery are compliance.
    to = (
        primary_signer.email
        if primary_signer and primary_signer.email
        else link.recipient_email or dealer.email
    )
    delivery_ok = True
    if to and "@" in to:
        from app.services.email.ses_client import send_raw_email

        try:
            delivery = send_raw_email(
                to_emails=[to],
                subject=f"Your executed {title} — Qualified Commercial",
                body_text=(
                    f"Attached is your fully executed {title}, signed "
                    f"{doc.signed_at:%B %d, %Y}. The final page is its certificate of "
                    f"completion. Keep this copy for your records.\n\n"
                    f"Qualified Commercial"
                ),
                attachments=[(f"{doc.template_key}-executed.pdf", executed_pdf, "application/pdf")],
            )
            delivery_ok = delivery.ok
        except Exception:  # noqa: BLE001 — the signature is already sealed; mail is retryable
            logger.exception("executed-copy email failed for contract %s", doc.id)
            delivery_ok = False

    from app.services.payment_authorization import presign_private_s3_object

    download_url = presign_private_s3_object(
        doc.executed_s3_key,
        ttl_seconds=3600,
        download_filename=f"{dealer.case_ref or 'QC'}-business-financing-application.pdf",
    )

    return RoomSignResult(
        signed=True,
        certificate_file_id=None,
        message=(
            "Signed. Your executed copy is ready to download and has been emailed to you."
            if delivery_ok
            else "Signed. Your executed copy is ready to download; email delivery needs attention."
        ),
        execution_status="executed" if delivery_ok else "delivery_warning",
        pdf_sha256=executed_sha,
        download_url=download_url,
    )


@router.post(
    "/public/room/{token}/sign",
    response_model=RoomSignResult,
    status_code=status.HTTP_201_CREATED,
)
async def public_room_sign(
    token: str,
    payload: RoomSignRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> RoomSignResult:
    """PUBLIC. Sign one requires_signature item from the client's own room.

    The signing engine is the intake flows' _sign_requested_document, imported
    read-only rather than re-implemented: same hashes, same certificate, same
    audit row, same email copy to the signer. Re-implementing a signature
    pipeline is how two flows drift into producing different evidence for the
    same legal act.

    The engine needs only bucket_id and bucket_upload_link_id from its intake
    argument, so the room's own link satisfies it via a shim. If the engine
    ever grows a deeper dependency on the intake row, the shim fails loudly
    with AttributeError rather than signing with wrong provenance.
    """
    try:
        link, dealer = await client_room.resolve_room(db, token, payload.passcode)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    from types import SimpleNamespace

    from app.routers.dealer_ai_intake import (  # imported read-only
        DealerDocumentSignRequest,
        _sign_requested_document,
    )

    shim = SimpleNamespace(bucket_id=link.bucket_id, bucket_upload_link_id=link.id)
    sign_payload = DealerDocumentSignRequest(
        requested_document_id=payload.requested_document_id,
        typed_name=payload.typed_name,
        esign_consent=payload.esign_consent,
        signature_data_url=payload.signature_data_url,
    )
    result_file = await _sign_requested_document(
        db,
        shim,  # type: ignore[arg-type] — duck-typed on the two fields the engine reads
        sign_payload,
        request,
        actor_name=payload.typed_name,
        actor_email=(link.recipient_email or dealer.email or ""),
    )
    await log_action(
        db, dealer.id, None, "document.signed.client", "doc_request",
        entity_id=payload.requested_document_id,
        after={"signer": payload.typed_name, "via": "client_room", "file_id": str(result_file.id)},
    )
    await db.commit()
    return RoomSignResult(
        signed=True,
        certificate_file_id=result_file.id,
        message="Signed. A copy of the executed document has been emailed to you.",
        execution_status="executed",
    )


@router.post("/public/room/{token}/bank-consent", response_model=BankConsentState)
async def public_room_bank_consent(
    token: str,
    payload: RoomBankConsentGrant,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BankConsentState:
    """PUBLIC. The client authorises the bank connection from their own room.

    The room authenticates the ROOM, not a person, so consenter_name is the only
    human identity in the record — which is why the form asks for it and why it
    is stored verbatim.
    """
    try:
        _link, dealer = await client_room.resolve_room(db, token, payload.passcode)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    try:
        await bank_consent.record(
            db,
            dealer_id=dealer.id,
            method="self_web",
            consenter_name=payload.consenter_name,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await log_action(
        db,
        dealer.id,
        None,
        "plaid.consent.client",
        "dealer",
        entity_id=dealer.id,
        after={
            "via": "client_room",
            "link_id": str(_link.id),
            "consenter_name": payload.consenter_name,
        },
    )
    await db.commit()
    cs = await bank_consent.state(db, dealer.id)
    d = bank_consent.disclosure()
    return BankConsentState(
        granted=cs.granted, version=cs.version, at=cs.at,
        consenter_name=cs.consenter_name,
        disclosure_version=d["version"], disclosure_text=d["text"],
    )


@router.post("/public/room/{token}/plaid/link-token", response_model=PlaidLinkTokenRead)
async def public_room_link_token(
    token: str, payload: RoomPasscode, db: AsyncSession = Depends(get_db)
) -> PlaidLinkTokenRead:
    """PUBLIC. Start Plaid Link from the client's own room.

    Until now Plaid was `require_team` on every route, which meant the only way
    a business owner could get statements to us was to find, download and
    upload twelve PDFs. That is the step files die on.

    The token names the file; the caller never does. See
    client_room.resolve_room for what that guarantees."""
    try:
        link, dealer = await client_room.resolve_room(db, token, payload.passcode)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if not await bank_consent.has_consent(db, dealer.id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _NO_BANK_CONSENT)
    _plaid_cooldown("link", dealer.id, 10)
    try:
        pt = await plaid_client.create_link_token(
            dealer_id=str(dealer.id),
            dealer_name=dealer.legal_name or dealer.name,
            # The room is public; its OAuth return must be a public page, not
            # the team app's authenticated one.
            redirect_override=plaid_client.room_redirect_uri() or None,
        )
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return PlaidLinkTokenRead(link_token=pt)


@router.post(
    "/public/room/{token}/plaid/{item_pk}/update-link-token",
    response_model=PlaidLinkTokenRead,
)
async def public_room_update_link_token(
    token: str,
    item_pk: UUID,
    payload: RoomPlaidUpdateLink,
    db: AsyncSession = Depends(get_db),
) -> PlaidLinkTokenRead:
    try:
        _link, dealer = await client_room.resolve_room(db, token, payload.passcode)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if not await bank_consent.has_consent(db, dealer.id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _NO_BANK_CONSENT)
    item = await _dealer_plaid_item(db, dealer.id, item_pk)
    try:
        link_token = await plaid_client.create_update_link_token(
            access_token=plaid_lifecycle.decrypted_access_token(item),
            client_user_id=str(dealer.id),
            display_name=dealer.legal_name or dealer.name,
            redirect_override=plaid_client.room_redirect_uri() or None,
            account_selection_enabled=(
                payload.account_selection_enabled
                or item.update_mode_account_selection
            ),
        )
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return PlaidLinkTokenRead(link_token=link_token)


@router.post(
    "/public/room/{token}/plaid/{item_pk}/update-complete",
    response_model=PublicPlaidItemRead,
)
async def public_room_update_complete(
    token: str,
    item_pk: UUID,
    payload: RoomPasscode,
    db: AsyncSession = Depends(get_db),
) -> PublicPlaidItemRead:
    try:
        link, dealer = await client_room.resolve_room(db, token, payload.passcode)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    item = await _dealer_plaid_item(db, dealer.id, item_pk)
    try:
        await plaid_lifecycle.complete_update(item)
    except plaid_client.PlaidUnavailable as exc:
        await db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await log_action(
        db,
        dealer.id,
        None,
        "plaid.update_mode.completed.client",
        "plaid_item",
        entity_id=item.id,
        after={"via": "client_room", "link_id": str(link.id)},
    )
    await db.commit()
    return next(row for row in await _safe_plaid_items(db, dealer.id) if row.id == item.id)


@router.delete(
    "/public/room/{token}/plaid/{item_pk}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def public_room_disconnect_bank(
    token: str,
    item_pk: UUID,
    payload: RoomPasscode,
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        link, dealer = await client_room.resolve_room(db, token, payload.passcode)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    item = await _dealer_plaid_item(db, dealer.id, item_pk)
    was_primary = item.is_primary_operating
    try:
        await plaid_lifecycle.disconnect_item(db, item)
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if was_primary:
        replacement = next(
            (candidate for candidate in await _plaid_items(db, dealer.id) if candidate.id != item.id),
            None,
        )
        if replacement:
            await _make_primary_operating_bank(db, dealer.id, replacement)
    await log_action(
        db,
        dealer.id,
        None,
        "plaid.disconnect.client",
        "plaid_item",
        entity_id=item.id,
        after={"via": "client_room", "link_id": str(link.id)},
    )
    await db.commit()


@router.post(
    "/public/room/{token}/plaid/exchange",
    response_model=PublicPlaidResult,
    status_code=status.HTTP_201_CREATED,
)
async def public_room_plaid_exchange(
    token: str,
    payload: RoomPlaidExchange,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> PublicPlaidResult:
    """PUBLIC. Finish Link from the client's room.

    Returns only what the owner needs to see. The authenticated version returns
    the whole item row, which carries the encrypted access token's metadata and
    internal status; none of that belongs in a response to an unauthenticated
    caller, however harmless it looks."""
    try:
        link, dealer = await client_room.resolve_room(db, token, payload.passcode)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    _plaid_cooldown("exchange", dealer.id, 5)
    try:
        access_token, item_id = await plaid_client.exchange_public_token(payload.public_token)
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    await _lock_dealer_related_writes(db, dealer.id)
    existing = (
        await db.execute(select(DealerPlaidItem).where(DealerPlaidItem.item_id == item_id))
    ).scalar_one_or_none()
    if existing is not None and existing.dealer_id != dealer.id:
        # Same guard as the authenticated path. A bank item belongs to exactly
        # one file, and re-pointing it would silently move somebody's
        # transactions onto another business.
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This bank connection belongs to another file"
        )
    if existing is not None:
        existing.encrypted_access_token = plaid_client.encrypt_token(access_token)
        existing.environment = plaid_client.environment()
        existing.status, existing.error = "active", None
        existing.update_mode_reason = None
        existing.update_mode_account_selection = False
        existing.next_refresh_at = datetime.now(timezone.utc)
        item = existing
    else:
        item = DealerPlaidItem(
            dealer_id=dealer.id,
            item_id=item_id,
            institution_name=(payload.institution_name or "")[:160] or None,
            encrypted_access_token=plaid_client.encrypt_token(access_token),
            environment=plaid_client.environment(),
            status="active",
            next_refresh_at=datetime.now(timezone.utc),
        )
        db.add(item)
    await db.flush()
    if payload.is_primary_operating is True or not await _has_primary_operating_bank(db, dealer.id):
        await _make_primary_operating_bank(db, dealer.id, item)
    # No `user` to attribute this to, so the audit row records the room the
    # owner came through. "The client did it themselves" is exactly the fact
    # worth being able to prove later.
    await log_action(
        db, dealer.id, None, "plaid.connect.client", "plaid_item", entity_id=item.id,
        after={
            "institution": item.institution_name,
            "via": "client_room",
            "link_id": str(link.id),
            "is_primary_operating": item.is_primary_operating,
        },
    )
    await db.commit()
    background.add_task(_background_plaid_first_sync, item.id)
    return PublicPlaidResult(
        connected=True,
        institution_name=item.institution_name,
        message="Your bank is connected. We are pulling your statements now.",
    )


async def _lock_dealer_related_writes(db: AsyncSession, dealer_id: UUID) -> None:
    """Serialize owner and bank-list mutations whose limits span multiple rows."""
    await db.execute(
        select(DealerBusiness.id)
        .where(DealerBusiness.id == dealer_id)
        .with_for_update()
    )


@router.post(
    "/public/room/{token}/plaid/{item_pk}/primary",
    response_model=PublicPlaidItemRead,
)
async def public_room_set_primary_bank(
    token: str,
    item_pk: UUID,
    payload: RoomPasscode,
    db: AsyncSession = Depends(get_db),
) -> PublicPlaidItemRead:
    """Let the client identify the operating bank without exposing secrets."""
    try:
        link, dealer = await client_room.resolve_room(db, token, payload.passcode)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    await _lock_dealer_related_writes(db, dealer.id)
    item = (
        await db.execute(
            select(DealerPlaidItem).where(
                DealerPlaidItem.id == item_pk,
                DealerPlaidItem.dealer_id == dealer.id,
                DealerPlaidItem.status != "removed",
                DealerPlaidItem.environment == plaid_client.environment(),
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bank connection not found")
    await _make_primary_operating_bank(db, dealer.id, item)
    await log_action(
        db,
        dealer.id,
        None,
        "plaid.primary.client",
        "plaid_item",
        entity_id=item.id,
        after={"via": "client_room", "link_id": str(link.id)},
    )
    await db.commit()
    months = await _plaid_statement_months_by_item(db, dealer.id)
    return PublicPlaidItemRead(
        id=item.id,
        institution_name=item.institution_name,
        accounts_label=item.accounts_label,
        status=item.status,
        is_primary_operating=True,
        last_pulled_at=item.last_pulled_at,
        statement_months=months.get(item.id, []),
    )


async def _background_plaid_first_sync(item_pk: UUID) -> None:
    from app.db import SessionLocal

    try:
        async with SessionLocal() as db:
            item = (
                await db.execute(select(DealerPlaidItem).where(DealerPlaidItem.id == item_pk))
            ).scalar_one_or_none()
            if item is not None:
                await plaid_sync.sync_item(db, item)
                await db.commit()
    except Exception:
        logger.exception("dealer-os plaid: first sync failed for %s", item_pk)


@router.post("/dealers/{dealer_id}/plaid/refresh", response_model=PlaidRefreshResult)
async def plaid_refresh(
    dealer_id: UUID,
    request: Request,
    background: BackgroundTasks,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlaidRefreshResult:
    """Refresh now: queue a pull of any statements not yet ingested across the
    dealer's connected banks. Runs in the background (each statement PDF goes
    through extraction — minutes, not a request); idempotent by construction
    (statement-id dedupe), so mashing the button is harmless. Statements
    appear in Files as they extract."""
    require_super_admin(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    await _require_training_live_action(
        db,
        dealer=dealer,
        user=user,
        request=request,
        action="Refresh Plaid statements",
        provider="Plaid",
        recipient=dealer.name,
        effect="Queue live Plaid statement retrieval and extraction.",
    )
    _plaid_cooldown("refresh", dealer.id, 60)
    items = [i for i in await _plaid_items(db, dealer.id) if i.status != "removed"]
    for item in items:
        background.add_task(_background_plaid_first_sync, item.id)
    await log_action(
        db,
        dealer.id,
        user,
        "plaid.refresh.recovery",
        "dealer",
        entity_id=dealer.id,
        after={"queued": len(items)},
    )
    await db.commit()
    return PlaidRefreshResult(queued=len(items))


@router.patch("/dealers/{dealer_id}/plaid/{item_pk}", response_model=PlaidItemRead)
async def plaid_patch(
    dealer_id: UUID,
    item_pk: UUID,
    payload: PlaidItemPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerPlaidItem:
    """Client primary-bank choice or super-admin refresh recovery settings."""
    require_team_or_dealer(user)
    if payload.is_primary_operating is not None and user.role != Role.DEALER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the client may select the main operating bank",
        )
    if payload.auto_refresh is not None and user.role != Role.SUPER_ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only a super admin may change automatic bank refresh",
        )
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    await _lock_dealer_related_writes(db, dealer.id)
    item = (
        await db.execute(
            select(DealerPlaidItem).where(
                DealerPlaidItem.id == item_pk, DealerPlaidItem.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bank connection not found for this client")
    if payload.auto_refresh is not None:
        item.auto_refresh = payload.auto_refresh
        if payload.auto_refresh and item.next_refresh_at is None:
            item.next_refresh_at = datetime.now(timezone.utc)
        await log_action(
            db, dealer.id, user, "plaid.auto_refresh", "plaid_item", entity_id=item.id,
            after={"auto_refresh": payload.auto_refresh},
        )
    if payload.is_primary_operating is True:
        await _make_primary_operating_bank(db, dealer.id, item)
        await log_action(
            db, dealer.id, user, "plaid.primary.client", "plaid_item", entity_id=item.id,
            after={"is_primary_operating": True, "via": "authenticated_client"},
        )
    elif payload.is_primary_operating is False and item.is_primary_operating:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Select another main operating bank instead of clearing the current one",
        )
    await db.commit()
    await db.refresh(item)
    return item


@router.delete("/dealers/{dealer_id}/plaid/{item_pk}", status_code=status.HTTP_204_NO_CONTENT)
async def plaid_remove(
    dealer_id: UUID, item_pk: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> None:
    """Disconnect at the owning client's request or as super-admin recovery."""
    if user.role == Role.DEALER:
        dealer = await resolve_dealer_scope(db, user, dealer_id)
        action = "plaid.disconnect.client"
    elif user.role == Role.SUPER_ADMIN:
        dealer = await _load_visible_dealer(db, dealer_id, user)
        action = "plaid.disconnect.recovery"
    else:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the client or a super admin may disconnect a bank",
        )
    await _lock_dealer_related_writes(db, dealer.id)
    item = (
        await db.execute(
            select(DealerPlaidItem).where(
                DealerPlaidItem.id == item_pk, DealerPlaidItem.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bank connection not found for this client")
    was_primary = item.is_primary_operating
    try:
        await plaid_lifecycle.disconnect_item(db, item)
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if was_primary:
        replacement = next(
            (
                candidate
                for candidate in await _plaid_items(db, dealer.id)
                if candidate.id != item.id and candidate.status != "removed"
            ),
            None,
        )
        if replacement is not None:
            await _make_primary_operating_bank(db, dealer.id, replacement)
    await log_action(
        db,
        dealer.id,
        user,
        action,
        "plaid_item",
        entity_id=item.id,
        after={
            "retained_statement_evidence": True,
            "via": "authenticated_client" if user.role == Role.DEALER else "super_admin",
        },
    )
    await db.commit()


@router.delete(
    "/dealers/{dealer_id}/plaid",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def purge_dealer_plaid_on_offboarding(
    dealer_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove every Plaid Item and Asset Report when the file is offboarded."""
    require_super_admin(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    try:
        removed = await plaid_lifecycle.purge_owner_connections(
            db, dealer_id=dealer.id
        )
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    await log_action(
        db,
        dealer.id,
        user,
        "plaid.offboarding_purge",
        "dealer",
        entity_id=dealer.id,
        after={"removed_items": removed},
    )
    await db.commit()


async def _dealer_asset_report(
    db: AsyncSession, dealer_id: UUID, report_id: UUID
) -> PlaidAssetReport:
    report = (
        await db.execute(
            select(PlaidAssetReport).where(
                PlaidAssetReport.id == report_id,
                PlaidAssetReport.dealer_id == dealer_id,
            )
        )
    ).scalar_one_or_none()
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Asset Report not found")
    return report


@router.post(
    "/dealers/{dealer_id}/plaid/asset-reports",
    response_model=PlaidAssetReportRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_dealer_asset_report(
    dealer_id: UUID,
    payload: PlaidAssetReportCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlaidAssetReport:
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    if not await bank_consent.has_consent(db, dealer.id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The client must accept the current bank disclosure first",
        )
    try:
        report = await plaid_lifecycle.create_asset_report(
            db,
            items=await _plaid_items(db, dealer.id),
            dealer_id=dealer.id,
            days_requested=payload.days_requested,
        )
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    await log_action(
        db,
        dealer.id,
        user,
        "plaid.asset_report.requested",
        "plaid_asset_report",
        entity_id=report.id,
        after={"days_requested": report.days_requested},
    )
    await db.commit()
    await db.refresh(report)
    return report


@router.get("/dealers/{dealer_id}/plaid/asset-reports/{report_id}/pdf")
async def download_dealer_asset_report(
    dealer_id: UUID,
    report_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    report = await _dealer_asset_report(db, dealer.id, report_id)
    report_token = plaid_client.decrypt_token(report.encrypted_asset_report_token)
    if report.status != "ready" or not report_token:
        raise HTTPException(status.HTTP_409_CONFLICT, "Asset Report is not ready")
    try:
        content = await plaid_client.asset_report_pdf(report_token)
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="asset-report-{report.id}.pdf"'},
    )


@router.delete(
    "/dealers/{dealer_id}/plaid/asset-reports/{report_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_dealer_asset_report(
    dealer_id: UUID,
    report_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    require_super_admin(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    report = await _dealer_asset_report(db, dealer.id, report_id)
    try:
        await plaid_lifecycle.remove_asset_report(report)
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    await log_action(
        db,
        dealer.id,
        user,
        "plaid.asset_report.removed",
        "plaid_asset_report",
        entity_id=report.id,
        after={"reason": "no_longer_needed"},
    )
    await db.commit()


# --- Refinance workbench (0126) ------------------------------------------------


def _refi_program_specs(settings: dict) -> list[dict]:
    """DSCR-model programs with desk overrides applied — what the workbench's
    program picker drafts terms from. Triplets are [conservative, typical,
    aggressive]; the aggressive DSCR is the floor the scenario is graded at."""
    out: list[dict] = []
    for key in PATH_KEYS:
        sizing = (settings.get(key) or {}).get("sizing") or DEFAULT_SIZING.get(key)
        if not sizing or sizing.get("model") != "dscr":
            continue
        dscr = sizing["dscr"]
        terms = sizing["term_months"]
        out.append(
            {
                "path_key": key,
                "label": PATH_LABELS[key],
                "annual_rate_pct": round(float(sizing["annual_rate"]) * 100, 3),
                "term_months": int(terms[1]),
                "dscr_typical": float(dscr[1]),
                "dscr_floor": float(dscr[2]),
                "ceiling": float(sizing["ceiling"]),
            }
        )
    return out


async def _refi_debt_rows(db: AsyncSession, dealer) -> list[DealerDebt]:
    return (
        (
            await db.execute(
                select(DealerDebt)
                .where(DealerDebt.dealer_id == dealer.id, DealerDebt.status == "active")
                .order_by(DealerDebt.monthly_payment.desc().nullslast())
            )
        )
        .scalars()
        .all()
    )


@router.get("/dealers/{dealer_id}/refinance", response_model=RefinanceRead)
async def dealer_refinance(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> RefinanceRead:
    """The refinance workbench: the debt stack with each lender's OBSERVED
    ledger behavior (vendor-matched debits), plus the desk's DSCR programs
    to draft replacement terms from, plus the current baseline metrics."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    debts = await _refi_debt_rows(db, dealer)
    events, _overrides, _self_names = await _load_vendor_inputs(db, dealer)
    observed = refinance_svc.observed_monthly(events, debts)

    # per-debt debit stats for the match chips (one pass over the ledger,
    # same containment-tolerant matcher as observed_monthly)
    stats: dict[str, dict] = {}
    debt_keys = [d.vendor_key for d in debts if d.vendor_key]
    if debt_keys:
        for row in events:
            if float(row.amount or 0) >= 0:
                continue
            event_key = vendors.normalize_vendor(row.description or "")
            for dk in debt_keys:
                if refinance_svc.key_matches(dk, event_key):
                    st = stats.setdefault(dk, {"count": 0, "last": None})
                    st["count"] += 1
                    if st["last"] is None or row.occurred_on > st["last"]:
                        st["last"] = row.occurred_on
                    break

    inputs = await load_metric_inputs(db, dealer.id)
    metrics = compute_metrics(
        inputs.periods, inputs.addbacks_annual_verified, inputs.targets, fallbacks=inputs.fallbacks
    )
    summary = simulate.summarize(metrics)
    monthly_ds = (metrics.get("dscr") or {}).get("monthly_debt_service")

    rows: list[RefiDebtRead] = []
    for d in debts:
        by_month = observed.get(d.id) or {}
        st = stats.get(d.vendor_key or "", {})
        months = sorted(by_month)
        monthly_avg = (
            round(sum(by_month.values()) / len(by_month), 2) if by_month else None
        )
        rows.append(
            RefiDebtRead(
                **{k: getattr(d, k) for k in RefiDebtRead.model_fields if hasattr(d, k)},
                monthly_eq=refinance_svc.monthly_equivalent(d),
                financing_cost_monthly=refinance_svc.financing_cost_monthly(d),
                payoff_est=refinance_svc.payoff_estimate(d),
                refi_eligible=d.category not in refinance_svc.NEVER_REFI_CATEGORIES,
                observed=RefiObservedRead(
                    matched=bool(by_month),
                    debit_count=int(st.get("count") or 0),
                    months_observed=len(months),
                    monthly_avg=monthly_avg,
                    last_seen=st.get("last"),
                    by_month={m.strftime("%Y-%m"): round(v, 2) for m, v in by_month.items()},
                ),
            )
        )

    settings = await _global_program_settings(db)
    dscr_m = metrics.get("dscr") or {}
    return RefinanceRead(
        debts=rows,
        programs=[RefiProgramRead(**spec) for spec in _refi_program_specs(settings)],
        total_debt_service_monthly=float(monthly_ds) if monthly_ds is not None else 0.0,
        dscr_current=summary.get("dscr"),
        ebitda_bankable=summary.get("ebitda_bankable"),
        adb_current=summary.get("adb"),
        dscr_source=dscr_m.get("source"),
        ebitda_source=dscr_m.get("ebitda_source"),
        dscr_cash_flow=dscr_m.get("cash_flow"),
        net_cash_flow_monthly=dscr_m.get("net_cash_flow_monthly"),
        dscr_draft=dscr_m.get("draft"),
        dscr_display=dscr_m.get("display"),
    )


@router.post("/dealers/{dealer_id}/refinance/simulate", response_model=RefinanceSimulateRead)
async def simulate_refinance(
    dealer_id: UUID,
    payload: RefinanceSimulateRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RefinanceSimulateRead:
    """Read-only refinance what-if: replay the statements WITHOUT the selected
    lenders' observed debits (debt service falls, embedded financing cost
    returns to EBITDA, balances stop draining), layer the replacement note's
    payment in, and rerun the real metric engine on the adjusted months.
    Persists nothing — same discipline as /simulate."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    debts = await _refi_debt_rows(db, dealer)
    by_id = {d.id: d for d in debts}
    selected: list[DealerDebt] = []
    for debt_id in dict.fromkeys(payload.debt_ids):  # dedupe, order-preserving
        d = by_id.get(debt_id)
        if d is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Debt row not found for this client")
        if d.category in refinance_svc.NEVER_REFI_CATEGORIES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"{d.lender} is a working line ({d.category}) — not a refinance target",
            )
        selected.append(d)

    settings = await _global_program_settings(db)
    specs = {spec["path_key"]: spec for spec in _refi_program_specs(settings)}
    path_key = payload.path_key or "conventional"
    spec = specs.get(path_key)
    if spec is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"path_key must be a DSCR-sized program: {', '.join(sorted(specs))}",
        )

    inputs = await load_metric_inputs(db, dealer.id)
    baseline_metrics = compute_metrics(
        inputs.periods, inputs.addbacks_annual_verified, inputs.targets, fallbacks=inputs.fallbacks
    )
    baseline = simulate.summarize(baseline_metrics)

    payoff_total = round(sum(refinance_svc.payoff_estimate(d) for d in selected), 2)
    ebitda_addback_annual = round(
        sum(refinance_svc.financing_cost_monthly(d) for d in selected) * 12, 2
    )
    amount = payoff_total if payload.amount is None else float(payload.amount)
    new_pay = refinance_svc.new_loan_payment(amount, payload.annual_rate_pct, payload.term_months)

    events, _overrides, _self_names = await _load_vendor_inputs(db, dealer)
    # Attribution stays vendor_key-only: a keyless row replays at its stated
    # monthly equivalent (conservative). Name-derived identities are used for
    # EXACT-match suppression inside the scenario engine only — containment
    # attribution here would let a semi-generic lender name absorb a
    # different lender's debits and overstate the what-if (review-confirmed).
    observed = refinance_svc.observed_monthly(events, selected)
    scenario_periods = refinance_svc.removal_effects(
        inputs.periods, selected, observed, new_payment_monthly=new_pay
    )
    # The financing-cost add-back applies PERIOD-LEVEL only (removal_effects
    # raised ebitda_reported). When no period carries EBITDA the engine falls
    # back to the tax return, and _tax_ebitda has ALREADY added interest back
    # — stacking the add-back there double-counts it (review-confirmed).
    has_period_ebitda = any(p.get("ebitda_reported") is not None for p in inputs.periods)
    applied_addback_annual = ebitda_addback_annual if has_period_ebitda else 0.0
    # Scenario denominator: RECOMPUTE the composed pool with the selected rows
    # genuinely gone (out of the schedule tier, vendors suppressed from the
    # observed/draft tiers), then layer the replacement note's payment in.
    # The old approach shoved one bundled delta (new_pay - freed) into the
    # fallbacks with a per-key zero clamp — the moment the freed sum crossed
    # the pool size, the clamp swallowed the new payment too and the scenario
    # DSCR went null (the "flat DSCR on the second removal" bug). Recomputing
    # also fixes the stated-vs-observed mismatch: freed sums CONTRACT
    # payments, while the pool counts each row at its observed-wins amount.
    scenario_inputs = (
        await load_metric_inputs(db, dealer.id, exclude_debt_ids={d.id for d in selected})
        if selected
        else inputs
    )
    scenario_fallbacks = dict(scenario_inputs.fallbacks)
    if new_pay:
        # The new note is confirmed schedule-tier debt service (and counts in
        # the draft tier for the same reason).
        for key in ("debt_schedule_monthly", "debt_service_draft_monthly"):
            scenario_fallbacks[key] = round((scenario_fallbacks.get(key) or 0.0) + new_pay, 2)
    scenario_metrics = compute_metrics(
        scenario_periods,
        inputs.addbacks_annual_verified,
        inputs.targets,
        fallbacks=scenario_fallbacks,
    )
    scenario = simulate.summarize(scenario_metrics)

    # Retained DS falls straight out of the scenario engine (its monthly_ds
    # already includes the new payment on every path) — never derived by
    # subtracting stated contract sums from an observed-wins pool again.
    scenario_ds = (scenario_metrics.get("dscr") or {}).get("monthly_debt_service")
    retained = max(0.0, float(scenario_ds) - new_pay) if scenario_ds is not None else 0.0
    proforma_ds = round(retained + new_pay, 2)
    # Freed cash and savings on the SAME observed-wins basis as the engine
    # pools, so no tile can ever contradict the DSCR direction (stated
    # contract sums used to flip the savings sign whenever observed debits
    # diverged from the contract — review-confirmed).
    baseline_ds = (baseline_metrics.get("dscr") or {}).get("monthly_debt_service")
    if baseline_ds is not None:
        freed = round(max(0.0, float(baseline_ds) - retained), 2)
    else:
        freed = round(sum(refinance_svc.monthly_equivalent(d) for d in selected), 2)
    # Savings derives FROM freed so the tiles always satisfy the arithmetic a
    # broker does in their head (savings = freed - new payment) — even in the
    # commingled-lender edge where a kept sibling's stated contract exceeds
    # the ledger's observed total and retained lands above baseline.
    savings = round(freed - new_pay, 2)
    adb_lift = round(freed * 0.5, 2)  # uniform-debit estimate, mirrored in removal_effects
    ebitda_after_annual = scenario.get("ebitda_bankable")
    max_at_floor = (
        refinance_svc.max_principal(
            float(ebitda_after_annual),
            retained,
            spec["dscr_floor"],
            payload.annual_rate_pct,
            payload.term_months,
            ceiling=spec["ceiling"],
        )
        if ebitda_after_annual is not None
        else 0.0
    )

    dscr_after = scenario.get("dscr")
    if dscr_after is None and proforma_ds > 0.01 and ebitda_after_annual is not None:
        # Hard floor on the workbench law: while there is pro-forma debt
        # service to cover, the DSCR-after is ALWAYS a number — derived
        # deterministically from the same two figures the panel shows.
        dscr_after = round(float(ebitda_after_annual) / (proforma_ds * 12.0), 3)
        scenario["dscr"] = dscr_after
    if not selected:
        verdict = "no_selection"
    elif dscr_after is None:
        # Zero pro-forma debt service means DSCR is undefined because there is
        # nothing left to service — that's debt-free, not not-yet.
        verdict = (
            "feasible"
            if ebitda_after_annual is not None and proforma_ds <= 0.01
            else "not_yet"
        )
    elif dscr_after >= spec["dscr_typical"]:
        verdict = "feasible"
    elif dscr_after >= spec["dscr_floor"]:
        verdict = "conditional"
    else:
        verdict = "not_yet"

    return RefinanceSimulateRead(
        baseline=SimulateMetrics(**baseline),
        scenario=SimulateMetrics(**scenario),
        derived=RefinanceScenarioRead(
            payoff_total=payoff_total,
            freed_monthly=freed,
            new_payment_monthly=new_pay,
            retained_ds_monthly=round(retained, 2),
            proforma_ds_monthly=proforma_ds,
            savings_monthly=savings,
            ebitda_addback_annual=applied_addback_annual,
            adb_lift_estimate=adb_lift,
            amount=round(amount, 2),
            max_principal_at_floor=max_at_floor,
            headroom=round(max_at_floor - amount, 2),
            dscr_floor=spec["dscr_floor"],
            dscr_typical=spec["dscr_typical"],
            verdict=verdict,
        ),
    )


# --- Owners, business profile & credit (0118) ---------------------------------

_MAX_OWNERS_PER_FILE = 5
_CREDIT_OWNER_THRESHOLD = 20.0


def _normalized_owner_email(value: str | None) -> str | None:
    clean = (value or "").strip().lower()
    return clean or None


async def _assert_owner_email_unique(
    db: AsyncSession,
    dealer_id: UUID,
    email: str | None,
    *,
    exclude_owner_id: UUID | None = None,
) -> None:
    normalized = _normalized_owner_email(email)
    if normalized is None:
        return
    stmt = select(DealerOwner.id).where(
        DealerOwner.dealer_id == dealer_id,
        func.lower(DealerOwner.email) == normalized,
    )
    if exclude_owner_id is not None:
        stmt = stmt.where(DealerOwner.id != exclude_owner_id)
    if (await db.execute(stmt.limit(1))).scalar_one_or_none() is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Each owner must use a different email address",
        )


async def _make_primary_operating_bank(
    db: AsyncSession, dealer_id: UUID, item: DealerPlaidItem
) -> None:
    """Select exactly one primary bank without exposing token-bearing rows."""
    await db.execute(
        sa_update(DealerPlaidItem)
        .where(
            DealerPlaidItem.dealer_id == dealer_id,
            DealerPlaidItem.id != item.id,
            DealerPlaidItem.is_primary_operating.is_(True),
        )
        .values(is_primary_operating=False)
    )
    item.is_primary_operating = True


async def _has_primary_operating_bank(db: AsyncSession, dealer_id: UUID) -> bool:
    return (
        await db.execute(
            select(DealerPlaidItem.id)
            .where(
                DealerPlaidItem.dealer_id == dealer_id,
                DealerPlaidItem.status != "removed",
                DealerPlaidItem.environment == plaid_client.environment(),
                DealerPlaidItem.is_primary_operating.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None


async def _plaid_statement_months_by_item(
    db: AsyncSession, dealer_id: UUID
) -> dict[UUID, list[str]]:
    rows = (
        await db.execute(
            select(DealerDocument.plaid_item_id, DealerDocument.extracted).where(
                DealerDocument.dealer_id == dealer_id,
                DealerDocument.plaid_item_id.is_not(None),
            )
        )
    ).all()
    coverage: dict[UUID, set[str]] = {}
    for item_id, extracted in rows:
        if item_id is None or not isinstance(extracted, dict):
            continue
        for month in extracted.get("months") or []:
            if isinstance(month, str) and re.fullmatch(r"\d{4}-\d{2}", month):
                coverage.setdefault(item_id, set()).add(month)
    return {item_id: sorted(months) for item_id, months in coverage.items()}


async def _safe_plaid_items(
    db: AsyncSession, dealer_id: UUID
) -> list[PublicPlaidItemRead]:
    months = await _plaid_statement_months_by_item(db, dealer_id)
    return [
        PublicPlaidItemRead(
            id=item.id,
            institution_name=item.institution_name,
            accounts_label=item.accounts_label,
            status=item.status,
            environment=item.environment,
            update_mode_reason=item.update_mode_reason,
            update_mode_account_selection=item.update_mode_account_selection,
            is_primary_operating=item.is_primary_operating,
            last_pulled_at=item.last_pulled_at,
            statement_months=months.get(item.id, []),
        )
        for item in await _plaid_items(db, dealer_id)
    ]


async def _owner_requirement_state(db: AsyncSession, dealer_id: UUID) -> dict:
    owners = list(
        (
            await db.execute(
                select(DealerOwner)
                .where(DealerOwner.dealer_id == dealer_id)
                .order_by(DealerOwner.ownership_pct.desc().nullslast(), DealerOwner.last_name)
            )
        )
        .scalars()
        .all()
    )
    total = round(sum(float(owner.ownership_pct or 0) for owner in owners), 2)
    ownership_complete = bool(owners) and abs(total - 100.0) < 0.005
    required = [
        owner for owner in owners if float(owner.ownership_pct or 0) >= _CREDIT_OWNER_THRESHOLD
    ]
    completed = [owner for owner in required if owner.credit_pulled_at is not None]
    missing_email = [owner for owner in required if not _normalized_owner_email(owner.email)]
    missing_phone = [owner for owner in required if not consent_delivery.normalize_phone(owner.phone)]
    missing_contact = [
        owner for owner in required
        if owner in missing_email or owner in missing_phone
    ]
    pending = [owner for owner in required if owner.credit_pulled_at is None]
    return {
        "owners": owners,
        "ownership_total": total,
        "ownership_complete": ownership_complete,
        "required": required,
        "completed": completed,
        "missing_email": missing_email,
        "missing_phone": missing_phone,
        "missing_contact": missing_contact,
        "contact_complete": not missing_contact,
        "pending": pending,
    }


async def _application_pre_screen_state(
    db: AsyncSession,
    dealer: DealerBusiness,
    owner_state: dict | None = None,
) -> dict:
    owner_state = owner_state or await _owner_requirement_state(db, dealer.id)
    row = (
        await db.execute(
            select(DealerApplicationPreScreen).where(
                DealerApplicationPreScreen.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    file_answers = dict(row.file_answers or {}) if row else {}
    owner_answers = dict(row.owner_answers or {}) if row else {}
    required_ids = [str(owner.id) for owner in owner_state["required"]]
    completed_ids = [
        owner_id
        for owner_id in required_ids
        if application_prescreen.owner_answer_complete(owner_answers.get(owner_id))
    ]
    incomplete_ids = [owner_id for owner_id in required_ids if owner_id not in completed_ids]
    blockers: list[str] = []
    if incomplete_ids:
        blockers.append(f"Complete eligibility for {len(incomplete_ids)} required owner(s).")
    # Step 1.5 is personal eligibility only. Business questions are collected
    # in Step 4 and remain unresolved routing facts until answered there.
    complete = not blockers and bool(required_ids)
    verified_scores = {
        str(owner.id): owner.credit_score for owner in owner_state["required"]
    }
    profile = (
        await db.execute(
            select(DealerApplicationProfile).where(
                DealerApplicationProfile.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    years_in_business = None
    if dealer.started_on:
        years_in_business = max((date.today() - dealer.started_on).days / 365.2425, 0)
    taxonomy_status = "unclassified"
    taxonomy_ids = [
        value
        for value in (dealer.industry_entry_id, dealer.subindustry_entry_id, dealer.activity_entry_id)
        if value is not None
    ]
    if len(taxonomy_ids) == 3:
        taxonomy_rows = list(
            (
                await db.execute(
                    select(ApplicationTaxonomyEntry).where(
                        ApplicationTaxonomyEntry.id.in_(taxonomy_ids)
                    )
                )
            ).scalars().all()
        )
        taxonomy_status = (
            "pending" if any(entry.status == "pending" for entry in taxonomy_rows)
            else "official" if len(taxonomy_rows) == 3
            else "unclassified"
        )
    application_facts = {
        "years_in_business": years_in_business,
        "annual_revenue": float(profile.annual_sales) if profile and profile.annual_sales is not None else None,
        "annual_cash_flow_available_for_debt": (
            float(profile.annual_cash_flow_available_for_debt)
            if profile and profile.annual_cash_flow_available_for_debt is not None
            else None
        ),
        "monthly_debt_payments": (
            float(profile.monthly_debt_payments)
            if profile and profile.monthly_debt_payments is not None
            else None
        ),
        "owner_count": len(owner_state["owners"]),
        "naics_code": dealer.naics_code,
        "taxonomy_status": taxonomy_status,
        "state": dealer.state,
    }
    # Verified evidence enriches a separate result. It never overwrites the
    # original Step 1.5 self-report snapshot.
    statement_months, missing_statement_months, _statement_source = (
        await _statement_month_coverage(db, dealer.id)
    )
    metric_inputs = await load_metric_inputs(db, dealer.id)
    # Coverage is returned in ascending order. Use the newest six completed
    # months for current-program metrics when a file contains a longer history.
    statement_month_set = set(sorted(statement_months)[-6:])
    period_rows = [
        row
        for row in metric_inputs.periods
        if row.get("period") and row["period"].strftime("%Y-%m") in statement_month_set
    ][:6]
    debts = list(
        (
            await db.execute(
                select(DealerDebt).where(
                    DealerDebt.dealer_id == dealer.id,
                    DealerDebt.status == "active",
                )
            )
        ).scalars().all()
    )
    official_statements_complete = len(statement_months) >= 6 and not missing_statement_months
    annualized_bank_sales = None
    if (
        official_statements_complete
        and len(period_rows) >= 6
        and all(row.get("deposits") is not None for row in period_rows)
    ):
        annualized_bank_sales = round(
            sum(float(row["deposits"]) for row in period_rows) / len(period_rows) * 12,
            2,
        )
    metric_tree = compute_metrics(
        metric_inputs.periods,
        metric_inputs.addbacks_annual_verified,
        metric_inputs.targets,
        fallbacks=metric_inputs.fallbacks,
    )
    verified_dscr = (metric_tree.get("dscr") or {}).get("current")

    statement_documents = list(
        (
            await db.execute(
                select(DealerDocument).where(
                    DealerDocument.dealer_id == dealer.id,
                    DealerDocument.status == "extracted",
                )
            )
        ).scalars().all()
    )
    negative_dates: set[str] = set()
    negative_balance_evidence_seen = False
    for document in statement_documents:
        extracted = document.extracted if isinstance(document.extracted, dict) else {}
        for month in extracted.get("months") or []:
            if not isinstance(month, dict) or str(month.get("month") or "") not in statement_month_set:
                continue
            raw_negative_dates = month.get("negative_balance_dates")
            if isinstance(raw_negative_dates, list):
                negative_balance_evidence_seen = True
            for raw_date in raw_negative_dates or []:
                value = str(raw_date or "").strip()
                if value:
                    negative_dates.add(value)

    def _counts_mca_or_sba(debt: DealerDebt) -> bool:
        evidence = debt.evidence if isinstance(debt.evidence, dict) else {}
        descriptor = " ".join(
            str(value or "").lower()
            for value in (
                debt.category,
                debt.lender,
                debt.notes,
                evidence.get("program_type"),
                evidence.get("loan_type"),
            )
        )
        if any(exempt in descriptor for exempt in ("eidl", "paycheck protection", " ppp", "sba 504", "504 loan")):
            return False
        return str(debt.category or "").lower() in {
            "mca", "sba", "merchant_cash_advance", "sba_loan"
        } or "merchant cash advance" in descriptor

    mca_sba_debts = [debt for debt in debts if _counts_mca_or_sba(debt)]
    mca_ages: list[int] = []
    for debt in mca_sba_debts:
        evidence = debt.evidence if isinstance(debt.evidence, dict) else {}
        raw_date = next(
            (
                evidence.get(key)
                for key in ("funded_on", "funded_at", "origination_date", "funding_date")
                if evidence.get(key)
            ),
            None,
        )
        try:
            funded_on = date.fromisoformat(str(raw_date)[:10]) if raw_date else None
        except ValueError:
            funded_on = None
        if funded_on is not None:
            mca_ages.append(max((date.today() - funded_on).days, 0))

    confirmations = set((profile.field_confirmations or {}).keys()) if profile else set()
    financial_suggestions: dict[str, dict] = {}

    def _suggest(field: str, value, source: str, evidence: str) -> None:
        if value is None or field in confirmations:
            return
        if profile is not None and getattr(profile, field, None) is not None:
            return
        try:
            normalized = round(float(value), 2)
        except (TypeError, ValueError):
            return
        if normalized != normalized or normalized in {float("inf"), float("-inf")}:
            return
        financial_suggestions[field] = {
            "value": normalized,
            "source": source,
            "label": "Extracted",
            "evidence": evidence,
        }

    ebitda_metrics = metric_tree.get("ebitda") or {}
    dscr_metrics = metric_tree.get("dscr") or {}
    _suggest(
        "annual_sales",
        annualized_bank_sales,
        "verified_bank_statements",
        f"Annualized from {len(period_rows)} qualifying statement months.",
    )
    _suggest(
        "annual_cash_flow_available_for_debt",
        ebitda_metrics.get("bankable"),
        "verified_financial_metrics",
        "Bankable annual cash flow calculated from extracted financial evidence.",
    )
    _suggest(
        "monthly_debt_payments",
        dscr_metrics.get("monthly_debt_service"),
        "debt_schedule_and_bank_activity",
        "Monthly debt service calculated from active obligations and observed payments.",
    )
    mca_balances = [
        float(debt.payoff_amount or debt.balance)
        for debt in debts
        if str(debt.category or "").lower() in {"mca", "merchant_cash_advance"}
        and (debt.payoff_amount is not None or debt.balance is not None)
    ]
    sba_balances = [
        float(debt.payoff_amount or debt.balance)
        for debt in debts
        if str(debt.category or "").lower() in {"sba", "sba_loan"}
        and (debt.payoff_amount is not None or debt.balance is not None)
    ]
    if mca_balances:
        _suggest(
            "existing_mca_balance",
            sum(mca_balances),
            "confirmed_debt_schedule",
            f"Sum of {len(mca_balances)} active MCA obligation(s).",
        )
    if sba_balances:
        _suggest(
            "existing_sba_balance",
            sum(sba_balances),
            "confirmed_debt_schedule",
            f"Sum of {len(sba_balances)} active SBA obligation(s).",
        )
    ucc_count = sum(bool((debt.evidence or {}).get("ucc")) for debt in debts)
    if ucc_count:
        _suggest(
            "active_ucc_filings",
            ucc_count,
            "debt_schedule_evidence",
            f"{ucc_count} active obligation(s) include UCC evidence.",
        )

    application_facts.update(
        {
            "official_bank_statements": official_statements_complete,
            "positive_month_end_count": sum(
                row.get("ending_balance") is not None and float(row["ending_balance"]) > 0
                for row in period_rows
            ) if period_rows else None,
            "nsf_count": sum(int(row.get("nsf_count") or 0) for row in period_rows) if period_rows else None,
            "negative_balance_days": len(negative_dates) if negative_balance_evidence_seen else None,
            "annualized_bank_sales": annualized_bank_sales,
            "verified_dscr": float(verified_dscr) if verified_dscr is not None else None,
            "mca_count": len(mca_sba_debts),
            "youngest_mca_days": min(mca_ages) if len(mca_ages) == len(mca_sba_debts) and mca_ages else (None if mca_sba_debts else 0),
            "active_ucc_count": sum(bool((debt.evidence or {}).get("ucc")) for debt in debts),
        }
    )
    self_report_result = application_prescreen.screen_application(
        requested_amount=float(dealer.funding_goal or dealer.client_requested_amount or 0),
        refinance_debt=bool(file_answers.get("refinance_debt")),
        required_owner_ids=required_ids,
        owner_answers=owner_answers,
        verified_credit_by_owner={},
        file_answers=file_answers,
        application_facts=application_facts,
    ) if complete else None
    has_verified_credit = any(value is not None for value in verified_scores.values())
    verified_result = application_prescreen.screen_application(
        requested_amount=float(dealer.funding_goal or dealer.client_requested_amount or 0),
        refinance_debt=bool(file_answers.get("refinance_debt")),
        required_owner_ids=required_ids,
        owner_answers=owner_answers,
        verified_credit_by_owner=verified_scores,
        file_answers=file_answers,
        application_facts=application_facts,
    ) if complete and (has_verified_credit or application_facts["official_bank_statements"]) else None
    result = verified_result or self_report_result
    applicable_questions = application_prescreen.applicable_business_questions(
        naics_code=dealer.naics_code,
        routing_result=result,
    )
    business_question_blockers = application_prescreen.business_answer_blockers(
        applicable_questions,
        file_answers,
    )
    return {
        "row": row,
        "rules_version": row.rules_version if row else application_prescreen.RULES_VERSION,
        "file_answers": file_answers,
        "owner_answers": owner_answers,
        "required_owner_ids": required_ids,
        "completed_owner_ids": completed_ids,
        "incomplete_owner_ids": incomplete_ids,
        "complete": complete,
        "blockers": blockers,
        "routing_result": result,
        "self_report_routing_result": self_report_result or (
            dict(row.self_report_routing_result or {}) if row and row.self_report_routing_result else None
        ),
        "verified_routing_result": verified_result or (
            dict(row.verified_routing_result or {}) if row and row.verified_routing_result else None
        ),
        "routing_history": list(row.routing_history or []) if row else [],
        "completed_at": row.completed_at if row else None,
        "applicable_business_questions": applicable_questions,
        "business_questions_complete": not business_question_blockers,
        "business_question_blockers": business_question_blockers,
        "financial_suggestions": financial_suggestions,
    }


def _pre_screen_read(state: dict) -> ApplicationPreScreenRead:
    return ApplicationPreScreenRead(
        rules_version=state["rules_version"],
        file_answers=state["file_answers"],
        owner_answers=state["owner_answers"],
        required_owner_ids=[UUID(value) for value in state["required_owner_ids"]],
        completed_owner_ids=[UUID(value) for value in state["completed_owner_ids"]],
        incomplete_owner_ids=[UUID(value) for value in state["incomplete_owner_ids"]],
        complete=state["complete"],
        blockers=state["blockers"],
        routing_result=state["routing_result"],
        self_report_routing_result=state.get("self_report_routing_result"),
        verified_routing_result=state.get("verified_routing_result"),
        routing_history=state.get("routing_history", []),
        completed_at=state["completed_at"],
        applicable_business_questions=state.get("applicable_business_questions", []),
        business_questions_complete=state.get("business_questions_complete", False),
        business_question_blockers=state.get("business_question_blockers", []),
    )


@router.get(
    "/dealers/{dealer_id}/pre-screen",
    response_model=ApplicationPreScreenRead,
)
async def get_application_pre_screen(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ApplicationPreScreenRead:
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return _pre_screen_read(await _application_pre_screen_state(db, dealer))


@router.patch(
    "/dealers/{dealer_id}/pre-screen",
    response_model=ApplicationPreScreenRead,
)
async def patch_application_pre_screen(
    dealer_id: UUID,
    body: ApplicationPreScreenPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationPreScreenRead:
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    owner_state = await _owner_requirement_state(db, dealer.id)
    row = (
        await db.execute(
            select(DealerApplicationPreScreen).where(
                DealerApplicationPreScreen.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = DealerApplicationPreScreen(
            dealer_id=dealer.id,
            rules_version=application_prescreen.RULES_VERSION,
            file_answers={},
            owner_answers={},
        )
        db.add(row)
        await db.flush()

    file_answers = dict(row.file_answers or {})
    owner_answers = dict(row.owner_answers or {})
    if body.refinance_debt is not None:
        file_answers["refinance_debt"] = body.refinance_debt
    if body.file_answers is not None:
        incoming_file = dict(body.file_answers)
        unknown_file = set(incoming_file) - set(application_prescreen.ALLOWED_FILE_FIELDS)
        if unknown_file:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Unsupported business eligibility fields: {', '.join(sorted(unknown_file))}",
            )
        if any(not isinstance(value, bool) for value in incoming_file.values()):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Business eligibility answers must be yes or no.",
            )
        file_answers.update(incoming_file)
    if body.owner_id is not None:
        owner = next((item for item in owner_state["owners"] if item.id == body.owner_id), None)
        if owner is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Owner not found for this client")
        incoming = dict(body.owner_answers or {})
        unknown = set(incoming) - set(application_prescreen.ALLOWED_OWNER_FIELDS)
        if unknown:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Unsupported eligibility fields: {', '.join(sorted(unknown))}",
            )
        current = dict(owner_answers.get(str(owner.id)) or {})
        current.update(incoming)
        if "bankruptcy_timing" in current and current["bankruptcy_timing"] not in application_prescreen.BANKRUPTCY_VALUES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid bankruptcy timing")
        if "felony_timing" in current and current["felony_timing"] not in application_prescreen.FELONY_VALUES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid felony timing")
        if "residency_status" in current and current["residency_status"] not in application_prescreen.RESIDENCY_VALUES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid residency status")
        owner_boolean_fields = set(application_prescreen.REQUIRED_OWNER_FIELDS) - {
            "residency_status", "bankruptcy_timing", "felony_timing"
        }
        invalid_boolean_fields = sorted(
            key for key in owner_boolean_fields
            if key in current and not isinstance(current.get(key), bool)
        )
        if invalid_boolean_fields:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Owner eligibility answers must be yes or no: {', '.join(invalid_boolean_fields)}",
            )
        owner_answers[str(owner.id)] = current

    prior_snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "rules_version": row.rules_version,
        "file_answers": dict(row.file_answers or {}),
        "owner_answers": dict(row.owner_answers or {}),
        "self_report_routing_result": row.self_report_routing_result or row.routing_result,
        "verified_routing_result": row.verified_routing_result,
    }
    history = list(row.routing_history or [])
    if prior_snapshot["file_answers"] or prior_snapshot["owner_answers"]:
        history.append(prior_snapshot)
    row.rules_version = application_prescreen.RULES_VERSION
    row.routing_history = history[-25:]
    row.file_answers = file_answers
    row.owner_answers = owner_answers
    await db.flush()
    state = await _application_pre_screen_state(db, dealer, owner_state)
    row.routing_result = state["routing_result"]
    row.self_report_routing_result = state["self_report_routing_result"]
    row.verified_routing_result = state["verified_routing_result"]
    if state["complete"]:
        row.completed_at = row.completed_at or datetime.now(timezone.utc)
        row.completed_by_user_id = user.id
        state["completed_at"] = row.completed_at
    else:
        row.completed_at = None
        row.completed_by_user_id = None
        state["completed_at"] = None
    await log_action(
        db,
        dealer.id,
        user,
        "application.pre_screen_updated",
        "application_pre_screen",
        entity_id=row.id,
        after={
            "complete": state["complete"],
            "required_owner_ids": state["required_owner_ids"],
            "rules_version": row.rules_version,
        },
    )
    await db.commit()
    return _pre_screen_read(state)


@router.get(
    "/dealers/{dealer_id}/underwriting-resolution",
    response_model=UnderwritingResolutionRead,
)
async def get_underwriting_resolution(
    dealer_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UnderwritingResolutionRead:
    """One Step 4 view of the current route, blockers, and drafted change.

    Reading this endpoint may persist a new recommendation when verified facts
    make the current amount or program unsupported. It never applies the draft.
    """

    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    state = await _application_pre_screen_state(db, dealer)
    profile = (
        await db.execute(
            select(DealerApplicationProfile).where(
                DealerApplicationProfile.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    recommendation, recommendation_created = await routing_resolution.ensure_recommendation(
        db,
        dealer,
        profile,
        state.get("routing_result"),
        user.id,
    )
    exceptions = list(
        (
            await db.execute(
                select(DealerProgramRuleResolution)
                .where(DealerProgramRuleResolution.dealer_id == dealer.id)
                .order_by(DealerProgramRuleResolution.created_at.desc())
            )
        ).scalars().all()
    )
    if recommendation is not None and recommendation_created:
        await log_action(
            db,
            dealer.id,
            user,
            "application.recommendation_drafted",
            "application_recommendation",
            entity_id=recommendation.id,
            after={
                "current_amount": recommendation.current_amount,
                "current_program": recommendation.current_program,
                "recommended_amount": recommendation.recommended_amount,
                "recommended_program": recommendation.recommended_program,
                "rules_version": recommendation.rules_version,
            },
        )
    await db.commit()
    programs = list((state.get("routing_result") or {}).get("programs") or [])
    direct_viable = any(row.get("status") in {"recommended", "potential"} for row in programs)
    return UnderwritingResolutionRead(
        rules_version=state["rules_version"],
        original_amount=float(dealer.client_requested_amount or dealer.funding_goal) if (dealer.client_requested_amount or dealer.funding_goal) is not None else None,
        working_amount=float(dealer.funding_goal) if dealer.funding_goal is not None else None,
        original_program=dealer.client_requested_program,
        working_program=profile.selected_program if profile else None,
        recommended=ApplicationRecommendationRead.model_validate(recommendation) if recommendation else None,
        programs=programs,
        blockers=routing_resolution.blockers(state.get("routing_result")),
        applicable_business_questions=state.get("applicable_business_questions", []),
        business_questions_complete=state.get("business_questions_complete", False),
        business_question_blockers=state.get("business_question_blockers", []),
        financial_suggestions=state.get("financial_suggestions", {}),
        exception_requests=[ProgramRuleResolutionRead.model_validate(row) for row in exceptions],
        direct_program_viable=direct_viable,
        signing_mode="program_package" if direct_viable else "qc_summary_booking",
    )


@router.post(
    "/dealers/{dealer_id}/application-recommendations/{recommendation_id}/respond",
    response_model=ApplicationRecommendationRead,
)
async def respond_to_application_recommendation(
    dealer_id: UUID,
    recommendation_id: UUID,
    payload: ApplicationRecommendationResponse,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerApplicationRecommendation:
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    row = (
        await db.execute(
            select(DealerApplicationRecommendation)
            .where(
                DealerApplicationRecommendation.id == recommendation_id,
                DealerApplicationRecommendation.dealer_id == dealer.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recommendation not found for this file")
    if row.status != "pending":
        return row

    profile = (
        await db.execute(
            select(DealerApplicationProfile).where(
                DealerApplicationProfile.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        profile = DealerApplicationProfile(dealer_id=dealer.id, updated_by_user_id=user.id)
        db.add(profile)
        await db.flush()

    applied_amount = payload.amount if payload.action == "edit" else (
        float(row.recommended_amount) if row.recommended_amount is not None else None
    )
    applied_program = payload.program_key if payload.action == "edit" else row.recommended_program
    if payload.action in {"apply", "edit"}:
        if applied_amount is None or not applied_program:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "A supported amount and program are required before applying the draft.",
            )
        if applied_program not in routing_resolution.PROGRAM_RANGES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose a supported direct program")
        active_envelopes = list(
            (
                await db.execute(
                    select(ContractEnvelope).where(
                        ContractEnvelope.dealer_id == dealer.id,
                        ContractEnvelope.status.notin_(["void"]),
                    )
                )
            ).scalars().all()
        )
        if any(envelope.status == "executed" for envelope in active_envelopes):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Executed documents are immutable. Keep the original for super-admin review.",
            )
        if any(envelope.status == "out_for_signature" for envelope in active_envelopes):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Void the sent signing package before applying a new amount or program.",
            )
        for envelope in active_envelopes:
            if envelope.status in {"draft", "ready", "failed"}:
                envelope.status = "void"
                envelope.voided_at = datetime.now(timezone.utc)
                envelope.voided_by_user_id = user.id
                envelope.void_reason = "Replaced by an acknowledged Step 4 routing recommendation."

        dealer.client_requested_amount = dealer.client_requested_amount or dealer.funding_goal
        dealer.client_requested_program = dealer.client_requested_program or profile.selected_program
        dealer.funding_goal = Decimal(str(applied_amount))
        profile.selected_program = applied_program
        profile.updated_by_user_id = user.id
        row.response_amount = Decimal(str(applied_amount))
        row.response_program = applied_program
        row.status = "applied" if payload.action == "apply" else "edited_and_applied"
    else:
        row.status = "kept_for_review"
    row.response_note = payload.note
    row.responded_by_user_id = user.id
    row.responded_at = datetime.now(timezone.utc)
    await log_action(
        db,
        dealer.id,
        user,
        "application.recommendation_responded",
        "application_recommendation",
        entity_id=row.id,
        after={
            "action": payload.action,
            "response_amount": row.response_amount,
            "response_program": row.response_program,
            "status": row.status,
            "rules_version": row.rules_version,
        },
    )
    await db.commit()
    await db.refresh(row)
    return row


@router.post(
    "/dealers/{dealer_id}/program-exceptions",
    response_model=ProgramRuleResolutionRead,
    status_code=status.HTTP_201_CREATED,
)
async def request_program_exception(
    dealer_id: UUID,
    payload: ProgramExceptionRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerProgramRuleResolution:
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    row = DealerProgramRuleResolution(
        dealer_id=dealer.id,
        program_key=payload.program_key,
        rule_key=payload.rule_key,
        kind=payload.kind,
        source=payload.source,
        current_value=payload.current_value,
        recommended_action=payload.recommended_action,
        status="requested",
        rep_note=payload.note,
        requested_by_user_id=user.id,
        requested_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.flush()
    await log_action(
        db, dealer.id, user, "application.exception_requested", "program_rule_resolution",
        entity_id=row.id, after=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(row)
    return row


@router.patch(
    "/dealers/{dealer_id}/program-exceptions/{resolution_id}",
    response_model=ProgramRuleResolutionRead,
)
async def decide_program_exception(
    dealer_id: UUID,
    resolution_id: UUID,
    payload: ProgramExceptionDecision,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerProgramRuleResolution:
    require_super_admin(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    row = (
        await db.execute(
            select(DealerProgramRuleResolution).where(
                DealerProgramRuleResolution.id == resolution_id,
                DealerProgramRuleResolution.dealer_id == dealer.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Exception request not found")
    row.status = payload.decision
    row.resolution_note = payload.note
    row.resolved_by_user_id = user.id
    row.resolved_at = datetime.now(timezone.utc)
    await log_action(
        db, dealer.id, user, f"application.exception_{payload.decision}", "program_rule_resolution",
        entity_id=row.id, after=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/dealers/{dealer_id}/owners", response_model=list[OwnerRead])
async def list_owners(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerOwner]:
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return list(
        (
            await db.execute(
                select(DealerOwner)
                .where(DealerOwner.dealer_id == dealer.id)
                .order_by(DealerOwner.created_at.asc(), DealerOwner.id.asc())
            )
        )
        .scalars()
        .all()
    )


def _primary_owner_conflict(requested_primary: bool, dealer_has_primary: bool) -> bool:
    """Pure (0125): a second primary owner is never allowed — is_primary marks
    the login's own person and there is exactly one of those per dealer."""
    return requested_primary and dealer_has_primary


@router.post("/dealers/{dealer_id}/owners", response_model=OwnerRead, status_code=status.HTTP_201_CREATED)
async def create_owner(
    dealer_id: UUID, body: OwnerCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerOwner:
    # Dealers may DISCLOSE owners on their own file (the >= 20% principals a
    # lender file requires); edits/deletes stay team-only.
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    await _lock_dealer_related_writes(db, dealer.id)
    owner_count = int(
        (
            await db.execute(
                select(func.count()).select_from(DealerOwner).where(
                    DealerOwner.dealer_id == dealer.id
                )
            )
        ).scalar_one()
    )
    if owner_count >= _MAX_OWNERS_PER_FILE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "A file may contain at most five owners",
        )
    await _assert_owner_email_unique(db, dealer.id, body.email)
    if body.is_primary:
        # 0125: at most ONE primary per dealer — is_primary marks the login's
        # own person, and there is only one of those.
        existing_primary = (
            await db.execute(
                select(DealerOwner.id)
                .where(DealerOwner.dealer_id == dealer.id, DealerOwner.is_primary.is_(True))
                .limit(1)
            )
        ).scalar_one_or_none()
        if _primary_owner_conflict(body.is_primary, existing_primary is not None):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "This client already has a primary owner",
            )
    values = body.model_dump()
    values["email"] = _normalized_owner_email(body.email)
    row = DealerOwner(dealer_id=dealer.id, **values)
    db.add(row)
    await log_action(db, dealer.id, user, "owner.create", "owner", after=body.model_dump(mode="json"))
    try:
        await db.commit()
    except IntegrityError:
        # uq_dos_owners_one_primary — a concurrent create won the primary slot.
        await db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "This client already has a primary owner"
        ) from None
    await db.refresh(row)
    return row


# 0125: the only owner field a DEALER login may PATCH. Everything else
# (identity, address, DOB, primary flag) stays team-curated so the consent
# and credit story is never rewritten from the client side.
_DEALER_PATCHABLE_OWNER_FIELDS = frozenset({"ownership_pct"})


def _dealer_owner_patch_violation(fields: set[str] | frozenset[str]) -> str | None:
    """Pure guard: which PATCHed fields a dealer is not allowed to touch.
    Returns a 422 detail string, or None when the patch is acceptable."""
    blocked = sorted(set(fields) - _DEALER_PATCHABLE_OWNER_FIELDS)
    if blocked:
        return "Clients may only update ownership_pct on an owner (not: " + ", ".join(blocked) + ")"
    return None


@router.patch("/dealers/{dealer_id}/owners/{owner_id}", response_model=OwnerRead)
async def patch_owner(
    dealer_id: UUID,
    owner_id: UUID,
    body: OwnerPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerOwner:
    # 0125: DEALER logins may now PATCH, but ONLY ownership_pct — the split
    # is a disclosure fact the client owns; everything else stays team-only.
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    await _lock_dealer_related_writes(db, dealer.id)
    row = (
        await db.execute(
            select(DealerOwner).where(DealerOwner.id == owner_id, DealerOwner.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Owner not found for this client")
    patch = body.model_dump(exclude_unset=True)
    if user.role == Role.DEALER:
        violation = _dealer_owner_patch_violation(set(patch))
        if violation is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, violation)
    if "email" in patch:
        patch["email"] = _normalized_owner_email(patch["email"])
        await _assert_owner_email_unique(
            db, dealer.id, patch["email"], exclude_owner_id=row.id
        )
    before_required = row.credit_required
    for k, v in patch.items():
        setattr(row, k, v)
    await log_action(
        db, dealer.id, user, "owner.update", "owner",
        entity_id=row.id, after=jsonable_encoder(patch),
    )
    if before_required != row.credit_required:
        await log_action(
            db,
            dealer.id,
            user,
            "owner.credit_threshold_changed",
            "owner",
            entity_id=row.id,
            after={"credit_required": row.credit_required, "ownership_pct": float(row.ownership_pct or 0)},
        )
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/dealers/{dealer_id}/owners/{owner_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_owner(
    dealer_id: UUID, owner_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> None:
    require_team(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    row = (
        await db.execute(
            select(DealerOwner).where(DealerOwner.id == owner_id, DealerOwner.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Owner not found for this client")
    if row.invite_sent_at is not None or row.credit_pulled_at is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This owner has credit authorization history and cannot be deleted",
        )
    await db.delete(row)
    await log_action(db, dealer.id, user, "owner.delete", "owner", entity_id=owner_id)
    await db.commit()


@router.get("/dealers/{dealer_id}/business-credit", response_model=BusinessCreditRead)
async def business_credit(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> BusinessCreditRead:
    """Business credit read off observed payment behaviour.

    No commercial bureau file is required to say something true about how this
    business pays — the ledger already shows every recurring obligation and
    whether it was met."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    rows, overrides, self_names = await _load_vendor_inputs(db, dealer)
    rolled, _ = _rollup_from_inputs(rows, overrides, self_names)
    nsf = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(DealerFinancialPeriod.nsf_count), 0))
                .where(DealerFinancialPeriod.dealer_id == dealer.id)
                .order_by()
            )
        ).scalar_one()
        or 0
    )
    summary = business_credit_svc.summarize(rolled, today=date.today(), nsf_6mo=nsf)

    # 0119: the tradeline rows behind the scalar summary — the SAME
    # select_tradelines predicate summarize() counts, so
    # len(tradeline_rows) == summary["tradelines"] always holds.
    tradelines = business_credit_svc.select_tradelines(rolled)
    # key -> {account_id: [count, |total|]} in ONE pass over outbound events.
    acct_stats: dict[str, dict[UUID | None, list[float]]] = {}
    for r in rows:
        amt = float(r.amount or 0)
        if amt >= 0:
            continue
        k = vendors.normalize_vendor(r.description or "")
        if not k:
            continue
        s = acct_stats.setdefault(k, {}).setdefault(r.account_id, [0, 0.0])
        s[0] += 1
        s[1] += abs(amt)

    def dominant_account(key: str) -> UUID | None:
        stats = acct_stats.get(key) or {}
        if not stats:
            return None
        return max(stats.items(), key=lambda kv: (kv[1][0], kv[1][1]))[0]

    dominants = {v.key: dominant_account(v.key) for v in tradelines}
    names = await _account_names(
        db, dealer.id, list({a for a in dominants.values() if a is not None})
    )
    tradeline_rows = [
        TradelineRead(
            vendor_key=v.key,
            sample_description=v.sample_description,
            category=v.category,
            monthly_payment=round(abs(v.monthly_average), 2),
            months=v.months,
            first_seen=v.first_seen,
            last_seen=v.last_seen,
            on_time_pct=business_credit_svc.on_time_pct_for(v),
            account_id=dominants.get(v.key),
            account_name=(
                names.get(dominants[v.key]) if dominants.get(v.key) is not None else None
            ),
        )
        for v in tradelines
    ]
    return BusinessCreditRead(**summary, tradeline_rows=tradeline_rows)


_OWNER_PULL_REQUIRED_FIELDS = ("dob", "street", "city", "state", "zip")

_ADDITIONAL_OWNER_CONSENT_DETAIL = (
    "Additional owners consent through a secure link your advisor shares with them directly."
)
_ALREADY_PULLED_DETAIL = (
    "Credit was already run for this owner — ask your advisor if a refresh is needed."
)


def _owner_missing_pull_fields(owner: object) -> list[str]:
    """Pure: which bureau-required owner fields are still empty."""
    return [
        f for f in _OWNER_PULL_REQUIRED_FIELDS if getattr(owner, f, None) in (None, "")
    ]


def _dealer_self_pull_violation(
    is_primary: bool, credit_pulled_at: datetime | None
) -> tuple[int, str] | None:
    """Pure guard for a DEALER-initiated pull (0125): the client may pull the
    PRIMARY owner (their own person) exactly once. Additional owners consent
    through their own secure link — never via the client on their behalf.
    Returns (http_status, detail) when blocked, None when allowed."""
    if not is_primary:
        return (status.HTTP_422_UNPROCESSABLE_ENTITY, _ADDITIONAL_OWNER_CONSENT_DETAIL)
    if credit_pulled_at is not None:
        return (status.HTTP_409_CONFLICT, _ALREADY_PULLED_DETAIL)
    return None


async def _run_owner_soft_pull(
    db: AsyncSession,
    dealer: DealerBusiness,
    owner: DealerOwner,
    consent_recorded_by: str,
    *,
    ssn: str | None = None,
    actor: User | None = None,
) -> SoftPullResult:
    """Shared soft-pull execution core (0125): field-completeness check, the
    credit_pull_core gateway call, and the summary echo onto the owner row.
    Both the authed endpoint and the public consent link run THIS one path.

    Callers must have validated FCRA consent already — consent_recorded_by
    names whose acknowledgement authorized the pull and lands in the audit
    trail. On gateway failure the transaction is rolled back and ok=False is
    returned; on success changes are FLUSHED, not committed — the caller owns
    the commit so it can bundle its own writes (e.g. token consumption)
    atomically with the pull echo."""
    missing = _owner_missing_pull_fields(owner)
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"The bureau needs these owner fields first: {', '.join(missing)}",
        )

    from app.services.credit_pull_core import (  # imported read-only
        SoftPullApplicant,
        SoftPullDenied,
        SoftPullUnavailable,
        run_soft_pull,
    )

    owner_pk = owner.id  # captured pre-rollback: expired instances can't lazy-load async
    async def _record_provider_failure(status_value: str, category: str, detail: str) -> SoftPullResult:
        await db.rollback()
        current = await db.get(DealerOwner, owner_pk)
        if current is not None:
            current.credit_workflow_status = status_value
            current.credit_provider_error_category = category[:48]
            current.credit_delivery_detail = detail[:240]
            await db.commit()
        return SoftPullResult(ok=False, detail=detail)

    client = await _resolve_owner_client(db, dealer, owner)
    try:
        pull = await run_soft_pull(
            db,
            client=client,
            applicant=SoftPullApplicant(
                legal_first_name=owner.first_name,
                legal_last_name=owner.last_name,
                dob=owner.dob,
                street=owner.street,
                city=owner.city,
                state=owner.state,
                zip=owner.zip,
                ssn=ssn,
            ),
            actor=actor,
        )
    except SoftPullUnavailable as exc:
        return await _record_provider_failure("provider_unavailable", "provider_unavailable", str(exc))
    except SoftPullDenied as exc:
        return await _record_provider_failure("declined", getattr(exc, "code", "bureau_denied"), str(exc))
    except Exception as exc:  # transport/validation — surfaced, never swallowed
        logger.exception("dealer-os: soft pull failed for owner %s", owner_pk)
        return await _record_provider_failure("failed", type(exc).__name__, f"Credit pull failed: {exc}")

    owner.credit_score = getattr(pull, "score", None)
    owner.credit_tier = _credit_tier(getattr(pull, "score", None))
    owner.credit_pulled_at = datetime.now(timezone.utc)
    owner.credit_pull_id = pull.id
    owner.credit_workflow_status = "completed"
    owner.credit_provider_error_category = None
    owner.credit_delivery_detail = None
    provider_id = re.search(r"(?:^|;)\s*isoftpull_id=([^;]+)", pull.notes or "")
    owner.credit_provider_request_id = provider_id.group(1).strip()[:120] if provider_id else None
    await log_action(
        db, dealer.id, actor, "owner.soft_pull", "owner",
        entity_id=owner.id,
        after={
            "score": owner.credit_score,
            "tier": owner.credit_tier,
            "consent_recorded_by": consent_recorded_by,
        },
    )
    return SoftPullResult(ok=True)


@router.post("/dealers/{dealer_id}/owners/{owner_id}/soft-pull", response_model=SoftPullResult)
async def owner_soft_pull(
    dealer_id: UUID,
    owner_id: UUID,
    body: SoftPullRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SoftPullResult:
    """Run a personal soft pull on one owner through the existing iSoftPull
    gateway (app.services.credit_pull_core), imported read-only.

    FCRA consent is a hard precondition — the gateway is never called without
    an explicit, recorded acknowledgement. Only the RESULT SUMMARY is echoed
    onto the owner row; the governed record stays in credit_pulls and no SSN
    is persisted here. A DEALER login may self-run ONLY the primary owner
    (their own person, is_primary) and only while no pull exists yet —
    additional owners consent through their own secure link (0125). Team
    initiators are unrestricted, as before.

    DELIBERATELY NOT open to a FIELD_REP. Every other read and write on a case
    was widened for the application workflow; this one was not. The workflow's
    credit step only ever SENDS an authorization for the applicant to complete
    themselves, so a rep has no need to call this, and the call asserts FCRA
    permissible-purpose consent on another person's behalf. That is a legal act
    and it should stay with the desk."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    await _require_training_live_action(
        db,
        dealer=dealer,
        user=user,
        request=request,
        action="Run soft credit pull",
        provider="iSoftPull",
        recipient=str(owner_id),
        effect="Submit the owner's information to the live credit provider.",
    )
    owner = (
        await db.execute(
            select(DealerOwner).where(DealerOwner.id == owner_id, DealerOwner.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Owner not found for this client")
    owner_state = await _owner_requirement_state(db, dealer.id)
    if not owner_state["ownership_complete"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Ownership must total 100.00% before a credit pull can run",
        )
    pre_screen = await _application_pre_screen_state(db, dealer, owner_state)
    if not pre_screen["complete"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Complete the Step 1 eligibility checkpoint before a credit pull can run",
        )
    if not owner.credit_required:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Credit is not required for an owner below 20% ownership",
        )
    if user.role == Role.DEALER:
        blocked = _dealer_self_pull_violation(owner.is_primary, owner.credit_pulled_at)
        if blocked is not None:
            raise HTTPException(blocked[0], blocked[1])
    if not body.fcra_consent:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "FCRA permissible-purpose consent is required before a credit pull",
        )
    result = await _run_owner_soft_pull(
        db, dealer, owner,
        consent_recorded_by=(user.name or user.email or str(user.id)),
        ssn=body.ssn,
        actor=user,
    )
    if not result.ok:
        return result
    await db.commit()
    await db.refresh(owner)
    return SoftPullResult(ok=True, owner=OwnerRead.model_validate(owner))


def _credit_tier(score: int | None) -> str | None:
    """Tier 1 / Tier 2 as the capital-path rules read them."""
    if score is None:
        return None
    if score >= 720:
        return "Tier 1"
    if score >= 660:
        return "Tier 2"
    return "Tier 3"


async def _resolve_owner_client(db: AsyncSession, dealer, owner) -> object:
    """run_soft_pull keys its record to a Client. Find or create the one that
    represents this owner so the pull lands against a stable subject rather
    than a throwaway row."""
    from app.models.client import Client

    if owner.email:
        existing = (
            await db.execute(
                select(Client).where(func.lower(Client.email) == owner.email.lower()).limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    client = Client(
        first_name=owner.first_name,
        last_name=owner.last_name,
        email=owner.email or f"owner-{owner.id}@dealer-os.local",
        phone=owner.phone,
    )
    db.add(client)
    await db.flush()
    return client


# --- Field-rep production (0130) ----------------------------------------------


def _town_key(dealer: DealerBusiness) -> str:
    city = (dealer.city or "").strip()
    state = (dealer.state or "").strip()
    label = ", ".join(part for part in (city, state) if part)
    return label or "Unknown town"


def _location_rows(
    rows: dict[str, dict[str, object]],
    *,
    approved_only: bool = False,
    limit: int = 6,
) -> list[RepLocationMetric]:
    ordered = sorted(
        rows.items(),
        key=lambda item: (
            -int(item[1].get("approved_or_fundable" if approved_only else "opened") or 0),
            -int(item[1].get("opened") or 0),
            item[0],
        ),
    )
    out: list[RepLocationMetric] = []
    for label, counts in ordered:
        approved = int(counts.get("approved_or_fundable") or 0)
        if approved_only and approved <= 0:
            continue
        out.append(
            RepLocationMetric(
                location=label,
                city=counts.get("city") if isinstance(counts.get("city"), str) else None,
                state=counts.get("state") if isinstance(counts.get("state"), str) else None,
                zip=counts.get("zip") if isinstance(counts.get("zip"), str) else None,
                opened=int(counts.get("opened") or 0),
                approved_or_fundable=approved,
            )
        )
        if len(out) >= limit:
            break
    return out


def _rep_production_access_scope(user: User) -> tuple[str, UUID | None]:
    """Resolve reporting scope before any production rows are queried."""
    require_team_or_rep(user)
    if user.role == Role.FIELD_REP:
        return "own", user.id
    return "firm", None


@router.get("/rep-production", response_model=RepProductionRead)
async def rep_production(
    user: CurrentUser,
    days: int = 90,
    db: AsyncSession = Depends(get_db),
) -> RepProductionRead:
    """What the field team has brought in, scoped to the viewer.

    Field reps see only files they own. Loan executives and super admins see
    firm-wide production. Ownership is read from DealerBusiness.owner_user_id
    rather than the pipeline table, so files opened before the pipeline existed
    still count — they simply carry no status.

    The number that matters most here is `with_documents`, not `files_opened`.
    A rep can open twenty files in an afternoon and none of them are production
    until a client actually sends something, so counting files alone rewards
    exactly the wrong behaviour.
    """
    scope, owner_user_id = _rep_production_access_scope(user)
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 730)))

    production_filters = [
        DealerBusiness.owner_user_id.is_not(None),
        DealerBusiness.created_at >= since,
        DealerBusiness.is_training.is_(False),
    ]
    if owner_user_id is not None:
        production_filters.append(DealerBusiness.owner_user_id == owner_user_id)

    rows = (
        await db.execute(
            select(DealerBusiness, DealerRepLead, User)
            .outerjoin(DealerRepLead, DealerRepLead.dealer_id == DealerBusiness.id)
            .outerjoin(User, User.id == DealerBusiness.owner_user_id)
            .where(*production_filters)
            .order_by(DealerBusiness.created_at.desc())
        )
    ).all()

    dealer_ids = [d.id for d, _, _ in rows]
    scores: dict[UUID, float | None] = {}
    doc_counts: dict[UUID, int] = {}
    statement_months: dict[UUID, set[str]] = {}
    if dealer_ids:
        # Latest snapshot score per dealer, one query.
        snap_rows = (
            await db.execute(
                select(DealerMetricSnapshot.dealer_id, DealerMetricSnapshot.score)
                .where(DealerMetricSnapshot.dealer_id.in_(dealer_ids))
                .order_by(DealerMetricSnapshot.dealer_id, DealerMetricSnapshot.created_at.desc())
                .distinct(DealerMetricSnapshot.dealer_id)
            )
        ).all()
        scores = {did: (float(sc) if sc is not None else None) for did, sc in snap_rows}
        doc_rows = (
            await db.execute(
                select(DealerDocument.dealer_id, func.count(DealerDocument.id))
                .where(DealerDocument.dealer_id.in_(dealer_ids))
                .group_by(DealerDocument.dealer_id)
            )
        ).all()
        doc_counts = {did: int(n) for did, n in doc_rows}
        period_rows = (
            await db.execute(
                select(
                    DealerFinancialPeriod.dealer_id,
                    DealerFinancialPeriod.period,
                    DealerFinancialPeriod.deposits,
                    DealerFinancialPeriod.withdrawals,
                ).where(DealerFinancialPeriod.dealer_id.in_(dealer_ids))
            )
        ).all()
        for did, period, deposits, withdrawals in period_rows:
            if deposits is not None or withdrawals is not None:
                statement_months.setdefault(did, set()).add(f"{period.year:04d}-{period.month:02d}")
        extracted_doc_rows = (
            await db.execute(
                select(
                    DealerDocument.dealer_id,
                    DealerDocument.content_type,
                    DealerDocument.plaid_item_id,
                    DealerDocument.kind,
                    DealerDocument.detected_kind,
                    DealerDocument.extracted,
                ).where(
                    DealerDocument.dealer_id.in_(dealer_ids),
                    DealerDocument.status == "extracted",
                )
            )
        ).all()
        for did, content_type, plaid_item_id, kind, detected_kind, extracted in extracted_doc_rows:
            effective = detected_kind or _KIND_TO_DETECTED.get(kind)
            if effective != "bank_statement":
                continue
            if plaid_item_id is None and str(content_type or "").lower() != "application/pdf":
                continue
            for m in (extracted or {}).get("months") or []:
                key = str(m.get("month") or "") if isinstance(m, dict) else ""
                if _COVERAGE_MONTH_RE.match(key):
                    statement_months.setdefault(did, set()).add(key)

        # The funnel's three verification facts, one query each rather than
        # per-file. A desk with four hundred files in the window would
        # otherwise fire twelve hundred queries to draw six bars.
        linked = {
            did
            for did, months in statement_months.items()
            if len(months) >= 3
            and not recurrence.compute_freshness(months, date.today(), window=3).get("missing_months")
        }
        pulled = {
            did
            for (did,) in (
                await db.execute(
                    select(DealerOwner.dealer_id)
                    .where(
                        DealerOwner.dealer_id.in_(dealer_ids),
                        DealerOwner.credit_pulled_at.is_not(None),
                    )
                    .distinct()
                )
            ).all()
        }
        # "Sent" is the audit trail, not a flag on the file: a rep who sent an
        # authorization and got nothing back is the single biggest drop in this
        # funnel, and it only shows if sending is counted separately from
        # returning.
        asked = {
            did
            for (did,) in (
                await db.execute(
                    select(DealerAuditLog.dealer_id)
                    .where(
                        DealerAuditLog.dealer_id.in_(dealer_ids),
                        DealerAuditLog.action.in_(
                            [
                                "client_request.bank_connect",
                                "client_request.bank_upload",
                                "owner.credit_invite",
                            ]
                        ),
                    )
                    .distinct()
                )
            ).all()
        }
    else:
        linked, pulled, asked = set(), set(), set()

    by_rep: dict[UUID | None, RepProduction] = {}
    requested_amounts: dict[UUID | None, list[float]] = {}
    approved_amounts: dict[UUID | None, list[float]] = {}
    approved_counts: dict[UUID | None, int] = {}
    approved_amount_sources: dict[UUID | None, dict[str, int]] = {}
    industry_totals: dict[UUID | None, dict[str, dict[str, int]]] = {}
    town_totals: dict[UUID | None, dict[str, dict[str, object]]] = {}
    zip_totals: dict[UUID | None, dict[str, dict[str, object]]] = {}
    for dealer, lead, rep in rows:
        key = dealer.owner_user_id
        if key not in by_rep:
            by_rep[key] = RepProduction(
                rep_user_id=key,
                rep_name=(rep.name if rep else None) or "Unassigned",
                rep_email=rep.email if rep else None,
            )
        bucket = by_rep[key]
        requested_amounts.setdefault(key, [])
        approved_amounts.setdefault(key, [])
        approved_counts.setdefault(key, 0)
        approved_amount_sources.setdefault(key, {})
        industry_totals.setdefault(key, {})
        town_totals.setdefault(key, {})
        zip_totals.setdefault(key, {})
        docs = doc_counts.get(dealer.id, 0)
        score = scores.get(dealer.id)
        status_val = lead.status if lead else None
        decision_val = lead.decision if lead else None
        is_approved_or_fundable = decision_val == "fundable" or status_val == "complete"
        if dealer.funding_goal is not None:
            try:
                goal_amount = float(dealer.funding_goal)
            except (TypeError, ValueError):
                goal_amount = None
            if goal_amount is not None and goal_amount > 0:
                requested_amounts[key].append(goal_amount)
                if is_approved_or_fundable:
                    approved_amounts[key].append(goal_amount)
                    approved_amount_sources[key]["dealer_funding_goal"] = (
                        approved_amount_sources[key].get("dealer_funding_goal", 0) + 1
                    )
        industry = (dealer.industry or "Uncategorized").strip() or "Uncategorized"
        industry_bucket = industry_totals[key].setdefault(
            industry, {"opened": 0, "approved_or_fundable": 0}
        )
        industry_bucket["opened"] += 1
        if is_approved_or_fundable:
            approved_counts[key] += 1
            industry_bucket["approved_or_fundable"] += 1

        town_label = _town_key(dealer)
        town_bucket = town_totals[key].setdefault(
            town_label,
            {
                "opened": 0,
                "approved_or_fundable": 0,
                "city": dealer.city,
                "state": dealer.state,
                "zip": None,
            },
        )
        town_bucket["opened"] = int(town_bucket["opened"]) + 1
        if is_approved_or_fundable:
            town_bucket["approved_or_fundable"] = int(town_bucket["approved_or_fundable"]) + 1

        zip_label = (dealer.zip or "").strip() or "Unknown ZIP"
        zip_bucket = zip_totals[key].setdefault(
            zip_label,
            {
                "opened": 0,
                "approved_or_fundable": 0,
                "city": None,
                "state": dealer.state,
                "zip": (dealer.zip or "").strip() or None,
            },
        )
        zip_bucket["opened"] = int(zip_bucket["opened"]) + 1
        if is_approved_or_fundable:
            zip_bucket["approved_or_fundable"] = int(zip_bucket["approved_or_fundable"]) + 1

        # Measured at the verification line rather than at file-open, which is
        # the whole point of the funnel: opening a file costs a rep nothing.
        bucket.funnel.opened += 1
        is_linked = dealer.id in linked
        is_pulled = dealer.id in pulled
        if dealer.id in asked or is_linked or is_pulled:
            bucket.funnel.authorizations_sent += 1
        if is_linked:
            bucket.funnel.bank_linked += 1
        if is_pulled:
            bucket.funnel.credit_returned += 1
        if is_linked and is_pulled:
            bucket.funnel.verified += 1
        if status_val in ("forms_out", "signed", "complete"):
            bucket.funnel.application_submitted += 1
        if status_val in ("signed", "complete"):
            bucket.funnel.contract_executed += 1

        bucket.files.append(
            RepFileRow(
                dealer_id=dealer.id,
                name=dealer.name,
                city=dealer.city,
                state=dealer.state,
                industry=dealer.industry,
                status=status_val,
                decision=decision_val,
                score=score,
                documents=docs,
                created_at=dealer.created_at,
                last_activity=dealer.updated_at,
            )
        )
        bucket.files_opened += 1
        if docs > 0:
            bucket.with_documents += 1
        if status_val in REP_LEAD_TERMINAL:
            if status_val == "complete":
                bucket.complete += 1
            elif status_val == "declined":
                bucket.declined += 1
            else:
                bucket.stalled += 1
        else:
            bucket.active += 1
        if decision_val == "fundable":
            bucket.fundable += 1
        if bucket.last_activity is None or (
            dealer.updated_at and dealer.updated_at > bucket.last_activity
        ):
            bucket.last_activity = dealer.updated_at

    for bucket in by_rep.values():
        seen = [f.score for f in bucket.files if f.score is not None]
        bucket.avg_score = round(sum(seen) / len(seen), 1) if seen else None
        bucket.files.sort(key=lambda f: f.created_at, reverse=True)
        key = bucket.rep_user_id
        requested = requested_amounts.get(key, [])
        approved = approved_amounts.get(key, [])
        approved_or_fundable = approved_counts.get(key, 0)
        sources = approved_amount_sources.get(key, {})
        industries = industry_totals.get(key, {})
        towns = town_totals.get(key, {})
        zips = zip_totals.get(key, {})
        source_label = "none"
        if sources:
            source_label = "mixed" if len(sources) > 1 else next(iter(sources))
        bucket.insights = RepProductionInsights(
            underwriting_ready=bucket.funnel.verified,
            approved_or_fundable=approved_or_fundable,
            underwriting_ready_ratio=(
                round(bucket.funnel.verified / bucket.files_opened * 100, 1)
                if bucket.files_opened
                else None
            ),
            approved_or_fundable_ratio=(
                round(approved_or_fundable / bucket.files_opened * 100, 1)
                if bucket.files_opened
                else None
            ),
            document_ratio=(
                round(bucket.with_documents / bucket.files_opened * 100, 1)
                if bucket.files_opened
                else None
            ),
            contract_execution_ratio=(
                round(bucket.funnel.contract_executed / bucket.files_opened * 100, 1)
                if bucket.files_opened
                else None
            ),
            amount_metrics=RepAmountMetric(
                average_requested=round(sum(requested) / len(requested), 2) if requested else None,
                average_approved=round(sum(approved) / len(approved), 2) if approved else None,
                approved_amount_source=source_label,
                approved_amount_source_counts=sources,
            ),
            top_new_app_industries=[
                RepCategoryMetric(
                    industry=name,
                    opened=counts["opened"],
                    approved_or_fundable=counts["approved_or_fundable"],
                )
                for name, counts in sorted(
                    industries.items(), key=lambda item: (-item[1]["opened"], item[0])
                )[:6]
            ],
            top_approved_industries=[
                RepCategoryMetric(
                    industry=name,
                    opened=counts["opened"],
                    approved_or_fundable=counts["approved_or_fundable"],
                )
                for name, counts in sorted(
                    industries.items(),
                    key=lambda item: (-item[1]["approved_or_fundable"], -item[1]["opened"], item[0]),
                )
                if counts["approved_or_fundable"] > 0
            ][:6],
            top_new_app_towns=_location_rows(towns),
            top_approved_towns=_location_rows(towns, approved_only=True),
            top_new_app_zip_codes=_location_rows(zips),
            top_approved_zip_codes=_location_rows(zips, approved_only=True),
        )

    reps = sorted(by_rep.values(), key=lambda r: (-r.files_opened, r.rep_name))

    totals = RepProduction(
        rep_name="All reps",
        files_opened=sum(r.files_opened for r in reps),
        active=sum(r.active for r in reps),
        complete=sum(r.complete for r in reps),
        declined=sum(r.declined for r in reps),
        stalled=sum(r.stalled for r in reps),
        with_documents=sum(r.with_documents for r in reps),
        fundable=sum(r.fundable for r in reps),
    )
    for stage in (
        "opened",
        "authorizations_sent",
        "bank_linked",
        "credit_returned",
        "verified",
        "application_submitted",
        "contract_executed",
    ):
        setattr(totals.funnel, stage, sum(getattr(r.funnel, stage) for r in reps))
    all_scores = [f.score for r in reps for f in r.files if f.score is not None]
    totals.avg_score = round(sum(all_scores) / len(all_scores), 1) if all_scores else None
    totals.last_activity = max((r.last_activity for r in reps if r.last_activity), default=None)
    all_requested = [amount for values in requested_amounts.values() for amount in values]
    all_approved = [amount for values in approved_amounts.values() for amount in values]
    all_sources: dict[str, int] = {}
    for source_counts in approved_amount_sources.values():
        for source, count in source_counts.items():
            all_sources[source] = all_sources.get(source, 0) + count
    total_industries: dict[str, dict[str, int]] = {}
    for per_rep in industry_totals.values():
        for name, counts in per_rep.items():
            target = total_industries.setdefault(name, {"opened": 0, "approved_or_fundable": 0})
            target["opened"] += counts["opened"]
            target["approved_or_fundable"] += counts["approved_or_fundable"]
    total_towns: dict[str, dict[str, object]] = {}
    for per_rep in town_totals.values():
        for label, counts in per_rep.items():
            target = total_towns.setdefault(
                label,
                {
                    "opened": 0,
                    "approved_or_fundable": 0,
                    "city": counts.get("city"),
                    "state": counts.get("state"),
                    "zip": None,
                },
            )
            target["opened"] = int(target["opened"]) + int(counts.get("opened") or 0)
            target["approved_or_fundable"] = int(target["approved_or_fundable"]) + int(
                counts.get("approved_or_fundable") or 0
            )
    total_zips: dict[str, dict[str, object]] = {}
    for per_rep in zip_totals.values():
        for label, counts in per_rep.items():
            target = total_zips.setdefault(
                label,
                {
                    "opened": 0,
                    "approved_or_fundable": 0,
                    "city": None,
                    "state": counts.get("state"),
                    "zip": counts.get("zip"),
                },
            )
            target["opened"] = int(target["opened"]) + int(counts.get("opened") or 0)
            target["approved_or_fundable"] = int(target["approved_or_fundable"]) + int(
                counts.get("approved_or_fundable") or 0
            )
    total_approved_or_fundable = sum(approved_counts.values())
    totals.insights = RepProductionInsights(
        underwriting_ready=totals.funnel.verified,
        approved_or_fundable=total_approved_or_fundable,
        underwriting_ready_ratio=(
            round(totals.funnel.verified / totals.files_opened * 100, 1)
            if totals.files_opened
            else None
        ),
        approved_or_fundable_ratio=(
            round(total_approved_or_fundable / totals.files_opened * 100, 1)
            if totals.files_opened
            else None
        ),
        document_ratio=(
            round(totals.with_documents / totals.files_opened * 100, 1)
            if totals.files_opened
            else None
        ),
        contract_execution_ratio=(
            round(totals.funnel.contract_executed / totals.files_opened * 100, 1)
            if totals.files_opened
            else None
        ),
        amount_metrics=RepAmountMetric(
            average_requested=round(sum(all_requested) / len(all_requested), 2)
            if all_requested
            else None,
            average_approved=round(sum(all_approved) / len(all_approved), 2)
            if all_approved
            else None,
            approved_amount_source=(
                "mixed" if len(all_sources) > 1 else (next(iter(all_sources)) if all_sources else "none")
            ),
            approved_amount_source_counts=all_sources,
        ),
        top_new_app_industries=[
            RepCategoryMetric(
                industry=name,
                opened=counts["opened"],
                approved_or_fundable=counts["approved_or_fundable"],
            )
            for name, counts in sorted(
                total_industries.items(), key=lambda item: (-item[1]["opened"], item[0])
            )[:6]
        ],
        top_approved_industries=[
            RepCategoryMetric(
                industry=name,
                opened=counts["opened"],
                approved_or_fundable=counts["approved_or_fundable"],
            )
            for name, counts in sorted(
                total_industries.items(),
                key=lambda item: (-item[1]["approved_or_fundable"], -item[1]["opened"], item[0]),
            )
            if counts["approved_or_fundable"] > 0
        ][:6],
        top_new_app_towns=_location_rows(total_towns),
        top_approved_towns=_location_rows(total_towns, approved_only=True),
        top_new_app_zip_codes=_location_rows(total_zips),
        top_approved_zip_codes=_location_rows(total_zips, approved_only=True),
    )

    return RepProductionRead(scope=scope, since=since, totals=totals, reps=reps)


# --- Owner credit-consent invites (0125) --------------------------------------
# Consent for a pull must come from the person the pull is ABOUT. The primary
# owner (the login's own person) consents in-app; every ADDITIONAL owner gets a
# one-time secure link minted by the super admin and shared with that owner
# directly. Only the sha256 of the token is stored — the plaintext exists
# exactly once, in the mint response.


def _hash_invite_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _credit_score_band(score: int | None) -> str | None:
    """Pure: 50-point band for the public page — e.g. 712 -> "700–749".
    The exact score never renders on an unauthenticated page."""
    if score is None:
        return None
    lo = (int(score) // 50) * 50
    return f"{lo}–{lo + 49}"


async def _resolve_consent_owner(db: AsyncSession, token: str) -> DealerOwner:
    """Owner row for a live consent token — 404 on unknown/consumed (a used
    token has its hash NULLed, so it stops matching the moment it's spent)."""
    if not token:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This consent link is no longer valid")
    owner = (
        await db.execute(
            select(DealerOwner).where(DealerOwner.invite_token_hash == _hash_invite_token(token))
        )
    ).scalar_one_or_none()
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This consent link is no longer valid")
    return owner


@router.post(
    "/dealers/{dealer_id}/owners/{owner_id}/credit-invite", response_model=CreditInviteResult
)
async def owner_credit_invite(
    dealer_id: UUID,
    owner_id: UUID,
    request: Request,
    user: CurrentUser,
    payload: CreditInviteRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> CreditInviteResult:
    """Mint the one-time credit-consent link for an owner, and optionally send it.

    The plaintext token is returned ONCE here; only its sha256 is stored, and
    re-minting kills the previous link instantly.

    Open to the owning field rep as well as the team. A rep sitting with a
    business owner is exactly who needs this: consent has to come from the
    person the pull is about, so the rep texts or emails them a link they open
    on their own phone rather than the rep ever handling their details.
    resolve_dealer_scope confines a rep to their own files."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    owner = (
        await db.execute(
            select(DealerOwner).where(DealerOwner.id == owner_id, DealerOwner.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Owner not found for this client")
    await _require_training_live_action(
        db,
        dealer=dealer,
        user=user,
        request=request,
        action="Send credit authorization",
        provider="SES / SMS / iSoftPull",
        recipient=owner.email or owner.phone,
        effect="Create and optionally deliver a live credit-consent link.",
    )
    owner_state = await _owner_requirement_state(db, dealer.id)
    if not owner_state["ownership_complete"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Ownership must total 100.00% before credit links are sent; current total is {owner_state['ownership_total']:.2f}%",
        )
    pre_screen = await _application_pre_screen_state(db, dealer, owner_state)
    if not pre_screen["complete"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Complete the Step 1 eligibility checkpoint before sending credit authorizations",
        )
    if not owner.credit_required:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Credit authorization is required only for owners with 20% or more ownership",
        )
    if not _normalized_owner_email(owner.email):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "This owner needs an email before a credit authorization can be sent",
        )
    if not consent_delivery.normalize_phone(owner.phone):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "This owner needs a valid personal phone before a credit authorization can be sent",
        )
    token = secrets.token_urlsafe(32)
    owner.invite_token_hash = _hash_invite_token(token)
    owner.invite_sent_at = datetime.now(timezone.utc)
    owner.invite_opened_at = None  # a fresh link has not been opened yet
    owner.credit_workflow_status = "link_created"
    owner.credit_delivery_detail = None
    await log_action(
        db, dealer.id, user, "owner.credit_invite", "owner",
        entity_id=owner.id, after={"invite_sent_at": owner.invite_sent_at.isoformat()},
    )
    await db.commit()

    path = f"/credit-consent#t={token}"
    req = payload or CreditInviteRequest()
    if req.channel == "none":
        return CreditInviteResult(token=token, path=path)

    # Delivery is best-effort and reported honestly. The token is already
    # minted and valid, so a failed send is recoverable by reading the link
    # out; silently claiming success is not.
    delivery = await consent_delivery.deliver_link_checked(
        db,
        channel=req.channel,
        to_email=owner.email,
        to_phone=owner.phone,
        business_name=dealer.name,
        purpose="authorise a soft credit check",
        path=path,
        rep_name=user.name,
    )
    await log_action(
        db,
        dealer.id,
        user,
        "owner.credit_invite_delivery",
        "owner",
        entity_id=owner.id,
        after={
            "delivered": delivery.ok,
            "channel": delivery.channel,
            "recipient": owner.email,
        },
    )


    owner.credit_workflow_status = "sent" if delivery.ok else "delivery_failed"
    owner.credit_delivery_detail = (delivery.detail or "")[:240] or None
    await db.commit()
    return CreditInviteResult(
        token=token,
        path=path,
        delivered=delivery.ok,
        channel=delivery.channel,
        detail=delivery.detail,
    )


@router.post(
    "/dealers/{dealer_id}/owners/credit-invites",
    response_model=BulkCreditInviteResult,
)
async def bulk_owner_credit_invites(
    dealer_id: UUID,
    payload: BulkCreditInviteRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BulkCreditInviteResult:
    """Send one isolated consent link to every required owner still pending."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    await _require_training_live_action(
        db,
        dealer=dealer,
        user=user,
        request=request,
        action="Send all credit authorizations",
        provider="SES / SMS / iSoftPull",
        recipient=dealer.name,
        effect="Create and deliver live credit-consent links to every pending required owner.",
    )
    owner_state = await _owner_requirement_state(db, dealer.id)
    if not owner_state["ownership_complete"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Ownership must total 100.00% before credit links are sent; current total is {owner_state['ownership_total']:.2f}%",
        )
    pre_screen = await _application_pre_screen_state(db, dealer, owner_state)
    if not pre_screen["complete"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Complete the Step 1 eligibility checkpoint before sending credit authorizations",
        )
    if owner_state["missing_contact"]:
        names = ", ".join(owner.full_name for owner in owner_state["missing_contact"])
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Add a valid personal email and phone for every required owner before sending: {names}",
        )

    results: list[OwnerCreditInviteResult] = []
    for owner in owner_state["pending"]:
        try:
            sent = await owner_credit_invite(
                dealer_id=dealer_id,
                owner_id=owner.id,
                request=request,
                user=user,
                payload=CreditInviteRequest(channel=payload.channel),
                db=db,
            )
            results.append(
                OwnerCreditInviteResult(
                    owner_id=owner.id,
                    owner_name=owner.full_name,
                    token=sent.token,
                    path=sent.path,
                    delivered=sent.delivered,
                    channel=sent.channel,
                    detail=sent.detail,
                )
            )
        except HTTPException as exc:
            results.append(
                OwnerCreditInviteResult(
                    owner_id=owner.id,
                    owner_name=owner.full_name,
                    detail=str(exc.detail),
                )
            )
    return BulkCreditInviteResult(items=results)


@router.get("/public/credit-consent/{token}", response_model=PublicConsentView)
async def public_credit_consent_view(
    token: str, db: AsyncSession = Depends(get_db)
) -> PublicConsentView:
    """PUBLIC (no auth — the unguessable one-time token IS the credential).
    Shows the owner just enough to recognize themself and the business, plus
    which bureau-required fields we still need. First open stamps
    invite_opened_at so the advisor can see the link landed."""
    owner = await _resolve_consent_owner(db, token)
    dealer = await db.get(DealerBusiness, owner.dealer_id)
    if owner.invite_opened_at is None:
        owner.invite_opened_at = datetime.now(timezone.utc)
        owner.credit_workflow_status = "opened"
        await db.commit()
    return PublicConsentView(
        first_name=owner.first_name,
        last_name=owner.last_name,
        last_initial=(owner.last_name or "")[:1],
        email=owner.email or "",
        phone=owner.phone or "",
        dealer_name=dealer.name if dealer is not None else "",
        fields_needed=_owner_missing_pull_fields(owner),
        completed=owner.credit_pulled_at is not None,
        # The short-lived, high-entropy, one-time owner token is the consent
        # credential. A separate document-room passcode is unrelated and made
        # emailed owner links impossible to complete independently.
        requires_code=False,
    )


@router.post("/public/credit-consent/{token}", response_model=PublicConsentResult)
async def public_credit_consent_submit(
    token: str, body: PublicConsentSubmit, db: AsyncSession = Depends(get_db)
) -> PublicConsentResult:
    """PUBLIC consent submission: the owner acknowledges FCRA permissible
    purpose themself, fills ONLY the fields we're missing (never overwrites
    what the advisor already has), and the SAME soft-pull gateway path runs.
    Success consumes the token; the response is tier + a 50-point band only —
    never the exact score, never the pull summary."""
    # ATOMIC consume-first: two concurrent submits with the same token must
    # never both reach the bureau. The UPDATE ... RETURNING claims the token;
    # the loser sees zero rows and gets the dead-link 404. On gateway failure
    # the hash is restored so the owner can retry.
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    claimed = (
        await db.execute(
            sa_update(DealerOwner)
            .where(DealerOwner.invite_token_hash == token_hash)
            .values(invite_token_hash=None)
            .returning(DealerOwner.id)
        )
    ).scalar_one_or_none()
    if claimed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This consent link is no longer valid")
    owner = await db.get(DealerOwner, claimed)

    async def _release_token() -> None:
        owner.invite_token_hash = token_hash
        await db.commit()

    if owner.credit_pulled_at is not None:
        await db.commit()  # keep the token consumed — the work is done
        raise HTTPException(status.HTTP_409_CONFLICT, _ALREADY_PULLED_DETAIL)
    if not body.fcra_consent:
        owner.credit_workflow_status = "declined"
        owner.credit_provider_error_category = "consent_declined"
        await _release_token()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "FCRA permissible-purpose consent is required before a credit pull",
        )
    dealer = await db.get(DealerBusiness, owner.dealer_id)
    if dealer is None:  # orphaned row — treat like a dead link, not a 500
        await db.commit()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This consent link is no longer valid")
    owner_state = await _owner_requirement_state(db, dealer.id)
    if not owner_state["ownership_complete"]:
        await _release_token()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The ownership schedule is being updated. Ask your representative before continuing.",
        )
    if not owner.credit_required:
        await db.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This owner no longer requires a credit authorization for this file.",
        )
    first_name = body.first_name.strip()
    last_name = body.last_name.strip()
    email = _normalized_owner_email(str(body.email))
    phone = consent_delivery.normalize_phone(body.phone)
    if not first_name or not last_name or email is None or phone is None:
        await _release_token()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Confirm your first name, last name, personal email, and valid phone number",
        )
    try:
        await _assert_owner_email_unique(
            db,
            dealer.id,
            email,
            exclude_owner_id=owner.id,
        )
    except HTTPException:
        await _release_token()
        raise
    owner.first_name = first_name
    owner.last_name = last_name
    owner.email = email
    owner.phone = phone
    for f in _OWNER_PULL_REQUIRED_FIELDS:
        value = getattr(body, f)
        if value is not None and getattr(owner, f, None) in (None, ""):
            setattr(owner, f, value)
    result = await _run_owner_soft_pull(
        db, dealer, owner,
        consent_recorded_by=f"consent-link:{owner.first_name} {owner.last_name}",
        actor=None,  # audit rows record the system actor; the token is the consent trail
    )
    if not result.ok:
        # FIX: never surface raw gateway/bureau internals on a public page —
        # log server-side, hand the owner a fixed, actionable message. The
        # token is restored so they can retry once things recover.
        logger.warning("public consent pull failed for owner %s: %s", owner.id, result.detail)
        await _release_token()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "We couldn't complete the credit check right now — please try again "
            "shortly, or your advisor will follow up.",
        )
    await db.commit()
    await db.refresh(owner)
    return PublicConsentResult(
        credit_tier=owner.credit_tier,
        credit_score_band=_credit_score_band(owner.credit_score),
        completed=True,
    )


# --- Desk admin (0120): program settings, groups, payment timing & shifts -----


def _program_setting_read(
    path_key: str, row: DealerProgramSetting | None, by_name: str | None
) -> ProgramSettingRead:
    return ProgramSettingRead(
        path_key=path_key,
        label=PATH_LABELS[path_key],
        model=path_model(path_key),
        sizing_default=DEFAULT_SIZING.get(path_key),
        sizing_override=row.sizing if row is not None else None,
        requirements_default=DEFAULT_REQUIREMENTS.get(path_key, []),
        requirements_override=row.requirements if row is not None else None,
        approved_at=row.approved_at if row is not None else None,
        updated_by_name=by_name,
    )


@router.get("/program-settings", response_model=ProgramSettingsRead)
async def list_program_settings(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ProgramSettingsRead:
    """Every program: code defaults side by side with the desk override.
    Team-readable; writes are super-admin only."""
    require_team(user)
    rows = {
        r.path_key: r
        for r in (await db.execute(select(DealerProgramSetting))).scalars()
    }
    editor_ids = [r.updated_by_user_id for r in rows.values() if r.updated_by_user_id is not None]
    names: dict[UUID, str] = {}
    if editor_ids:
        names = {
            uid: (name or email)
            for uid, name, email in (
                await db.execute(
                    select(User.id, User.name, User.email).where(User.id.in_(editor_ids))
                )
            ).all()
        }
    programs = [
        _program_setting_read(
            key,
            rows.get(key),
            names.get(rows[key].updated_by_user_id) if key in rows else None,
        )
        for key in PATH_KEYS
    ]
    return ProgramSettingsRead(programs=programs)


@router.put("/program-settings/{path_key}", response_model=ProgramSettingRead)
async def update_program_setting(
    path_key: str,
    payload: ProgramSettingUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProgramSettingRead:
    """Desk-approve a program override (super-admin only). Each field present
    in the body replaces the stored override WHOLESALE (never a deep merge);
    an explicit null clears that field back to the code default. 422 on any
    shape violation — collateral paths accept requirements but never sizing."""
    require_super_admin(user)
    if path_key not in PATH_KEYS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown program")
    fields = payload.model_dump(exclude_unset=True)
    try:
        if fields.get("sizing") is not None:
            fields["sizing"] = validate_sizing(path_key, fields["sizing"])
        if fields.get("requirements") is not None:
            fields["requirements"] = validate_requirements(fields["requirements"])
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    row = (
        await db.execute(
            select(DealerProgramSetting).where(DealerProgramSetting.path_key == path_key)
        )
    ).scalar_one_or_none()
    if row is None:
        row = DealerProgramSetting(path_key=path_key)
        db.add(row)
        try:
            await db.flush()
        except IntegrityError:
            # Concurrent first write for the same program — take the winner's row.
            await db.rollback()
            row = (
                await db.execute(
                    select(DealerProgramSetting).where(
                        DealerProgramSetting.path_key == path_key
                    )
                )
            ).scalar_one()
    if "sizing" in fields:
        row.sizing = fields["sizing"]
    if "requirements" in fields:
        row.requirements = fields["requirements"]
    now = datetime.now(timezone.utc)
    row.approved_at = now
    row.updated_by_user_id = user.id
    by_name = (user.name or user.email or "")[:120]
    # The row carries its own change history (dos_audit_log is dealer-scoped
    # and cannot record global actions) — capped at the last 20 approvals.
    entry = {
        "at": now.isoformat(),
        "by_name": by_name,
        "sizing": row.sizing,
        "requirements": row.requirements,
    }
    row.history = ((row.history or []) + [entry])[-20:]
    await db.commit()
    await db.refresh(row)
    return _program_setting_read(path_key, row, by_name or None)


@router.delete("/program-settings/{path_key}", status_code=status.HTTP_204_NO_CONTENT)
async def reset_program_setting(
    path_key: str, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> None:
    """Reset one program to the code defaults (super-admin only). Idempotent —
    resetting an untouched program is already a 204."""
    require_super_admin(user)
    if path_key not in PATH_KEYS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown program")
    row = (
        await db.execute(
            select(DealerProgramSetting).where(DealerProgramSetting.path_key == path_key)
        )
    ).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()


# --- Dealer groups (client files) ---------------------------------------------


@router.get("/groups", response_model=list[GroupRead])
async def list_groups(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[GroupRead]:
    require_team(user)
    rows = (
        await db.execute(
            select(DealerGroup, func.count(DealerBusiness.id))
            .outerjoin(DealerBusiness, DealerBusiness.group_id == DealerGroup.id)
            .group_by(DealerGroup.id)
            .order_by(DealerGroup.name.asc())
        )
    ).all()
    return [GroupRead(id=g.id, name=g.name, member_count=int(count)) for g, count in rows]


def _clean_group_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Group name cannot be blank")
    return cleaned


@router.post("/groups", response_model=GroupRead, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: GroupCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> GroupRead:
    require_team(user)
    group = DealerGroup(name=_clean_group_name(payload.name))
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return GroupRead(id=group.id, name=group.name, member_count=0)


@router.patch("/groups/{group_id}", response_model=GroupRead)
async def rename_group(
    group_id: UUID, payload: GroupPatch, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> GroupRead:
    require_team(user)
    group = await _require_group(db, group_id)
    group.name = _clean_group_name(payload.name)
    await db.commit()
    await db.refresh(group)
    member_count = (
        await db.execute(
            select(func.count())
            .select_from(DealerBusiness)
            .where(DealerBusiness.group_id == group.id)
        )
    ).scalar_one()
    return GroupRead(id=group.id, name=group.name, member_count=int(member_count))


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> None:
    require_team(user)
    group = await _require_group(db, group_id)
    member_count = (
        await db.execute(
            select(func.count())
            .select_from(DealerBusiness)
            .where(DealerBusiness.group_id == group.id)
        )
    ).scalar_one()
    if member_count:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Group still has member businesses — detach them first",
        )
    await db.delete(group)
    await db.commit()


# --- Payment timing & shifts --------------------------------------------------

_TIMING_WINDOW_MONTHS = 6
_TIMING_WINDOW_DAYS = 183
_TIMING_EVENT_CAP = 5000


@router.get("/dealers/{dealer_id}/payment-timing", response_model=PaymentTimingRead)
async def dealer_payment_timing(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> PaymentTimingRead:
    """When in the month money actually moves (trailing 6 months): per-day
    in/out totals, the big outflow days with their dominant vendors, and the
    recurring/debt-like vendors that are payment-shift candidates."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    # One ledger load feeds both the full-history rollup (vendor identity,
    # cadence, debt flags) and the windowed day-of-month analysis.
    rows, overrides, self_names = await _load_vendor_inputs(db, dealer)
    rolled, _ = _rollup_from_inputs(rows, overrides, self_names)
    cutoff = date.today() - timedelta(days=_TIMING_WINDOW_DAYS)
    windowed = sorted(
        (r for r in rows if r.occurred_on >= cutoff),
        key=lambda r: r.occurred_on,
        reverse=True,
    )[:_TIMING_EVENT_CAP]
    # Statement cutoffs read the FULL ledger, not the 6-month window: the
    # cutoff is a stable property of the account's statement cycle and every
    # observed month sharpens the median.
    cutoffs = payment_timing.cutoff_days(rows)
    names = await _account_names(
        db, dealer.id, [c["account_id"] for c in cutoffs if c["account_id"] is not None]
    )
    for c in cutoffs:
        c["account_name"] = names.get(c["account_id"])
    manual_keys = {
        vendors.normalize_vendor(r.description or "")
        for r in rows
        if isinstance(r.flags, dict) and r.flags.get("manual_recurrence") == "recurring"
    } - {""}
    return PaymentTimingRead(
        **payment_timing.analyze_timing(
            windowed, rolled, months_window=_TIMING_WINDOW_MONTHS,
            force_recurring_keys=manual_keys or None,
        ),
        cutoffs=cutoffs,
    )


@router.get("/dealers/{dealer_id}/timing/optimize", response_model=TimingOptimizeRead)
async def optimize_timing(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> TimingOptimizeRead:
    """Deterministic draft of the best payment/deposit timing moves (0121):
    early-month outflows pushed to just before the earliest statement cutoff,
    tightly-clustered recurring deposits pulled earlier. Team-only, read-only
    — persists NOTHING; each row is a candidate dos_payment_shift the desk
    can stage explicitly. Moves already on the dealer's table (any status)
    are never re-proposed."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    rows, overrides, self_names = await _load_vendor_inputs(db, dealer)
    rolled, _ = _rollup_from_inputs(rows, overrides, self_names)
    window_start = date.today() - timedelta(days=_TIMING_WINDOW_DAYS)
    windowed = sorted(
        (r for r in rows if r.occurred_on >= window_start),
        key=lambda r: r.occurred_on,
        reverse=True,
    )[:_TIMING_EVENT_CAP]
    timing = payment_timing.analyze_timing(
        windowed, rolled, months_window=_TIMING_WINDOW_MONTHS
    )
    existing = (
        (
            await db.execute(
                select(DealerPaymentShift).where(DealerPaymentShift.dealer_id == dealer.id)
            )
        )
        .scalars()
        .all()
    )
    staged_keys = frozenset(
        timing_optimizer.shift_key(s.direction, s.vendor_key, s.label) for s in existing
    )
    drafts = timing_optimizer.draft_optimized_shifts(
        timing["recurring"], payment_timing.cutoff_days(rows), staged_keys=staged_keys
    )
    return TimingOptimizeRead(
        shifts=drafts,
        total_est_adb=round(sum(d["est_adb_impact"] for d in drafts), 2),
        computed_at=datetime.now(timezone.utc),
    )


def _shift_estimate(
    monthly_amount, from_day: int, to_day: int, direction: str = "out"
) -> float | None:
    if monthly_amount is None:
        return None
    return payment_timing.adb_impact(
        float(monthly_amount), from_day, to_day, direction=direction
    )


async def _load_shift(db: AsyncSession, dealer_id: UUID, shift_id: UUID) -> DealerPaymentShift:
    shift = await db.get(DealerPaymentShift, shift_id)
    if shift is None or shift.dealer_id != dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment shift not found")
    return shift


@router.get("/dealers/{dealer_id}/payment-shifts", response_model=list[PaymentShiftRead])
async def list_payment_shifts(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerPaymentShift]:
    """Team sees the whole pipeline; a DEALER login never sees drafts."""
    require_team_or_dealer_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    q = select(DealerPaymentShift).where(DealerPaymentShift.dealer_id == dealer.id)
    if user.role == Role.DEALER:
        q = q.where(DealerPaymentShift.status != "draft")
    return (
        (await db.execute(q.order_by(DealerPaymentShift.created_at.desc()))).scalars().all()
    )


@router.post(
    "/dealers/{dealer_id}/payment-shifts",
    response_model=PaymentShiftRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_payment_shift(
    dealer_id: UUID,
    payload: PaymentShiftCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerPaymentShift:
    """Draft a payment-date shift (team only). est_adb_impact is always
    computed server-side from monthly_amount and the day move."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    if payload.from_day == payload.to_day:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "from_day and to_day must differ")
    label = payload.label.strip()
    if not label:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "label cannot be blank")
    shift = DealerPaymentShift(
        dealer_id=dealer.id,
        vendor_key=payload.vendor_key,
        direction=payload.direction,
        label=label,
        from_day=payload.from_day,
        to_day=payload.to_day,
        monthly_amount=payload.monthly_amount,
        est_adb_impact=_shift_estimate(
            payload.monthly_amount,
            payload.from_day,
            payload.to_day,
            direction=payload.direction,
        ),
        rationale=payload.rationale,
        created_by_user_id=user.id,
    )
    db.add(shift)
    await db.flush()
    await log_action(
        db, dealer.id, user, "payment_shift.create", "payment_shift",
        entity_id=shift.id,
        after={
            "label": label,
            "direction": payload.direction,
            "from_day": payload.from_day,
            "to_day": payload.to_day,
            "monthly_amount": payload.monthly_amount,
        },
    )
    await db.commit()
    await db.refresh(shift)
    return shift


async def _sync_shift_plan_action(
    db: AsyncSession, dealer: DealerBusiness, shift: DealerPaymentShift, new_status: str
) -> None:
    """Keep the Plan of Action in lockstep with the shift lifecycle.

    proposed  -> create (once) a PUBLISHED per-vendor action: "Call {vendor}:
                 move the payment date" — proposing IS telling the client, so
                 it appears on their plan immediately.
    done      -> the linked action completes.
    dismissed | pulled back to draft -> an action the client hasn't completed
                 is withdrawn (deleted); a completed one stays as history.
    Flushes, never commits."""
    if new_status == "proposed" and shift.plan_action_id is None:
        est = float(shift.est_adb_impact or 0.0)
        if shift.direction == "out":
            title = f"Call {shift.label}: move the payment date"[:200]
            detail = (
                f"Request a payment-date change under the vendor's terms: pay on day "
                f"{shift.to_day} instead of ~day {shift.from_day}. "
                + (shift.rationale or "Real vendor terms — never statement-date window dressing.")
            )
        else:
            title = f"Tighten collections with {shift.label}"[:200]
            detail = (
                f"Adjust invoicing/collection terms so this deposit lands ~day "
                f"{shift.to_day} instead of ~day {shift.from_day}. "
                + (shift.rationale or "Real receivables change — never statement-date timing.")
            )
        action = DealerPlanAction(
            dealer_id=dealer.id,
            title=title,
            detail=detail,
            category="liquidity",
            owner="Client",
            timeline="next billing cycle",
            status="todo",
            expected_effect=(f"ADB {'+' if est >= 0 else '-'}${abs(est):,.0f}" if est else None),
            published=True,
        )
        db.add(action)
        await db.flush()
        shift.plan_action_id = action.id
        return
    if shift.plan_action_id is None:
        return
    action = await db.get(DealerPlanAction, shift.plan_action_id)
    if action is None:
        shift.plan_action_id = None
        return
    if new_status == "done":
        action.status = "done"
    elif new_status in ("dismissed", "draft") and action.status != "done":
        await db.delete(action)
        shift.plan_action_id = None


@router.patch("/dealers/{dealer_id}/payment-shifts/{shift_id}", response_model=PaymentShiftRead)
async def update_payment_shift(
    dealer_id: UUID,
    shift_id: UUID,
    payload: PaymentShiftPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerPaymentShift:
    """Edit days/rationale or move the shift through its lifecycle
    (draft -> proposed -> done|dismissed; terminal states only reopen to
    proposed). The ADB estimate is recomputed whenever a day changes."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    shift = await _load_shift(db, dealer.id, shift_id)
    changes = payload.model_dump(exclude_unset=True)
    new_status = changes.get("status")
    if new_status is not None and new_status != shift.status:
        if not payment_timing.can_transition(shift.status, new_status):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Cannot move a '{shift.status}' shift to '{new_status}'",
            )
    from_day = changes.get("from_day") or shift.from_day
    to_day = changes.get("to_day") or shift.to_day
    if from_day == to_day:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "from_day and to_day must differ")
    # direction is fixed at creation (not patchable); a deposit ('in') row
    # can only ever move EARLIER, whatever days the patch proposes.
    if shift.direction == "in" and to_day >= from_day:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "direction 'in' moves a deposit EARLIER: to_day must be before from_day",
        )
    days_changed = from_day != shift.from_day or to_day != shift.to_day
    shift.from_day = from_day
    shift.to_day = to_day
    if "rationale" in changes:
        shift.rationale = changes["rationale"]
    if days_changed:
        shift.est_adb_impact = _shift_estimate(
            shift.monthly_amount, from_day, to_day, direction=shift.direction
        )
    if new_status is not None and new_status != shift.status:
        before_status = shift.status
        shift.status = new_status
        await _sync_shift_plan_action(db, dealer, shift, new_status)
        await log_action(
            db, dealer.id, user, "payment_shift.status", "payment_shift",
            entity_id=shift.id,
            before={"status": before_status},
            after={"status": new_status},
        )
    await db.commit()
    await db.refresh(shift)
    return shift


@router.delete(
    "/dealers/{dealer_id}/payment-shifts/{shift_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_payment_shift(
    dealer_id: UUID, shift_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> None:
    """Only drafts are deletable — anything the dealer may have seen is
    dismissed (audited), never erased."""
    require_team(user)
    dealer = await _load_visible_dealer(db, dealer_id, user)
    shift = await _load_shift(db, dealer.id, shift_id)
    if shift.status != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only draft shifts can be deleted — dismiss instead"
        )
    await db.delete(shift)
    await db.commit()
