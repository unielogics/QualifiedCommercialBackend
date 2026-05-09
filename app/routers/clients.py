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
    if user.role == Role.CLIENT and user.client:
        return stmt.where(Client.id == user.client.id)
    if user.role == Role.BROKER and user.broker:
        return stmt.where(Client.broker_id == user.broker.id)
    return stmt


@router.get("", response_model=list[ClientRead])
async def list_clients(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[ClientRead]:
    stmt = _scope(user, select(Client).order_by(Client.name))
    rows = (await db.execute(stmt)).scalars().all()
    return [ClientRead.model_validate(r) for r in rows]


@router.get("/me", response_model=ClientRead)
async def get_my_client(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ClientRead:
    """Return the current user's linked Client record. Used by the desktop
    Profile page so it doesn't need to know its own client_id."""
    if not user.client:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Current user has no linked client record"
        )
    return ClientRead.model_validate(user.client)


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
    return ClientRead.model_validate(client)


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
    if user.role == Role.CLIENT:
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
    stmt = _scope(user, select(Client).where(Client.id == client_id))
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    return ClientRead.model_validate(row)


@router.post("", response_model=ClientRead, status_code=status.HTTP_201_CREATED)
async def create_client(
    payload: ClientCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ClientRead:
    if user.role == Role.CLIENT:
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

    if user.role == Role.CLIENT:
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
    await db.flush()
    await db.refresh(client)
    return ClientRead.model_validate(client)


# ── Lead → Prequal handoff (alembic 0029) ────────────────────────────
#
# When the agent has nurtured a lead enough that they want the firm's
# funding team to look at it, they fire this endpoint (either via the
# "Ready for Prequalification" button on /clients/[id] or through a
# tool the AI Secretary calls on their behalf). It:
#
#   1. Validates the lead has the minimum fields the funding team
#      needs to triage (name, email, side-specific price, address).
#   2. Creates a PrequalRequest from `Client.lead_intake` so the
#      existing `/admin/prequal-requests` queue picks it up.
#   3. Spawns an AITask in the funding-team queue so AI Inbox surfaces
#      it for whoever's on triage that day.
#   4. Stamps `client.lead_promotion_status = "agent_requested_review"`.
#
# Idempotent on retry — checks lead_promotion_status before firing.

class PrequalHandoffResponse(BaseModel):
    prequal_request_id: UUID
    client_id: UUID
    lead_promotion_status: str


@router.post("/{client_id}/request-prequalification", response_model=PrequalHandoffResponse)
async def request_prequalification(
    client_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PrequalHandoffResponse:
    """Hand a lead off to the funding team. Creates a PrequalRequest
    + AITask. Agent-only — must own the client."""
    if user.role == Role.CLIENT:
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

    # Notify the funding team via AI Inbox — picks up alongside other
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

    return PrequalHandoffResponse(
        prequal_request_id=prequal.id,
        client_id=client.id,
        lead_promotion_status=client.lead_promotion_status,
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
    drops one of these into the agent's AI Inbox so the actual side
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
    # Mirror onto the realtor_profile so the AI sees the status update.
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
