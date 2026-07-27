"""Deal-scoped multi-party chat — the (A) thread.

Same shape as routers/loan_workspace.send_chat but keyed on Deal
instead of Loan. Broker, client, and AI all participate. Modes
mirror the loan workspace surface: chat (client/operator takeover),
live_chat (broker takeover), broker_question (private Q&A with AI),
broker_suggestion (drafts an Elara Inbox task), instruct (rule for AI).

On promotion (services/handoff.promote_deal_to_loan) the full thread
is summarized into a single broker_internal LoanChatMessage at the
top of the new loan's workspace chat, so the funding team inherits
the pre-funding history.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import false as sql_false, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.enums import DealChatMode, DealChatRole, Role
from app.models.activity import Activity
from app.models.client import Client as ClientModel
from app.models.deal import Deal
from app.models.deal_chat_message import DealChatMessage
from app.models.user import User
from app.schemas.loan_workspace import (
    ChatMessageRead,
    ChatSendRequest,
    ChatSendResponse,
)
from app.services.ai.bedrock_client import get_client, model_light
from app.services.ai.usage import tracked_messages_create
from app.services.chat_names import serialize_chat, serialize_chat_one
from app.services.push import fire_and_forget_push
from app.scoping import regional_manager_broker_ids_subquery

log = logging.getLogger(__name__)

router = APIRouter(prefix="/deals/{deal_id}", tags=["deal-chat"])


_MODE_ALLOWED_ROLES: dict[DealChatMode, set[Role]] = {
    DealChatMode.CHAT: {Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.REGIONAL_MANAGER, Role.CLIENT},
    DealChatMode.INSTRUCT: {Role.SUPER_ADMIN, Role.BROKER, Role.LOAN_EXEC},
    DealChatMode.BROKER_QUESTION: {Role.BROKER, Role.SUPER_ADMIN, Role.LOAN_EXEC},
    DealChatMode.BROKER_SUGGESTION: {Role.BROKER},
    DealChatMode.LIVE_CHAT: {Role.BROKER, Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.REGIONAL_MANAGER},
}


_chat_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def _load_deal(db: AsyncSession, deal_id: UUID, user: User) -> Deal:
    """Load a deal, scoped by role.

    - CLIENT: only their own deals (via client_id → user.client.id).
    - BROKER: only deals on clients they own (Client.broker_id ==
      user.broker.id — the same rule scoping.py uses for Client/Loan
      everywhere else, not Deal.assigned_agent_id, which is a freely
      settable, independently-drifting field that doesn't reliably track
      the client's actual owning broker). Fails closed (sql_false()) when
      user.broker is None, matching scoping.py's defense-in-depth — without
      it, a BROKER-role user with no linked Broker row fell through to the
      unfiltered base query and could load ANY deal by id.
    - SUPER_ADMIN / LOAN_EXEC: any.
    """
    stmt = select(Deal).where(Deal.id == deal_id)
    if user.role == Role.CLIENT:
        if user.client is None:
            stmt = stmt.where(sql_false())
        else:
            stmt = stmt.where(Deal.client_id == user.client.id)
    elif user.role == Role.BROKER:
        if user.broker is None:
            stmt = stmt.where(sql_false())
        else:
            stmt = stmt.where(
                Deal.client_id.in_(select(ClientModel.id).where(ClientModel.broker_id == user.broker.id))
            )
    elif user.role == Role.REGIONAL_MANAGER:
        stmt = stmt.where(
            Deal.client_id.in_(
                select(ClientModel.id).where(ClientModel.broker_id.in_(regional_manager_broker_ids_subquery(user)))
            )
        )
    deal = (await db.execute(stmt)).scalar_one_or_none()
    if deal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deal not found")
    return deal


def _is_human_takeover(mode: DealChatMode, role: Role) -> bool:
    if mode == DealChatMode.CHAT and role in (Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.REGIONAL_MANAGER):
        return True
    if mode == DealChatMode.LIVE_CHAT and role in (
        Role.BROKER,
        Role.SUPER_ADMIN,
        Role.LOAN_EXEC,
        Role.REGIONAL_MANAGER,
    ):
        return True
    return False


@router.get("/chat", response_model=list[ChatMessageRead])
async def list_deal_chat(
    deal_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[ChatMessageRead]:
    await _load_deal(db, deal_id, user)
    stmt = (
        select(DealChatMessage)
        .where(DealChatMessage.deal_id == deal_id)
        .order_by(DealChatMessage.created_at.asc())
    )
    if user.role == Role.CLIENT:
        stmt = stmt.where(DealChatMessage.client_visible.is_(True))
    rows = (await db.execute(stmt)).scalars().all()
    return await serialize_chat(db, list(rows))


@router.post("/chat", response_model=ChatSendResponse, status_code=status.HTTP_201_CREATED)
async def send_deal_chat(
    deal_id: UUID,
    payload: ChatSendRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ChatSendResponse:
    """Mirror of loan_workspace.send_chat, deal-scoped.

    INSTRUCT / BROKER_SUGGESTION are deferred to the post-promotion
    surface for now (they reference loan_instructions and ai_tasks,
    which are loan-scoped). Allowed modes on a Deal: CHAT, LIVE_CHAT,
    BROKER_QUESTION.
    """
    deal = await _load_deal(db, deal_id, user)

    if Role(user.role) not in _MODE_ALLOWED_ROLES[payload.mode]:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Role {user.role} cannot use mode {payload.mode}",
        )

    if payload.mode in (DealChatMode.INSTRUCT, DealChatMode.BROKER_SUGGESTION):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Instructions and broker suggestions are only available after a deal is promoted to a loan.",
        )

    is_broker_q = payload.mode == DealChatMode.BROKER_QUESTION

    if _is_human_takeover(payload.mode, Role(user.role)):
        actor_role = (
            DealChatRole.BROKER
            if Role(user.role) == Role.BROKER
            else DealChatRole.SUPER_ADMIN
        )
        msg = DealChatMessage(
            deal_id=deal.id,
            from_role=actor_role,
            from_user_id=user.id,
            body=payload.body,
            client_visible=True,
        )
        db.add(msg)
        # No per-deal AI pause column today; takeovers just persist
        # client-visible. If/when you need a deal-level pause, add a
        # column ai_paused_until on deals and mirror the loan logic.
        # Activity table is keyed on loan_id / client_id (no deal_id
        # column yet — see alembic 0035). Scope to client so the agent
        # workspace activity feed picks it up.
        db.add(
            Activity(
                client_id=deal.client_id,
                actor_id=user.id,
                actor_label=user.email,
                kind=(
                    "deal_chat.broker_takeover"
                    if Role(user.role) == Role.BROKER
                    else "deal_chat.operator_takeover"
                ),
                summary=f"{user.email} ({user.role}) took over deal {deal.title}",
            )
        )
        await db.flush()
        await db.refresh(msg)

        # Push fan-out to the client.
        client_row = (
            await db.execute(select(ClientModel).where(ClientModel.id == deal.client_id))
        ).scalar_one_or_none()
        if client_row and client_row.user_id:
            fire_and_forget_push(
                client_row.user_id,
                title=_push_title_for(actor_role.value),
                body=payload.body[:100],
                data={"kind": "deal_chat_message", "deal_id": str(deal.id)},
            )

        return ChatSendResponse(
            kind="message",
            message=await serialize_chat_one(db, msg),
            paused_until=None,
        )

    # Client send (mode=chat) or broker_question (private Q&A with AI).
    from_role = DealChatRole.BROKER_INTERNAL if is_broker_q else DealChatRole.CLIENT
    client_visible = not is_broker_q
    msg = DealChatMessage(
        deal_id=deal.id,
        from_role=from_role,
        from_user_id=user.id,
        body=payload.body,
        client_visible=client_visible,
    )
    db.add(msg)
    await db.flush()
    await db.refresh(msg)

    # AI auto-reply.
    lock = _chat_locks[str(deal.id)]
    async with lock:
        ai_msg = await _generate_ai_reply(db, deal, user, client_visible=client_visible)

    return ChatSendResponse(
        kind="message",
        message=await serialize_chat_one(db, msg),
        ai_reply=await serialize_chat_one(db, ai_msg) if ai_msg else None,
    )


def _push_title_for(actor_role: str) -> str:
    if actor_role in ("super_admin", "loan_exec"):
        return "Your operator"
    if actor_role == "broker":
        return "Your agent"
    if actor_role == "ai":
        return "Elara"
    return "Deal update"


async def _generate_ai_reply(
    db: AsyncSession,
    deal: Deal,
    requesting_user: User,
    *,
    client_visible: bool,
) -> DealChatMessage | None:
    """Anthropic call mirroring loan_workspace._generate_ai_reply but
    on a deal-scoped thread. Context is the deal's living_profile +
    last 20 deal_chat_messages turns.
    """
    settings = get_settings()

    from app.services.ai.chat_rollup import WINDOW as _CHAT_WINDOW, maybe_rollup as _chat_rollup

    history_stmt = (
        select(DealChatMessage)
        .where(DealChatMessage.deal_id == deal.id)
        .order_by(DealChatMessage.created_at.asc())
        .limit(200)
    )
    if requesting_user.role == Role.CLIENT:
        history_stmt = history_stmt.where(DealChatMessage.client_visible.is_(True))
    history = list((await db.execute(history_stmt)).scalars().all())

    def _role(m):
        return "assistant" if m.from_role == DealChatRole.AI else "user"

    window = history[-_CHAT_WINDOW:] if history else []
    older = history[: max(0, len(history) - _CHAT_WINDOW)]
    older_pairs = [(_role(m), m.body or "") for m in older]
    profile = dict(deal.living_profile) if isinstance(deal.living_profile, dict) else {}
    prev_summary = profile.get("chat_summary")
    prev_count = int(profile.get("chat_summary_count") or 0)
    new_summary, new_count = await _chat_rollup(
        older_pairs, prev_summary, prev_count,
        meta={"deal_id": deal.id, "client_id": deal.client_id},
    )
    if new_summary != prev_summary or new_count != prev_count:
        from sqlalchemy.orm.attributes import flag_modified

        profile["chat_summary"] = new_summary
        profile["chat_summary_count"] = new_count
        deal.living_profile = profile
        flag_modified(deal, "living_profile")

    turns = [{"role": _role(m), "content": m.body} for m in window]
    if not turns or turns[-1]["role"] != "user":
        return None

    system = (
        "You are the Qualified Commercial Deal Workspace AI, scoped to a "
        "pre-funding deal. Stay focused on this single deal — buyer search, "
        "seller listing, or investor purchase. Be concise (1-3 sentences "
        "unless asked to expand). Help the agent nurture the lead.\n\n"
        f"Deal: {deal.title} (type={deal.deal_type}, status={deal.status}).\n"
        f"Address: {deal.address or 'TBD'} {deal.city or ''} {deal.state or ''}.\n"
    )
    if new_summary:
        system += "\n\nPRIOR CONVERSATION SUMMARY:\n" + new_summary

    if not settings.ai_provider_enabled:
        reply_text = "(stub) I would normally answer here once Bedrock is enabled."
    else:
        try:
            from app.services.ai.orchestrator import run as orchestrator_run

            result = await orchestrator_run(
                turns,  # type: ignore[arg-type]
                tier="light",
                db=db,
                feature="deal_chat",
                max_tokens=500,
                system=system,
                meta={"activity": "deal_chat", "deal_id": deal.id, "client_id": deal.client_id},
            )
            reply_text = "".join(
                b.get("text", "")
                for b in result.get("content", [])
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
            if not reply_text:
                reply_text = "(no reply)"
        except Exception as exc:  # noqa: BLE001
            log.warning("Deal AI reply failed: %s", exc)
            return None

    ai_msg = DealChatMessage(
        deal_id=deal.id,
        from_role=DealChatRole.AI,
        from_user_id=None,
        body=reply_text,
        client_visible=client_visible,
    )
    db.add(ai_msg)
    await db.flush()
    await db.refresh(ai_msg)

    if client_visible:
        client_row = (
            await db.execute(select(ClientModel).where(ClientModel.id == deal.client_id))
        ).scalar_one_or_none()
        if client_row and client_row.user_id:
            fire_and_forget_push(
                client_row.user_id,
                title="Elara",
                body=reply_text[:100],
                data={"kind": "deal_chat_message", "deal_id": str(deal.id)},
            )
    return ai_msg
