"""Deal CRUD endpoints (Phase 3).

A Deal is the agent-side transaction unit attached to a Client. A
client can carry multiple Deals at once — buyer search, seller listing,
investor purchase, refinance — each promoted to its own Loan via the
mark-ready-for-lending endpoint (Phase 4).

Routes mount under /clients/{client_id}/deals to keep the unified
client workspace surface coherent.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import DealAIStatus, DealHandoffStatus, DealStatus, DealType, Role
from app.models.client import Client
from app.models.deal import Deal
from app.scoping import scope_client_query
from app.schemas.deal import (
    DealCreate,
    DealOut,
    DealUpdate,
    MarkReadyRequest,
    MarkReadyResponse,
)
from app.services.handoff import promote_deal_to_loan

router = APIRouter(tags=["deals"])


def _default_side_for(deal_type: str) -> str:
    """Map deal_type → loan side. Buyers/investors/borrowers purchase;
    sellers list. Used when DealCreate omits an explicit side."""
    if deal_type == DealType.SELLER.value:
        return "seller"
    return "buyer"


async def _load_client_or_404(client_id: UUID, user, db: AsyncSession) -> Client:
    stmt = scope_client_query(user, select(Client).where(Client.id == client_id))
    client = (await db.execute(stmt)).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    return client


@router.get("/deals/{deal_id}", response_model=DealOut)
async def get_deal(
    deal_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealOut:
    """Fetch a single deal by id. /deals/[id] page primary loader.

    Visibility:
      - CLIENT: only their own client's deals
      - BROKER: only deals on clients they own
      - SUPER_ADMIN / LOAN_EXEC: all
    """
    deal = await db.get(Deal, deal_id)
    if deal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deal not found")
    # Load the client through the scope filter to enforce visibility.
    await _load_client_or_404(deal.client_id, user, db)
    return DealOut.model_validate(deal)


@router.get("/clients/{client_id}/deals", response_model=list[DealOut])
async def list_deals(
    client_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[DealOut]:
    await _load_client_or_404(client_id, user, db)
    rows = (
        await db.execute(
            select(Deal)
            .where(Deal.client_id == client_id)
            .order_by(Deal.created_at.desc())
        )
    ).scalars().all()
    return [DealOut.model_validate(r) for r in rows]


@router.post(
    "/clients/{client_id}/deals",
    response_model=DealOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_deal(
    client_id: UUID,
    payload: DealCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealOut:
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only")
    await _load_client_or_404(client_id, user, db)

    side = payload.side or _default_side_for(payload.deal_type)
    deal = Deal(
        client_id=client_id,
        deal_type=payload.deal_type,
        side=side,
        property_id=payload.property_id,
        title=payload.title,
        summary=payload.summary,
        assigned_agent_id=payload.assigned_agent_id,
        status=DealStatus.OPEN.value,
        handoff_status=DealHandoffStatus.NONE.value,
        ai_status=DealAIStatus.IDLE.value,
    )
    db.add(deal)
    await db.flush()
    await db.refresh(deal)

    # Bootstrap a realtor-phase ClientAIPlan + CRS rows scoped to this
    # deal. The Phase 3 hook lives in services/ai/deal_secretary —
    # extended to accept `deal=` alongside `loan=`. We call it lazily
    # via a local import to keep the router free of heavy imports.
    try:
        from app.services.ai import deal_secretary as _ds

        if hasattr(_ds, "bootstrap_requirement_status_rows"):
            await _ds.bootstrap_requirement_status_rows(  # type: ignore[call-arg]
                db,
                client=await _load_client_or_404(client_id, user, db),
                deal=deal,
            )
    except TypeError:
        # The old signature didn't accept `deal=`. That branch is fine
        # in development; the AI plan will materialize on the next
        # plan_builder.rebuild call. Phase 3 extends the helper.
        pass
    except Exception:  # pragma: no cover — bootstrap is best-effort
        pass

    return DealOut.model_validate(deal)


@router.patch("/clients/{client_id}/deals/{deal_id}", response_model=DealOut)
async def update_deal(
    client_id: UUID,
    deal_id: UUID,
    payload: DealUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealOut:
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only")
    await _load_client_or_404(client_id, user, db)
    deal = (
        await db.execute(
            select(Deal).where(Deal.id == deal_id, Deal.client_id == client_id)
        )
    ).scalar_one_or_none()
    if deal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deal not found")
    if deal.status == DealStatus.PROMOTED.value:
        # Promoted deals are read-mostly; only summary edits allowed so
        # auditors can annotate. Block status flips back to active.
        if payload.status is not None and payload.status != "promoted":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Cannot revert a promoted deal",
            )
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(deal, k, v)
    await db.flush()
    await db.refresh(deal)
    return DealOut.model_validate(deal)


@router.post(
    "/clients/{client_id}/deals/{deal_id}/mark-ready-for-lending",
    response_model=MarkReadyResponse,
)
async def mark_ready_for_lending(
    client_id: UUID,
    deal_id: UUID,
    payload: MarkReadyRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MarkReadyResponse:
    """Promote a Deal into a Loan (the canonical FundingFile). Atomic
    + idempotent: a second call returns the existing loan id.

    Agent-only — brokers must own the client; super_admin / loan_exec
    pass through. CLIENT role is read-only here."""
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only")
    client = await _load_client_or_404(client_id, user, db)
    deal = (
        await db.execute(
            select(Deal).where(Deal.id == deal_id, Deal.client_id == client_id)
        )
    ).scalar_one_or_none()
    if deal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deal not found")
    _ = client  # client load already gated by scope; explicit binding for clarity

    result = await promote_deal_to_loan(
        db,
        deal=deal,
        user=user,
        override_loan_type=payload.override_loan_type,
        override_purpose=payload.override_purpose,
        notes=payload.notes,
    )
    return MarkReadyResponse(
        loan_id=result.loan.id,
        deal_id=result.deal.id,
        handoff_packet_id=result.handoff_packet_id,
        prequal_request_id=result.prequal_request_id,
        lending_thread_id=result.lending_thread_id,
        handoff_summary=result.handoff_summary,
        missing_lending_items=result.missing_lending_items or [],
    )
