"""AI Agents — a broker's configurable AI workers (alembic 0063).

A real-estate agent (the `BROKER` role) builds named **AI Agents**
through an 11-step builder. Each agent is trained conversationally,
given an approved playbook, assigned a slice of the broker's existing
pipeline + clients via targeting rules (never bulk import), tested in
a warm-up playground, and launched.

This module is purely additive — it does not touch the existing
cadence engine, AI Inbox, or per-client `realtor_profile`. A live AI
Agent runs on its own scheduler job (`job_ai_agent_pass`) scoped only
to that agent's `ai_agent_leads`.

Mental model — one `AIAgent` row + one table per builder step:
  Step 1  Basics & Identity   → ai_agents (this row)
  Step 2  Goal                → ai_agent_goals
  Step 3  Knowledge           → ai_agent_knowledge_links (+ reuse
                                 AIKnowledgeDocument)
  Step 4  Targeting           → ai_agent_targeting (+ ai_agent_leads)
  Step 5  Training Studio     → ai_agent_training_sessions / _messages
  Step 6  Playbook            → ai_agent_playbooks
  Step 7  Showing Guide       → ai_agent_showing_guides
  Step 8  Voice & Exit        → ai_agent_exit_rules + ai_voice_profiles (the
                                 named, reusable tonality templates)
  Step 9  Test Scenarios      → ai_agent_test_scenarios
  Steps 10-11 (Launch / Warm-up) are computed from the gate + status.

Statuses + kinds are kept as plain strings (no DB enum) for migration
simplicity — the StrEnums in `app/enums.py` are the source of truth
and routers validate against them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


# Default follow-up cadence — day offsets from enrollment. Mirrors the
# campaign builder's Email-1 / FU-1 / FU-2 / FU-3 / break-up shape.
DEFAULT_CADENCE: list[int] = [0, 2, 5, 8, 12]


class AIAgent(TimestampMixin, Base):
    """Core row — Step 1 Basics & Identity lives here."""

    __tablename__ = "ai_agents"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    broker_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("brokers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    """The broker's user id — mirrors `AIKnowledgeDocument.agent_user_id`
    so knowledge-link queries + S3 namespacing line up."""

    # --- Step 1: Basics ---
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="custom", server_default="custom"
    )
    """AIAgentKind."""
    audience: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Step 1: Identity ---
    ai_display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    """The name the AI introduces itself as. NULL falls back to the
    firm AI identity ("Quinn")."""
    persona_mode: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="virtual_secretary",
        server_default="virtual_secretary",
    )
    """AIAgentPersonaMode — virtual_secretary | agent_persona."""

    # --- Lifecycle + governance ---
    status: Mapped[str] = mapped_column(
        String(28), nullable=False, default="draft", server_default="draft"
    )
    """AIAgentStatus."""
    send_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="draft_first",
        server_default="draft_first",
    )
    """AIAgentSendMode. `auto` requires warm-up complete."""
    warmup_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    """True between the first warm-up send and "Activate AI Agent"."""

    # --- Step 8: cadence (exit rules live in their own table) ---
    max_followups: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4, server_default="4"
    )
    cadence: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, default=lambda: list(DEFAULT_CADENCE),
        server_default="[0, 2, 5, 8, 12]",
    )
    """Default day offsets the engine uses between touchpoints. Not
    user-configurable in the builder — follow-up timing is the AI's
    job. The broker only controls when to STOP (exit rules)."""

    voice_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_voice_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    """Link to a reusable AIVoiceProfile — the broker's tonality
    baseline (how they greet, how they ask for late items, etc.).
    One profile can be shared across many AI agents."""

    last_tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AIAgentGoal(TimestampMixin, Base):
    """Step 2 — what the AI is trying to achieve + what's out of bounds.
    One row per agent."""

    __tablename__ = "ai_agent_goals"
    __table_args__ = (
        UniqueConstraint("ai_agent_id", name="uq_ai_agent_goal_agent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ai_agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Required
    primary_goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_cta: Mapped[str | None] = mapped_column(Text, nullable=True)
    handoff_triggers: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    """When the AI hands the conversation back to the human broker."""

    # Recommended extras
    success_definition: Mapped[str | None] = mapped_column(Text, nullable=True)
    qualified_reply_definition: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_reply_boundaries: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )


class AIAgentKnowledgeLink(TimestampMixin, Base):
    """Step 3 — attaches an existing account-owned `AIKnowledgeDocument`
    to an AI Agent. A doc can link to many agents (reuse without
    re-uploading)."""

    __tablename__ = "ai_agent_knowledge_links"
    __table_args__ = (
        UniqueConstraint(
            "ai_agent_id", "knowledge_document_id",
            name="uq_ai_agent_knowledge_link",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ai_agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    knowledge_document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attach_to_emails: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    """When true the AI may attach this file to outbound emails (it
    decides per email)."""


class AIAgentTargeting(TimestampMixin, Base):
    """Step 4 — the internal targeting engine. Not an importer: it
    continuously finds the right people from work already in QC. One
    row per agent."""

    __tablename__ = "ai_agent_targeting"
    __table_args__ = (
        UniqueConstraint("ai_agent_id", name="uq_ai_agent_targeting_agent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ai_agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    domain: Mapped[str] = mapped_column(
        String(16), nullable=False, default="clients", server_default="clients"
    )
    """AIAgentDomain — pipeline | clients | both."""
    include_rules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    """Predicate over deals/clients: stage, deal_type, lead_temperature,
    days_since_close, has_active_file, language, tags…"""
    exclude_rules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    """Overlap guards: active_listing, in_loan_process,
    owned_by_other_ai_agent, recently_contacted_by_human."""
    enrollment_mode: Mapped[str] = mapped_column(
        String(12), nullable=False, default="review", server_default="review"
    )
    """auto = enroll matches straight to active; review = land them in
    pending_review for the broker to confirm."""
    last_targeting_pass_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AIAgentLead(TimestampMixin, Base):
    """Step 4 enrollment join — one enrolled contact within an agent's
    roster. Populated by the targeting pass, never by import."""

    __tablename__ = "ai_agent_leads"
    __table_args__ = (
        UniqueConstraint(
            "ai_agent_id", "client_id", name="uq_ai_agent_lead_client"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ai_agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("deals.id", ondelete="SET NULL"),
        nullable=True,
    )
    """The pipeline file this enrollment is about (pipeline-domain
    agents). NULL for client-domain enrollments."""

    status: Mapped[str] = mapped_column(
        String(20), nullable=False,
        default="pending_review", server_default="pending_review",
    )
    """AIAgentLeadStatus."""
    next_action_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts_made: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_outbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AIAgentTrainingSession(TimestampMixin, Base):
    """Step 5 — one conversational training run between the broker and
    the AI 'real-estate coach'."""

    __tablename__ = "ai_agent_training_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ai_agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Set when the broker marks the training conversation done — the
    activation gate reads this."""


class AIAgentTrainingMessage(TimestampMixin, Base):
    """Step 5 — one turn in a training session transcript."""

    __tablename__ = "ai_agent_training_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_agent_training_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(12), nullable=False)
    """user (the broker) | assistant (the AI coach)."""
    content: Mapped[str] = mapped_column(Text, nullable=False)


class AIAgentPlaybook(TimestampMixin, Base):
    """Step 6 — heavy-AI synthesis of training + knowledge into a
    structured playbook. Must be approved. One row per agent."""

    __tablename__ = "ai_agent_playbooks"
    __table_args__ = (
        UniqueConstraint("ai_agent_id", name="uq_ai_agent_playbook_agent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ai_agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    """thesis, persona, target_pains, value_prop, primary_hook,
    primary_cta, objection_map, allowed_claims, prohibited_claims,
    handoff_rules, exit_rules, ai_operating_instructions."""
    generation_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="idle", server_default="idle"
    )
    """idle | generating | ready | failed — drained by
    job_ai_agent_synthesis_drain."""
    generation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="draft", server_default="draft"
    )
    """draft | approved."""
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


class AIAgentShowingGuide(TimestampMixin, Base):
    """Step 7 — heavy-AI showing & discovery playbook. Must be
    approved. One row per agent."""

    __tablename__ = "ai_agent_showing_guides"
    __table_args__ = (
        UniqueConstraint("ai_agent_id", name="uq_ai_agent_showing_guide_agent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ai_agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    """goal, pre_showing_confirmation_template, agenda,
    discovery_questions, qualification_questions,
    post_showing_followup_template, next_step_checklist,
    handoff_summary_template."""
    generation_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="idle", server_default="idle"
    )
    generation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="draft", server_default="draft"
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AIAgentExitRules(TimestampMixin, Base):
    """Step 8 — when the AI Agent gives up on a lead. One row per agent."""

    __tablename__ = "ai_agent_exit_rules"
    __table_args__ = (
        UniqueConstraint("ai_agent_id", name="uq_ai_agent_exit_rules_agent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ai_agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    max_email_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    max_no_reply_followups: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4, server_default="4"
    )
    max_days_in_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=14, server_default="14"
    )


class AIVoiceProfile(TimestampMixin, Base):
    """A reusable, named tonality baseline.

    The broker writes a handful of templates (greeting, asking for a
    late item, communicating "under contract", etc.) once — and then
    attaches the same profile to any AI Agent. The composer injects
    them as a "voice & tone" block in the system prompt so every
    outbound message matches the broker's style.

    Stored at broker scope (not per AI Agent) so it can be linked to
    many agents and renamed/reused freely."""

    __tablename__ = "ai_voice_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    broker_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("brokers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    """Display name — e.g. "My friendly buyer voice"."""

    templates: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    """{situation_key: body} — the situation_key is from the small
    catalog the UI exposes (greeting | due_soon | late_item |
    under_contract | check_in). Empty/missing keys are simply omitted
    from the prompt."""


class AIAgentTestScenario(TimestampMixin, Base):
    """Step 9 — a prompt run against the AI in the test studio."""

    __tablename__ = "ai_agent_test_scenarios"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ai_agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    ai_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class AIAgentMessage(TimestampMixin, Base):
    """The AI Agent's outbox — every message the agent composes, whether
    drafted (Step 11 / live draft-first), warm-up, or auto-sent.

    Serves three jobs at once: the draft-first review queue, the warm-up
    playground transcript, and the sent-message audit trail. Self-
    contained — `AITask` is loan-scoped and `AIOutreachEvent` requires a
    task assignment, so neither fits a client-domain AI Agent."""

    __tablename__ = "ai_agent_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ai_agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_agent_leads.id", ondelete="SET NULL"),
        nullable=True,
    )
    """The enrolled contact this message is for. NULL for a warm-up
    test send that isn't tied to a real enrollment."""
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="SET NULL"),
        nullable=True,
    )

    touchpoint_key: Mapped[str] = mapped_column(
        String(40), nullable=False, default="intro", server_default="intro"
    )
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, default="email", server_default="email"
    )
    subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft"
    )
    """draft | approved | sent | dismissed | failed."""
    is_warmup: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
