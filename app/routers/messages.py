from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, get_db
from app.deps import CurrentUser
from app.enums import MessageFrom, Role
from app.models.client import Client
from app.models.loan import Loan
from app.models.message import Message
from app.schemas.message import MessageCreate, MessageRead
from app.scoping import scope_loan_query
from app.services import file_events
from app.ws import channel

router = APIRouter(prefix="/messages", tags=["messages"])
log = logging.getLogger(__name__)


@router.get("", response_model=list[MessageRead])
async def list_messages(
    loan_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[MessageRead]:
    visible = (
        await db.execute(scope_loan_query(user, select(Loan.id).where(Loan.id == loan_id)))
    ).scalar_one_or_none()
    if visible is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    stmt = select(Message).where(Message.loan_id == loan_id).order_by(Message.sent_at)
    rows = (await db.execute(stmt)).scalars().all()
    return [MessageRead.model_validate(r) for r in rows]


def _message_event_title(user) -> str:
    """What the timeline says about a message on the loan thread — who wrote,
    never what they wrote."""
    if user.role == Role.CLIENT:
        return f"Message from {user.name or 'your client'}"
    if user.role in (Role.BROKER, Role.REGIONAL_MANAGER, Role.FIELD_REP):
        return "Message from your agent"
    return "Message from the desk"


async def _message_notice_reached(db: AsyncSession, loan: Loan, from_role: str) -> set[UUID]:
    """The seats `notify_message_sent` reaches — the agents and the desk for
    a client's message, the client for anyone else's — so the file timeline
    does not tell them twice."""
    from app.services.notifications import loan_agent_user_ids, users_with_roles

    try:
        if from_role == "client":
            reached = await loan_agent_user_ids(db, loan)
            reached.update(user.id for user in await users_with_roles(db, Role.LOAN_EXEC, Role.SUPER_ADMIN))
            return reached
        client = await db.get(Client, loan.client_id)
        return {client.user_id} if client and client.user_id else set()
    except Exception:  # noqa: BLE001
        log.exception("message notice recipients failed loan=%s", loan.id)
        return set()


@router.post("", response_model=MessageRead, status_code=status.HTTP_201_CREATED)
async def send_message(
    payload: MessageCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> MessageRead:
    loan = await db.get(Loan, payload.loan_id)
    visible = (
        await db.execute(scope_loan_query(user, select(Loan.id).where(Loan.id == payload.loan_id)))
    ).scalar_one_or_none()
    if loan is None or visible is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    msg = Message(
        loan_id=loan.id,
        body=payload.body,
        from_role=payload.from_role if user.role != Role.CLIENT else MessageFrom.CLIENT,
        is_draft=payload.is_draft,
    )
    db.add(msg)
    await db.flush()
    await db.refresh(msg)
    try:
        from app.services.notifications import notify_message_sent

        role_value = msg.from_role.value if hasattr(msg.from_role, "value") else str(msg.from_role)
        await notify_message_sent(db, loan=loan, from_role=role_value, actor=user)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("message notification failed loan=%s message=%s", loan.id, msg.id)
    # File timeline: one line for the message, never its body. The message
    # notice above already reached the seats it fans out to.
    sent_from = msg.from_role.value if hasattr(msg.from_role, "value") else str(msg.from_role)
    if not getattr(msg, "is_draft", False):
        # An AI-drafted message awaiting review is not something that happened
        # on the file yet.
        await file_events.emit(
            db,
            loan_id=loan.id,
            kind="message.sent",
            visibility=file_events.VISIBILITY_CLIENT,
            title=_message_event_title(user),
            actor=user,
            target_type="message",
            target_id=msg.id,
            already_notified=await _message_notice_reached(db, loan, sent_from),
        )
    await channel.broadcast(loan.deal_id, {"kind": "message", "message": MessageRead.model_validate(msg).model_dump(mode="json")})
    return MessageRead.model_validate(msg)


@router.websocket("/ws/{deal_id}")
async def messages_ws(websocket: WebSocket, deal_id: str) -> None:
    """Per-deal channel. Client subscribes by deal_id (e.g. L-2598)."""
    await channel.connect(deal_id, websocket)
    try:
        while True:
            await websocket.receive_text()  # ignore inbound for now
    except WebSocketDisconnect:
        channel.disconnect(deal_id, websocket)
