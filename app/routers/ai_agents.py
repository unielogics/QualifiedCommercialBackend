"""AI Agents — the broker's 11-step builder + live-engine API.

Every route is scoped to `Role.BROKER` and to the caller's own
`broker_id`. This router is purely additive — it does not touch the
existing AI Inbox, cadence engine, or any other account type.

See `app/services/ai/ai_agent.py` for gate evaluation, targeting,
heavy synthesis, and the message composer.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import (
    AIAgentDomain,
    AIAgentKind,
    AIAgentPersonaMode,
    AIAgentSendMode,
    AIAgentStatus,
    ClientStage,
    Role,
)
from app.models.ai_agent import (
    AIAgent,
    AIAgentExitRules,
    AIAgentGoal,
    AIAgentKnowledgeLink,
    AIAgentLead,
    AIAgentMessage,
    AIAgentPlaybook,
    AIAgentShowingGuide,
    AIAgentTargeting,
    AIAgentTestScenario,
    AIAgentTrainingMessage,
    AIAgentTrainingSession,
    AIVoiceProfile,
)
from app.models.ai_knowledge_document import AIKnowledgeDocument
from app.models.broker import Broker
from app.models.client import Client
from app.services.ai import ai_agent as svc

router = APIRouter(prefix="/ai-agents", tags=["ai-agents"])
log = logging.getLogger(__name__)


# ── helpers ─────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _broker_for(db: AsyncSession, user) -> Broker:
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "AI Agents are agent-only.")
    broker = (
        await db.execute(select(Broker).where(Broker.user_id == user.id))
    ).scalar_one_or_none()
    if broker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No broker profile.")
    return broker


async def _agent_or_404(db: AsyncSession, user, agent_id: uuid.UUID) -> AIAgent:
    broker = await _broker_for(db, user)
    agent = await db.get(AIAgent, agent_id)
    if agent is None or agent.broker_id != broker.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI Agent not found.")
    return agent


def _ser_agent(agent: AIAgent) -> dict[str, Any]:
    return {
        "id": str(agent.id),
        "name": agent.name,
        "kind": agent.kind,
        "audience": agent.audience,
        "ai_display_name": agent.ai_display_name,
        "persona_mode": agent.persona_mode,
        "status": agent.status,
        "send_mode": agent.send_mode,
        "warmup_mode": agent.warmup_mode,
        "max_followups": agent.max_followups,
        "cadence": agent.cadence,
        "voice_profile_id": str(agent.voice_profile_id) if agent.voice_profile_id else None,
        "last_tested_at": agent.last_tested_at.isoformat() if agent.last_tested_at else None,
        "activated_at": agent.activated_at.isoformat() if agent.activated_at else None,
        "created_at": agent.created_at.isoformat() if agent.created_at else None,
    }


def _validate(value: str, enum_cls, field: str) -> str:
    try:
        return enum_cls(value).value
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Invalid {field}: {value}"
        ) from None


# ── Step 1: Basics + list/create ────────────────────────────────────


class AgentCreate(BaseModel):
    name: str
    kind: str = "custom"
    audience: str | None = None


class AgentPatch(BaseModel):
    name: str | None = None
    kind: str | None = None
    audience: str | None = None
    ai_display_name: str | None = None
    persona_mode: str | None = None
    send_mode: str | None = None
    max_followups: int | None = None
    cadence: list[int] | None = None
    voice_profile_id: uuid.UUID | None = None


@router.get("")
async def list_agents(user: CurrentUser, db: AsyncSession = Depends(get_db)):
    broker = await _broker_for(db, user)
    agents = list(
        (
            await db.execute(
                select(AIAgent)
                .where(AIAgent.broker_id == broker.id)
                .order_by(AIAgent.created_at.desc())
            )
        ).scalars().all()
    )
    out = []
    for a in agents:
        lead_n = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(AIAgentLead)
                    .where(AIAgentLead.ai_agent_id == a.id)
                    .where(AIAgentLead.status.in_(["pending_review", "active", "replied"]))
                )
            ).scalar()
            or 0
        )
        row = _ser_agent(a)
        row["lead_count"] = lead_n
        row["steps"] = await svc.step_states(db, a)
        out.append(row)
    return out


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    broker = await _broker_for(db, user)
    kind = _validate(payload.kind, AIAgentKind, "kind")
    agent = AIAgent(
        broker_id=broker.id,
        owner_user_id=user.id,
        name=payload.name.strip()[:160] or "Untitled AI Agent",
        kind=kind,
        audience=payload.audience,
        status=AIAgentStatus.DRAFT,
    )
    db.add(agent)
    await db.flush()
    await db.commit()
    return _ser_agent(agent)


@router.get("/{agent_id}")
async def get_agent(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    row = _ser_agent(agent)
    row["steps"] = await svc.step_states(db, agent)
    row["gate_blockers"] = await svc.evaluate_gate(db, agent)
    return row


@router.patch("/{agent_id}")
async def patch_agent(
    agent_id: uuid.UUID,
    payload: AgentPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent_or_404(db, user, agent_id)
    if payload.name is not None:
        agent.name = payload.name.strip()[:160] or agent.name
    if payload.kind is not None:
        agent.kind = _validate(payload.kind, AIAgentKind, "kind")
    if payload.audience is not None:
        agent.audience = payload.audience
    if payload.ai_display_name is not None:
        agent.ai_display_name = payload.ai_display_name.strip()[:120] or None
    if payload.persona_mode is not None:
        agent.persona_mode = _validate(
            payload.persona_mode, AIAgentPersonaMode, "persona_mode"
        )
    if payload.send_mode is not None:
        agent.send_mode = _validate(payload.send_mode, AIAgentSendMode, "send_mode")
    if payload.max_followups is not None:
        agent.max_followups = max(0, min(20, payload.max_followups))
    if payload.cadence is not None:
        agent.cadence = [int(x) for x in payload.cadence][:20]
    if payload.voice_profile_id is not None:
        vp = await db.get(AIVoiceProfile, payload.voice_profile_id)
        if vp is None or vp.broker_id != agent.broker_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Voice profile not found.")
        agent.voice_profile_id = vp.id
    await db.commit()
    return _ser_agent(agent)


@router.delete("/{agent_id}")
async def archive_agent(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    agent.status = AIAgentStatus.ARCHIVED
    agent.archived_at = _now()
    await db.commit()
    return {"ok": True}


# ── Step 2: Goal ────────────────────────────────────────────────────


class GoalIn(BaseModel):
    primary_goal: str | None = None
    primary_cta: str | None = None
    handoff_triggers: list[Any] = []
    success_definition: str | None = None
    qualified_reply_definition: str | None = None
    auto_reply_boundaries: dict[str, Any] = {}


def _ser_goal(g: AIAgentGoal | None) -> dict[str, Any]:
    if g is None:
        return {}
    return {
        "primary_goal": g.primary_goal,
        "primary_cta": g.primary_cta,
        "handoff_triggers": g.handoff_triggers,
        "success_definition": g.success_definition,
        "qualified_reply_definition": g.qualified_reply_definition,
        "auto_reply_boundaries": g.auto_reply_boundaries,
    }


@router.get("/{agent_id}/goal")
async def get_goal(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    g = (
        await db.execute(select(AIAgentGoal).where(AIAgentGoal.ai_agent_id == agent.id))
    ).scalar_one_or_none()
    return _ser_goal(g)


@router.put("/{agent_id}/goal")
async def put_goal(
    agent_id: uuid.UUID,
    payload: GoalIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent_or_404(db, user, agent_id)
    g = (
        await db.execute(select(AIAgentGoal).where(AIAgentGoal.ai_agent_id == agent.id))
    ).scalar_one_or_none()
    if g is None:
        g = AIAgentGoal(ai_agent_id=agent.id)
        db.add(g)
    g.primary_goal = payload.primary_goal
    g.primary_cta = payload.primary_cta
    g.handoff_triggers = payload.handoff_triggers or []
    g.success_definition = payload.success_definition
    g.qualified_reply_definition = payload.qualified_reply_definition
    g.auto_reply_boundaries = payload.auto_reply_boundaries or {}
    await db.commit()
    return _ser_goal(g)


# ── Step 3: Knowledge links ─────────────────────────────────────────


class KnowledgeLinkIn(BaseModel):
    knowledge_document_id: uuid.UUID
    attach_to_emails: bool = False


@router.get("/{agent_id}/knowledge-links")
async def list_knowledge_links(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    rows = list(
        (
            await db.execute(
                select(AIAgentKnowledgeLink, AIKnowledgeDocument)
                .join(
                    AIKnowledgeDocument,
                    AIKnowledgeDocument.id == AIAgentKnowledgeLink.knowledge_document_id,
                )
                .where(AIAgentKnowledgeLink.ai_agent_id == agent.id)
            )
        ).all()
    )
    return [
        {
            "id": str(link.id),
            "knowledge_document_id": str(doc.id),
            "filename": doc.filename,
            "doc_type": doc.doc_type,
            "summary": doc.summary,
            "status": doc.status,
            "attach_to_emails": link.attach_to_emails,
        }
        for link, doc in rows
    ]


@router.post("/{agent_id}/knowledge-links", status_code=status.HTTP_201_CREATED)
async def add_knowledge_link(
    agent_id: uuid.UUID,
    payload: KnowledgeLinkIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent_or_404(db, user, agent_id)
    doc = await db.get(AIKnowledgeDocument, payload.knowledge_document_id)
    if doc is None or doc.agent_user_id != user.id or doc.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Knowledge document not found.")
    existing = (
        await db.execute(
            select(AIAgentKnowledgeLink)
            .where(AIAgentKnowledgeLink.ai_agent_id == agent.id)
            .where(AIAgentKnowledgeLink.knowledge_document_id == doc.id)
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = AIAgentKnowledgeLink(
            ai_agent_id=agent.id,
            knowledge_document_id=doc.id,
            attach_to_emails=payload.attach_to_emails,
        )
        db.add(existing)
    else:
        existing.attach_to_emails = payload.attach_to_emails
    await db.commit()
    return {"ok": True}


@router.delete("/{agent_id}/knowledge-links/{link_id}")
async def remove_knowledge_link(
    agent_id: uuid.UUID,
    link_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent_or_404(db, user, agent_id)
    link = await db.get(AIAgentKnowledgeLink, link_id)
    if link is None or link.ai_agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found.")
    await db.delete(link)
    await db.commit()
    return {"ok": True}


# ── Step 4: Targeting ───────────────────────────────────────────────


class TargetingIn(BaseModel):
    domain: str = "clients"
    include_rules: dict[str, Any] = {}
    exclude_rules: dict[str, Any] = {}
    enrollment_mode: str = "review"


@router.get("/{agent_id}/targeting")
async def get_targeting(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    t = (
        await db.execute(
            select(AIAgentTargeting).where(AIAgentTargeting.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if t is None:
        return {}
    return {
        "domain": t.domain,
        "include_rules": t.include_rules,
        "exclude_rules": t.exclude_rules,
        "enrollment_mode": t.enrollment_mode,
        "last_targeting_pass_at": t.last_targeting_pass_at.isoformat()
        if t.last_targeting_pass_at
        else None,
    }


@router.put("/{agent_id}/targeting")
async def put_targeting(
    agent_id: uuid.UUID,
    payload: TargetingIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent_or_404(db, user, agent_id)
    domain = _validate(payload.domain, AIAgentDomain, "domain")
    if payload.enrollment_mode not in ("auto", "review"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Bad enrollment_mode.")
    t = (
        await db.execute(
            select(AIAgentTargeting).where(AIAgentTargeting.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if t is None:
        t = AIAgentTargeting(ai_agent_id=agent.id)
        db.add(t)
    t.domain = domain
    t.include_rules = payload.include_rules or {}
    t.exclude_rules = payload.exclude_rules or {}
    t.enrollment_mode = payload.enrollment_mode
    await db.commit()
    return {"ok": True}


@router.get("/{agent_id}/targeting/preview")
async def preview_targeting(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    return await svc.preview_targeting(db, agent)


@router.post("/{agent_id}/targeting/run")
async def run_targeting_now(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    result = await svc.run_targeting(db, agent)
    await db.commit()
    return result


@router.get("/{agent_id}/leads")
async def list_leads(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    rows = list(
        (
            await db.execute(
                select(AIAgentLead, Client)
                .join(Client, Client.id == AIAgentLead.client_id)
                .where(AIAgentLead.ai_agent_id == agent.id)
                .order_by(AIAgentLead.enrolled_at.desc())
            )
        ).all()
    )
    return [
        {
            "id": str(lead.id),
            "client_id": str(client.id),
            "name": client.name,
            "email": client.email,
            "stage": str(client.stage),
            "status": lead.status,
            "attempts_made": lead.attempts_made,
            "next_action_at": lead.next_action_at.isoformat()
            if lead.next_action_at
            else None,
        }
        for lead, client in rows
    ]


# ── Step 11 helper: warm-up contact enrollment ──────────────────────


def _enter_warmup(agent: AIAgent) -> None:
    """Selecting/creating a warm-up contact flips the agent into active
    warm-up mode — the AI starts working the delegated contacts; the
    broker can leave and come back to review + activate."""
    if agent.status not in (AIAgentStatus.ACTIVE, AIAgentStatus.ARCHIVED):
        agent.status = AIAgentStatus.ACTIVE
        agent.warmup_mode = True
        if agent.activated_at is None:
            agent.activated_at = _now()


async def _enroll_lead(db: AsyncSession, agent: AIAgent, client_id: uuid.UUID) -> bool:
    """Enroll one client as an active lead. Returns True if newly added."""
    existing = (
        await db.execute(
            select(AIAgentLead)
            .where(AIAgentLead.ai_agent_id == agent.id)
            .where(AIAgentLead.client_id == client_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status in ("exited", "handed_off"):
            existing.status = "active"
            existing.next_action_at = _now()
        return False
    db.add(
        AIAgentLead(
            ai_agent_id=agent.id,
            client_id=client_id,
            status="active",
            next_action_at=_now(),
        )
    )
    return True


class AssignLeadsIn(BaseModel):
    client_ids: list[uuid.UUID]


class CreateWarmupContactIn(BaseModel):
    name: str
    email: str
    phone: str | None = None


@router.post("/{agent_id}/leads/assign")
async def assign_leads(
    agent_id: uuid.UUID,
    payload: AssignLeadsIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    """Delegate one or more existing clients to this agent as warm-up
    contacts. Flips the agent into warm-up mode."""
    agent = await _agent_or_404(db, user, agent_id)
    added = 0
    for cid in payload.client_ids:
        client = await db.get(Client, cid)
        if client is None or client.broker_id != agent.broker_id:
            continue
        if await _enroll_lead(db, agent, cid):
            added += 1
    if payload.client_ids:
        _enter_warmup(agent)
    await db.commit()
    return {"assigned": added}


@router.post("/{agent_id}/leads/create")
async def create_warmup_contact(
    agent_id: uuid.UUID,
    payload: CreateWarmupContactIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    """Create a brand-new contact and delegate it to this agent for
    warm-up. Reuses an existing client when the email already matches."""
    agent = await _agent_or_404(db, user, agent_id)
    email = (payload.email or "").strip()
    client: Client | None = None
    if email:
        client = (
            await db.execute(
                select(Client)
                .where(Client.broker_id == agent.broker_id)
                .where(func.lower(Client.email) == email.lower())
            )
        ).scalars().first()
    if client is None:
        client = Client(
            name=payload.name.strip()[:160] or "New contact",
            email=email or None,
            phone=(payload.phone or None),
            broker_id=agent.broker_id,
            stage=ClientStage.LEAD,
        )
        db.add(client)
        await db.flush()
    await _enroll_lead(db, agent, client.id)
    _enter_warmup(agent)
    await db.commit()
    return {"client_id": str(client.id), "name": client.name}


# ── Step 5: Training Studio ─────────────────────────────────────────


class TrainingTurnIn(BaseModel):
    message: str


@router.get("/{agent_id}/training")
async def get_training(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    session = (
        await db.execute(
            select(AIAgentTrainingSession)
            .where(AIAgentTrainingSession.ai_agent_id == agent.id)
            .order_by(AIAgentTrainingSession.created_at.desc())
        )
    ).scalars().first()
    if session is None:
        return {"session_id": None, "completed": False, "messages": []}
    msgs = list(
        (
            await db.execute(
                select(AIAgentTrainingMessage)
                .where(AIAgentTrainingMessage.session_id == session.id)
                .order_by(AIAgentTrainingMessage.created_at)
            )
        ).scalars().all()
    )
    return {
        "session_id": str(session.id),
        "completed": session.completed_at is not None,
        "messages": [{"role": m.role, "content": m.content} for m in msgs],
    }


@router.post("/{agent_id}/training/messages")
async def post_training_turn(
    agent_id: uuid.UUID,
    payload: TrainingTurnIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent_or_404(db, user, agent_id)
    session = (
        await db.execute(
            select(AIAgentTrainingSession)
            .where(AIAgentTrainingSession.ai_agent_id == agent.id)
            .where(AIAgentTrainingSession.completed_at.is_(None))
            .order_by(AIAgentTrainingSession.created_at.desc())
        )
    ).scalars().first()
    if session is None:
        session = AIAgentTrainingSession(ai_agent_id=agent.id)
        db.add(session)
        await db.flush()

    db.add(
        AIAgentTrainingMessage(
            session_id=session.id, role="user", content=payload.message[:8000]
        )
    )
    await db.flush()

    history = list(
        (
            await db.execute(
                select(AIAgentTrainingMessage)
                .where(AIAgentTrainingMessage.session_id == session.id)
                .order_by(AIAgentTrainingMessage.created_at)
            )
        ).scalars().all()
    )
    reply = "Thanks — tell me more about how you'd describe this to a new lead."
    try:
        from app.services.ai.orchestrator import run

        convo = [
            {"role": m.role if m.role in ("user", "assistant") else "user", "content": m.content}
            for m in history
        ]
        result = await run(
            convo,
            tier="light",
            max_tokens=600,
            system=(
                "You are a friendly real-estate sales consultant interviewing an "
                "agent to learn their product, market, offer, common objections, "
                f"and tone for an AI worker called '{agent.name}'. Ask ONE focused "
                "question at a time. Be concise and warm. After you have enough, "
                "tell the agent they can finish the training."
            ),
        )
        text = svc._text_of(result)
        if text:
            reply = text
    except Exception as exc:  # noqa: BLE001
        log.warning("training turn failed: %s", exc)

    db.add(
        AIAgentTrainingMessage(session_id=session.id, role="assistant", content=reply)
    )
    if agent.status == AIAgentStatus.DRAFT:
        agent.status = AIAgentStatus.TRAINING_IN_PROGRESS
    await db.commit()
    return {"session_id": str(session.id), "reply": reply}


@router.post("/{agent_id}/training/complete")
async def complete_training(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    session = (
        await db.execute(
            select(AIAgentTrainingSession)
            .where(AIAgentTrainingSession.ai_agent_id == agent.id)
            .where(AIAgentTrainingSession.completed_at.is_(None))
            .order_by(AIAgentTrainingSession.created_at.desc())
        )
    ).scalars().first()
    if session is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No training session to complete.")
    session.completed_at = _now()
    if agent.status in (AIAgentStatus.DRAFT, AIAgentStatus.TRAINING_IN_PROGRESS):
        agent.status = AIAgentStatus.NEEDS_REVIEW
    await db.commit()
    return {"ok": True}


# ── Steps 6 & 7: Playbook + Showing Guide ───────────────────────────


def _ser_synth(row) -> dict[str, Any]:
    if row is None:
        return {"generation_status": "idle", "approval_status": "draft", "content": {}}
    return {
        "content": row.content,
        "generation_status": row.generation_status,
        "generation_error": row.generation_error,
        "approval_status": row.approval_status,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
    }


@router.get("/{agent_id}/playbook")
async def get_playbook(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    row = (
        await db.execute(
            select(AIAgentPlaybook).where(AIAgentPlaybook.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    return _ser_synth(row)


@router.post("/{agent_id}/playbook/generate")
async def generate_playbook(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    row = (
        await db.execute(
            select(AIAgentPlaybook).where(AIAgentPlaybook.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = AIAgentPlaybook(ai_agent_id=agent.id)
        db.add(row)
    row.generation_status = "generating"
    row.generation_error = None
    await db.commit()
    return {"generation_status": "generating"}


@router.post("/{agent_id}/playbook/approve")
async def approve_playbook(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    row = (
        await db.execute(
            select(AIAgentPlaybook).where(AIAgentPlaybook.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if row is None or row.generation_status != "ready":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Playbook not ready to approve.")
    row.approval_status = "approved"
    row.approved_at = _now()
    await db.commit()
    return _ser_synth(row)


class SynthEditIn(BaseModel):
    content: dict[str, Any]


@router.put("/{agent_id}/playbook")
async def edit_playbook(
    agent_id: uuid.UUID,
    payload: SynthEditIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent_or_404(db, user, agent_id)
    row = (
        await db.execute(
            select(AIAgentPlaybook).where(AIAgentPlaybook.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = AIAgentPlaybook(ai_agent_id=agent.id, generation_status="ready")
        db.add(row)
    row.content = payload.content
    row.approval_status = "draft"
    await db.commit()
    return _ser_synth(row)


@router.get("/{agent_id}/showing-guide")
async def get_showing_guide(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    row = (
        await db.execute(
            select(AIAgentShowingGuide).where(AIAgentShowingGuide.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    return _ser_synth(row)


@router.post("/{agent_id}/showing-guide/generate")
async def generate_showing_guide(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    row = (
        await db.execute(
            select(AIAgentShowingGuide).where(AIAgentShowingGuide.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = AIAgentShowingGuide(ai_agent_id=agent.id)
        db.add(row)
    row.generation_status = "generating"
    row.generation_error = None
    await db.commit()
    return {"generation_status": "generating"}


@router.post("/{agent_id}/showing-guide/approve")
async def approve_showing_guide(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    row = (
        await db.execute(
            select(AIAgentShowingGuide).where(AIAgentShowingGuide.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if row is None or row.generation_status != "ready":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Showing guide not ready to approve."
        )
    row.approval_status = "approved"
    row.approved_at = _now()
    await db.commit()
    return _ser_synth(row)


# ── Step 8: Follow-ups + Exit + Sample messages ─────────────────────


class ExitRulesIn(BaseModel):
    max_email_attempts: int = 5
    max_no_reply_followups: int = 4
    max_days_in_sequence: int = 14


@router.get("/{agent_id}/exit-rules")
async def get_exit_rules(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    row = (
        await db.execute(
            select(AIAgentExitRules).where(AIAgentExitRules.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if row is None:
        return {}
    return {
        "max_email_attempts": row.max_email_attempts,
        "max_no_reply_followups": row.max_no_reply_followups,
        "max_days_in_sequence": row.max_days_in_sequence,
    }


@router.put("/{agent_id}/exit-rules")
async def put_exit_rules(
    agent_id: uuid.UUID,
    payload: ExitRulesIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent_or_404(db, user, agent_id)
    row = (
        await db.execute(
            select(AIAgentExitRules).where(AIAgentExitRules.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = AIAgentExitRules(ai_agent_id=agent.id)
        db.add(row)
    row.max_email_attempts = max(1, min(50, payload.max_email_attempts))
    row.max_no_reply_followups = max(0, min(50, payload.max_no_reply_followups))
    row.max_days_in_sequence = max(1, min(365, payload.max_days_in_sequence))
    await db.commit()
    return {"ok": True}


# ── Step 9: Test scenarios ──────────────────────────────────────────


class TestRunIn(BaseModel):
    prompt: str


@router.get("/{agent_id}/test-scenarios")
async def list_test_scenarios(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    rows = list(
        (
            await db.execute(
                select(AIAgentTestScenario)
                .where(AIAgentTestScenario.ai_agent_id == agent.id)
                .order_by(AIAgentTestScenario.created_at.desc())
            )
        ).scalars().all()
    )
    return [
        {
            "id": str(r.id),
            "prompt": r.prompt,
            "ai_response": r.ai_response,
            "reviewed": r.reviewed,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/{agent_id}/test")
async def run_test(
    agent_id: uuid.UUID,
    payload: TestRunIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent_or_404(db, user, agent_id)
    system = await svc._system_prompt(db, agent)
    reply = ""
    try:
        from app.services.ai.orchestrator import run as orun

        result = await orun(
            [
                {
                    "role": "user",
                    "content": (
                        "Respond exactly as this AI Agent would to the following "
                        f"lead message:\n\n{payload.prompt}"
                    ),
                }
            ],
            tier="light",
            max_tokens=800,
            system=system,
        )
        reply = svc._text_of(result)
    except Exception as exc:  # noqa: BLE001
        log.warning("test run failed: %s", exc)
        reply = "(AI unavailable — could not generate a test response.)"

    row = AIAgentTestScenario(
        ai_agent_id=agent.id, prompt=payload.prompt[:8000], ai_response=reply
    )
    db.add(row)
    agent.last_tested_at = _now()
    await db.commit()
    return {"id": str(row.id), "ai_response": reply}


@router.post("/{agent_id}/test-scenarios/{scenario_id}/review")
async def review_test_scenario(
    agent_id: uuid.UUID,
    scenario_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent_or_404(db, user, agent_id)
    row = await db.get(AIAgentTestScenario, scenario_id)
    if row is None or row.ai_agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scenario not found.")
    row.reviewed = True
    await db.commit()
    return {"ok": True}


# ── Step 10: gate ───────────────────────────────────────────────────


@router.get("/{agent_id}/gate")
async def get_gate(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    blockers = await svc.evaluate_gate(db, agent)
    return {"clear": len(blockers) == 0, "blockers": blockers}


# ── Step 11: Warm-up + outbox + activation ──────────────────────────


class WarmupSendIn(BaseModel):
    client_id: uuid.UUID | None = None
    touchpoint_key: str = "intro"
    channel: str = "email"


@router.get("/{agent_id}/messages")
async def list_messages(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    rows = list(
        (
            await db.execute(
                select(AIAgentMessage)
                .where(AIAgentMessage.ai_agent_id == agent.id)
                .order_by(AIAgentMessage.created_at.desc())
                .limit(100)
            )
        ).scalars().all()
    )
    return [
        {
            "id": str(m.id),
            "client_id": str(m.client_id) if m.client_id else None,
            "touchpoint_key": m.touchpoint_key,
            "channel": m.channel,
            "subject": m.subject,
            "body": m.body,
            "status": m.status,
            "is_warmup": m.is_warmup,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in rows
    ]


@router.post("/{agent_id}/warmup-send")
async def warmup_send(
    agent_id: uuid.UUID,
    payload: WarmupSendIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    agent = await _agent_or_404(db, user, agent_id)
    client: Client | None = None
    if payload.client_id is not None:
        client = await db.get(Client, payload.client_id)
        if client is None or client.broker_id != agent.broker_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found.")

    composed = await svc.compose_message(
        db, agent, client=client, touchpoint_key=payload.touchpoint_key,
        channel=payload.channel,
    )
    msg = AIAgentMessage(
        ai_agent_id=agent.id,
        client_id=client.id if client else None,
        touchpoint_key=payload.touchpoint_key[:40],
        channel=payload.channel[:16],
        subject=composed["subject"][:300],
        body=composed["body"],
        status="draft",
        is_warmup=True,
    )
    db.add(msg)
    # First warm-up send flips the agent into active warm-up mode.
    if agent.status not in (AIAgentStatus.ACTIVE, AIAgentStatus.ARCHIVED):
        agent.status = AIAgentStatus.ACTIVE
        agent.warmup_mode = True
        agent.activated_at = _now()
    await db.commit()
    return {
        "id": str(msg.id),
        "subject": msg.subject,
        "body": msg.body,
        "warmup_mode": agent.warmup_mode,
    }


@router.post("/{agent_id}/activate")
async def activate_agent(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    """Activate / graduate. Runs the gate; on a clear gate the agent goes
    fully active and warm-up mode is cleared."""
    agent = await _agent_or_404(db, user, agent_id)
    blockers = await svc.evaluate_gate(db, agent)
    if blockers:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"message": "Activation blocked.", "blockers": blockers},
        )
    agent.status = AIAgentStatus.ACTIVE
    agent.warmup_mode = False
    if agent.activated_at is None:
        agent.activated_at = _now()
    await db.commit()
    return _ser_agent(agent)


@router.post("/{agent_id}/pause")
async def pause_agent(
    agent_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    agent = await _agent_or_404(db, user, agent_id)
    agent.status = AIAgentStatus.PAUSED
    await db.commit()
    return _ser_agent(agent)
