from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser, require_role
from app.enums import AITaskPriority, AITaskStatus, CalendarEventStatus, DocStatus, LoanStage, Role
from app.models.ai_task import AITask
from app.models.broker import Broker
from app.models.client import Client
from app.models.document import Document
from app.models.event import CalendarEvent
from app.models.loan import Loan
from app.models.regional_manager import RegionalManagerAgent
from app.models.user import User
from app.services import clerk as clerk_service

router = APIRouter(prefix="/regional-managers", tags=["regional-managers"])


class PortfolioMetrics(BaseModel):
    agent_count: int
    client_count: int
    active_loans: int
    pipeline_value: float
    funded_ytd: float
    pull_through: float | None
    high_priority_tasks: int
    overdue_items: int


class RegionalManagerSummary(BaseModel):
    id: UUID
    email: EmailStr | str
    name: str
    created_at: datetime | None = None
    metrics: PortfolioMetrics


class ManagedAgentRead(BaseModel):
    user_id: UUID
    email: EmailStr | str
    name: str
    broker_id: UUID | None = None
    display_name: str | None = None
    linked_at: datetime
    metrics: PortfolioMetrics


class RegionalManagerDetail(RegionalManagerSummary):
    agents: list[ManagedAgentRead]


class RegionalManagerInvite(BaseModel):
    email: EmailStr
    name: str


class RegionalManagerAgentUpsert(BaseModel):
    agent_user_id: UUID | None = None
    email: EmailStr | None = None
    name: str | None = None


def _linked_agent_user_ids(manager_user_id: UUID):
    return select(RegionalManagerAgent.agent_user_id).where(
        RegionalManagerAgent.manager_user_id == manager_user_id
    )


def _portfolio_broker_ids(manager_user_id: UUID):
    return select(Broker.id).where(
        or_(
            Broker.user_id == manager_user_id,
            Broker.user_id.in_(_linked_agent_user_ids(manager_user_id)),
        )
    )


def _single_broker_id(broker_id: UUID):
    return select(Broker.id).where(Broker.id == broker_id)


async def _metrics(db: AsyncSession, broker_ids_stmt) -> PortfolioMetrics:
    today = date.today()
    year_start = date(today.year, 1, 1)
    now = datetime.now(timezone.utc)

    agent_count = int(
        (await db.execute(select(func.count(Broker.id)).where(Broker.id.in_(broker_ids_stmt)))).scalar()
        or 0
    )
    client_count = int(
        (await db.execute(select(func.count(Client.id)).where(Client.broker_id.in_(broker_ids_stmt)))).scalar()
        or 0
    )
    active_loans = int(
        (
            await db.execute(
                select(func.count(Loan.id)).where(
                    Loan.broker_id.in_(broker_ids_stmt),
                    Loan.stage != LoanStage.FUNDED,
                )
            )
        ).scalar()
        or 0
    )
    pipeline_value = float(
        (
            await db.execute(
                select(func.coalesce(func.sum(Loan.amount), 0)).where(
                    Loan.broker_id.in_(broker_ids_stmt),
                    Loan.stage != LoanStage.FUNDED,
                )
            )
        ).scalar()
        or 0
    )
    funded_ytd = float(
        (
            await db.execute(
                select(func.coalesce(func.sum(Loan.amount), 0)).where(
                    Loan.broker_id.in_(broker_ids_stmt),
                    Loan.stage == LoanStage.FUNDED,
                    Loan.close_date.is_not(None),
                    Loan.close_date >= year_start,
                )
            )
        ).scalar()
        or 0
    )
    funded_count = int(
        (
            await db.execute(
                select(func.count(Loan.id)).where(
                    Loan.broker_id.in_(broker_ids_stmt),
                    Loan.stage == LoanStage.FUNDED,
                )
            )
        ).scalar()
        or 0
    )
    total_lifecycle = active_loans + funded_count
    pull_through = (funded_count / total_lifecycle) if total_lifecycle else None
    high_priority_tasks = int(
        (
            await db.execute(
                select(func.count(AITask.id)).where(
                    AITask.loan_id.in_(select(Loan.id).where(Loan.broker_id.in_(broker_ids_stmt))),
                    AITask.status == AITaskStatus.PENDING,
                    AITask.priority == AITaskPriority.HIGH,
                )
            )
        ).scalar()
        or 0
    )
    overdue_docs = int(
        (
            await db.execute(
                select(func.count(Document.id))
                .join(Loan, Loan.id == Document.loan_id)
                .where(
                    Loan.broker_id.in_(broker_ids_stmt),
                    Document.status == DocStatus.REQUESTED,
                    Document.due_date.is_not(None),
                    Document.due_date < today,
                )
            )
        ).scalar()
        or 0
    )
    overdue_calendar = int(
        (
            await db.execute(
                select(func.count(CalendarEvent.id)).where(
                    CalendarEvent.loan_id.in_(select(Loan.id).where(Loan.broker_id.in_(broker_ids_stmt))),
                    CalendarEvent.status == CalendarEventStatus.PENDING,
                    CalendarEvent.starts_at < now,
                )
            )
        ).scalar()
        or 0
    )
    return PortfolioMetrics(
        agent_count=agent_count,
        client_count=client_count,
        active_loans=active_loans,
        pipeline_value=pipeline_value,
        funded_ytd=funded_ytd,
        pull_through=pull_through,
        high_priority_tasks=high_priority_tasks,
        overdue_items=overdue_docs + overdue_calendar,
    )


async def _manager_or_404(db: AsyncSession, manager_user_id: UUID) -> User:
    row = (
        await db.execute(
            select(User).where(
                User.id == manager_user_id,
                User.role == Role.REGIONAL_MANAGER,
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Regional manager not found")
    return row


async def _agents_for_manager(db: AsyncSession, manager_user_id: UUID) -> list[ManagedAgentRead]:
    rows = (
        await db.execute(
            select(RegionalManagerAgent, User, Broker)
            .join(User, User.id == RegionalManagerAgent.agent_user_id)
            .outerjoin(Broker, Broker.user_id == User.id)
            .where(RegionalManagerAgent.manager_user_id == manager_user_id, User.deleted_at.is_(None))
            .order_by(User.name)
        )
    ).all()
    out: list[ManagedAgentRead] = []
    for link, user, broker in rows:
        broker_id = getattr(broker, "id", None)
        out.append(
            ManagedAgentRead(
                user_id=user.id,
                email=user.email,
                name=user.name,
                broker_id=broker_id,
                display_name=getattr(broker, "display_name", None),
                linked_at=link.created_at,
                metrics=await _metrics(db, _single_broker_id(broker_id)) if broker_id else PortfolioMetrics(
                    agent_count=0,
                    client_count=0,
                    active_loans=0,
                    pipeline_value=0,
                    funded_ytd=0,
                    pull_through=None,
                    high_priority_tasks=0,
                    overdue_items=0,
                ),
            )
        )
    return out


async def _detail(db: AsyncSession, manager: User) -> RegionalManagerDetail:
    return RegionalManagerDetail(
        id=manager.id,
        email=manager.email,
        name=manager.name,
        created_at=manager.created_at,
        metrics=await _metrics(db, _portfolio_broker_ids(manager.id)),
        agents=await _agents_for_manager(db, manager.id),
    )


async def _invite_or_get_user(db: AsyncSession, email: str, name: str, role: Role) -> User:
    normalized = email.lower()
    existing = (await db.execute(select(User).where(User.email == normalized))).scalar_one_or_none()
    if existing is not None and existing.deleted_at is None:
        if existing.role != role:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Existing user is {existing.role}, not {role}.")
        return existing
    if existing is not None:
        existing.deleted_at = None
        existing.name = name
        existing.role = role
        existing.clerk_id = None
        user = existing
    else:
        user = User(email=normalized, name=name, role=role, clerk_id=None)
        db.add(user)
    await db.flush()
    await db.refresh(user)
    await clerk_service.invite_user(email=normalized, name=name, role=role)
    return user


@router.get("", response_model=list[RegionalManagerSummary])
async def list_regional_managers(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[RegionalManagerSummary]:
    managers = (
        await db.execute(
            select(User)
            .where(User.role == Role.REGIONAL_MANAGER, User.deleted_at.is_(None))
            .order_by(User.name)
        )
    ).scalars().all()
    return [
        RegionalManagerSummary(
            id=m.id,
            email=m.email,
            name=m.name,
            created_at=m.created_at,
            metrics=await _metrics(db, _portfolio_broker_ids(m.id)),
        )
        for m in managers
    ]


@router.post("", response_model=RegionalManagerSummary, status_code=status.HTTP_201_CREATED)
async def invite_regional_manager(
    body: RegionalManagerInvite,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> RegionalManagerSummary:
    manager = await _invite_or_get_user(db, str(body.email), body.name, Role.REGIONAL_MANAGER)
    return RegionalManagerSummary(
        id=manager.id,
        email=manager.email,
        name=manager.name,
        created_at=manager.created_at,
        metrics=await _metrics(db, _portfolio_broker_ids(manager.id)),
    )


@router.get("/me/agents", response_model=list[ManagedAgentRead])
async def list_my_agents(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[ManagedAgentRead]:
    if user.role != Role.REGIONAL_MANAGER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Regional manager role required")
    return await _agents_for_manager(db, user.id)


@router.post("/me/agents", response_model=ManagedAgentRead, status_code=status.HTTP_201_CREATED)
async def invite_my_agent(
    body: RegionalManagerAgentUpsert,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ManagedAgentRead:
    if user.role != Role.REGIONAL_MANAGER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Regional manager role required")
    if body.agent_user_id is None and (body.email is None or not body.name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "agent_user_id or email/name is required")
    return await _link_agent(db, manager_user_id=user.id, body=body, created_by_id=user.id)


@router.delete("/me/agents/{agent_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_my_agent(
    agent_user_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    if user.role != Role.REGIONAL_MANAGER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Regional manager role required")
    await _unlink_agent(db, manager_user_id=user.id, agent_user_id=agent_user_id)


@router.get("/{manager_user_id}", response_model=RegionalManagerDetail)
async def get_regional_manager(
    manager_user_id: UUID,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> RegionalManagerDetail:
    return await _detail(db, await _manager_or_404(db, manager_user_id))


@router.post("/{manager_user_id}/agents", response_model=ManagedAgentRead, status_code=status.HTTP_201_CREATED)
async def assign_agent_to_manager(
    manager_user_id: UUID,
    body: RegionalManagerAgentUpsert,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> ManagedAgentRead:
    await _manager_or_404(db, manager_user_id)
    if body.agent_user_id is None and (body.email is None or not body.name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "agent_user_id or email/name is required")
    return await _link_agent(db, manager_user_id=manager_user_id, body=body, created_by_id=user.id)


@router.delete("/{manager_user_id}/agents/{agent_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_agent_from_manager(
    manager_user_id: UUID,
    agent_user_id: UUID,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _manager_or_404(db, manager_user_id)
    await _unlink_agent(db, manager_user_id=manager_user_id, agent_user_id=agent_user_id)


async def _link_agent(
    db: AsyncSession,
    *,
    manager_user_id: UUID,
    body: RegionalManagerAgentUpsert,
    created_by_id: UUID,
) -> ManagedAgentRead:
    if body.agent_user_id is not None:
        agent = (
            await db.execute(
                select(User).where(
                    User.id == body.agent_user_id,
                    User.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if agent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
        if agent.role != Role.BROKER:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only Agent users can be added to a regional portfolio")
    else:
        agent = await _invite_or_get_user(db, str(body.email), body.name or "", Role.BROKER)

    existing = (
        await db.execute(
            select(RegionalManagerAgent).where(
                RegionalManagerAgent.manager_user_id == manager_user_id,
                RegionalManagerAgent.agent_user_id == agent.id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = RegionalManagerAgent(
            manager_user_id=manager_user_id,
            agent_user_id=agent.id,
            created_by_id=created_by_id,
        )
        db.add(existing)
        await db.flush()
        await db.refresh(existing)

    broker = (await db.execute(select(Broker).where(Broker.user_id == agent.id))).scalar_one_or_none()
    broker_id = getattr(broker, "id", None)
    return ManagedAgentRead(
        user_id=agent.id,
        email=agent.email,
        name=agent.name,
        broker_id=broker_id,
        display_name=getattr(broker, "display_name", None),
        linked_at=existing.created_at,
        metrics=await _metrics(db, _single_broker_id(broker_id)) if broker_id else PortfolioMetrics(
            agent_count=0,
            client_count=0,
            active_loans=0,
            pipeline_value=0,
            funded_ytd=0,
            pull_through=None,
            high_priority_tasks=0,
            overdue_items=0,
        ),
    )


async def _unlink_agent(db: AsyncSession, *, manager_user_id: UUID, agent_user_id: UUID) -> None:
    link = (
        await db.execute(
            select(RegionalManagerAgent).where(
                RegionalManagerAgent.manager_user_id == manager_user_id,
                RegionalManagerAgent.agent_user_id == agent_user_id,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Portfolio agent link not found")
    await db.delete(link)
    await db.flush()
