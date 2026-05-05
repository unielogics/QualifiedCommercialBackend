"""Global search — ⌘K modal. Results grouped by client."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.client import Client
from app.models.document import Document
from app.models.loan import Loan
from app.models.message import Message
from app.schemas.search import GroupedResults, SearchResult

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=list[GroupedResults])
async def search(
    q: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    limit_per_kind: int = 10,
) -> list[GroupedResults]:
    if not q.strip():
        return []
    pattern = f"%{q}%"

    # Restrict scope by role
    client_filter = []
    loan_filter = []
    if user.role == Role.BROKER and user.broker:
        loan_filter.append(Loan.broker_id == user.broker.id)
        client_filter.append(Client.broker_id == user.broker.id)
    if user.role == Role.CLIENT and user.client:
        loan_filter.append(Loan.client_id == user.client.id)
        client_filter.append(Client.id == user.client.id)

    grouped: dict[UUID, list[SearchResult]] = defaultdict(list)
    client_names: dict[UUID, str] = {}

    # Clients
    cstmt = select(Client).where(or_(Client.name.ilike(pattern), Client.email.ilike(pattern))).limit(limit_per_kind)
    for f in client_filter:
        cstmt = cstmt.where(f)
    for c in (await db.execute(cstmt)).scalars().all():
        client_names[c.id] = c.name
        grouped[c.id].append(SearchResult(kind="client", id=c.id, title=c.name, subtitle=c.email, client_id=c.id))

    # Loans
    lstmt = select(Loan).where(or_(Loan.deal_id.ilike(pattern), Loan.address.ilike(pattern))).limit(limit_per_kind)
    for f in loan_filter:
        lstmt = lstmt.where(f)
    loans = (await db.execute(lstmt)).scalars().all()
    for loan in loans:
        if loan.client_id not in client_names:
            c = await db.get(Client, loan.client_id)
            if c:
                client_names[loan.client_id] = c.name
        grouped[loan.client_id].append(
            SearchResult(
                kind="loan",
                id=loan.id,
                title=f"{loan.deal_id} — {loan.address}",
                subtitle=f"{loan.type} · ${float(loan.amount):,.0f}",
                client_id=loan.client_id,
                loan_id=loan.id,
            )
        )

    # Documents
    dstmt = (
        select(Document, Loan)
        .join(Loan, Document.loan_id == Loan.id)
        .where(Document.name.ilike(pattern))
        .limit(limit_per_kind)
    )
    for f in loan_filter:
        dstmt = dstmt.where(f)
    for doc, loan in (await db.execute(dstmt)).all():
        grouped[loan.client_id].append(
            SearchResult(
                kind="doc",
                id=doc.id,
                title=doc.name,
                subtitle=loan.deal_id,
                client_id=loan.client_id,
                loan_id=loan.id,
            )
        )

    # Messages (search body)
    mstmt = (
        select(Message, Loan)
        .join(Loan, Message.loan_id == Loan.id)
        .where(Message.body.ilike(pattern))
        .limit(limit_per_kind)
    )
    for f in loan_filter:
        mstmt = mstmt.where(f)
    for msg, loan in (await db.execute(mstmt)).all():
        snippet = msg.body[:80] + ("…" if len(msg.body) > 80 else "")
        grouped[loan.client_id].append(
            SearchResult(
                kind="message",
                id=msg.id,
                title=snippet,
                subtitle=loan.deal_id,
                client_id=loan.client_id,
                loan_id=loan.id,
            )
        )

    # Pad missing client names
    for cid in grouped:
        if cid not in client_names:
            c = await db.get(Client, cid)
            if c:
                client_names[cid] = c.name

    return [
        GroupedResults(client_id=cid, client_name=client_names.get(cid, "Unknown"), items=items)
        for cid, items in grouped.items()
    ]
