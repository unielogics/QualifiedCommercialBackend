from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import AITaskPriority, AITaskSource, AITaskStatus, Role
from app.models.ai_task import AITask
from app.models.client import Client
from app.models.prequal_request import PrequalRequest
from app.scoping import scope_client_query
from app.schemas.client import ClientCreate, ClientRead, ClientSelfUpdate, ClientUpdate
from app.services.ai.client_summarizer import refresh_client_summary

router = APIRouter(prefix="/clients", tags=["clients"])


class LivingProfileRead(BaseModel):
    """Account-wide AI-aggregated profile (Phase 8). Empty fields when
    the aggregator has never run for this client."""

    client_id: UUID
    living_profile: dict[str, Any] | None
    living_summary: str | None
    living_refreshed_at: datetime | None


def _scope(user, stmt):
    return scope_client_query(user, stmt)


@router.get("", response_model=list[ClientRead])
async def list_clients(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[ClientRead]:
    """List clients visible to the calling user. Returns broker_name on
    each row so the operator pipeline header (super-admin / UW view)
    can show the agent owning the relationship without an extra
    round-trip per row."""
    from sqlalchemy.orm import selectinload
    stmt = _scope(
        user,
        select(Client).options(selectinload(Client.broker)).order_by(Client.name),
    )
    rows = (await db.execute(stmt)).scalars().all()
    out: list[ClientRead] = []
    for r in rows:
        d = ClientRead.model_validate(r).model_dump()
        broker_obj = getattr(r, "broker", None)
        d["broker_name"] = getattr(broker_obj, "display_name", None) if broker_obj else None
        out.append(ClientRead.model_validate(d))
    return out


def _client_read_for_self(client: Client) -> ClientRead:
    """ClientRead for the CLIENT themselves — strips agent_private
    known_facts (agent notes) from realtor_profile before it reaches the
    borrower. Builds a transient dict rather than mutating the persisted
    row (same reshaping pattern as get_client's d = ...model_dump())."""
    from app.services.ai.realtor_profile import filter_known_facts_for_client

    d = ClientRead.model_validate(client).model_dump()
    d["realtor_profile"] = filter_known_facts_for_client(d.get("realtor_profile"))
    return ClientRead.model_validate(d)


@router.get("/me", response_model=ClientRead)
async def get_my_client(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ClientRead:
    """Return the current user's linked Client record. Used by the desktop
    Profile page so it doesn't need to know its own client_id."""
    if not user.client:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Current user has no linked client record"
        )
    return _client_read_for_self(user.client)


@router.patch("/me", response_model=ClientRead)
async def update_my_client(
    payload: ClientSelfUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClientRead:
    """Self-edit: a CLIENT-role user updates their own profile. Only the
    safe-to-self-edit fields land here (see ClientSelfUpdate). Tier,
    FICO, broker assignment, funded totals stay broker/super-admin only."""
    client = user.client
    if client is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Current user has no linked client record"
        )
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(client, k, v)
    await db.flush()
    await db.refresh(client)
    return _client_read_for_self(client)


@router.get("/me/living-profile", response_model=LivingProfileRead)
async def get_my_living_profile(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LivingProfileRead:
    """Borrower self-read of the account-wide AI profile. Returns the
    last-refreshed snapshot — call POST /clients/me/summary/refresh
    to force a fresh aggregation."""
    if not user.client:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Current user has no linked client record"
        )
    return LivingProfileRead(
        client_id=user.client.id,
        living_profile=user.client.living_profile,
        living_summary=user.client.living_summary,
        living_refreshed_at=user.client.living_refreshed_at,
    )


@router.post("/me/summary/refresh", response_model=LivingProfileRead)
async def refresh_my_living_profile(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LivingProfileRead:
    """Force-refresh the borrower's living profile. Synchronous — the
    aggregator does a single Haiku call, typically <2s. Use sparingly
    on the borrower side; the daily 3am cron is the steady state."""
    if not user.client:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Current user has no linked client record"
        )
    await refresh_client_summary(db, user.client.id)
    fresh = (await db.execute(
        select(Client).where(Client.id == user.client.id)
    )).scalar_one()
    return LivingProfileRead(
        client_id=fresh.id,
        living_profile=fresh.living_profile,
        living_summary=fresh.living_summary,
        living_refreshed_at=fresh.living_refreshed_at,
    )


@router.get("/{client_id}/living-profile", response_model=LivingProfileRead)
async def get_client_living_profile(
    client_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LivingProfileRead:
    """Operator read — broker / super-admin sees the borrower's
    account-wide AI profile."""
    if user.role == Role.CLIENT:
        # Borrowers must use /me/living-profile (which scopes to themselves
        # without taking a client_id parameter).
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Use /clients/me/living-profile")
    stmt = _scope(user, select(Client).where(Client.id == client_id))
    client = (await db.execute(stmt)).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    return LivingProfileRead(
        client_id=client.id,
        living_profile=client.living_profile,
        living_summary=client.living_summary,
        living_refreshed_at=client.living_refreshed_at,
    )


@router.post("/{client_id}/summary/refresh", response_model=LivingProfileRead)
async def refresh_client_living_profile(
    client_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LivingProfileRead:
    """Operator-triggered manual refresh of a specific client's profile."""
    if user.role in {Role.CLIENT, Role.REGIONAL_MANAGER}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator role required")
    stmt = _scope(user, select(Client).where(Client.id == client_id))
    client = (await db.execute(stmt)).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    await refresh_client_summary(db, client.id)
    fresh = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one()
    return LivingProfileRead(
        client_id=fresh.id,
        living_profile=fresh.living_profile,
        living_summary=fresh.living_summary,
        living_refreshed_at=fresh.living_refreshed_at,
    )


@router.get("/{client_id}", response_model=ClientRead)
async def get_client(client_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ClientRead:
    from sqlalchemy.orm import selectinload
    stmt = _scope(
        user,
        select(Client)
        .where(Client.id == client_id)
        .options(selectinload(Client.user)),
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    # Project presence from the linked User row (if any).
    d = ClientRead.model_validate(row).model_dump()
    linked = getattr(row, "user", None)
    d["last_seen_at"] = getattr(linked, "last_seen_at", None) if linked else None
    return ClientRead.model_validate(d)


@router.get("/{client_id}/ai-agents")
async def get_client_ai_agents(
    client_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    """The AI Agents currently assigned to this client. Drives the
    Active-agent strip on the client detail page (Pause / Resume /
    Remove). Excludes already-exited leads."""
    from app.models.ai_agent import AIAgent, AIAgentLead

    client = (await db.execute(_scope(user, select(Client).where(Client.id == client_id)))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")

    rows = list(
        (
            await db.execute(
                select(AIAgentLead, AIAgent)
                .join(AIAgent, AIAgent.id == AIAgentLead.ai_agent_id)
                .where(AIAgentLead.client_id == client_id)
                .where(AIAgentLead.status != "exited")
                .order_by(AIAgentLead.enrolled_at.desc())
            )
        ).all()
    )
    return [
        {
            "lead_id": str(lead.id),
            "ai_agent_id": str(agent.id),
            "name": agent.name,
            "ai_display_name": agent.ai_display_name,
            "kind": agent.kind,
            "status": lead.status,
            "deal_id": str(lead.deal_id) if lead.deal_id else None,
            "attempts_made": lead.attempts_made,
            "last_outbound_at": lead.last_outbound_at.isoformat()
            if lead.last_outbound_at
            else None,
        }
        for lead, agent in rows
    ]


# ── Unified workspace aggregate (Phase 2) ────────────────────────────
#
# Single round-trip that backs the new /clients/[id]/workspace UI.
# Returns the client profile, deal summary list, funding files (Loans)
# summary list, AI plan snapshot, recent activity tail, agent notes,
# documents roll-up, and a server-computed permissions block.
#
# Deals and AgentTask counts are wired progressively as later phases
# land (Phase 3 Deal model, Phase 7 AgentTask model). Until then those
# fields return empty lists / null counts.


@router.get("/{client_id}/workspace")
async def get_client_workspace(
    client_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    tab: str | None = None,
    deal_id: UUID | None = None,
    funding_file_id: UUID | None = None,
    loan_id: UUID | None = None,
):
    from sqlalchemy.orm import selectinload

    from app.models.activity import Activity
    from app.models.client_ai_plan import ClientAIPlan
    from app.models.deal import Deal
    from app.models.loan import Loan
    from app.scoping import scope_client_query
    from app.schemas.deal import DealOut
    from app.schemas.workspace import (
        FundingFileSummary,
        WorkspaceActivityRow,
        WorkspaceAISummary,
        WorkspaceDocumentsSummary,
        WorkspaceNoteRow,
        WorkspaceOut,
        WorkspacePermissions,
        WorkspaceSelectedContext,
        WorkspaceTabCounts,
    )

    stmt = scope_client_query(
        user,
        select(Client)
        .where(Client.id == client_id)
        .options(selectinload(Client.user)),
    )
    client = (await db.execute(stmt)).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")

    # Project presence onto the ClientRead payload (same pattern as get_client).
    client_payload = ClientRead.model_validate(client).model_dump()
    linked = getattr(client, "user", None)
    client_payload["last_seen_at"] = getattr(linked, "last_seen_at", None) if linked else None
    client_read = ClientRead.model_validate(client_payload)

    # Funding files: all loans for this client, ordered most recent first.
    loans_stmt = (
        select(Loan)
        .where(Loan.client_id == client_id)
        .order_by(Loan.created_at.desc())
    )
    loans = (await db.execute(loans_stmt)).scalars().all()
    funding_files: list[FundingFileSummary] = []
    for loan in loans:
        funding_files.append(
            FundingFileSummary(
                id=loan.id,
                deal_id=loan.deal_id,
                client_id=loan.client_id,
                side=str(loan.side) if loan.side is not None else None,
                stage=str(loan.stage),
                address=loan.address,
                amount=float(loan.amount) if loan.amount is not None else None,
                # Loan.funding_file_kind / source_deal_id / handoff_summary
                # land in alembic 0048 (Phase 4). Until then these are null.
                funding_file_kind=getattr(loan, "funding_file_kind", None),
                source_deal_id=getattr(loan, "source_deal_id", None),
                handoff_summary=getattr(loan, "handoff_summary", None),
                created_at=loan.created_at,
                updated_at=loan.updated_at,
            )
        )

    # AI summary: pull the client-level ClientAIPlan (loan_id IS NULL,
    # phase=realtor) when present; otherwise fall back to the most
    # recent loan-level plan if any.
    ai_plan = (await db.execute(
        select(ClientAIPlan)
        .where(ClientAIPlan.client_id == client_id, ClientAIPlan.loan_id.is_(None))
        .order_by(ClientAIPlan.computed_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if ai_plan is None and loans:
        ai_plan = (await db.execute(
            select(ClientAIPlan)
            .where(
                ClientAIPlan.client_id == client_id,
                ClientAIPlan.loan_id.is_not(None),
            )
            .order_by(ClientAIPlan.computed_at.desc())
            .limit(1)
        )).scalar_one_or_none()

    outreach_mode = "portal_auto"
    if ai_plan is not None:
        settings = ai_plan.ai_secretary_settings or {}
        outreach_mode = settings.get("outreach_mode", "portal_auto")
    ai_state_map = {
        "off": "human_only",
        "draft_first": "draft_first",
        "portal_auto": "deployed",
        "portal_email": "deployed",
        "portal_email_sms": "deployed",
    }
    ai_state = ai_state_map.get(outreach_mode, "idle")

    outstanding = 0
    if ai_plan is not None:
        outstanding = sum(
            1
            for item in (ai_plan.required_items or [])
            if isinstance(item, dict) and item.get("status") in {"missing", "asked"}
        )

    ai_summary = WorkspaceAISummary(
        state=ai_state,
        outstanding_followups=outstanding,
        current_blocker=None,
        next_follow_up_at=None,
        next_best_question=ai_plan.next_best_question if ai_plan else None,
        readiness_score=ai_plan.readiness_score if ai_plan else None,
    )

    # Documents roll-up — count documents across this client's loans.
    # Cheap COUNT(*) queries rather than loading the rows.
    from sqlalchemy import func as sqlfunc
    from app.models.document import Document

    loan_ids = [loan.id for loan in loans]
    if loan_ids:
        total_q = await db.execute(
            select(sqlfunc.count(Document.id)).where(Document.loan_id.in_(loan_ids))
        )
        missing_q = await db.execute(
            select(sqlfunc.count(Document.id)).where(
                Document.loan_id.in_(loan_ids),
                Document.status == "missing",
            )
        )
        pending_q = await db.execute(
            select(sqlfunc.count(Document.id)).where(
                Document.loan_id.in_(loan_ids),
                Document.status == "pending",
            )
        )
        documents_summary = WorkspaceDocumentsSummary(
            total=int(total_q.scalar() or 0),
            missing=int(missing_q.scalar() or 0),
            pending_review=int(pending_q.scalar() or 0),
        )
    else:
        documents_summary = WorkspaceDocumentsSummary(total=0, missing=0, pending_review=0)

    # Activity tail — last 20 across the client's loans + client-level rows.
    activity_q = (
        select(Activity)
        .where(
            (Activity.client_id == client_id)
            | (Activity.loan_id.in_(loan_ids) if loan_ids else (Activity.client_id == client_id))
        )
        .order_by(Activity.occurred_at.desc())
        .limit(20)
    )
    activity_rows = (await db.execute(activity_q)).scalars().all()
    activity_tail = [
        WorkspaceActivityRow(
            at=a.occurred_at,
            kind=a.kind,
            summary=a.summary or "",
            actor=a.actor_label,
        )
        for a in activity_rows
    ]

    # Notes — read agent notes off realtor_profile.known_facts as the
    # existing detail page does. Stored on the Client row directly.
    notes: list[WorkspaceNoteRow] = []
    profile = client.realtor_profile or {}
    facts = profile.get("known_facts") if isinstance(profile, dict) else None
    if isinstance(facts, list):
        for idx, raw in enumerate(facts):
            if not isinstance(raw, dict) or raw.get("source") != "agent":
                continue
            captured = raw.get("captured_at")
            try:
                at = datetime.fromisoformat(str(captured)) if captured else None
            except (TypeError, ValueError):
                at = None
            if at is None:
                continue
            notes.append(
                WorkspaceNoteRow(
                    id=f"note-{idx}",
                    author=str(raw.get("author") or "agent"),
                    at=at,
                    body=str(raw.get("value") or ""),
                )
            )

    # Permissions — server-computed from the calling user's role + ownership.
    is_admin = user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC}
    is_regional_manager = user.role == Role.REGIONAL_MANAGER
    is_broker = user.role == Role.BROKER and user.broker is not None and client.broker_id == user.broker.id
    is_client_self = user.role == Role.CLIENT and user.client is not None and user.client.id == client.id

    permissions = WorkspacePermissions(
        can_mark_ready_for_lending=bool(is_broker or is_admin),
        can_edit_underwriting=bool(is_admin),
        can_create_deals=bool(is_broker or is_admin),
        can_create_funding_files=bool(is_admin),
        can_assign_ai=bool(is_broker or is_admin),
        can_edit_client_fields=bool(is_broker or is_admin or is_client_self),
        can_view_funding_tab=bool(is_broker or is_admin or is_regional_manager),
    )

    # Recommended tab — pick based on role + state when caller didn't
    # specify one. Frontend honors ?tab= first, then this hint, then
    # its own fallback.
    recommended_tab: str | None = None
    if tab is None:
        if is_admin:
            recommended_tab = "funding" if loans else "documents"
        elif is_broker:
            if outstanding > 0:
                recommended_tab = "ai-follow-up"
            elif loans:
                recommended_tab = "funding"
            else:
                recommended_tab = "deals"
        else:
            recommended_tab = "overview"

    selected_context = WorkspaceSelectedContext(
        tab=tab,
        deal_id=deal_id,
        funding_file_id=funding_file_id,
        loan_id=loan_id,
        recommended_tab=recommended_tab,
    )

    # Deals — populated from the Deal table (Phase 3).
    deal_rows = (
        await db.execute(
            select(Deal)
            .where(Deal.client_id == client_id)
            .order_by(Deal.created_at.desc())
        )
    ).scalars().all()
    deals_out = [DealOut.model_validate(d) for d in deal_rows]

    # AgentTask open count (Phase 7) — used for the Tasks tab pill.
    from app.models.agent_task import AgentTask

    open_tasks_count = int(
        (
            await db.execute(
                select(sqlfunc.count(AgentTask.id)).where(
                    AgentTask.client_id == client_id,
                    AgentTask.status.in_(["open", "in_progress", "waiting"]),
                )
            )
        ).scalar()
        or 0
    )

    tab_counts = WorkspaceTabCounts(
        deals=len(deals_out),
        funding=len(funding_files),
        tasks=open_tasks_count,
        ai_follow_up=outstanding,
        documents=documents_summary.total,
    )

    return WorkspaceOut(
        client=client_read,
        deals=deals_out,
        funding_files=funding_files,
        documents_summary=documents_summary,
        ai_summary=ai_summary,
        activity_tail=activity_tail,
        notes=notes,
        role_permissions=permissions,
        selected_context=selected_context,
        tab_counts=tab_counts,
    )


@router.post("", response_model=ClientRead, status_code=status.HTTP_201_CREATED)
async def create_client(
    payload: ClientCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ClientRead:
    if user.role in {Role.CLIENT, Role.REGIONAL_MANAGER}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only")
    data = payload.model_dump()
    # Brokers never get to assign ownership — even if they send
    # broker_id in the payload, we hard-stamp from the session so a
    # crafted request can't put another broker's name on a client.
    # Super-admin / loan_exec keep their ability to assign explicitly.
    if user.role == Role.BROKER and user.broker:
        data["broker_id"] = user.broker.id
    client = Client(**data)
    db.add(client)
    await db.flush()
    await db.refresh(client)
    return ClientRead.model_validate(client)


@router.patch("/{client_id}", response_model=ClientRead)
async def update_client(
    client_id: UUID,
    payload: ClientUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClientRead:
    """Partial update. Only fields present in the payload are applied.

    Stage-transition side effects (alembic 0024):
      - When `stage` flips to CONTACTED for the first time and
        `contacted_at` isn't already set, stamp it `now()`. Lets
        agents PATCH `{stage: 'contacted'}` without separately
        updating the timestamp the funnel reads."""
    from datetime import datetime as _dt, timezone as _tz
    from app.enums import ClientStage as _CS

    if user.role in {Role.CLIENT, Role.REGIONAL_MANAGER}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only")
    stmt = _scope(user, select(Client).where(Client.id == client_id))
    client = (await db.execute(stmt)).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    sent = payload.model_fields_set
    if "broker_id" in sent and user.role == Role.BROKER:
        # Same hard-stamp rule as create — a broker can't reassign
        # their own clients to another broker via PATCH.
        sent.discard("broker_id")
    # Capture the prior broker_id BEFORE applying patches so the
    # auto-flip rule below can decide whether this is a NULL→set
    # transition that should also bump experience_mode.
    prev_broker_id = client.broker_id
    for k, v in payload.model_dump(exclude_unset=True).items():
        if k == "broker_id" and user.role == Role.BROKER:
            continue
        setattr(client, k, v)
    if (
        "stage" in sent
        and payload.stage == _CS.CONTACTED
        and client.contacted_at is None
    ):
        client.contacted_at = _dt.now(_tz.utc)
    # Mobile-mode auto-flip — when a super_admin / loan_exec assigns a
    # broker to a previously-unassigned client AND the client has no
    # explicit experience mode set, default to "guided" with the same
    # stamps the intake / adoption paths use. This stops the agent-
    # assignment from leaving mobile stuck on self_directed (the bug
    # franco@unielogics.com hit). The operator can still override via
    # the explicit toggle on /clients/[id] afterwards.
    if (
        "broker_id" in sent
        and client.broker_id is not None
        and prev_broker_id is None
        and client.client_experience_mode is None
        and user.role != Role.BROKER
    ):
        client.client_experience_mode = "guided"
        if client.client_experience_mode_reason is None:
            client.client_experience_mode_reason = "agent_invited"
        if client.client_experience_mode_locked_by is None:
            client.client_experience_mode_locked_by = "agent"
    await db.flush()
    await db.refresh(client)
    return ClientRead.model_validate(client)


# ── Lead → Prequal handoff (alembic 0029) ────────────────────────────
#
# When the agent has nurtured a lead enough that they want the firm's
# funding team to look at it, they fire this endpoint (either via the
# "Ready for Prequalification" button on /clients/[id] or through a
# tool Elara calls on their behalf). It:
#
#   1. Validates the lead has the minimum fields the funding team
#      needs to triage (name, email, side-specific price, address).
#   2. Creates a PrequalRequest from `Client.lead_intake` so the
#      existing `/admin/prequal-requests` queue picks it up.
#   3. Spawns an AITask in the funding-team queue so Elara Inbox surfaces
#      it for whoever's on triage that day.
#   4. Stamps `client.lead_promotion_status = "agent_requested_review"`.
#
# Idempotent on retry — checks lead_promotion_status before firing.

class PrequalHandoffResponse(BaseModel):
    prequal_request_id: UUID
    client_id: UUID
    lead_promotion_status: str
    # Lending Handoff Packet (alembic 0031). Returned so the desktop
    # can show a confirmation summary right after the click.
    handoff_packet_id: UUID | None = None
    lending_thread_id: UUID | None = None
    handoff_summary: str | None = None
    first_lending_question: str | None = None
    missing_lending_items: list[str] = []


@router.post("/{client_id}/request-prequalification", response_model=PrequalHandoffResponse)
async def request_prequalification(
    client_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PrequalHandoffResponse:
    """Hand a lead off to the funding team. Creates a PrequalRequest
    + AITask. Agent-only — must own the client."""
    if user.role in {Role.CLIENT, Role.REGIONAL_MANAGER}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Borrowers can't trigger prequal handoffs")

    client = (
        await db.execute(select(Client).where(Client.id == client_id))
    ).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")

    # Brokers can only hand off their own clients. Super-admin /
    # underwriter aren't restricted here — they normally use intake
    # directly, but the handoff path stays open for completeness.
    if user.role == Role.BROKER and user.broker and client.broker_id != user.broker.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your client")

    # Idempotency — once promoted, don't double-fire.
    if client.lead_promotion_status in ("agent_requested_review", "funding_reviewing", "promoted_to_intake"):
        existing = (
            await db.execute(
                select(PrequalRequest)
                .where(PrequalRequest.requester_id == (client.user_id or user.id))
                .order_by(PrequalRequest.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return PrequalHandoffResponse(
                prequal_request_id=existing.id,
                client_id=client.id,
                lead_promotion_status=client.lead_promotion_status,
            )

    # Pull what the agent captured at lead-creation time. Free-shape
    # JSONB — we do best-effort dict access with safe fallbacks.
    intake = client.lead_intake if isinstance(client.lead_intake, dict) else {}
    property_blob = intake.get("property") or {}
    numbers_blob = intake.get("numbers") or {}

    address = (property_blob.get("address") or "").strip()
    city = (property_blob.get("city") or "").strip()
    state = (property_blob.get("state") or "").strip()
    target_property_address = ", ".join(p for p in (address, city, state) if p) or "Property TBD"

    # Side-specific price field. Buyer = purchase_price; seller = listing_price.
    side = client.client_type or "buyer"
    if side == "seller":
        purchase_price = float(numbers_blob.get("listing_price") or 0)
    else:
        # Range OR exact — prefer exact, fall back to midpoint of range.
        exact = numbers_blob.get("purchase_price")
        if exact:
            purchase_price = float(exact)
        else:
            lo = float(numbers_blob.get("price_range_low") or 0)
            hi = float(numbers_blob.get("price_range_high") or 0)
            purchase_price = (lo + hi) / 2 if (lo and hi) else (lo or hi)

    # Validation gate — minimum completion fields before the funding
    # team will look at this. Avoid promoting half-built leads.
    errors: list[str] = []
    if not client.name.strip():
        errors.append("Client name required")
    if not (client.email or client.phone):
        errors.append("Either email or phone required")
    if purchase_price <= 0:
        errors.append("Buyer needs a purchase price (or range); seller needs a listing price")
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"errors": errors})

    # Default loan_type to DSCR — funding team can change on review.
    # The agent doesn't pick a loan program (that's the funding team's
    # call). DSCR is the safe default since refis (the most common
    # carryover from leads) are DSCR-only today.
    loan_type = "dscr"

    # Requester = the borrower's own User row when one exists (created
    # via Clerk invite at lead time), otherwise the agent themselves
    # so the FK doesn't violate.
    requester_id = client.user_id or user.id

    prequal = PrequalRequest(
        loan_id=None,  # populated when the borrower accepts an offer / funding creates the loan
        requester_id=requester_id,
        target_property_address=target_property_address,
        purchase_price=purchase_price,
        requested_loan_amount=purchase_price * 0.75,  # placeholder — funding team adjusts
        loan_type=loan_type,
        expected_closing_date=None,
        borrower_notes=intake.get("handoff_note") or None,
    )
    db.add(prequal)
    await db.flush()

    # Notify the funding team via Elara Inbox — picks up alongside other
    # pipeline tasks. loan_id is NULL (firm-wide alert) so super-admin
    # / underwriter see it via the null-loan-task widening rule.
    db.add(
        AITask(
            loan_id=None,
            source=AITaskSource.PIPELINE,
            priority=AITaskPriority.MEDIUM,
            status=AITaskStatus.PENDING,
            action="prequal_handoff_requested",
            title=f"Prequal review requested · {client.name}",
            summary=(
                f"Agent {user.name or user.email} handed off {client.name} "
                f"({side}) for prequalification. Address: {target_property_address}. "
                f"Approx ${purchase_price:,.0f}. Open the prequal queue to review."
            ),
            agent="lead_handoff",
            confidence=1.0,
            draft_payload={
                "prequal_request_id": str(prequal.id),
                "client_id": str(client.id),
                "agent_id": str(user.id),
            },
        )
    )

    client.lead_promotion_status = "agent_requested_review"
    await db.flush()
    await db.refresh(client)

    # ── Lending Handoff Packet (alembic 0031) ──────────────────────
    # The button doesn't just flip a flag — it creates a structured
    # bridge between the Realtor AI's relationship work and the
    # Lending AI's loan-intelligence work. We:
    #
    #   1. Find the client's realtor thread (if any) so we can link
    #      back to the conversation history.
    #   2. Build the handoff packet — extracted facts, missing
    #      lending items, recommended path, first lending question.
    #   3. Persist the packet.
    #   4. Spawn the lending-phase thread (parent_thread_id pointing
    #      at the realtor thread, handoff_packet_id pointing at the
    #      packet, prequal_request_id linking the quote).
    #   5. Drop the first lending AI message into the new thread —
    #      deterministic for v1; the AI takes over from message 2.
    from app.models.ai_chat_thread import AIChatThread
    from app.models.lending_handoff_packet import LendingHandoffPacket
    from app.services.ai.handoff_builder import build_handoff_packet

    realtor_thread = (
        await db.execute(
            select(AIChatThread)
            .where(
                AIChatThread.user_id == user.id,
                AIChatThread.client_id == client.id,
                AIChatThread.loan_id.is_(None),
            )
            .order_by(AIChatThread.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    packet_payload = await build_handoff_packet(
        db,
        client=client,
        agent_id=user.id,
        realtor_thread_id=realtor_thread.id if realtor_thread else None,
    )
    packet_payload["prequal_request_id"] = prequal.id

    packet = LendingHandoffPacket(**packet_payload)
    db.add(packet)
    await db.flush()

    # Mark the realtor thread as phase=realtor (legacy threads were
    # NULL) — gives a clean audit trail post-handoff.
    if realtor_thread is not None and realtor_thread.phase is None:
        realtor_thread.phase = "realtor"

    # Spawn the lending-phase thread linked back to the realtor side.
    # Shared with services/handoff.promote_deal_to_loan's Deal-sourced
    # handoff path via lending_handoff_shared — the two builders read
    # different inputs (Client.lead_intake here, Deal+ClientAIPlan there)
    # and stay intentionally separate, but the thread-spawn shape is now
    # one implementation instead of two independently-maintained copies.
    from app.services.lending_handoff_shared import spawn_lending_thread

    lending_thread = await spawn_lending_thread(
        db,
        user_id=user.id,
        client=client,
        loan_id=None,
        packet=packet,
        packet_payload=packet_payload,
        realtor_thread_id=realtor_thread.id if realtor_thread else None,
        prequal_request_id=prequal.id,
    )

    await db.flush()
    await db.refresh(packet)

    return PrequalHandoffResponse(
        prequal_request_id=prequal.id,
        client_id=client.id,
        lead_promotion_status=client.lead_promotion_status,
        handoff_packet_id=packet.id,
        lending_thread_id=lending_thread.id if lending_thread else None,
        handoff_summary=packet.handoff_summary,
        first_lending_question=packet.first_lending_question,
        missing_lending_items=packet.missing_lending_items or [],
    )


# ── Realtor AI ChatAction confirm-endpoints (alembic 0030) ───────────
#
# Each ChatAction the Realtor AI emits has a backend partner that
# fires when the agent taps the card. v1 ships stubs — they record
# the intent on Client.realtor_profile + spawn an AITask in the
# agent's queue so the work isn't lost. Full template / DocuSign /
# Twilio integrations land in follow-up.


class RealtorActionResponse(BaseModel):
    client_id: UUID
    action_kind: str
    ai_task_id: UUID | None = None


def _realtor_action_response(
    *, client_id: UUID, action_kind: str, ai_task_id: UUID | None
) -> RealtorActionResponse:
    return RealtorActionResponse(
        client_id=client_id,
        action_kind=action_kind,
        ai_task_id=ai_task_id,
    )


async def _spawn_realtor_action_task(
    db: AsyncSession,
    *,
    user: CurrentUser,
    client: Client,
    action_kind: str,
    title: str,
    summary: str,
    payload: dict | None = None,
) -> AITask:
    """Common spawner — every Realtor ChatAction confirm-endpoint
    drops one of these into the agent's Elara Inbox so the actual side
    effect (sending the buyer agreement, scheduling picture day,
    etc.) is queued for the operator's approval."""
    task = AITask(
        loan_id=None,
        source=AITaskSource.PIPELINE,
        priority=AITaskPriority.MEDIUM,
        status=AITaskStatus.PENDING,
        action=action_kind,
        title=title,
        summary=summary,
        agent="realtor_ai",
        confidence=1.0,
        draft_payload={
            "client_id": str(client.id),
            "agent_id": str(user.id),
            **(payload or {}),
        },
    )
    db.add(task)
    await db.flush()
    return task


@router.post("/{client_id}/send-buyer-agreement", response_model=RealtorActionResponse)
async def send_buyer_agreement(
    client_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RealtorActionResponse:
    """v1 stub — records intent + spawns AITask. The actual buyer
    agency agreement template + DocuSign send lands in a follow-up."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    if user.broker and client.broker_id != user.broker.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your client")

    task = await _spawn_realtor_action_task(
        db, user=user, client=client,
        action_kind="send_buyer_agreement",
        title=f"Send buyer agreement · {client.name}",
        summary=(
            f"Agent {user.name or user.email} requested sending the buyer "
            f"agency agreement to {client.name}. Template + DocuSign "
            f"integration is a v2 follow-up — for now, this is a tracked "
            f"reminder."
        ),
    )
    # Mirror onto the realtor_profile so Elara sees the status update.
    if client.realtor_profile:
        bp = client.realtor_profile.get("buyer_profile") or {}
        bp["buyer_agreement_status"] = "sent"
        client.realtor_profile["buyer_profile"] = bp
        await db.flush()
    return _realtor_action_response(
        client_id=client.id, action_kind="send_buyer_agreement", ai_task_id=task.id,
    )


@router.post("/{client_id}/send-listing-agreement", response_model=RealtorActionResponse)
async def send_listing_agreement(
    client_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RealtorActionResponse:
    """v1 stub — records intent + spawns AITask."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    if user.broker and client.broker_id != user.broker.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your client")

    task = await _spawn_realtor_action_task(
        db, user=user, client=client,
        action_kind="send_listing_agreement",
        title=f"Send listing agreement · {client.name}",
        summary=(
            f"Agent {user.name or user.email} requested sending the listing "
            f"agreement to {client.name}. Template + DocuSign integration "
            f"is a v2 follow-up."
        ),
    )
    if client.realtor_profile:
        sp = client.realtor_profile.get("seller_profile") or {}
        sp["listing_agreement_status"] = "sent"
        client.realtor_profile["seller_profile"] = sp
        await db.flush()
    return _realtor_action_response(
        client_id=client.id, action_kind="send_listing_agreement", ai_task_id=task.id,
    )


@router.post("/{client_id}/mark-finance-ready", response_model=RealtorActionResponse)
async def mark_finance_ready(
    client_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RealtorActionResponse:
    """Flips the realtor_profile.relationship_stage = 'finance_ready'
    + Client.stage = 'verified' so the funding team can pick it up.
    Doesn't fire the prequal handoff yet — that's a separate confirm
    via /request-prequalification."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    if user.broker and client.broker_id != user.broker.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your client")

    profile = client.realtor_profile or {}
    profile["relationship_stage"] = "finance_ready"
    client.realtor_profile = profile
    # Keep Client.stage aligned — verified means the agent has done
    # the discovery + agreement work.
    from app.enums import ClientStage as _CS
    if client.stage != _CS.VERIFIED:
        client.stage = _CS.VERIFIED
    await db.flush()
    return _realtor_action_response(
        client_id=client.id, action_kind="mark_client_finance_ready", ai_task_id=None,
    )


# ── Client AI Plan (Phase 2) ───────────────────────────────────────


class ClientAIPlanRead(BaseModel):
    """Resolved active AI plan for one (client, loan?) scope.

    Mirrors `client_ai_plan` 1:1. The agent's portal renders this as
    the AI Plan card on `/clients/[id]`."""
    client_id: UUID
    loan_id: UUID | None
    current_phase: str
    custom_instructions: str | None
    required_items: list[dict]
    waived_items: list[dict]
    ai_suggested_items: list[dict]
    next_best_question: str | None
    next_best_action: dict | None
    readiness_score: int | None
    active_playbook_versions: list[dict]
    computed_at: datetime


class ClientAIPlanPatch(BaseModel):
    """Per-client overrides the agent can apply on top of their playbooks.

    waive_keys / unwaive_keys are toggles against client_requirement_status.
    custom_instructions free-text gets persisted on client_ai_plan.
    ai_secretary_settings is a partial JSONB merge — used by the broker
    mobile NurtureControls to flip auto-nurture / pick cadence preset.
    Any keys not provided are left untouched on the existing row."""
    custom_instructions: str | None = None
    waive_keys: list[str] | None = None
    unwaive_keys: list[str] | None = None
    ai_secretary_settings: dict | None = None
    rebuild: bool = True


@router.get("/{client_id}/ai-plan", response_model=ClientAIPlanRead)
async def get_client_ai_plan(
    client_id: UUID,
    user: CurrentUser,
    loan_id: UUID | None = None,
    db: AsyncSession = Depends(get_db),
) -> ClientAIPlanRead:
    """Return the active AI plan for this (client, loan) scope.
    Triggers a rebuild on every read so the plan stays fresh —
    cheap (a handful of queries)."""
    if user.role not in (Role.BROKER, Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    if user.role == Role.BROKER and user.broker and client.broker_id != user.broker.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your client")

    from app.services.ai.plan_builder import rebuild as rebuild_plan
    plan = await rebuild_plan(db, client_id=client_id, loan_id=loan_id)
    return ClientAIPlanRead(
        client_id=plan.client_id,
        loan_id=plan.loan_id,
        current_phase=plan.current_phase,
        custom_instructions=plan.custom_instructions,
        required_items=list(plan.required_items or []),
        waived_items=list(plan.waived_items or []),
        ai_suggested_items=list(plan.ai_suggested_items or []),
        next_best_question=plan.next_best_question,
        next_best_action=plan.next_best_action,
        readiness_score=plan.readiness_score,
        active_playbook_versions=list(plan.active_playbook_versions or []),
        computed_at=plan.computed_at,
    )


@router.patch("/{client_id}/ai-plan", response_model=ClientAIPlanRead)
async def patch_client_ai_plan(
    client_id: UUID,
    payload: ClientAIPlanPatch,
    user: CurrentUser,
    loan_id: UUID | None = None,
    db: AsyncSession = Depends(get_db),
) -> ClientAIPlanRead:
    """Apply per-client overrides:
      - waive_keys: marks each requirement as `waived` (or `not_applicable`)
        in client_requirement_status. Only succeeds for keys whose
        underlying requirement has can_agent_override=True; others are
        skipped silently. Funding-required items can NOT be waived
        from the agent side.
      - unwaive_keys: flips a previously waived key back to `missing`.
      - custom_instructions: free-text per-client guidance.

    All changes write `ai_audit_events` rows. Triggers a rebuild after
    applying so the response reflects the new plan."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only override surface")
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    if user.broker and client.broker_id != user.broker.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your client")

    from app.models.client_ai_plan import ClientAIPlan
    from app.models.client_requirement_status import ClientRequirementStatus
    from app.services.ai.audit import record_event
    from app.services.ai.plan_builder import rebuild as rebuild_plan
    from app.services.ai.requirement_resolver import resolve_requirements

    # Index of agent-overridable keys for this scope, used to enforce
    # the "agents can't waive funding-required items" rule.
    profile = client.realtor_profile or {}
    bp = profile.get("buyer_profile") or {}
    ctx = {
        "client_type": profile.get("client_type") or client.client_type,
        "financing_needed": bp.get("financing_needed"),
        "target_property_type": bp.get("target_property_type"),
        "under_contract": bp.get("under_contract"),
    }
    side = "buyer" if (profile.get("client_type") in ("buyer", "buyer_and_seller")) else (
        "seller" if profile.get("client_type") == "seller" else None
    )
    resolved = await resolve_requirements(
        db,
        client_id=client_id, loan_id=loan_id,
        phase="lending" if loan_id else "realtor",
        side=side, agent_id=user.id, context=ctx,
    )
    overridable = {r.requirement_key for r in resolved if r.can_agent_override}

    async def _set_status(key: str, status_value: str) -> None:
        existing = (await db.execute(
            select(ClientRequirementStatus).where(
                ClientRequirementStatus.client_id == client_id,
                ClientRequirementStatus.loan_id == loan_id if loan_id is not None else ClientRequirementStatus.loan_id.is_(None),
                ClientRequirementStatus.requirement_key == key,
            )
        )).scalar_one_or_none()
        old = existing.status if existing else None
        if existing is None:
            db.add(ClientRequirementStatus(
                client_id=client_id, loan_id=loan_id, requirement_key=key,
                status=status_value, source="client_custom",
            ))
        else:
            existing.status = status_value
            existing.source = "client_custom"
        await record_event(
            db, event_type="requirement_waived" if status_value == "waived" else "client_override_added",
            actor_type="user", actor_id=user.id, client_id=client_id, loan_id=loan_id,
            requirement_key=key, old_value={"status": old} if old else None,
            new_value={"status": status_value},
        )

    for key in (payload.waive_keys or []):
        if key in overridable:
            await _set_status(key, "waived")
    for key in (payload.unwaive_keys or []):
        if key in overridable:
            await _set_status(key, "missing")

    if payload.custom_instructions is not None:
        plan_row = (await db.execute(
            select(ClientAIPlan).where(
                ClientAIPlan.client_id == client_id,
                ClientAIPlan.loan_id == loan_id if loan_id is not None else ClientAIPlan.loan_id.is_(None),
            )
        )).scalar_one_or_none()
        if plan_row is not None:
            old_instr = plan_row.custom_instructions
            plan_row.custom_instructions = payload.custom_instructions or None
            await record_event(
                db, event_type="client_custom_instructions_updated",
                actor_type="user", actor_id=user.id, client_id=client_id, loan_id=loan_id,
                old_value={"text": old_instr} if old_instr else None,
                new_value={"text": payload.custom_instructions},
            )
            await db.flush()

    # Phase 7 — broker NurtureControls writes ai_secretary_settings here
    # to drive the Client-AI nurturing tier on the realtor-phase plan.
    # Merge the incoming partial into the existing JSONB (any keys not
    # provided stay as-is). Audit-logged like the other PATCH operations.
    if payload.ai_secretary_settings is not None:
        plan_row = (await db.execute(
            select(ClientAIPlan).where(
                ClientAIPlan.client_id == client_id,
                ClientAIPlan.loan_id == loan_id if loan_id is not None else ClientAIPlan.loan_id.is_(None),
            )
        )).scalar_one_or_none()
        if plan_row is not None:
            old_settings = dict(plan_row.ai_secretary_settings or {})
            merged = {**old_settings, **(payload.ai_secretary_settings or {})}
            plan_row.ai_secretary_settings = merged
            await record_event(
                db, event_type="ai_secretary_settings_updated",
                actor_type="user", actor_id=user.id, client_id=client_id, loan_id=loan_id,
                old_value=old_settings or None,
                new_value=merged,
            )
            await db.flush()
            # NOTE: When outreach_mode flips from "off" → anything else
            # on a realtor-phase plan, the next cadence_engine tick will
            # see this client as eligible IF AITaskAssignment rows
            # exist. Pre-loan materialization of pending_assignments is
            # a follow-up — see plan §7.4 in
            # QCDashboard/docs/qcbackend-patches notes.

    plan = await rebuild_plan(db, client_id=client_id, loan_id=loan_id) if payload.rebuild else None
    if plan is None:
        # Caller asked us to skip rebuild — return the row as-is.
        from app.models.client_ai_plan import ClientAIPlan as _CAIP
        plan = (await db.execute(
            select(_CAIP).where(
                _CAIP.client_id == client_id,
                _CAIP.loan_id == loan_id if loan_id is not None else _CAIP.loan_id.is_(None),
            )
        )).scalar_one_or_none()
        if plan is None:
            plan = await rebuild_plan(db, client_id=client_id, loan_id=loan_id)

    return ClientAIPlanRead(
        client_id=plan.client_id,
        loan_id=plan.loan_id,
        current_phase=plan.current_phase,
        custom_instructions=plan.custom_instructions,
        required_items=list(plan.required_items or []),
        waived_items=list(plan.waived_items or []),
        ai_suggested_items=list(plan.ai_suggested_items or []),
        next_best_question=plan.next_best_question,
        next_best_action=plan.next_best_action,
        readiness_score=plan.readiness_score,
        active_playbook_versions=list(plan.active_playbook_versions or []),
        computed_at=plan.computed_at,
    )


# ── Send intake link (Phase 7 nurturing — broker → lead) ────────────


class SendIntakeLinkRequest(BaseModel):
    """Channel preference — null lets the backend pick based on
    contact_permission + which contact info we have (phone vs email)."""
    channel: str | None = None


class SendIntakeLinkResponse(BaseModel):
    url: str
    sent_via: str
    sent_at: datetime


@router.post(
    "/{client_id}/send-intake-link",
    response_model=SendIntakeLinkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def send_intake_link(
    client_id: UUID,
    payload: SendIntakeLinkRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SendIntakeLinkResponse:
    """Generate a sign-up link for this client and actually send it.

    Channel resolution: explicit > phone (sms) > email > portal. The "sms"
    branch goes through sms_service.send_sms_checked, which resolves the
    suppression list from the database before sending — a number that replied
    STOP anywhere is unreachable everywhere. On any failure, including an
    unconfigured transport, sent_via falls back to "portal" rather than
    silently pretending to text the client. The "email" branch sends via
    send_as_user (the
    broker's connected Gmail, SES fallback) — sent_via only reports "email"
    when that send genuinely succeeds; otherwise it reports "portal" so the
    broker isn't told a message went out when it didn't. Every outcome logs
    an Activity row for the audit trail.

    Role: BROKER (must own the client), SUPER_ADMIN, LOAN_EXEC.
    """
    from datetime import datetime as _dt, timezone as _tz
    from app.models.activity import Activity
    from app.config import get_settings
    from app.services.email.user_mailer import send_as_user

    if user.role in {Role.CLIENT, Role.REGIONAL_MANAGER}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator-only action")
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    if user.role == Role.BROKER and user.broker and client.broker_id != user.broker.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your client")
    if client.contact_permission == "save_lead_only":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This client was saved as lead-only. Enable outreach on the client before sending an intake link.",
        )

    # Channel resolution: explicit > phone (sms) > email > portal.
    requested_via = (payload.channel or "").lower() or None
    if requested_via not in {"portal", "email", "sms"}:
        if client.phone:
            requested_via = "sms"
        elif client.email:
            requested_via = "email"
        else:
            requested_via = "portal"

    settings = get_settings()
    url = f"{settings.frontend_app_url.rstrip('/')}/sign-up"

    sent_via = "portal"
    send_detail = "No email on file — share the link directly."
    if requested_via == "sms":
        from app.services import sms as sms_service

        sms_result = await sms_service.send_sms_checked(
            db,
            to_phone=client.phone,
            body=(
                f"{user.name or user.email} has invited you to your secure "
                f"Qualified Commercial file. Sign up here: {url}\n\n"
                "Reply STOP to opt out."
            ),
        )
        if sms_result.ok:
            sent_via = "sms"
            send_detail = f"Texted to {client.phone}."
        else:
            # Stays "portal" on failure. The broker is told what happened rather
            # than being left to assume a text went out.
            send_detail = f"Text send failed ({sms_result.detail}) — share the link directly."
    elif requested_via == "email" and client.email:
        result = await send_as_user(
            db,
            user.id,
            to_emails=[client.email],
            subject="Your secure Qualified Commercial file",
            body_text=(
                f"Hi {client.name},\n\n"
                f"{user.name or user.email} has invited you to your secure Qualified Commercial file. "
                f"Sign up here to get started:\n{url}\n\n"
                "If you weren't expecting this, you can safely ignore this email."
            ),
        )
        if result.ok:
            sent_via = "email"
            send_detail = f"Emailed to {client.email}."
        else:
            send_detail = f"Email send failed ({result.detail}) — share the link directly."

    now = _dt.now(_tz.utc)
    db.add(
        Activity(
            client_id=client_id,
            actor_id=user.id,
            actor_label=user.email,
            kind="nurture.intake_link_sent",
            summary=f"{user.email} sent intake link via {sent_via}. {send_detail}",
        )
    )
    await db.flush()

    return SendIntakeLinkResponse(url=url, sent_via=sent_via, sent_at=now)


# ── Agent reassignment (alembic 0097) ───────────────────────────────
#
# Moves a client from one agent to another. Writes an audit row (the
# append-only history), updates Client.current_agent_id (leaves
# originating_agent_id untouched — that's who first captured the lead,
# never changes), and reassigns Loan.broker_id on the client's active
# loans so open AITasks (which have no agent column of their own —
# they inherit ownership transitively via loan_id -> Loan.broker_id)
# follow the client to the new agent automatically.
#
# Narrower than the generic PATCH /clients/{id}'s broker_id path (which
# also allows LOAN_EXEC) — reassignment is a more sensitive action, so
# this is SUPER_ADMIN only, by design.


class ReassignAgentRequest(BaseModel):
    to_agent_id: UUID
    reason: str | None = None


@router.patch("/{client_id}/agent", response_model=ClientRead)
async def reassign_agent(
    client_id: UUID,
    payload: ReassignAgentRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClientRead:
    """Reassign a client's owning agent. Super-admin only."""
    from app.models.agent_reassignment_audit import AgentReassignmentAudit
    from app.models.broker import Broker
    from app.models.loan import Loan
    from app.models.user import User

    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin only")

    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")

    to_agent = (await db.execute(select(User).where(User.id == payload.to_agent_id))).scalar_one_or_none()
    if to_agent is None or to_agent.role != Role.BROKER:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "to_agent_id must be an existing broker user")

    from_agent_id = client.current_agent_id

    db.add(
        AgentReassignmentAudit(
            client_id=client_id,
            from_agent_id=from_agent_id,
            to_agent_id=payload.to_agent_id,
            reason=payload.reason,
            performed_by_user_id=user.id,
        )
    )

    client.current_agent_id = payload.to_agent_id

    # Transfer open AITasks by reassigning the underlying loans' broker_id —
    # AITask has no direct agent column, it inherits ownership via
    # loan_id -> Loan.broker_id, which the existing broker-scoped task
    # queries (app/routers/ai_tasks.py) already filter on.
    to_broker = (await db.execute(select(Broker).where(Broker.user_id == payload.to_agent_id))).scalar_one_or_none()
    if to_broker is not None:
        from_broker = None
        if from_agent_id is not None:
            from_broker = (
                await db.execute(select(Broker).where(Broker.user_id == from_agent_id))
            ).scalar_one_or_none()
        loans_stmt = select(Loan).where(Loan.client_id == client_id)
        if from_broker is not None:
            loans_stmt = loans_stmt.where(Loan.broker_id == from_broker.id)
        loans = (await db.execute(loans_stmt)).scalars().all()
        for loan in loans:
            loan.broker_id = to_broker.id

    await db.flush()
    await db.refresh(client)
    return ClientRead.model_validate(client)


# ── Client Properties (alembic 0034) ────────────────────────────────


from decimal import Decimal as _Decimal


class ClientPropertyRead(BaseModel):
    id: UUID
    client_id: UUID
    side: str
    status: str
    address: str | None
    city: str | None
    state: str | None
    zip: str | None
    property_type: str | None
    target_price: _Decimal | None
    list_price: _Decimal | None
    sold_price: _Decimal | None
    bedrooms: int | None
    bathrooms: _Decimal | None
    sqft: int | None
    units: int | None
    notes: str | None
    linked_loan_id: UUID | None
    created_at: datetime
    updated_at: datetime


class ClientPropertyCreate(BaseModel):
    side: str  # buyer_target | seller_listing
    status: str = "active"
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    property_type: str | None = None
    target_price: _Decimal | None = None
    list_price: _Decimal | None = None
    sold_price: _Decimal | None = None
    bedrooms: int | None = None
    bathrooms: _Decimal | None = None
    sqft: int | None = None
    units: int | None = None
    notes: str | None = None
    linked_loan_id: UUID | None = None


class ClientPropertyUpdate(BaseModel):
    status: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    property_type: str | None = None
    target_price: _Decimal | None = None
    list_price: _Decimal | None = None
    sold_price: _Decimal | None = None
    bedrooms: int | None = None
    bathrooms: _Decimal | None = None
    sqft: int | None = None
    units: int | None = None
    notes: str | None = None
    linked_loan_id: UUID | None = None


def _check_client_access(user, client) -> None:
    """Guard: only the agent owning the client (BROKER) or admins can
    touch the client's properties."""
    if user.role not in (Role.BROKER, Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")
    if user.role == Role.BROKER and user.broker and client.broker_id != user.broker.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your client")


def _serialize_property(row) -> ClientPropertyRead:
    return ClientPropertyRead(
        id=row.id, client_id=row.client_id,
        side=row.side, status=row.status,
        address=row.address, city=row.city, state=row.state, zip=row.zip,
        property_type=row.property_type,
        target_price=row.target_price, list_price=row.list_price, sold_price=row.sold_price,
        bedrooms=row.bedrooms, bathrooms=row.bathrooms, sqft=row.sqft, units=row.units,
        notes=row.notes, linked_loan_id=row.linked_loan_id,
        created_at=row.created_at, updated_at=row.updated_at,
    )


@router.get("/{client_id}/properties", response_model=list[ClientPropertyRead])
async def list_client_properties(
    client_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[ClientPropertyRead]:
    """Active + non-archived properties for one client. Buyer targets
    + seller listings appear in the same list — caller filters by side."""
    from app.models.client_property import ClientProperty
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    _check_client_access(user, client)
    rows = (await db.execute(
        select(ClientProperty)
        .where(ClientProperty.client_id == client_id, ClientProperty.status != "archived")
        .order_by(ClientProperty.created_at.desc())
    )).scalars().all()
    return [_serialize_property(r) for r in rows]


@router.post("/{client_id}/properties", response_model=ClientPropertyRead, status_code=status.HTTP_201_CREATED)
async def create_client_property(
    client_id: UUID,
    payload: ClientPropertyCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClientPropertyRead:
    from app.models.client_property import ClientProperty
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    _check_client_access(user, client)
    if payload.side not in ("buyer_target", "seller_listing"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "side must be buyer_target or seller_listing")
    row = ClientProperty(
        client_id=client_id,
        side=payload.side,
        status=payload.status or "active",
        address=payload.address, city=payload.city, state=payload.state, zip=payload.zip,
        property_type=payload.property_type,
        target_price=payload.target_price, list_price=payload.list_price, sold_price=payload.sold_price,
        bedrooms=payload.bedrooms, bathrooms=payload.bathrooms, sqft=payload.sqft, units=payload.units,
        notes=payload.notes, linked_loan_id=payload.linked_loan_id,
    )
    db.add(row)
    await db.flush()
    return _serialize_property(row)


@router.patch("/{client_id}/properties/{property_id}", response_model=ClientPropertyRead)
async def update_client_property(
    client_id: UUID,
    property_id: UUID,
    payload: ClientPropertyUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClientPropertyRead:
    from app.models.client_property import ClientProperty
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    _check_client_access(user, client)
    row = await db.get(ClientProperty, property_id)
    if row is None or row.client_id != client_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    await db.flush()
    return _serialize_property(row)


@router.delete("/{client_id}/properties/{property_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_client_property(
    client_id: UUID,
    property_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete by flipping status to 'archived'. Hard delete is
    intentionally not exposed — agents have asked for an undo affordance
    on dropped properties before."""
    from app.models.client_property import ClientProperty
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    _check_client_access(user, client)
    row = await db.get(ClientProperty, property_id)
    if row is None or row.client_id != client_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")
    row.status = "archived"
    await db.flush()


# ── Agent notes — write surface for the workspace's Notes tab ──────


class AgentNoteIn(BaseModel):
    text: str
    field: str | None = None  # optional grouping label, defaults to "note"


@router.post("/{client_id}/notes")
async def add_agent_note(
    client_id: UUID,
    payload: AgentNoteIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Append a free-form note to the client's realtor_profile.known_facts
    with source=agent, tagged agent_private. The Realtor Elara sees this on
    the next chat turn via apply_profile_patch's known_fact merge — but it
    is stripped before the client can read their own profile (see
    filter_known_facts_for_client, applied at GET/PATCH /clients/me)."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only")
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty note")
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    if user.broker and client.broker_id != user.broker.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your client")

    from app.services.ai.realtor_profile import apply_profile_patch
    profile = client.realtor_profile or {}
    patched = apply_profile_patch(
        profile,
        {"known_fact": {"field": payload.field or "note", "value": text, "source": "agent", "visibility": "agent_private"}},
        client_id=str(client_id),
        agent_id=str(user.id),
    )
    client.realtor_profile = patched
    await db.flush()
    return {"ok": True}


# ── Engagement / activity log (alembic 0035 adds client_id to Activity) ─


class EngagementSignalRead(BaseModel):
    id: UUID
    kind: str
    summary: str
    actor_label: str | None
    created_at: datetime  # sourced from Activity.occurred_at (the model has no created_at)
    payload: dict | None


class EngagementSignalCreate(BaseModel):
    kind: str           # call_logged | sms_sent | email_sent | meeting_held | note
    summary: str
    payload: dict | None = None


@router.get("/{client_id}/engagement", response_model=list[EngagementSignalRead])
async def list_client_engagement(
    client_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[EngagementSignalRead]:
    """Activity feed for one client — newest first. Includes manually-
    logged calls/SMS/meetings + AI-spawned events (anything written to
    Activity with client_id set)."""
    from app.models.activity import Activity

    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    _check_client_access(user, client)

    rows = (await db.execute(
        select(Activity)
        .where(Activity.client_id == client_id)
        .order_by(Activity.occurred_at.desc())
        .limit(100)
    )).scalars().all()
    return [
        EngagementSignalRead(
            id=r.id, kind=r.kind, summary=r.summary or "",
            actor_label=r.actor_label, created_at=r.occurred_at, payload=r.payload,
        )
        for r in rows
    ]


@router.post("/{client_id}/engagement", response_model=EngagementSignalRead, status_code=status.HTTP_201_CREATED)
async def log_client_engagement(
    client_id: UUID,
    payload: EngagementSignalCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> EngagementSignalRead:
    """Agent logs a call / SMS / meeting / email against a client.
    The AI's per-client thread sees these as historical context on the
    next chat turn (we don't auto-thread the activity into the chat,
    but the resolver can read them on rebuild)."""
    from app.models.activity import Activity

    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    _check_client_access(user, client)
    text = (payload.summary or "").strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty summary")

    actor_label = "agent" if user.role == Role.BROKER else "operator"
    row = Activity(
        client_id=client_id,
        actor_id=user.id,
        actor_label=actor_label,
        kind=payload.kind or "note",
        summary=text[:512],
        payload=payload.payload,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)  # populate server-defaulted occurred_at
    return EngagementSignalRead(
        id=row.id, kind=row.kind, summary=row.summary,
        actor_label=row.actor_label, created_at=row.occurred_at, payload=row.payload,
    )
