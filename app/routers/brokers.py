from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_role
from app.enums import Role
from app.models.broker import Broker
from app.schemas.broker import BrokerRead, LeaderboardEntry

router = APIRouter(prefix="/brokers", tags=["brokers"])


@router.get("", response_model=list[BrokerRead])
async def list_brokers(
    db: AsyncSession = Depends(get_db),
    user=Depends(require_role(Role.SUPER_ADMIN, Role.BROKER, Role.LOAN_EXEC)),
) -> list[BrokerRead]:
    rows = (await db.execute(select(Broker).order_by(Broker.display_name))).scalars().all()
    return [BrokerRead.model_validate(r) for r in rows]


@router.get(
    "/leaderboard",
    response_model=list[LeaderboardEntry],
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def leaderboard(db: AsyncSession = Depends(get_db)) -> list[LeaderboardEntry]:
    rows = (
        await db.execute(select(Broker).order_by(Broker.lifetime_points.desc()))
    ).scalars().all()
    return [
        LeaderboardEntry(
            rank=i + 1,
            broker_id=b.id,
            display_name=b.display_name,
            tier=b.tier,
            lifetime_points=b.lifetime_points,
            funded_total=float(b.funded_total or 0),
            funded_count=b.funded_count,
        )
        for i, b in enumerate(rows)
    ]
