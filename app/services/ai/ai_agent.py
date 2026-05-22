"""AI Agents — service layer.

Powers the broker's 11-step AI Agent builder + the live engine:

  * `step_states()`      — per-step missing/attention/done for the UI rail
  * `evaluate_gate()`    — the activation-gate blocker list
  * `generate_playbook()` / `generate_showing_guide()` — heavy Sonnet
                            synthesis, drained by job_ai_agent_synthesis_drain
  * `compose_message()`  — the per-pass / warm-up message composer (Haiku)
  * `run_targeting()`    — evaluates targeting rules → enrolls/retires leads
  * `classify_knowledge_document()` — Haiku knowledge classifier

Everything here is best-effort: AI calls that fail fall back to safe
templated behavior and never raise into the request path. This module
never touches the existing cadence engine / AI Inbox.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AIAgentStatus, ClientStage
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
from app.models.client import Client

log = logging.getLogger(__name__)


# ── small helpers ───────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text_of(result: dict[str, Any]) -> str:
    """Pull the concatenated text out of an orchestrator.run() result."""
    out: list[str] = []
    for block in result.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text", ""))
    return "".join(out).strip()


def _extract_json(raw: str) -> dict[str, Any]:
    """Best-effort JSON parse — tolerates ```json fences + prose."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        s = s[start : end + 1]
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def touchpoint_for_attempt(agent: AIAgent, attempt_index: int) -> str:
    """Map a 0-based attempt index onto a touchpoint key using the
    agent's cadence length."""
    cadence = agent.cadence or [0]
    last = len(cadence) - 1
    if attempt_index <= 0:
        return "intro"
    if attempt_index >= last:
        return "breakup"
    return f"followup_{attempt_index}"


# ── knowledge ───────────────────────────────────────────────────────


async def _agent_knowledge(db: AsyncSession, agent_id: UUID) -> list[AIKnowledgeDocument]:
    """The knowledge documents linked to this AI Agent (ready ones)."""
    rows = (
        await db.execute(
            select(AIKnowledgeDocument)
            .join(
                AIAgentKnowledgeLink,
                AIAgentKnowledgeLink.knowledge_document_id == AIKnowledgeDocument.id,
            )
            .where(AIAgentKnowledgeLink.ai_agent_id == agent_id)
            .where(AIKnowledgeDocument.deleted_at.is_(None))
        )
    ).scalars().all()
    return list(rows)


def _knowledge_block(docs: list[AIKnowledgeDocument]) -> str:
    """Compact knowledge summary block for prompt injection."""
    parts: list[str] = []
    for d in docs:
        if d.summary:
            parts.append(f"- {d.filename} ({d.doc_type or 'document'}): {d.summary}")
        elif d.parsed_text:
            parts.append(f"- {d.filename}: {d.parsed_text[:600]}")
    if not parts:
        return ""
    return "KNOWLEDGE BASE:\n" + "\n".join(parts)


async def classify_knowledge_document(
    db: AsyncSession, doc: AIKnowledgeDocument
) -> None:
    """Haiku classifier — fills doc_type / key_facts / summary. Called on
    the parse-complete path. Never raises."""
    if not doc.parsed_text:
        return
    try:
        from app.services.ai.orchestrator import run

        result = await run(
            [
                {
                    "role": "user",
                    "content": (
                        "Classify this document for a real-estate agent's AI "
                        "knowledge base. Return ONLY JSON: "
                        '{"doc_type": "product_overview|faq|pricing|case_study|'
                        'bio|market_data|other", "summary": "one paragraph", '
                        '"key_facts": ["fact", ...]}\n\n'
                        f"FILENAME: {doc.filename}\n\n"
                        f"CONTENT:\n{doc.parsed_text[:12000]}"
                    ),
                }
            ],
            tier="light",
            max_tokens=700,
            system="You classify documents. Output strict JSON only.",
        )
        data = _extract_json(_text_of(result))
        if data:
            doc.doc_type = str(data.get("doc_type") or "other")[:60]
            doc.summary = (data.get("summary") or None)
            kf = data.get("key_facts")
            doc.key_facts = kf if isinstance(kf, list) else None
            await db.flush()
    except Exception as exc:  # noqa: BLE001
        log.warning("knowledge classify failed for %s: %s", doc.id, exc)


# ── step states + activation gate ───────────────────────────────────


async def step_states(db: AsyncSession, agent: AIAgent) -> dict[str, str]:
    """missing | attention | done for each of the 11 builder steps."""
    aid = agent.id

    async def _scalar(stmt) -> Any:
        return (await db.execute(stmt)).scalar_one_or_none()

    async def _count(model) -> int:
        return int(
            (
                await db.execute(
                    select(func.count()).select_from(model).where(model.ai_agent_id == aid)
                )
            ).scalar()
            or 0
        )

    goal: AIAgentGoal | None = await _scalar(
        select(AIAgentGoal).where(AIAgentGoal.ai_agent_id == aid)
    )
    targeting: AIAgentTargeting | None = await _scalar(
        select(AIAgentTargeting).where(AIAgentTargeting.ai_agent_id == aid)
    )
    playbook: AIAgentPlaybook | None = await _scalar(
        select(AIAgentPlaybook).where(AIAgentPlaybook.ai_agent_id == aid)
    )
    guide: AIAgentShowingGuide | None = await _scalar(
        select(AIAgentShowingGuide).where(AIAgentShowingGuide.ai_agent_id == aid)
    )
    exit_rules: AIAgentExitRules | None = await _scalar(
        select(AIAgentExitRules).where(AIAgentExitRules.ai_agent_id == aid)
    )
    training_done = await _scalar(
        select(AIAgentTrainingSession.id)
        .where(AIAgentTrainingSession.ai_agent_id == aid)
        .where(AIAgentTrainingSession.completed_at.is_not(None))
    )
    knowledge_n = await _count(AIAgentKnowledgeLink)
    test_n = int(
        (
            await db.execute(
                select(func.count())
                .select_from(AIAgentTestScenario)
                .where(AIAgentTestScenario.ai_agent_id == aid)
                .where(AIAgentTestScenario.reviewed.is_(True))
            )
        ).scalar()
        or 0
    )
    states: dict[str, str] = {}
    states["basics"] = "done" if (agent.name and agent.kind) else "missing"
    states["goal"] = (
        "done"
        if goal and goal.primary_goal and goal.primary_cta
        else ("attention" if goal else "missing")
    )
    states["knowledge"] = "done" if knowledge_n > 0 else "missing"
    states["targeting"] = (
        "done" if targeting and targeting.include_rules else "missing"
    )
    # Step 5 (Training Studio) now embeds playbook + showing-guide
    # generation inline — it's "done" only when the chat is complete
    # AND both synthesized artifacts are approved.
    playbook_ok = bool(playbook and playbook.approval_status == "approved")
    guide_ok = bool(guide and guide.approval_status == "approved")
    if training_done and playbook_ok and guide_ok:
        states["training"] = "done"
    elif training_done or playbook or guide:
        states["training"] = "attention"
    else:
        states["training"] = "missing"
    # Kept for the activation gate so blockers stay specific.
    states["playbook"] = (
        "done" if playbook_ok else ("attention" if playbook else "missing")
    )
    states["showing_guide"] = (
        "done" if guide_ok else ("attention" if guide else "missing")
    )
    # Step 8 is done when the broker has set exit rules AND linked a
    # voice profile. The follow-up cadence itself is the AI's job.
    has_voice = agent.voice_profile_id is not None
    states["followups"] = (
        "done"
        if exit_rules and has_voice
        else ("attention" if exit_rules or has_voice else "missing")
    )
    states["test"] = "done" if test_n > 0 else "attention"  # optional
    states["launch"] = (
        "done" if agent.status in (AIAgentStatus.READY_TO_ACTIVATE, AIAgentStatus.ACTIVE) else "missing"
    )
    states["warmup"] = "done" if agent.status == AIAgentStatus.ACTIVE else "missing"
    return states


async def evaluate_gate(db: AsyncSession, agent: AIAgent) -> list[str]:
    """The activation-gate blocker list — empty list == clear to launch."""
    states = await step_states(db, agent)
    blockers: list[str] = []

    if states["basics"] != "done":
        blockers.append("Add a name and workflow type (Step 1).")
    if states["goal"] != "done":
        blockers.append("Define the primary goal and CTA (Step 2).")
    if states["knowledge"] != "done":
        blockers.append("Upload or attach at least one knowledge file (Step 3).")
    if states["targeting"] != "done":
        blockers.append("Configure targeting rules (Step 4).")
    if states["training"] != "done":
        blockers.append("Complete a training session (Step 5).")
    if states["playbook"] != "done":
        blockers.append("Generate and approve the playbook (Step 6).")
    if states["showing_guide"] != "done":
        blockers.append("Generate and approve the showing guide (Step 7).")
    if states["followups"] != "done":
        blockers.append("Set exit rules and add sample messages (Step 8).")
    if states["test"] != "done":
        blockers.append("Review at least one test scenario (Step 9).")
    if agent.status == AIAgentStatus.ARCHIVED:
        blockers.append("This AI Agent is archived.")
    # Sending identity — only gates auto-send agents.
    if agent.send_mode == "auto":
        from app.config import get_settings

        cfg = get_settings()
        if not getattr(cfg, "ses_from_address", None):
            blockers.append(
                "Auto-send needs a verified sending identity — "
                "switch to draft-first or configure email sending."
            )
    return blockers


# ── targeting ───────────────────────────────────────────────────────


async def _matching_clients(
    db: AsyncSession, agent: AIAgent, targeting: AIAgentTargeting
) -> list[Client]:
    """Evaluate the include/exclude rules against the broker's clients.

    v1 supports the rule keys the builder exposes; unknown keys are
    ignored so the engine is forward-compatible with richer rules."""
    inc = targeting.include_rules or {}
    exc = targeting.exclude_rules or {}

    stmt = select(Client).where(Client.broker_id == agent.broker_id)

    stages = inc.get("stages")
    if isinstance(stages, list) and stages:
        stmt = stmt.where(Client.stage.in_(stages))

    temps = inc.get("lead_temperatures")
    if isinstance(temps, list) and temps:
        stmt = stmt.where(Client.lead_temperature.in_(temps))

    sources = inc.get("lead_sources")
    if isinstance(sources, list) and sources:
        stmt = stmt.where(Client.lead_source.in_(sources))

    ctypes = inc.get("client_types")
    if isinstance(ctypes, list) and ctypes:
        stmt = stmt.where(Client.client_type.in_(ctypes))

    # never-closed / closed-long-ago targeting
    if inc.get("never_closed") is True:
        stmt = stmt.where((Client.funded_count == 0) | (Client.funded_count.is_(None)))

    # Exclude: skip clients actively in the loan process.
    if exc.get("skip_in_loan_process") is True:
        stmt = stmt.where(
            Client.stage.notin_([ClientStage.PROCESSING, ClientStage.READY_FOR_LENDING])
        )
    # Always skip terminal "lost" clients.
    stmt = stmt.where(Client.stage != ClientStage.LOST)

    rows = list((await db.execute(stmt)).scalars().all())

    # Exclude: clients already worked by another AI Agent of this broker.
    if exc.get("skip_owned_by_other_ai_agent", True):
        owned = (
            await db.execute(
                select(AIAgentLead.client_id)
                .join(AIAgent, AIAgent.id == AIAgentLead.ai_agent_id)
                .where(AIAgent.broker_id == agent.broker_id)
                .where(AIAgentLead.ai_agent_id != agent.id)
                .where(AIAgentLead.status.in_(["pending_review", "active", "replied"]))
            )
        ).scalars().all()
        owned_set = set(owned)
        rows = [c for c in rows if c.id not in owned_set]

    # Only contactable clients (need an email for v1 email outreach).
    rows = [c for c in rows if (c.email or "").strip()]
    return rows


async def preview_targeting(
    db: AsyncSession, agent: AIAgent
) -> dict[str, Any]:
    """Count + sample of who matches the current targeting rules."""
    targeting = (
        await db.execute(
            select(AIAgentTargeting).where(AIAgentTargeting.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if targeting is None or not targeting.include_rules:
        return {"count": 0, "sample": []}
    clients = await _matching_clients(db, agent, targeting)
    return {
        "count": len(clients),
        "sample": [
            {"id": str(c.id), "name": c.name, "stage": str(c.stage)}
            for c in clients[:12]
        ],
    }


async def run_targeting(db: AsyncSession, agent: AIAgent) -> dict[str, int]:
    """Enroll new matches into ai_agent_leads; retire rows that no longer
    match. Returns {enrolled, retired}."""
    targeting = (
        await db.execute(
            select(AIAgentTargeting).where(AIAgentTargeting.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if targeting is None or not targeting.include_rules:
        return {"enrolled": 0, "retired": 0}

    clients = await _matching_clients(db, agent, targeting)
    match_ids = {c.id for c in clients}

    existing = list(
        (
            await db.execute(
                select(AIAgentLead).where(AIAgentLead.ai_agent_id == agent.id)
            )
        ).scalars().all()
    )
    existing_by_client = {l.client_id: l for l in existing}

    enroll_status = "active" if targeting.enrollment_mode == "auto" else "pending_review"
    enrolled = 0
    for c in clients:
        if c.id in existing_by_client:
            continue
        db.add(
            AIAgentLead(
                ai_agent_id=agent.id,
                client_id=c.id,
                status=enroll_status,
                next_action_at=_now(),
            )
        )
        enrolled += 1

    retired = 0
    for lead in existing:
        if lead.client_id not in match_ids and lead.status in (
            "pending_review",
            "active",
        ):
            lead.status = "exited"
            retired += 1

    targeting.last_targeting_pass_at = _now()
    await db.flush()
    return {"enrolled": enrolled, "retired": retired}


# ── prompt assembly + message composer ──────────────────────────────


async def _system_prompt(db: AsyncSession, agent: AIAgent) -> str:
    """Compose the AI Agent's system prompt: firm identity + agent
    identity + approved playbook + knowledge + sample messages."""
    parts: list[str] = []

    # Firm identity baseline.
    try:
        from app.services.ai.firm_identity import (
            load_firm_identity,
            render_identity_prefix,
        )

        identity = await load_firm_identity(db)
        prefix = render_identity_prefix(identity)
        if prefix:
            parts.append(prefix.strip())
    except Exception:  # noqa: BLE001
        pass

    # AI Agent identity.
    display = agent.ai_display_name or "your assistant"
    if agent.persona_mode == "agent_persona":
        parts.append(
            f"You write as the real-estate agent themselves. Sign as the agent."
        )
    else:
        parts.append(
            f'You are "{display}", a virtual secretary working on behalf of a '
            f"real-estate agent. Introduce yourself as such on the first message."
        )

    # Goal.
    goal = (
        await db.execute(select(AIAgentGoal).where(AIAgentGoal.ai_agent_id == agent.id))
    ).scalar_one_or_none()
    if goal:
        g = []
        if goal.primary_goal:
            g.append(f"PRIMARY GOAL: {goal.primary_goal}")
        if goal.primary_cta:
            g.append(f"PRIMARY CALL TO ACTION: {goal.primary_cta}")
        if goal.handoff_triggers:
            g.append(f"HAND OFF TO THE HUMAN AGENT WHEN: {goal.handoff_triggers}")
        if g:
            parts.append("\n".join(g))

    # Approved playbook.
    playbook = (
        await db.execute(
            select(AIAgentPlaybook).where(AIAgentPlaybook.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if playbook and playbook.approval_status == "approved" and playbook.content:
        parts.append("PLAYBOOK:\n" + json.dumps(playbook.content, indent=2)[:6000])

    # Knowledge.
    docs = await _agent_knowledge(db, agent.id)
    kb = _knowledge_block(docs)
    if kb:
        parts.append(kb)

    # Voice & tone — the broker's reusable tonality baseline.
    if agent.voice_profile_id is not None:
        vp = await db.get(AIVoiceProfile, agent.voice_profile_id)
        if vp is not None and isinstance(vp.templates, dict):
            lines = []
            for key, body in vp.templates.items():
                if not body:
                    continue
                label = key.replace("_", " ").upper()
                lines.append(f"[{label}]\n{body}")
            if lines:
                parts.append(
                    "VOICE & TONE — match how this broker actually writes "
                    "to clients (style references, not literal scripts):\n"
                    + "\n\n".join(lines)
                )

    parts.append(
        "Write concise, warm, professional outreach. Never invent facts, "
        "rates, or promises. Output JSON only when asked."
    )
    return "\n\n".join(parts)


async def training_system_prompt(db: AsyncSession, agent: AIAgent) -> str:
    """The Training Studio coach prompt — context-aware. Loads everything
    the broker has already configured (goal, knowledge summaries,
    targeting rules) and instructs the coach to challenge the broker
    with 5-10 targeted questions, filling gaps rather than restating
    the context back."""
    sections: list[str] = []

    sections.append(
        f"You are a senior real-estate sales consultant interviewing the "
        f"broker about the AI worker they're building, called \"{agent.name}\" "
        f"({agent.kind.replace('_', ' ')})."
    )

    if agent.audience:
        sections.append(f"AUDIENCE: {agent.audience}")

    goal = (
        await db.execute(select(AIAgentGoal).where(AIAgentGoal.ai_agent_id == agent.id))
    ).scalar_one_or_none()
    if goal:
        g: list[str] = []
        if goal.primary_goal:
            g.append(f"GOAL: {goal.primary_goal}")
        if goal.primary_cta:
            g.append(f"PRIMARY CTA: {goal.primary_cta}")
        if goal.handoff_triggers:
            g.append(f"HAND OFF WHEN: {goal.handoff_triggers}")
        if g:
            sections.append("\n".join(g))

    targeting = (
        await db.execute(
            select(AIAgentTargeting).where(AIAgentTargeting.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if targeting and (targeting.include_rules or targeting.exclude_rules):
        sections.append(
            "TARGETING — "
            f"domain={targeting.domain} include={targeting.include_rules} "
            f"exclude={targeting.exclude_rules}"
        )

    docs = await _agent_knowledge(db, agent.id)
    kb = _knowledge_block(docs)
    if kb:
        sections.append(kb[:6000])

    sections.append(
        "Your job: CHALLENGE the broker with 5–10 targeted, specific "
        "questions that fill the gaps in the context above (offer details, "
        "tone, objection handling, what a great vs a bad lead looks like, "
        "edge cases). Ask ONE question at a time. Be warm but pointed — "
        "don't restate what they already gave you. When you have enough "
        "to write a strong playbook, tell them they can finish the "
        "training."
    )
    return "\n\n".join(sections)


async def compose_message(
    db: AsyncSession,
    agent: AIAgent,
    *,
    client: Client | None,
    touchpoint_key: str,
    channel: str = "email",
    extra_context: str = "",
) -> dict[str, str]:
    """Compose one outbound message. Returns {subject, body}. Falls back
    to a simple templated message if the AI call fails."""
    system = await _system_prompt(db, agent)
    who = client.name if client else "the recipient"
    ctx = [f"RECIPIENT: {who}", f"TOUCHPOINT: {touchpoint_key}", f"CHANNEL: {channel}"]
    if client and client.stage:
        ctx.append(f"PIPELINE STAGE: {client.stage}")
    if client and client.living_summary:
        ctx.append(f"FILE SUMMARY: {client.living_summary}")
    if extra_context:
        ctx.append(extra_context)

    try:
        from app.services.ai.orchestrator import run

        result = await run(
            [
                {
                    "role": "user",
                    "content": (
                        "Compose this outreach message. Return ONLY JSON: "
                        '{"subject": "...", "body": "..."}\n\n' + "\n".join(ctx)
                    ),
                }
            ],
            tier="light",
            max_tokens=900,
            system=system,
        )
        data = _extract_json(_text_of(result))
        body = (data.get("body") or "").strip()
        subject = (data.get("subject") or "").strip()
        if body:
            return {"subject": subject or f"A quick note for {who}", "body": body}
    except Exception as exc:  # noqa: BLE001
        log.warning("compose_message failed for agent %s: %s", agent.id, exc)

    # Fallback template.
    return {
        "subject": f"Following up — {agent.name}",
        "body": (
            f"Hi {who},\n\nJust checking in. Let me know if there's anything "
            f"I can help with.\n\nBest,\n{agent.ai_display_name or 'Your assistant'}"
        ),
    }


# ── heavy synthesis: playbook + showing guide ───────────────────────


async def _training_transcript(db: AsyncSession, agent_id: UUID) -> str:
    sessions = list(
        (
            await db.execute(
                select(AIAgentTrainingSession.id).where(
                    AIAgentTrainingSession.ai_agent_id == agent_id
                )
            )
        ).scalars().all()
    )
    if not sessions:
        return ""
    msgs = list(
        (
            await db.execute(
                select(AIAgentTrainingMessage)
                .where(AIAgentTrainingMessage.session_id.in_(sessions))
                .order_by(AIAgentTrainingMessage.created_at)
            )
        ).scalars().all()
    )
    return "\n".join(f"{m.role.upper()}: {m.content}" for m in msgs)


async def generate_playbook(db: AsyncSession, agent: AIAgent) -> None:
    """Heavy Sonnet synthesis of training + knowledge → structured
    playbook JSONB. Drained by job_ai_agent_synthesis_drain. Never
    raises — failure flips generation_status='failed'."""
    playbook = (
        await db.execute(
            select(AIAgentPlaybook).where(AIAgentPlaybook.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if playbook is None:
        playbook = AIAgentPlaybook(ai_agent_id=agent.id)
        db.add(playbook)

    transcript = await _training_transcript(db, agent.id)
    docs = await _agent_knowledge(db, agent.id)
    goal = (
        await db.execute(select(AIAgentGoal).where(AIAgentGoal.ai_agent_id == agent.id))
    ).scalar_one_or_none()

    try:
        from app.services.ai.orchestrator import run

        prompt = (
            "Synthesize a structured outreach PLAYBOOK for a real-estate "
            "agent's AI worker. Return ONLY JSON with these keys: thesis, "
            "persona, target_pains, value_prop, primary_hook, primary_cta, "
            "objection_map, allowed_claims, prohibited_claims, handoff_rules, "
            "exit_rules, ai_operating_instructions.\n\n"
            f"AGENT WORKFLOW: {agent.kind} — {agent.name}\n"
            f"AUDIENCE: {agent.audience or 'n/a'}\n"
        )
        if goal:
            prompt += (
                f"GOAL: {goal.primary_goal}\nCTA: {goal.primary_cta}\n"
                f"HANDOFF: {goal.handoff_triggers}\n"
            )
        prompt += f"\nTRAINING TRANSCRIPT:\n{transcript[:14000]}\n"
        prompt += "\n" + _knowledge_block(docs)[:6000]

        result = await run(
            [{"role": "user", "content": prompt}],
            tier="heavy",
            max_tokens=3000,
            system=(
                "You are a senior real-estate sales strategist. Produce a "
                "precise, actionable playbook as strict JSON. No prose."
            ),
        )
        data = _extract_json(_text_of(result))
        if not data:
            raise ValueError("empty playbook synthesis")
        playbook.content = data
        playbook.generation_status = "ready"
        playbook.generation_error = None
        playbook.approval_status = "draft"
    except Exception as exc:  # noqa: BLE001
        log.warning("generate_playbook failed for agent %s: %s", agent.id, exc)
        playbook.generation_status = "failed"
        playbook.generation_error = str(exc)[:500]
    await db.flush()


async def generate_showing_guide(db: AsyncSession, agent: AIAgent) -> None:
    """Heavy Sonnet synthesis → showing & discovery guide JSONB."""
    guide = (
        await db.execute(
            select(AIAgentShowingGuide).where(AIAgentShowingGuide.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if guide is None:
        guide = AIAgentShowingGuide(ai_agent_id=agent.id)
        db.add(guide)

    transcript = await _training_transcript(db, agent.id)
    playbook = (
        await db.execute(
            select(AIAgentPlaybook).where(AIAgentPlaybook.ai_agent_id == agent.id)
        )
    ).scalar_one_or_none()

    try:
        from app.services.ai.orchestrator import run

        prompt = (
            "Produce a SHOWING & DISCOVERY GUIDE for a real-estate agent's "
            "AI worker. Return ONLY JSON with these keys: goal, "
            "pre_showing_confirmation_template, agenda, discovery_questions, "
            "qualification_questions, post_showing_followup_template, "
            "next_step_checklist, handoff_summary_template.\n\n"
            f"AGENT WORKFLOW: {agent.kind} — {agent.name}\n"
            f"TRAINING TRANSCRIPT:\n{transcript[:10000]}\n"
        )
        if playbook and playbook.content:
            prompt += "\nPLAYBOOK:\n" + json.dumps(playbook.content)[:4000]

        result = await run(
            [{"role": "user", "content": prompt}],
            tier="heavy",
            max_tokens=2600,
            system=(
                "You are a senior real-estate agent coach. Produce a precise "
                "discovery + showing playbook as strict JSON. No prose."
            ),
        )
        data = _extract_json(_text_of(result))
        if not data:
            raise ValueError("empty showing-guide synthesis")
        guide.content = data
        guide.generation_status = "ready"
        guide.generation_error = None
        guide.approval_status = "draft"
    except Exception as exc:  # noqa: BLE001
        log.warning("generate_showing_guide failed for agent %s: %s", agent.id, exc)
        guide.generation_status = "failed"
        guide.generation_error = str(exc)[:500]
    await db.flush()
