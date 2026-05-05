"""Deal Workspace router.

Per-loan AI surface combining instructions, persisted chat, named scenarios,
and HUD inline edits. The chat handler is the most interesting bit — its
`mode` field branches into one of four behaviors (see _handle_chat).

Mounted under /api/v1 in app/main.py.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser, require_role
from app.enums import (
    AITaskPriority,
    AITaskSource,
    AITaskStatus,
    DealChatMode,
    DealChatRole,
    FeedbackOutputType,
    FeedbackRating,
    Role,
)
from app.models.activity import Activity
from app.models.ai_feedback import AIFeedback
from app.models.ai_modify_correction import AIModifyCorrection
from app.models.ai_task import AITask
from app.models.hud import HudLineItem
from app.models.loan import Loan
from app.models.loan_chat_message import LoanChatMessage
from app.models.loan_instruction import LoanInstruction
from app.models.loan_scenario import LoanScenario
from app.models.user import User
from app.schemas.loan_workspace import (
    ChatMessageRead,
    ChatSendRequest,
    ChatSendResponse,
    CorrectionCreate,
    CorrectionRead,
    HudLinePatch,
    HudLineRead,
    InstructionCreate,
    InstructionRead,
    ScenarioBase,
    ScenarioRead,
    WorkspaceState,
)
from app.services.ai import engagement
from app.services.ai.anthropic_client import get_client, model_light
from app.services.ai.context import Audience, assemble_loan_context
from app.services.math import dscr as dscr_calc
from app.services.math import monthly_payment, pricing_quote

router = APIRouter(prefix="/loans/{loan_id}", tags=["workspace"])
log = logging.getLogger(__name__)

# Per-loan in-process lock so two concurrent client sends don't both trigger
# an AI auto-reply (real-world fix is a queue; this is good enough for v1).
_chat_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


# ── Helpers ────────────────────────────────────────────────────────────


async def _load_loan(db: AsyncSession, loan_id: UUID, user: User) -> Loan:
    """Load a loan, scoped by role (mirrors loans.py _scope_query)."""
    stmt = select(Loan).where(Loan.id == loan_id)
    if user.role == Role.CLIENT and user.client:
        stmt = stmt.where(Loan.client_id == user.client.id)
    elif user.role == Role.BROKER and user.broker:
        stmt = stmt.where(Loan.broker_id == user.broker.id)
    loan = (await db.execute(stmt)).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    return loan


def _audience_for(user: User) -> Audience:
    if user.role == Role.CLIENT:
        return "client"
    if user.role == Role.BROKER:
        return "broker"
    return "super_admin"


# ── Bundled state ──────────────────────────────────────────────────────


@router.get("/workspace/state", response_model=WorkspaceState)
async def get_workspace_state(
    loan_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> WorkspaceState:
    """One-shot bundled load so the workspace tab mounts in a single request."""
    loan = await _load_loan(db, loan_id, user)

    instructions = (
        await db.execute(
            select(LoanInstruction)
            .where(LoanInstruction.loan_id == loan_id, LoanInstruction.is_active.is_(True))
            .order_by(LoanInstruction.created_at.asc())
        )
    ).scalars().all()

    chat_q = select(LoanChatMessage).where(LoanChatMessage.loan_id == loan_id).order_by(
        LoanChatMessage.created_at.asc()
    )
    if user.role == Role.CLIENT:
        chat_q = chat_q.where(LoanChatMessage.client_visible.is_(True))
    chat_messages = (await db.execute(chat_q)).scalars().all()

    scenarios = (
        await db.execute(
            select(LoanScenario)
            .where(LoanScenario.loan_id == loan_id)
            .order_by(LoanScenario.created_at.desc())
        )
    ).scalars().all()

    hud_lines = (
        await db.execute(
            select(HudLineItem).where(HudLineItem.loan_id == loan_id).order_by(HudLineItem.code)
        )
    ).scalars().all()

    fb_counts = {"up": 0, "down": 0}
    fb_rows = (
        await db.execute(
            select(AIFeedback.rating, func.count(AIFeedback.id))
            .where(AIFeedback.loan_id == loan_id)
            .group_by(AIFeedback.rating)
        )
    ).all()
    for rating, count in fb_rows:
        fb_counts[str(rating)] = int(count)

    return WorkspaceState(
        instructions=[InstructionRead.model_validate(i) for i in instructions],
        chat_messages=[ChatMessageRead.model_validate(m) for m in chat_messages],
        scenarios=[ScenarioRead.model_validate(s) for s in scenarios],
        hud_lines=[HudLineRead.model_validate(h) for h in hud_lines],
        ai_paused_until=loan.ai_paused_until,
        feedback_summary=fb_counts,
    )


# ── Instructions ───────────────────────────────────────────────────────


@router.get("/instructions", response_model=list[InstructionRead])
async def list_instructions(
    loan_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[InstructionRead]:
    await _load_loan(db, loan_id, user)
    rows = (
        await db.execute(
            select(LoanInstruction)
            .where(LoanInstruction.loan_id == loan_id, LoanInstruction.is_active.is_(True))
            .order_by(LoanInstruction.created_at.asc())
        )
    ).scalars().all()
    return [InstructionRead.model_validate(r) for r in rows]


@router.post("/instructions", response_model=InstructionRead, status_code=status.HTTP_201_CREATED)
async def create_instruction(
    loan_id: UUID,
    body: InstructionCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> InstructionRead:
    if user.role not in {Role.SUPER_ADMIN, Role.BROKER, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only operator-team users can add instructions")
    loan = await _load_loan(db, loan_id, user)
    inst = LoanInstruction(loan_id=loan.id, body=body.body, created_by=user.id, is_active=True)
    db.add(inst)
    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=user.id,
            actor_label=user.email,
            kind="instruction.created",
            summary=f"Instruction added: {body.body[:80]}",
        )
    )
    await db.flush()
    await db.refresh(inst)
    return InstructionRead.model_validate(inst)


@router.delete("/instructions/{instruction_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_instruction(
    loan_id: UUID,
    instruction_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    if user.role not in {Role.SUPER_ADMIN, Role.BROKER, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    await _load_loan(db, loan_id, user)
    inst = (
        await db.execute(
            select(LoanInstruction).where(
                LoanInstruction.id == instruction_id, LoanInstruction.loan_id == loan_id
            )
        )
    ).scalar_one_or_none()
    if inst is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Instruction not found")
    inst.is_active = False
    inst.deactivated_at = datetime.now(timezone.utc)
    await db.flush()


# ── Chat ───────────────────────────────────────────────────────────────


@router.get("/chat", response_model=list[ChatMessageRead])
async def list_chat(
    loan_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[ChatMessageRead]:
    await _load_loan(db, loan_id, user)
    stmt = (
        select(LoanChatMessage)
        .where(LoanChatMessage.loan_id == loan_id)
        .order_by(LoanChatMessage.created_at.asc())
    )
    if user.role == Role.CLIENT:
        stmt = stmt.where(LoanChatMessage.client_visible.is_(True))
    rows = (await db.execute(stmt)).scalars().all()
    return [ChatMessageRead.model_validate(r) for r in rows]


# Per-mode allowed roles. CLIENT can only send `chat` (ignoring mode).
_MODE_ALLOWED_ROLES: dict[DealChatMode, set[Role]] = {
    DealChatMode.CHAT: {Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.CLIENT},
    DealChatMode.INSTRUCT: {Role.SUPER_ADMIN, Role.BROKER, Role.LOAN_EXEC},
    DealChatMode.BROKER_QUESTION: {Role.BROKER, Role.SUPER_ADMIN, Role.LOAN_EXEC},
    DealChatMode.BROKER_SUGGESTION: {Role.BROKER},
}


@router.post("/chat", response_model=ChatSendResponse, status_code=status.HTTP_201_CREATED)
async def send_chat(
    loan_id: UUID,
    payload: ChatSendRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ChatSendResponse:
    """The polymorphic chat handler. See plan §3 for the routing table."""
    loan = await _load_loan(db, loan_id, user)

    if Role(user.role) not in _MODE_ALLOWED_ROLES[payload.mode]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Role {user.role} cannot use mode {payload.mode}")

    # ── instruct ────────────────────────────────────────────────────────
    if payload.mode == DealChatMode.INSTRUCT:
        inst = LoanInstruction(loan_id=loan.id, body=payload.body, created_by=user.id, is_active=True)
        db.add(inst)
        db.add(
            Activity(
                loan_id=loan.id,
                actor_id=user.id,
                actor_label=user.email,
                kind="instruction.created",
                summary=f"Instruction (via chat): {payload.body[:80]}",
            )
        )
        await db.flush()
        await db.refresh(inst)
        return ChatSendResponse(kind="instruction", instruction=InstructionRead.model_validate(inst))

    # ── broker_suggestion ──────────────────────────────────────────────
    if payload.mode == DealChatMode.BROKER_SUGGESTION:
        title = payload.body[:80] + ("…" if len(payload.body) > 80 else "")
        task = AITask(
            loan_id=loan.id,
            source=AITaskSource.BROKER_SUGGESTION,
            priority=AITaskPriority.MEDIUM,
            status=AITaskStatus.PENDING,
            action="review_broker_suggestion",
            title=title,
            summary=payload.body,
            confidence=0.7,
            agent="broker_suggestion",
            draft_payload={"submitted_by": str(user.id), "submitted_by_email": user.email},
        )
        db.add(task)
        db.add(
            Activity(
                loan_id=loan.id,
                actor_id=user.id,
                actor_label=user.email,
                kind="ai_task.broker_suggestion",
                summary=f"Broker suggestion filed: {title}",
            )
        )
        await db.flush()
        await db.refresh(task)
        return ChatSendResponse(kind="ai_task", ai_task_id=task.id)

    # ── chat / broker_question ─────────────────────────────────────────
    is_broker_q = payload.mode == DealChatMode.BROKER_QUESTION
    if payload.mode == DealChatMode.CHAT and user.role == Role.SUPER_ADMIN:
        # Super-admin override: persist + pause AI for 1h, no auto-reply.
        msg = LoanChatMessage(
            loan_id=loan.id,
            from_role=DealChatRole.SUPER_ADMIN,
            from_user_id=user.id,
            body=payload.body,
            client_visible=True,
        )
        db.add(msg)
        paused_until = engagement.pause(loan)
        db.add(
            Activity(
                loan_id=loan.id,
                actor_id=user.id,
                actor_label=user.email,
                kind="ai.paused_by_super_admin",
                summary=f"Super-admin sent manual reply; AI paused until {paused_until.isoformat()}",
            )
        )
        await db.flush()
        await db.refresh(msg)
        return ChatSendResponse(
            kind="message",
            message=ChatMessageRead.model_validate(msg),
            paused_until=paused_until,
        )

    # Regular client send (or broker_question — both auto-reply).
    from_role = DealChatRole.BROKER_INTERNAL if is_broker_q else DealChatRole.CLIENT
    client_visible = not is_broker_q
    msg = LoanChatMessage(
        loan_id=loan.id,
        from_role=from_role,
        from_user_id=user.id,
        body=payload.body,
        client_visible=client_visible,
    )
    db.add(msg)
    await db.flush()
    await db.refresh(msg)

    paused_now = engagement.is_paused(loan)
    if paused_now and not is_broker_q:
        # Client send during pause — persist, no AI reply.
        return ChatSendResponse(
            kind="message",
            message=ChatMessageRead.model_validate(msg),
            paused_until=loan.ai_paused_until,
        )

    # Trigger AI auto-reply (synchronous, with per-loan lock).
    lock = _chat_locks[str(loan.id)]
    async with lock:
        ai_msg = await _generate_ai_reply(db, loan, user, client_visible=client_visible)
    return ChatSendResponse(
        kind="message",
        message=ChatMessageRead.model_validate(msg),
        ai_reply=ChatMessageRead.model_validate(ai_msg) if ai_msg else None,
    )


async def _generate_ai_reply(
    db: AsyncSession, loan: Loan, requesting_user: User, *, client_visible: bool
) -> LoanChatMessage | None:
    """Builds the prompt via the unified context assembly, calls Anthropic,
    persists the reply. Returns None if the upstream is unavailable (we don't
    block the user's own send on AI failures)."""
    settings = get_settings()
    audience: Audience = _audience_for(requesting_user)

    # Recent chat history → message turns for the LLM.
    history_stmt = (
        select(LoanChatMessage)
        .where(LoanChatMessage.loan_id == loan.id)
        .order_by(LoanChatMessage.created_at.desc())
        .limit(20)
    )
    if audience == "client":
        history_stmt = history_stmt.where(LoanChatMessage.client_visible.is_(True))
    history = list((await db.execute(history_stmt)).scalars().all())
    history.reverse()

    turns = [
        {
            "role": "assistant" if m.from_role == DealChatRole.AI else "user",
            "content": m.body,
        }
        for m in history
    ]
    if not turns or turns[-1]["role"] != "user":
        return None  # nothing to reply to

    system = (
        "You are the Qualified Commercial Deal Workspace AI. Stay scoped to "
        "this single loan. Be concise — 1-3 sentences unless asked to expand.\n\n"
        + await assemble_loan_context(db, loan, audience=audience)
    )

    if not settings.anthropic_api_key:
        # Stub when no key — keep the workspace usable in dev.
        reply_text = "(stub) I would normally answer here once ANTHROPIC_API_KEY is set."
    else:
        try:
            client = get_client()
            resp = await client.messages.create(
                model=model_light(),
                max_tokens=500,
                system=system,
                messages=turns,  # type: ignore[arg-type]
            )
            reply_text = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
            if not reply_text:
                reply_text = "(no reply)"
        except Exception as exc:  # noqa: BLE001
            log.warning("Workspace AI reply failed: %s", exc)
            return None

    ai_msg = LoanChatMessage(
        loan_id=loan.id,
        from_role=DealChatRole.AI,
        from_user_id=None,
        body=reply_text,
        client_visible=client_visible,
    )
    db.add(ai_msg)
    await db.flush()
    await db.refresh(ai_msg)
    return ai_msg


@router.post(
    "/chat/{message_id}/correction",
    response_model=CorrectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def attach_correction(
    loan_id: UUID,
    message_id: UUID,
    body: CorrectionCreate,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> CorrectionRead:
    """Super-admin: attach an AI Modify correction to a past AI turn."""
    msg = (
        await db.execute(
            select(LoanChatMessage).where(
                LoanChatMessage.id == message_id, LoanChatMessage.loan_id == loan_id
            )
        )
    ).scalar_one_or_none()
    if msg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat message not found")
    if msg.from_role != DealChatRole.AI:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Corrections can only target AI messages"
        )
    correction = AIModifyCorrection(
        loan_id=loan_id,
        target_message_id=message_id,
        correction=body.correction,
        created_by=user.id,
    )
    db.add(correction)
    await db.flush()
    await db.refresh(correction)
    return CorrectionRead.model_validate(correction)


@router.post("/ai/resume", status_code=status.HTTP_204_NO_CONTENT)
async def resume_ai(
    loan_id: UUID,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Clear an active engagement pause early (the 'Resume AI now' button)."""
    loan = await _load_loan(db, loan_id, user)
    engagement.resume(loan)
    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=user.id,
            actor_label=user.email,
            kind="ai.resumed_by_super_admin",
            summary="Super-admin cleared the engagement pause",
        )
    )
    await db.flush()


# ── Scenarios ──────────────────────────────────────────────────────────


@router.get("/scenarios", response_model=list[ScenarioRead])
async def list_scenarios(
    loan_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[ScenarioRead]:
    await _load_loan(db, loan_id, user)
    rows = (
        await db.execute(
            select(LoanScenario)
            .where(LoanScenario.loan_id == loan_id)
            .order_by(LoanScenario.created_at.desc())
        )
    ).scalars().all()
    return [ScenarioRead.model_validate(r) for r in rows]


@router.post("/scenarios", response_model=ScenarioRead, status_code=status.HTTP_201_CREATED)
async def save_scenario(
    loan_id: UUID,
    body: ScenarioBase,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ScenarioRead:
    """Save a scenario AND compute + cache its RecalcResponse so the chip can
    show numbers without re-running. Mirrors the math in routers/loans.recalc."""
    loan = await _load_loan(db, loan_id, user)

    base_rate = body.base_rate or float(loan.base_rate or 0.07)
    amount = body.loan_amount or float(loan.amount)
    quote = pricing_quote(base_rate, amount, body.discount_points)
    is_io = loan.type.value in {"fix_and_flip", "bridge", "ground_up"}
    term = loan.term_months or (12 if is_io else 360)
    pi = (
        round(amount * quote.final_rate / 12, 2)
        if is_io
        else round(monthly_payment(amount, quote.final_rate, term), 2)
    )
    annual_taxes = body.annual_taxes if body.annual_taxes is not None else float(loan.annual_taxes or 0)
    annual_insurance = (
        body.annual_insurance if body.annual_insurance is not None else float(loan.annual_insurance or 0)
    )
    monthly_hoa = body.monthly_hoa if body.monthly_hoa is not None else float(loan.monthly_hoa or 0)
    dscr_val = None
    if loan.monthly_rent and not is_io:
        dscr_val = dscr_calc(
            float(loan.monthly_rent),
            amount,
            quote.final_rate,
            term,
            annual_taxes,
            annual_insurance,
            monthly_hoa,
        )
    snapshot = {
        "final_rate": quote.final_rate,
        "monthly_pi": pi,
        "dscr": dscr_val,
        "cash_to_close_pricing": quote.broker_origination_dollars,
    }

    scenario = LoanScenario(
        loan_id=loan.id,
        name=body.name,
        discount_points=body.discount_points,
        loan_amount=body.loan_amount,
        base_rate=body.base_rate,
        annual_taxes=body.annual_taxes,
        annual_insurance=body.annual_insurance,
        monthly_hoa=body.monthly_hoa,
        ltv=body.ltv,
        recalc_snapshot=snapshot,
        created_by=user.id,
    )
    db.add(scenario)
    await db.flush()
    await db.refresh(scenario)
    return ScenarioRead.model_validate(scenario)


@router.delete("/scenarios/{scenario_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scenario(
    loan_id: UUID,
    scenario_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    await _load_loan(db, loan_id, user)
    scenario = (
        await db.execute(
            select(LoanScenario).where(
                LoanScenario.id == scenario_id, LoanScenario.loan_id == loan_id
            )
        )
    ).scalar_one_or_none()
    if scenario is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scenario not found")
    await db.delete(scenario)
    await db.flush()


# ── HUD inline edits ───────────────────────────────────────────────────


@router.patch("/hud/{line_id}", response_model=HudLineRead)
async def patch_hud_line(
    loan_id: UUID,
    line_id: UUID,
    body: HudLinePatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> HudLineRead:
    if user.role not in {Role.SUPER_ADMIN, Role.BROKER, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    await _load_loan(db, loan_id, user)
    line = (
        await db.execute(
            select(HudLineItem).where(HudLineItem.id == line_id, HudLineItem.loan_id == loan_id)
        )
    ).scalar_one_or_none()
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "HUD line not found")
    if not line.editable:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This HUD line is not editable")
    if body.label is not None:
        line.label = body.label
    if body.amount is not None:
        line.amount = body.amount
    if body.category is not None:
        line.category = body.category
    await db.flush()
    await db.refresh(line)
    return HudLineRead.model_validate(line)
