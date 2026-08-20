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
import re
import secrets
import zipfile
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, or_
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.db import SessionLocal, get_db
from app.deps import CurrentUser
from app.models.user import User
from app.services import clerk as clerk_service
from app.enums import Role

# READ-ONLY reuse: bucket models are queried/appended, never altered; the
# analysis-version constant keeps cache lookups aligned with the bucket AI.
from app.models.bucket import Bucket, BucketFile, BucketFileAnalysis
from app.services.bucket_ai import CURRENT_FILE_ANALYSIS_VERSION

from .deps import (
    load_dealer,
    require_super_admin,
    require_team,
    require_team_or_dealer,
    require_team_or_dealer_or_rep,
    require_team_or_rep,
    resolve_dealer_scope,
)
from .models import (
    DealerRepLead,
    DealerSmsConsent,
    DealerAIMessage,
    MESSAGE_CHANNELS,
    CLIENT_VISIBLE_CHANNELS,
    REP_LEAD_TERMINAL,
    DealerPlaidItem,
    DealerAccount,
    DealerAddback,
    DealerAlert,
    DealerAuditLog,
    DealerBusiness,
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
    RepFileRow,
    CreditInviteRequest,
    CreditInviteResult,
    CreditRead,
    CreditUpsert,
    DealerCreate,
    AIThreadAsk,
    DecisionRead,
    UnreadSummary,
    PublicPlaidResult,
    RoomPasscode,
    RoomPlaidExchange,
    ClientRequestResult,
    ClientRequestSend,
    SignatureRequestSend,
    AIThreadMessage,
    MessageEdit,
    SmsConsentIn,
    SmsConsentOut,
    SmsDisclosureOut,
    DealerInvite,
    DealerInviteResult,
    DealerListItem,
    DealerRead,
    DealerUpdate,
    DebtCreate,
    DebtDraftResult,
    PlaidExchange,
    PlaidItemPatch,
    PlaidItemRead,
    PlaidLinkTokenRead,
    PlaidRefreshResult,
    PlaidStateRead,
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
)
from .services import analyst, archive, buckets_link, business_credit as business_credit_svc, vendors, handoff as handoff_service, recurrence, report_pdf, rollups, storage
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
from .services import balance_health, client_room, consent_delivery, decision, file_chat, program_fit, sms_consent as sms_consent_svc, mca_readiness as mca_svc, payment_timing, plaid_client, plaid_sync, refinance as refinance_svc, simulate, timing_optimizer
from .services.targets import propose_targets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dealer-os", tags=["dealer-os"])


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
        out.append(item)
    return out


async def _require_group(db: AsyncSession, group_id: UUID) -> DealerGroup:
    group = await db.get(DealerGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    return group


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
            .order_by(DealerOwner.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    delivery = await consent_delivery.deliver_link_checked(
        db,
        channel=channel,
        to_email=(owner.email if owner and owner.email else None) or dealer.email,
        to_phone=(owner.phone if owner and owner.phone else None) or dealer.phone,
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
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    payload: ClientRequestSend | None = None,
) -> ClientRequestResult:
    """Email the owner their secure room so they can send bank statements.

    Named for what it does today, not for what it will do. Plaid is still
    `require_team` on every route, so a client opening this room can upload and
    nothing else; there is no unauthenticated link-token or exchange endpoint
    yet. When those exist the room gains a Connect button and this route keeps
    working unchanged, because the link is already the right one."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    req = payload or ClientRequestSend()
    room = await client_room.ensure_room(db, dealer)
    delivery = await _notify_client_request(
        db, dealer, user,
        purpose="send us your recent bank statements",
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
    "/dealers/{dealer_id}/signature-request",
    response_model=ClientRequestResult,
    status_code=status.HTTP_201_CREATED,
)
async def send_signature_request(
    dealer_id: UUID,
    payload: SignatureRequestSend,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClientRequestResult:
    """Ask the owner to sign something.

    Adds it to the same checklist their documents are on, so there is one list
    with everything outstanding on it rather than a separate signing inbox they
    have to be told about separately."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    room = await client_room.ensure_room(db, dealer)
    await client_room.request_document(
        db, dealer,
        name=payload.title,
        description=payload.note,
        category="signatures",
        requires_signature=True,
        signature_kind=payload.signature_kind,
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
    if payload.group_id is not None:
        await _require_group(db, payload.group_id)
    # sms_consent is captured alongside the file but is not a column on it: it
    # becomes its own evidence row below.
    fields = payload.model_dump(exclude={"sms_consent"})
    dealer = DealerBusiness(**fields, owner_user_id=user.id)
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
async def match_bucket_by_email(
    dealer_id: UUID, background: BackgroundTasks, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> DealerRead:
    """Explicitly find this dealer's intake bucket by email (no bucket creation —
    manual linking or ensure_bucket handle the rest)."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
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
    require_team_or_dealer(user)
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

    try:
        metrics = await _latest_snapshot_metrics(db, dealer.id)
    except HTTPException:
        d = decision.decide({"verdict": "no_data"}, None)
        return DecisionRead(**asdict(d), programs=[])

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

    # Which real programs this file reaches, easiest first. compute_paths
    # answers in seven generic categories; this answers in the fourteen the
    # desk actually submits to, so a rep can name the program rather than a
    # readiness percentage.
    docs = (
        (
            await db.execute(
                select(DealerDocument.filename).where(DealerDocument.dealer_id == dealer.id)
            )
        )
        .scalars()
        .all()
    )
    programs = program_fit.screen(
        dealer, tree, [{"name": f} for f in docs if f]
    )

    out = decision.decide(fundability, health)
    return DecisionRead(**asdict(out), programs=[asdict(p) for p in programs])


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
    require_team_or_dealer(user)
    if user.role == Role.DEALER:
        # A client may complete the always-required business-profile fields
        # on their OWN file — nothing else.
        dealer = await resolve_dealer_scope(db, user, dealer_id)
        changes = payload.model_dump(exclude_unset=True)
        allowed = {"legal_name", "ein", "naics_code", "entity_type", "started_on"}
        illegal = sorted(set(changes) - allowed)
        if illegal:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"These fields are maintained by your advisor: {', '.join(illegal)}",
            )
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
    require_team_or_dealer(user)
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
    dealer = await load_dealer(db, dealer_id)
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
    vendor_key = normalize_vendor(event.description or "")
    if payload.apply_similar and vendor_key:
        rows = (
            await db.execute(
                select(DealerCashEvent)
                .where(DealerCashEvent.dealer_id == dealer.id, DealerCashEvent.id != event.id)
                .order_by(DealerCashEvent.occurred_on.desc())
                .limit(2000)
            )
        ).scalars().all()
        similar = [r for r in rows if normalize_vendor(r.description or "") == vendor_key]
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
    dealer = await load_dealer(db, dealer_id)
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
    require_team_or_dealer(user)
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
    require_team_or_dealer(user)
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


@router.get("/dealers/{dealer_id}/pipeline", response_model=PipelineStatusRead)
async def pipeline_status(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> PipelineStatusRead:
    """Live ingestion state for the cockpit header.

    Deliberately cheap — the header polls this while work is moving. Five
    counting queries, no payload scans."""
    require_team_or_dealer(user)
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
    dealer = await load_dealer(db, dealer_id)
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
    dealer = await load_dealer(db, dealer_id)
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
    dealer = await load_dealer(db, dealer_id)
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


MAX_AUTO_INGEST = 60


async def _background_ingest_bucket_files(dealer_id: UUID) -> None:
    """Auto-ingest every not-yet-ingested file in the dealer's linked bucket.
    Ingestion is not optional — linking a bucket IS the instruction to pull
    its data into the metrics pipeline. Own session per file so one failure
    never poisons the rest; cached analyses map with zero model calls."""
    try:
        async with SessionLocal() as db:
            dealer = await db.get(DealerBusiness, dealer_id)
            if dealer is None or dealer.bucket_id is None:
                return
            ingested = set(
                (
                    await db.execute(
                        select(DealerDocument.bucket_file_id).where(
                            DealerDocument.dealer_id == dealer_id,
                            DealerDocument.bucket_file_id.is_not(None),
                        )
                    )
                ).scalars().all()
            )
            file_ids = [
                fid
                for fid in (
                    await db.execute(
                        select(BucketFile.id)
                        .where(
                            BucketFile.bucket_id == dealer.bucket_id,
                            BucketFile.deleted_at.is_(None),
                        )
                        .order_by(BucketFile.created_at.asc())
                    )
                ).scalars().all()
                if fid not in ingested
            ][:MAX_AUTO_INGEST]
        for fid in file_ids:
            async with SessionLocal() as db:
                try:
                    dealer = await db.get(DealerBusiness, dealer_id)
                    if dealer is None:
                        return
                    await _ingest_bucket_file_core(db, dealer, fid)
                    await db.commit()
                except Exception:
                    logger.exception(
                        "dealer-os: auto-ingest failed for bucket file %s (dealer %s)",
                        fid, dealer_id,
                    )
    except Exception:
        logger.exception("dealer-os: auto-ingest sweep failed for dealer %s", dealer_id)


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
    dealer = await load_dealer(db, dealer_id)
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
    require_team_or_dealer(user)
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
    dealer = await load_dealer(db, dealer_id)
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
    require_team_or_dealer(user)
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
    require_team_or_dealer(user)
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
    require_team_or_dealer(user)
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
    require_team_or_dealer(user)
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
    require_team_or_dealer(user)
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
    require_team_or_dealer(user)
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

    SessionCreate and the list and delete routes have existed since the
    sessions table landed; there was never a way to create one, so the only
    rows in it came from seeds. Open to the owning rep, because the rep is who
    arranges the follow-up visit.

    The client sees these read-only with a Join button, so a join_url that is
    wrong is worse than one that is absent."""
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

    visible = select(DealerBusiness.id)
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
    dealer = await load_dealer(db, dealer_id)
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
    dealer = await load_dealer(db, dealer_id)
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
    dealer = await load_dealer(db, dealer_id)
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
    """Ask the client for a document, and tell them you have.

    Open to the owning rep as well as the desk: a rep standing in the business
    is exactly who knows which statement is missing."""
    require_team_or_rep(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
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
    require_team_or_dealer(user)
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
    require_team_or_dealer(user)
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
    require_team_or_dealer(user)
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
    require_team(user)
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
    require_team(user)
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
    require_team(user)
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
    require_team(user)
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
    dealer = await load_dealer(db, dealer_id)
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
    require_team_or_dealer(user)
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
    dealer = await load_dealer(db, dealer_id)

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
                )
                .order_by(DealerPlaidItem.created_at)
            )
        )
        .scalars()
        .all()
    )


@router.get("/dealers/{dealer_id}/plaid", response_model=PlaidStateRead)
async def plaid_state(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> PlaidStateRead:
    """Connection panel state. enabled=false (keys not provisioned) renders a
    quiet disabled state — never an error."""
    require_team(user)  # gated off client accounts for now
    dealer = await load_dealer(db, dealer_id)
    items = await _plaid_items(db, dealer.id)
    return PlaidStateRead(
        enabled=plaid_client.enabled(),
        environment=plaid_client.environment(),
        items=[PlaidItemRead.model_validate(i) for i in items],
    )


@router.post("/dealers/{dealer_id}/plaid/link-token", response_model=PlaidLinkTokenRead)
async def plaid_link_token(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> PlaidLinkTokenRead:
    """Start a Plaid Link session — Statements product ONLY (bank statements;
    everything else still comes through upload). The client connects their
    own bank; team can run it alongside them."""
    require_team(user)  # gated off client accounts for now
    dealer = await load_dealer(db, dealer_id)
    _plaid_cooldown("link", dealer.id, 10)
    try:
        token = await plaid_client.create_link_token(
            dealer_id=str(dealer.id), dealer_name=dealer.name
        )
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    return PlaidLinkTokenRead(link_token=token)


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
    require_team(user)  # gated off client accounts for now
    dealer = await load_dealer(db, dealer_id)
    _plaid_cooldown("exchange", dealer.id, 5)
    try:
        access_token, item_id = await plaid_client.exchange_public_token(payload.public_token)
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    existing = (
        await db.execute(select(DealerPlaidItem).where(DealerPlaidItem.item_id == item_id))
    ).scalar_one_or_none()
    if existing is not None and existing.dealer_id != dealer.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This bank connection belongs to another file")
    if existing is not None:  # reconnect: refresh the token, revive the row
        existing.encrypted_access_token = plaid_client.encrypt_token(access_token)
        existing.status, existing.error = "active", None
        existing.next_refresh_at = datetime.now(timezone.utc)
        item = existing
    else:
        item = DealerPlaidItem(
            dealer_id=dealer.id,
            item_id=item_id,
            institution_name=(payload.institution_name or "")[:160] or None,
            encrypted_access_token=plaid_client.encrypt_token(access_token),
            status="active",
            # Safety net: the in-process background first sync is not durable
            # (a redeploy kills it) — a due next_refresh_at means the daily
            # scheduler sweep picks the item up regardless.
            next_refresh_at=datetime.now(timezone.utc),
        )
        db.add(item)
    await db.flush()
    await log_action(
        db, dealer.id, user, "plaid.connect", "plaid_item", entity_id=item.id,
        after={"institution": item.institution_name},
    )
    await db.commit()
    await db.refresh(item)
    background.add_task(_background_plaid_first_sync, item.id)
    return item


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
    _plaid_cooldown("link", dealer.id, 10)
    try:
        pt = await plaid_client.create_link_token(
            dealer_id=str(dealer.id), dealer_name=dealer.name
        )
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    return PlaidLinkTokenRead(link_token=pt)


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
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))

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
        existing.status, existing.error = "active", None
        existing.next_refresh_at = datetime.now(timezone.utc)
        item = existing
    else:
        item = DealerPlaidItem(
            dealer_id=dealer.id,
            item_id=item_id,
            institution_name=(payload.institution_name or "")[:160] or None,
            encrypted_access_token=plaid_client.encrypt_token(access_token),
            status="active",
            next_refresh_at=datetime.now(timezone.utc),
        )
        db.add(item)
    await db.flush()
    # No `user` to attribute this to, so the audit row records the room the
    # owner came through. "The client did it themselves" is exactly the fact
    # worth being able to prove later.
    await log_action(
        db, dealer.id, None, "plaid.connect.client", "plaid_item", entity_id=item.id,
        after={"institution": item.institution_name, "via": "client_room", "link_id": str(link.id)},
    )
    await db.commit()
    background.add_task(_background_plaid_first_sync, item.id)
    return PublicPlaidResult(
        connected=True,
        institution_name=item.institution_name,
        message="Your bank is connected. We are pulling your statements now.",
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
    dealer = await load_dealer(db, dealer_id)
    _plaid_cooldown("refresh", dealer.id, 60)
    items = [i for i in await _plaid_items(db, dealer.id) if i.status != "removed"]
    for item in items:
        background.add_task(_background_plaid_first_sync, item.id)
    return PlaidRefreshResult(queued=len(items))


@router.patch("/dealers/{dealer_id}/plaid/{item_pk}", response_model=PlaidItemRead)
async def plaid_patch(
    dealer_id: UUID,
    item_pk: UUID,
    payload: PlaidItemPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerPlaidItem:
    """Super-admin: pause or resume the 30-day automatic refresh for one
    bank. Paused items keep their connection and history; the scheduler
    simply skips them until resumed."""
    require_super_admin(user)
    dealer = await load_dealer(db, dealer_id)
    item = (
        await db.execute(
            select(DealerPlaidItem).where(
                DealerPlaidItem.id == item_pk, DealerPlaidItem.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bank connection not found for this client")
    item.auto_refresh = payload.auto_refresh
    if payload.auto_refresh and item.next_refresh_at is None:
        item.next_refresh_at = datetime.now(timezone.utc)
    await log_action(
        db, dealer.id, user, "plaid.auto_refresh", "plaid_item", entity_id=item.id,
        after={"auto_refresh": payload.auto_refresh},
    )
    await db.commit()
    await db.refresh(item)
    return item


@router.delete("/dealers/{dealer_id}/plaid/{item_pk}", status_code=status.HTTP_204_NO_CONTENT)
async def plaid_remove(
    dealer_id: UUID, item_pk: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> None:
    """Disconnect a bank (team). Documents already pulled stay — they are the
    dealer's statements; only the live connection goes."""
    require_team(user)
    dealer = await load_dealer(db, dealer_id)
    item = (
        await db.execute(
            select(DealerPlaidItem).where(
                DealerPlaidItem.id == item_pk, DealerPlaidItem.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bank connection not found for this client")
    token = plaid_client.decrypt_token(item.encrypted_access_token)
    if token:
        try:
            await plaid_client.item_remove(token)
        except plaid_client.PlaidUnavailable:
            pass  # best-effort — the row is retired regardless
    item.status = "removed"
    item.encrypted_access_token = None
    await log_action(db, dealer.id, user, "plaid.remove", "plaid_item", entity_id=item.id)
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
    dealer = await load_dealer(db, dealer_id)
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
    dealer = await load_dealer(db, dealer_id)
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


@router.get("/dealers/{dealer_id}/owners", response_model=list[OwnerRead])
async def list_owners(
    dealer_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[DealerOwner]:
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    return list(
        (
            await db.execute(
                select(DealerOwner)
                .where(DealerOwner.dealer_id == dealer.id)
                .order_by(DealerOwner.ownership_pct.desc().nullslast(), DealerOwner.last_name)
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
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
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
    row = DealerOwner(dealer_id=dealer.id, **body.model_dump())
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
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
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
    for k, v in patch.items():
        setattr(row, k, v)
    await log_action(
        db, dealer.id, user, "owner.update", "owner",
        entity_id=row.id, after=jsonable_encoder(patch),
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
    require_team_or_dealer(user)
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
        await db.rollback()
        return SoftPullResult(ok=False, detail=str(exc))
    except SoftPullDenied as exc:
        await db.rollback()
        return SoftPullResult(ok=False, detail=str(exc))
    except Exception as exc:  # transport/validation — surfaced, never swallowed
        await db.rollback()
        logger.exception("dealer-os: soft pull failed for owner %s", owner_pk)
        return SoftPullResult(ok=False, detail=f"Credit pull failed: {exc}")

    owner.credit_score = getattr(pull, "score", None)
    owner.credit_tier = _credit_tier(getattr(pull, "score", None))
    owner.credit_pulled_at = datetime.now(timezone.utc)
    owner.credit_pull_id = pull.id
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
    initiators are unrestricted, as before."""
    require_team_or_dealer(user)
    dealer = await resolve_dealer_scope(db, user, dealer_id)
    owner = (
        await db.execute(
            select(DealerOwner).where(DealerOwner.id == owner_id, DealerOwner.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Owner not found for this client")
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


@router.get("/rep-production", response_model=RepProductionRead)
async def rep_production(
    user: CurrentUser,
    days: int = 90,
    db: AsyncSession = Depends(get_db),
) -> RepProductionRead:
    """What the field team has brought in, per rep.

    Super-admin only. Reads ownership off DealerBusiness.owner_user_id rather
    than the pipeline table, so files opened before the pipeline existed still
    count — they simply carry no status.

    The number that matters most here is `with_documents`, not `files_opened`.
    A rep can open twenty files in an afternoon and none of them are production
    until a client actually sends something, so counting files alone rewards
    exactly the wrong behaviour.
    """
    require_super_admin(user)
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 730)))

    rows = (
        await db.execute(
            select(DealerBusiness, DealerRepLead, User)
            .outerjoin(DealerRepLead, DealerRepLead.dealer_id == DealerBusiness.id)
            .outerjoin(User, User.id == DealerBusiness.owner_user_id)
            .where(
                DealerBusiness.owner_user_id.is_not(None),
                DealerBusiness.created_at >= since,
            )
            .order_by(DealerBusiness.created_at.desc())
        )
    ).all()

    dealer_ids = [d.id for d, _, _ in rows]
    scores: dict[UUID, float | None] = {}
    doc_counts: dict[UUID, int] = {}
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

    by_rep: dict[UUID | None, RepProduction] = {}
    for dealer, lead, rep in rows:
        key = dealer.owner_user_id
        if key not in by_rep:
            by_rep[key] = RepProduction(
                rep_user_id=key,
                rep_name=(rep.name if rep else None) or "Unassigned",
                rep_email=rep.email if rep else None,
            )
        bucket = by_rep[key]
        docs = doc_counts.get(dealer.id, 0)
        score = scores.get(dealer.id)
        status_val = lead.status if lead else None

        bucket.files.append(
            RepFileRow(
                dealer_id=dealer.id,
                name=dealer.name,
                city=dealer.city,
                state=dealer.state,
                industry=dealer.industry,
                status=status_val,
                decision=lead.decision if lead else None,
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
        if (lead.decision if lead else None) == "fundable":
            bucket.fundable += 1
        if bucket.last_activity is None or (
            dealer.updated_at and dealer.updated_at > bucket.last_activity
        ):
            bucket.last_activity = dealer.updated_at

    for bucket in by_rep.values():
        seen = [f.score for f in bucket.files if f.score is not None]
        bucket.avg_score = round(sum(seen) / len(seen), 1) if seen else None
        bucket.files.sort(key=lambda f: f.created_at, reverse=True)

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
    all_scores = [f.score for r in reps for f in r.files if f.score is not None]
    totals.avg_score = round(sum(all_scores) / len(all_scores), 1) if all_scores else None
    totals.last_activity = max((r.last_activity for r in reps if r.last_activity), default=None)

    return RepProductionRead(since=since, totals=totals, reps=reps)


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
    token = secrets.token_urlsafe(32)
    owner.invite_token_hash = _hash_invite_token(token)
    owner.invite_sent_at = datetime.now(timezone.utc)
    owner.invite_opened_at = None  # a fresh link has not been opened yet
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
        to_email=req.to_email or owner.email or dealer.email,
        to_phone=req.to_phone or owner.phone or dealer.phone,
        business_name=dealer.name,
        purpose="authorise a soft credit check",
        path=path,
        rep_name=user.name,
    )
    return CreditInviteResult(
        token=token,
        path=path,
        delivered=delivery.ok,
        channel=delivery.channel,
        detail=delivery.detail,
    )


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
        await db.commit()
    return PublicConsentView(
        first_name=owner.first_name,
        last_initial=(owner.last_name or "")[:1],
        dealer_name=dealer.name if dealer is not None else "",
        fields_needed=_owner_missing_pull_fields(owner),
        completed=owner.credit_pulled_at is not None,
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
        await _release_token()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "FCRA permissible-purpose consent is required before a credit pull",
        )
    dealer = await db.get(DealerBusiness, owner.dealer_id)
    if dealer is None:  # orphaned row — treat like a dead link, not a 500
        await db.commit()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This consent link is no longer valid")
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
    require_team_or_dealer(user)
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
        normalize_vendor(r.description or "")
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
    dealer = await load_dealer(db, dealer_id)
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
    require_team_or_dealer(user)
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
    dealer = await load_dealer(db, dealer_id)
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
    dealer = await load_dealer(db, dealer_id)
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
    dealer = await load_dealer(db, dealer_id)
    shift = await _load_shift(db, dealer.id, shift_id)
    if shift.status != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only draft shifts can be deleted — dismiss instead"
        )
    await db.delete(shift)
    await db.commit()
