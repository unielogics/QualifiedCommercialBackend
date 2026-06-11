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
from app.models.hud_share_link import HudShareLink
from app.schemas.loan_workspace import (
    ChatMessageRead,
    ChatSendRequest,
    ChatSendResponse,
    CorrectionCreate,
    CorrectionRead,
    HudLineCreate,
    HudLinePatch,
    HudLineRead,
    HudShareLinkCreate,
    HudShareLinkRead,
    InstructionCreate,
    InstructionRead,
    PublicHudView,
    ScenarioBase,
    ScenarioRead,
    WorkspaceState,
)
from app.services.chat_names import serialize_chat, serialize_chat_one
from app.services.ai import engagement
from app.services.ai.anthropic_client import get_client, model_light
from app.services.ai.context import Audience, assemble_loan_context
from app.services.ai.usage import tracked_messages_create
from app.services.math import dscr as dscr_calc
from app.services.math import monthly_payment, pricing_quote
from app.models.client import Client as ClientModel


# Phase 7.5 — Push fan-out helper for workspace chat.
#
# Mobile devices subscribe to FCM via `/devices/push-tokens` so the
# borrower's phone gets a real-time signal when an operator / broker /
# AI writes into the loan's workspace chat. Without this, the chat
# silently lands in `loan_chat_messages` and the client never knows.
#
# Always best-effort: if FCM credentials aren't configured the call
# is a no-op (see `_ensure_initialized` in app/services/push.py).
def _notify_client(
    *,
    loan: Loan,
    client_user_id: UUID | None,
    actor_role: str,
    body: str,
) -> None:
    if client_user_id is None:
        # Lead-stage clients without a Clerk user can't receive pushes.
        return
    from app.services.push import fire_and_forget_push  # local import to avoid cycles
    title = _push_title_for(actor_role)
    preview = _trim_preview(body)
    fire_and_forget_push(
        client_user_id,
        title=title,
        body=preview,
        data={
            "kind": "loan_chat_message",
            "loan_id": str(loan.id),
        },
    )


def _push_title_for(actor_role: str) -> str:
    if actor_role == DealChatRole.SUPER_ADMIN.value or actor_role == "loan_exec":
        return "Your operator"
    if actor_role == DealChatRole.BROKER.value or actor_role == "broker":
        return "Your agent"
    if actor_role == DealChatRole.AI.value or actor_role == "ai":
        return "Elara"
    return "Loan update"


def _trim_preview(body: str, max_chars: int = 100) -> str:
    """Truncate on whitespace for clean previews on the lock screen."""
    body = body.strip()
    if len(body) <= max_chars:
        return body
    cut = body[: max_chars].rsplit(" ", 1)[0]
    return f"{cut}…" if cut else body[: max_chars] + "…"


# Phase 7.5.2 — Proactive AI re-engagement at pause expiry.
#
# When an operator/broker takeover sets `loan.ai_paused_until = now + 60min`,
# we schedule a one-shot APScheduler job at that timestamp. The job loads
# the loan + recent chat, calls the AI against that context, and posts a
# follow-up message so the client sees the AI is back. Push fan-out is
# part of the AI-reply path (same `_notify_client` call from
# `_generate_ai_reply` further down), so the client gets a real-time
# signal that the conversation has resumed.
#
# Job durability: APScheduler default is in-memory and loses jobs on
# container restart. The simplest backstop is in `send_chat`'s regular
# (non-takeover) branch: if `ai_paused_until` has expired AND no AI
# message has posted since the pause started, the next client send
# triggers a catch-up reply. Implementation of that fallback can land
# in a follow-up if the scheduled-job path proves insufficient.
def _schedule_resume_followup(*, loan_id: UUID, paused_until: datetime) -> None:
    try:
        from app.services.scheduler import scheduler
        from datetime import timedelta
        # Small jitter so we don't fire at the literal microsecond the
        # pause clears — gives engagement.is_paused a chance to settle.
        run_date = paused_until + timedelta(seconds=30)
        scheduler.add_job(
            _ai_followup_job,
            "date",
            run_date=run_date,
            args=[str(loan_id)],
            id=f"ai_followup_{loan_id}",
            replace_existing=True,
            misfire_grace_time=60 * 10,  # 10 min — survives brief restarts
        )
        log.info(
            "loan_workspace.resume_followup_scheduled loan=%s run_date=%s",
            loan_id, run_date.isoformat(),
        )
    except Exception as exc:  # noqa: BLE001
        # Best-effort — push notification has already gone out. The
        # client can still see the operator's message; just no AI
        # closing follow-up.
        log.warning("loan_workspace.resume_followup_schedule_failed: %s", exc)


async def _ai_followup_job(loan_id_str: str) -> None:
    """One-shot APScheduler callback. Opens a fresh DB session, loads the
    loan, and runs the AI against the current thread so it posts a
    closing follow-up after the operator's takeover window ends."""
    from app.db import SessionLocal
    from uuid import UUID as _UUID

    try:
        loan_uuid = _UUID(loan_id_str)
    except ValueError:
        log.warning("_ai_followup_job: bad loan_id %r", loan_id_str)
        return

    async with SessionLocal() as db:
        loan = (
            await db.execute(select(Loan).where(Loan.id == loan_uuid))
        ).scalar_one_or_none()
        if loan is None:
            log.info("_ai_followup_job: loan %s gone, skipping", loan_id_str)
            return
        # Only fire if the pause actually elapsed. Belt-and-suspenders
        # against scheduler restarts that might re-fire the job.
        if loan.ai_paused_until is not None and engagement.is_paused(loan):
            log.info("_ai_followup_job: loan=%s still paused, skipping", loan_id_str)
            return
        # Generate a follow-up message. We don't have a "requesting user"
        # in this auto-resume context — pass None and let
        # _generate_ai_followup default to client audience.
        ai_msg = await _generate_ai_followup(db, loan)
        if ai_msg is None:
            log.info("_ai_followup_job: loan=%s no follow-up emitted", loan_id_str)
            return
        db.add(
            Activity(
                loan_id=loan.id,
                actor_id=None,
                actor_label="ai",
                kind="ai.auto_resumed_with_followup",
                summary=f"AI auto-resumed and posted a follow-up after takeover ended",
            )
        )
        await db.commit()
        # Push notify the client so they discover the follow-up.
        client = (
            await db.execute(select(ClientModel).where(ClientModel.id == loan.client_id))
        ).scalar_one_or_none()
        _notify_client(
            loan=loan,
            client_user_id=getattr(client, "user_id", None),
            actor_role="ai",
            body=ai_msg.body,
        )


async def _generate_ai_followup(db: AsyncSession, loan: Loan) -> LoanChatMessage | None:
    """Like `_generate_ai_reply` but used for auto-resume after an
    operator takeover. The system prompt nudges the AI to acknowledge
    the operator's prior turn(s) and offer to take it from here."""
    settings = get_settings()

    # Recent chat (client-visible only) — windowed to WINDOW turns.
    from app.services.ai.chat_rollup import WINDOW as _CHAT_WINDOW, maybe_rollup as _chat_rollup

    history_stmt = (
        select(LoanChatMessage)
        .where(LoanChatMessage.loan_id == loan.id)
        .where(LoanChatMessage.client_visible.is_(True))
        .order_by(LoanChatMessage.created_at.asc())
        .limit(200)
    )
    history = list((await db.execute(history_stmt)).scalars().all())
    if not history:
        return None
    # Don't double-fire: if the most recent message is already from the
    # AI, the AI already followed up. Skip.
    if history[-1].from_role == DealChatRole.AI:
        return None

    def _role(m):
        return "assistant" if m.from_role == DealChatRole.AI else "user"

    window = history[-_CHAT_WINDOW:]
    older = history[: max(0, len(history) - _CHAT_WINDOW)]
    older_pairs = [(_role(m), m.body or "") for m in older]
    profile = dict(loan.living_profile) if isinstance(loan.living_profile, dict) else {}
    prev_summary = profile.get("chat_summary")
    prev_count = int(profile.get("chat_summary_count") or 0)
    new_summary, new_count = await _chat_rollup(
        older_pairs, prev_summary, prev_count,
        meta={"loan_id": loan.id, "broker_id": loan.broker_id},
    )
    if new_summary != prev_summary or new_count != prev_count:
        from sqlalchemy.orm.attributes import flag_modified

        profile["chat_summary"] = new_summary
        profile["chat_summary_count"] = new_count
        loan.living_profile = profile
        flag_modified(loan, "living_profile")

    turns = [{"role": _role(m), "content": m.body} for m in window]
    system = (
        "You are the Qualified Commercial Deal Workspace AI on a loan "
        "that just came back online after a human operator took over the "
        "conversation briefly. Read the recent exchange, acknowledge what "
        "the operator said in 1-2 sentences, then offer to help the client "
        "from here. Be brief and warm. Do not repeat the operator's content "
        "verbatim.\n\n"
        + await assemble_loan_context(db, loan, audience="client")
    )
    if new_summary:
        system += "\n\nPRIOR CONVERSATION SUMMARY:\n" + new_summary
    from app.services.ai.task_training import (
        load_task_config as _load_tc,
        render_training_block as _render_tb,
    )

    system += _render_tb(await _load_tc(db, "nurture_chat"))

    if not settings.anthropic_api_key:
        reply_text = "I'm back online — let me know how I can help from here."
    else:
        try:
            from app.services.ai.orchestrator import run as orchestrator_run

            result = await orchestrator_run(
                turns,  # type: ignore[arg-type]
                tier="light",
                db=db,
                feature="loan_workspace_chat",
                max_tokens=300,
                system=system,
                meta={"activity": "loan_chat", "loan_id": loan.id, "broker_id": loan.broker_id},
            )
            reply_text = "".join(
                b.get("text", "")
                for b in result.get("content", [])
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
            if not reply_text:
                reply_text = "I'm back — happy to help you with the next steps."
        except Exception as exc:  # noqa: BLE001
            log.warning("AI follow-up generation failed for loan %s: %s", loan.id, exc)
            return None

    msg = LoanChatMessage(
        loan_id=loan.id,
        from_role=DealChatRole.AI,
        from_user_id=None,
        body=reply_text,
        client_visible=True,
    )
    db.add(msg)
    await db.flush()
    await db.refresh(msg)
    return msg

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
        chat_messages=await serialize_chat(db, list(chat_messages)),
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
    return await serialize_chat(db, list(rows))


# Per-mode allowed roles. CLIENT can only send `chat` (ignoring mode).
def _is_human_takeover(mode: DealChatMode, role: Role) -> bool:
    '''CHAT (super_admin/loan_exec) or LIVE_CHAT (broker/super_admin/loan_exec).
    Both branches persist the message client-visible and pause the AI.'''
    if mode == DealChatMode.CHAT and role in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        return True
    if mode == DealChatMode.LIVE_CHAT and role in (Role.BROKER, Role.SUPER_ADMIN, Role.LOAN_EXEC):
        return True
    return False


_MODE_ALLOWED_ROLES: dict[DealChatMode, set[Role]] = {
    DealChatMode.CHAT: {Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.CLIENT},
    DealChatMode.INSTRUCT: {Role.SUPER_ADMIN, Role.BROKER, Role.LOAN_EXEC},
    DealChatMode.BROKER_QUESTION: {Role.BROKER, Role.SUPER_ADMIN, Role.LOAN_EXEC},
    DealChatMode.BROKER_SUGGESTION: {Role.BROKER},
    DealChatMode.LIVE_CHAT: {Role.BROKER, Role.SUPER_ADMIN, Role.LOAN_EXEC},
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
    if _is_human_takeover(payload.mode, Role(user.role)):
        # Operator / broker takeover: persist + pause AI for 1h, no AI auto-reply.
        # Push the message preview to the client's mobile so they actually
        # discover it (otherwise it sits silently in loan_chat_messages until
        # the 15s mobile ChatPane poll lands — which they may never trigger).
        actor_role = (
            DealChatRole.BROKER if Role(user.role) == Role.BROKER
            else DealChatRole.SUPER_ADMIN
        )
        msg = LoanChatMessage(
            loan_id=loan.id,
            from_role=actor_role,
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
                kind=("ai.paused_by_broker" if Role(user.role) == Role.BROKER else "ai.paused_by_super_admin"),
                summary=f"{user.email} ({user.role}) took over the chat; Elara paused until {paused_until.isoformat()}",
            )
        )
        await db.flush()
        await db.refresh(msg)

        # Push fan-out + schedule the AI re-engagement at pause expiry.
        client = (
            await db.execute(select(ClientModel).where(ClientModel.id == loan.client_id))
        ).scalar_one_or_none()
        _notify_client(
            loan=loan,
            client_user_id=getattr(client, "user_id", None),
            actor_role=actor_role.value,
            body=payload.body,
        )
        _schedule_resume_followup(loan_id=loan.id, paused_until=paused_until)

        return ChatSendResponse(
            kind="message",
            message=await serialize_chat_one(db, msg),
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
        attachment_document_id=payload.attachment_document_id,
    )
    db.add(msg)
    await db.flush()
    await db.refresh(msg)

    paused_now = engagement.is_paused(loan)
    if paused_now and not is_broker_q:
        # Client send during pause — persist, no AI reply.
        return ChatSendResponse(
            kind="message",
            message=await serialize_chat_one(db, msg),
            paused_until=loan.ai_paused_until,
        )

    # Trigger AI auto-reply (synchronous, with per-loan lock).
    lock = _chat_locks[str(loan.id)]
    async with lock:
        ai_msg = await _generate_ai_reply(db, loan, user, client_visible=client_visible)
    return ChatSendResponse(
        kind="message",
        message=await serialize_chat_one(db, msg),
        ai_reply=await serialize_chat_one(db, ai_msg) if ai_msg else None,
    )


async def _generate_ai_reply(
    db: AsyncSession, loan: Loan, requesting_user: User, *, client_visible: bool
) -> LoanChatMessage | None:
    """Builds the prompt via the unified context assembly, calls Anthropic,
    persists the reply. Returns None if the upstream is unavailable (we don't
    block the user's own send on AI failures)."""
    settings = get_settings()
    audience: Audience = _audience_for(requesting_user)

    # Agent working-hours gate. The borrower just sent a message, so this
    # is a client-initiated reply — the only after-hours rule that blocks
    # it is `block_all`. The default (do_not_initiate_reply_if_client_first)
    # explicitly permits replying after-hours when the client wrote first.
    # `portal_replies_only` also permits the portal channel by definition;
    # chat IS the portal channel here, so it allows too.
    from app.enums import OutreachChannel
    from app.services.ai.agent_settings import working_hours_for_loan
    from app.services.ai.working_hours import evaluate as evaluate_working_hours
    wh_rules = await working_hours_for_loan(db, loan)
    wh_decision = evaluate_working_hours(
        wh_rules,
        channel=OutreachChannel.PORTAL.value,
        is_client_initiated=True,
    )
    if not wh_decision.allow:
        log.info(
            "loan_workspace.ai_reply_blocked_by_working_hours loan=%s reason=%s",
            loan.id, wh_decision.reason,
        )
        return None

    # Full conversation in ascending order (capped so an enormous
    # thread can't dominate the rollup query). The recent WINDOW is
    # sent verbatim; everything older folds into a rolling summary.
    from app.services.ai.chat_rollup import WINDOW as _CHAT_WINDOW, maybe_rollup as _chat_rollup

    history_stmt = (
        select(LoanChatMessage)
        .where(LoanChatMessage.loan_id == loan.id)
        .order_by(LoanChatMessage.created_at.asc())
        .limit(200)
    )
    if audience == "client":
        history_stmt = history_stmt.where(LoanChatMessage.client_visible.is_(True))
    history = list((await db.execute(history_stmt)).scalars().all())

    def _role(m):
        return "assistant" if m.from_role == DealChatRole.AI else "user"

    window = history[-_CHAT_WINDOW:] if history else []
    older = history[: max(0, len(history) - _CHAT_WINDOW)]
    older_pairs = [(_role(m), m.body or "") for m in older]

    # Living-profile-stored rolling summary (no migration — JSONB).
    profile = dict(loan.living_profile) if isinstance(loan.living_profile, dict) else {}
    prev_summary = profile.get("chat_summary")
    prev_count = int(profile.get("chat_summary_count") or 0)
    new_summary, new_count = await _chat_rollup(
        older_pairs,
        prev_summary,
        prev_count,
        meta={"loan_id": loan.id, "broker_id": loan.broker_id},
    )
    if new_summary != prev_summary or new_count != prev_count:
        from sqlalchemy.orm.attributes import flag_modified

        profile["chat_summary"] = new_summary
        profile["chat_summary_count"] = new_count
        loan.living_profile = profile
        flag_modified(loan, "living_profile")

    turns = [{"role": _role(m), "content": m.body} for m in window]
    if not turns or turns[-1]["role"] != "user":
        return None  # nothing to reply to

    # Build the stable system block. Caching depends on this being
    # byte-identical across rapid follow-up turns within the same
    # conversation — orchestrator.run wraps it with cache_control.
    system = (
        "You are the Qualified Commercial Deal Workspace AI. Stay scoped to "
        "this single loan. Be concise — 1-3 sentences unless asked to expand.\n\n"
        + await assemble_loan_context(db, loan, audience=audience)
    )
    if new_summary:
        system += (
            "\n\nPRIOR CONVERSATION SUMMARY (older turns folded in):\n"
            + new_summary
        )
    # Operator training layer — borrower-facing chat only. Additive.
    if audience == "client":
        from app.services.ai.task_training import load_task_config, render_training_block

        system += render_training_block(await load_task_config(db, "nurture_chat"))

    if not settings.anthropic_api_key:
        # Stub when no key — keep the workspace usable in dev.
        reply_text = "(stub) I would normally answer here once ANTHROPIC_API_KEY is set."
    else:
        try:
            from app.services.ai.orchestrator import run as orchestrator_run

            result = await orchestrator_run(
                turns,  # type: ignore[arg-type]
                tier="light",
                db=db,
                feature="loan_workspace_chat",
                max_tokens=500,
                system=system,
                meta={
                    "activity": "loan_chat",
                    "loan_id": loan.id,
                    "broker_id": loan.broker_id,
                },
            )
            reply_text = "".join(
                b.get("text", "")
                for b in result.get("content", [])
                if isinstance(b, dict) and b.get("type") == "text"
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
    # Push fan-out: the client's mobile gets a real-time signal that the
    # AI replied. Skipped when the reply is broker-internal (Q&A) — that
    # exchange isn't client-visible.
    if client_visible:
        client_row = (
            await db.execute(select(ClientModel).where(ClientModel.id == loan.client_id))
        ).scalar_one_or_none()
        _notify_client(
            loan=loan,
            client_user_id=getattr(client_row, "user_id", None),
            actor_role=DealChatRole.AI.value,
            body=reply_text,
        )
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

    from app.services.activity_log import log_change
    await log_change(
        db,
        kind="ai_modify.correction_added",
        summary=f"AI Modify correction attached · {body.correction[:120]}",
        loan_id=loan_id,
        actor=user,
        payload={
            "correction_id": str(correction.id),
            "target_message_id": str(message_id),
            "correction": body.correction,
        },
    )
    await db.flush()

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

    from app.services.activity_log import log_change
    await log_change(
        db,
        kind="loan.scenario_saved",
        summary=(
            f"Scenario saved · {body.name} · "
            f"rate {snapshot['final_rate']}, P&I ${pi:,.0f}"
        ),
        loan_id=loan_id,
        actor=user,
        payload={
            "scenario_id": str(scenario.id),
            "name": body.name,
            "loan_amount": float(amount),
            "base_rate": float(base_rate),
            "discount_points": float(body.discount_points or 0),
            "snapshot": snapshot,
        },
    )

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

    # Snapshot for the diff payload.
    from app.services.activity_log import diff_changes, log_change
    before = {
        "label": line.label, "amount": line.amount, "category": line.category,
        "payee": line.payee, "note": line.note,
    }
    if body.label is not None:
        line.label = body.label
    if body.amount is not None:
        line.amount = body.amount
    if body.category is not None:
        line.category = body.category
    if body.payee is not None:
        line.payee = body.payee
    if body.note is not None:
        line.note = body.note
    after = {
        "label": line.label, "amount": line.amount, "category": line.category,
        "payee": line.payee, "note": line.note,
    }
    changes = diff_changes(before, after, fields=("label", "amount", "category", "payee", "note"))
    if changes:
        await log_change(
            db,
            kind="hud.line_edited",
            summary=f"HUD line edited · {line.label or '(no label)'}",
            loan_id=loan_id,
            actor=user,
            payload={"line_id": str(line.id), "changes": changes},
        )

    await db.flush()
    await db.refresh(line)
    return HudLineRead.model_validate(line)


# ── HUD list / add / delete (operator-side) ────────────────────────────


@router.get("/hud", response_model=list[HudLineRead])
async def list_hud_lines(
    loan_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[HudLineRead]:
    """All HUD lines for a loan, ordered by code then label so the
    editable table renders deterministically."""
    await _load_loan(db, loan_id, user)
    rows = (
        await db.execute(
            select(HudLineItem)
            .where(HudLineItem.loan_id == loan_id)
            .order_by(HudLineItem.code, HudLineItem.label)
        )
    ).scalars().all()
    return [HudLineRead.model_validate(r) for r in rows]


@router.post("/hud", response_model=HudLineRead)
async def create_hud_line(
    loan_id: UUID,
    body: HudLineCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> HudLineRead:
    """Operator adds a new line item to the HUD. Internal-only access
    so we don't accept anonymous additions through this endpoint —
    that path goes through /public/hud/{token}/lines."""
    if user.role not in {Role.SUPER_ADMIN, Role.BROKER, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    await _load_loan(db, loan_id, user)
    line = HudLineItem(
        loan_id=loan_id,
        code=body.code or "custom",
        label=body.label,
        amount=body.amount,
        category=body.category or "variable",
        editable=True,
        payee=body.payee,
        note=body.note,
    )
    db.add(line)
    await db.flush()
    await db.refresh(line)

    from app.services.activity_log import log_change
    await log_change(
        db,
        kind="hud.line_added",
        summary=f"HUD line added · {line.label or '(no label)'} ${float(line.amount or 0):,.2f}",
        loan_id=loan_id,
        actor=user,
        payload={
            "line_id": str(line.id),
            "code": line.code,
            "label": line.label,
            "amount": float(line.amount or 0),
            "category": line.category,
        },
    )
    return HudLineRead.model_validate(line)


@router.delete("/hud/{line_id}", status_code=204)
async def delete_hud_line(
    loan_id: UUID,
    line_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Operator removes a HUD line. Locked playbook lines (editable=false)
    can't be removed through this endpoint — funding team only path."""
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
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This HUD line is locked")

    from app.services.activity_log import log_change
    await log_change(
        db,
        kind="hud.line_deleted",
        summary=f"HUD line removed · {line.label or '(no label)'}",
        loan_id=loan_id,
        actor=user,
        payload={
            "line_id": str(line.id),
            "code": line.code,
            "label": line.label,
            "amount": float(line.amount or 0),
        },
    )

    await db.delete(line)
    await db.flush()


# ── HUD share links (operator-side) ────────────────────────────────────


def _new_share_token() -> str:
    """URL-safe token; ~256 bits of entropy. Long enough that brute
    forcing is uninteresting; short enough that the URL is shareable."""
    import secrets
    return secrets.token_urlsafe(32)


@router.get("/hud/shares", response_model=list[HudShareLinkRead])
async def list_hud_share_links(
    loan_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[HudShareLinkRead]:
    if user.role not in {Role.SUPER_ADMIN, Role.BROKER, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    await _load_loan(db, loan_id, user)
    rows = (
        await db.execute(
            select(HudShareLink)
            .where(HudShareLink.loan_id == loan_id)
            .order_by(HudShareLink.created_at.desc())
        )
    ).scalars().all()
    return [HudShareLinkRead.model_validate(r) for r in rows]


@router.post("/hud/shares", response_model=HudShareLinkRead)
async def create_hud_share_link(
    loan_id: UUID,
    body: HudShareLinkCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> HudShareLinkRead:
    """Mint a new public share link for the HUD. Returns the row with
    its token so the client can build the share URL on its side."""
    if user.role not in {Role.SUPER_ADMIN, Role.BROKER, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    await _load_loan(db, loan_id, user)
    link = HudShareLink(
        loan_id=loan_id,
        token=_new_share_token(),
        label=body.label,
        invitee_email=body.invitee_email,
        invitee_role=body.invitee_role,
        expires_at=body.expires_at,
        created_by=user.id,
    )
    db.add(link)
    await db.flush()
    await db.refresh(link)
    return HudShareLinkRead.model_validate(link)


@router.delete("/hud/shares/{share_id}", status_code=204)
async def revoke_hud_share_link(
    loan_id: UUID,
    share_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft revoke — we keep the row for audit so we can trace which
    line items came from which now-revoked invitee."""
    if user.role not in {Role.SUPER_ADMIN, Role.BROKER, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    await _load_loan(db, loan_id, user)
    link = (
        await db.execute(
            select(HudShareLink).where(
                HudShareLink.id == share_id, HudShareLink.loan_id == loan_id
            )
        )
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share link not found")
    if link.revoked_at is None:
        link.revoked_at = datetime.now(timezone.utc)
    await db.flush()


# ── PUBLIC HUD (token-resolved, no auth) ───────────────────────────────
#
# Exposed via a SEPARATE router with no `/loans/{id}` prefix and no auth
# requirement. The token is the ONLY identifier — we resolve loan +
# invitee from it. All write endpoints tag the new/edited lines with
# the share link id so the operator can see provenance.


public_router = APIRouter(prefix="/public/hud", tags=["public-hud"])


async def _resolve_share_link(token: str, db: AsyncSession) -> tuple[HudShareLink, Loan]:
    link = (
        await db.execute(select(HudShareLink).where(HudShareLink.token == token))
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share link not found")
    if link.revoked_at is not None:
        raise HTTPException(status.HTTP_410_GONE, "Share link has been revoked")
    if link.expires_at is not None and link.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_410_GONE, "Share link has expired")
    loan = (await db.execute(select(Loan).where(Loan.id == link.loan_id))).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    link.last_used_at = datetime.now(timezone.utc)
    await db.flush()
    return link, loan


@public_router.get("/{token}", response_model=PublicHudView)
async def public_hud_view(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> PublicHudView:
    link, loan = await _resolve_share_link(token, db)
    rows = (
        await db.execute(
            select(HudLineItem)
            .where(HudLineItem.loan_id == loan.id)
            .order_by(HudLineItem.code, HudLineItem.label)
        )
    ).scalars().all()
    return PublicHudView(
        loan_label=loan.deal_id or str(loan.id)[:8],
        loan_address=loan.address or "",
        invitee_label=link.label,
        invitee_role=link.invitee_role,
        revoked=link.revoked_at is not None,
        expired=link.expires_at is not None and link.expires_at < datetime.now(timezone.utc),
        lines=[HudLineRead.model_validate(r) for r in rows],
    )


@public_router.post("/{token}/lines", response_model=HudLineRead)
async def public_add_hud_line(
    token: str,
    body: HudLineCreate,
    db: AsyncSession = Depends(get_db),
) -> HudLineRead:
    link, loan = await _resolve_share_link(token, db)
    line = HudLineItem(
        loan_id=loan.id,
        code=body.code or "custom",
        label=body.label,
        amount=body.amount,
        category=body.category or "variable",
        editable=True,
        payee=body.payee,
        note=body.note,
        created_by_share_link_id=link.id,
    )
    db.add(line)
    await db.flush()
    await db.refresh(line)
    return HudLineRead.model_validate(line)


@public_router.patch("/{token}/lines/{line_id}", response_model=HudLineRead)
async def public_patch_hud_line(
    token: str,
    line_id: UUID,
    body: HudLinePatch,
    db: AsyncSession = Depends(get_db),
) -> HudLineRead:
    """External party can edit lines they themselves created. Anything
    else stays read-only from the public side."""
    link, loan = await _resolve_share_link(token, db)
    line = (
        await db.execute(
            select(HudLineItem).where(
                HudLineItem.id == line_id, HudLineItem.loan_id == loan.id
            )
        )
    ).scalar_one_or_none()
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "HUD line not found")
    if line.created_by_share_link_id != link.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only edit lines you created")
    if body.label is not None:
        line.label = body.label
    if body.amount is not None:
        line.amount = body.amount
    if body.category is not None:
        line.category = body.category
    if body.payee is not None:
        line.payee = body.payee
    if body.note is not None:
        line.note = body.note
    await db.flush()
    await db.refresh(line)
    return HudLineRead.model_validate(line)


@public_router.delete("/{token}/lines/{line_id}", status_code=204)
async def public_delete_hud_line(
    token: str,
    line_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    link, loan = await _resolve_share_link(token, db)
    line = (
        await db.execute(
            select(HudLineItem).where(
                HudLineItem.id == line_id, HudLineItem.loan_id == loan.id
            )
        )
    ).scalar_one_or_none()
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "HUD line not found")
    if line.created_by_share_link_id != link.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only delete lines you created")
    await db.delete(line)
    await db.flush()
