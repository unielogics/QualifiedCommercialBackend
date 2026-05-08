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
from app.enums import Role
from app.models.client import Client
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
