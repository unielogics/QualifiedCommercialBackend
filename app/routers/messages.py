from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, get_db
from app.deps import CurrentUser
from app.enums import MessageFrom, Role
from app.models.loan import Loan
from app.models.message import Message
from app.scoping import scope_loan_query
from app.schemas.message import MessageCreate, MessageRead
from app.ws import channel

router = APIRouter(prefix="/messages", tags=["messages"])


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
