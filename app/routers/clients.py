from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.client import Client
from app.schemas.client import ClientCreate, ClientRead, ClientUpdate

router = APIRouter(prefix="/clients", tags=["clients"])


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
    client = Client(**payload.model_dump())
    if user.role == Role.BROKER and user.broker and not client.broker_id:
        client.broker_id = user.broker.id
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
    """Partial update. Only fields present in the payload are applied."""
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only")
    stmt = _scope(user, select(Client).where(Client.id == client_id))
    client = (await db.execute(stmt)).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(client, k, v)
    await db.flush()
    await db.refresh(client)
    return ClientRead.model_validate(client)
