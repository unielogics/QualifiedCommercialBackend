"""Reading the SMS ledger.

One endpoint, filterable, newest first. It serves both screens the UI needs:
a client's SMS thread (`?client_id=`) and the operator-wide message log (no
filter). The writes happen elsewhere — send_sms_checked, the consent-delivery
seam, and the inbound webhook — this router only reads.

Visibility follows the client book: SUPER_ADMIN and LOAN_EXEC see everything,
a BROKER sees messages attached to their own clients and nothing unattributed,
and client/dealer-side roles see nothing at all. The deny is explicit because
an SMS body is borrower conversation, not metadata.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.client import Client
from app.models.sms_message import SMS_DIRECTIONS, SMS_STATUSES, SmsMessage
from app.scoping import scope_client_query

router = APIRouter(prefix="/sms", tags=["sms"])

_OPERATORS_ALL = {Role.SUPER_ADMIN, Role.LOAN_EXEC}


class SmsMessageRead(BaseModel):
    id: UUID
    direction: str
    phone_e164: str
    body: str | None
    provider: str
    provider_message_id: str
    status: str
    detail: str
    context: str
    client_id: UUID | None
    client_name: str | None = None
    delivered_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class SmsListResponse(BaseModel):
    messages: list[SmsMessageRead]
    #: Pass back as ?before= to fetch the next (older) page.
    next_before: datetime | None


def _visible_client_ids_stmt(user):
    """Subquery of client ids this user may read messages for."""
    return scope_client_query(user, select(Client.id))


@router.get("/messages", response_model=SmsListResponse)
async def list_messages(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    client_id: UUID | None = None,
    phone: str | None = None,
    direction: str | None = Query(None, pattern="^(outbound|inbound)$"),
    status_filter: str | None = Query(None, alias="status"),
    context: str | None = None,
    before: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> SmsListResponse:
    """The ledger, newest first, filtered.

    `before` is a keyset cursor on created_at — stable under new arrivals,
    which an offset would not be on a table that only ever grows.
    """
    if user.role in {Role.CLIENT, Role.REGIONAL_MANAGER, Role.DEALER, Role.DEALER_PARTNER, Role.FIELD_REP}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator-only view")
    if status_filter is not None and status_filter not in SMS_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"status must be one of {SMS_STATUSES}")

    stmt = (
        select(SmsMessage, Client.name)
        .join(Client, Client.id == SmsMessage.client_id, isouter=True)
        .order_by(SmsMessage.created_at.desc())
        .limit(limit)
    )

    if user.role not in _OPERATORS_ALL:
        # Broker book: only messages attached to their clients. Unattributed
        # rows (client_id NULL — e.g. inbound from an unknown number) stay
        # admin-only rather than leaking across books.
        stmt = stmt.where(SmsMessage.client_id.in_(_visible_client_ids_stmt(user)))

    if client_id is not None:
        stmt = stmt.where(SmsMessage.client_id == client_id)
    if phone:
        digits = "".join(ch for ch in phone if ch.isdigit())
        if digits:
            stmt = stmt.where(SmsMessage.phone_e164.like(f"%{digits[-10:]}"))
    if direction:
        stmt = stmt.where(SmsMessage.direction == direction)
    if status_filter:
        stmt = stmt.where(SmsMessage.status == status_filter)
    if context:
        stmt = stmt.where(SmsMessage.context == context)
    if before is not None:
        stmt = stmt.where(SmsMessage.created_at < before)

    rows = (await db.execute(stmt)).all()
    messages = []
    for msg, client_name in rows:
        item = SmsMessageRead.model_validate(msg)
        item.client_name = client_name
        messages.append(item)

    next_before = messages[-1].created_at if len(messages) == limit else None
    return SmsListResponse(messages=messages, next_before=next_before)


class SmsSummary(BaseModel):
    total: int
    outbound: int
    inbound: int
    delivered: int
    failed: int
    blocked: int


@router.get("/summary", response_model=SmsSummary)
async def summary(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SmsSummary:
    """Counts for the log header — one query, grouped."""
    if user.role in {Role.CLIENT, Role.REGIONAL_MANAGER, Role.DEALER, Role.DEALER_PARTNER, Role.FIELD_REP}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator-only view")

    stmt = select(SmsMessage.direction, SmsMessage.status, func.count()).group_by(
        SmsMessage.direction, SmsMessage.status
    )
    if user.role not in _OPERATORS_ALL:
        stmt = stmt.where(SmsMessage.client_id.in_(_visible_client_ids_stmt(user)))

    out = {"total": 0, "outbound": 0, "inbound": 0, "delivered": 0, "failed": 0, "blocked": 0}
    for direction, st, n in (await db.execute(stmt)).all():
        out["total"] += n
        if direction in out:
            out[direction] += n
        if st in ("delivered", "failed", "blocked"):
            out[st] += n
    return SmsSummary(**out)
