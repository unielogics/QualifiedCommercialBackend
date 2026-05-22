"""AI Agents — the broker's configurable AI workers.

Creates the 11-step builder's tables: one `ai_agents` row plus a table
per step (goal, knowledge links, targeting + leads, training sessions/
messages, playbook, showing guide, exit rules, sample messages, test
scenarios). Also adds the Haiku-classifier columns to the existing
`ai_knowledge_documents` table (additive, all nullable — account-wide
knowledge predating the classifier is unaffected).

Purely additive: no existing table is altered destructively, no other
account type is touched.

Revision ID: 0063
Revises: 0062
Create Date: 2026-05-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def _ts() -> list[sa.Column]:
    """The standard created_at / updated_at pair."""
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    # ── ai_agents — Step 1 Basics & Identity ────────────────────────
    op.create_table(
        "ai_agents",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_ts(),
        sa.Column(
            "broker_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("brokers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=32), server_default="custom", nullable=False),
        sa.Column("audience", sa.Text(), nullable=True),
        sa.Column("ai_display_name", sa.String(length=120), nullable=True),
        sa.Column(
            "persona_mode",
            sa.String(length=24),
            server_default="virtual_secretary",
            nullable=False,
        ),
        sa.Column("status", sa.String(length=28), server_default="draft", nullable=False),
        sa.Column(
            "send_mode", sa.String(length=16), server_default="draft_first", nullable=False
        ),
        sa.Column("warmup_mode", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("max_followups", sa.Integer(), server_default="4", nullable=False),
        sa.Column(
            "cadence", pg.JSONB(), server_default="[0, 2, 5, 8, 12]", nullable=False
        ),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ai_agents_broker_id", "ai_agents", ["broker_id"])
    op.create_index("ix_ai_agents_owner_user_id", "ai_agents", ["owner_user_id"])
    op.create_index("ix_ai_agents_status", "ai_agents", ["status"])

    # ── ai_agent_goals — Step 2 ─────────────────────────────────────
    op.create_table(
        "ai_agent_goals",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_ts(),
        sa.Column(
            "ai_agent_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ai_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("primary_goal", sa.Text(), nullable=True),
        sa.Column("primary_cta", sa.Text(), nullable=True),
        sa.Column("handoff_triggers", pg.JSONB(), server_default="[]", nullable=False),
        sa.Column("success_definition", sa.Text(), nullable=True),
        sa.Column("qualified_reply_definition", sa.Text(), nullable=True),
        sa.Column("auto_reply_boundaries", pg.JSONB(), server_default="{}", nullable=False),
        sa.UniqueConstraint("ai_agent_id", name="uq_ai_agent_goal_agent"),
    )
    op.create_index("ix_ai_agent_goals_agent", "ai_agent_goals", ["ai_agent_id"])

    # ── ai_agent_knowledge_links — Step 3 ───────────────────────────
    op.create_table(
        "ai_agent_knowledge_links",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_ts(),
        sa.Column(
            "ai_agent_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ai_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "knowledge_document_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ai_knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attach_to_emails", sa.Boolean(), server_default="false", nullable=False
        ),
        sa.UniqueConstraint(
            "ai_agent_id", "knowledge_document_id", name="uq_ai_agent_knowledge_link"
        ),
    )
    op.create_index(
        "ix_ai_agent_knowledge_links_agent", "ai_agent_knowledge_links", ["ai_agent_id"]
    )
    op.create_index(
        "ix_ai_agent_knowledge_links_doc",
        "ai_agent_knowledge_links",
        ["knowledge_document_id"],
    )

    # ── ai_agent_targeting — Step 4 ─────────────────────────────────
    op.create_table(
        "ai_agent_targeting",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_ts(),
        sa.Column(
            "ai_agent_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ai_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("domain", sa.String(length=16), server_default="clients", nullable=False),
        sa.Column("include_rules", pg.JSONB(), server_default="{}", nullable=False),
        sa.Column("exclude_rules", pg.JSONB(), server_default="{}", nullable=False),
        sa.Column(
            "enrollment_mode", sa.String(length=12), server_default="review", nullable=False
        ),
        sa.Column("last_targeting_pass_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("ai_agent_id", name="uq_ai_agent_targeting_agent"),
    )
    op.create_index("ix_ai_agent_targeting_agent", "ai_agent_targeting", ["ai_agent_id"])

    # ── ai_agent_leads — Step 4 enrollment join ─────────────────────
    op.create_table(
        "ai_agent_leads",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_ts(),
        sa.Column(
            "ai_agent_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ai_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "client_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "deal_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("deals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="pending_review",
            nullable=False,
        ),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts_made", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "enrolled_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("ai_agent_id", "client_id", name="uq_ai_agent_lead_client"),
    )
    op.create_index("ix_ai_agent_leads_agent", "ai_agent_leads", ["ai_agent_id"])
    op.create_index("ix_ai_agent_leads_client", "ai_agent_leads", ["client_id"])

    # ── ai_agent_training_sessions / _messages — Step 5 ─────────────
    op.create_table(
        "ai_agent_training_sessions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_ts(),
        sa.Column(
            "ai_agent_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ai_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_ai_agent_training_sessions_agent",
        "ai_agent_training_sessions",
        ["ai_agent_id"],
    )
    op.create_table(
        "ai_agent_training_messages",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_ts(),
        sa.Column(
            "session_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ai_agent_training_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=12), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_ai_agent_training_messages_session",
        "ai_agent_training_messages",
        ["session_id"],
    )

    # ── ai_agent_playbooks — Step 6 ─────────────────────────────────
    op.create_table(
        "ai_agent_playbooks",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_ts(),
        sa.Column(
            "ai_agent_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ai_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", pg.JSONB(), server_default="{}", nullable=False),
        sa.Column(
            "generation_status", sa.String(length=16), server_default="idle", nullable=False
        ),
        sa.Column("generation_error", sa.Text(), nullable=True),
        sa.Column(
            "approval_status", sa.String(length=12), server_default="draft", nullable=False
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.UniqueConstraint("ai_agent_id", name="uq_ai_agent_playbook_agent"),
    )
    op.create_index("ix_ai_agent_playbooks_agent", "ai_agent_playbooks", ["ai_agent_id"])

    # ── ai_agent_showing_guides — Step 7 ────────────────────────────
    op.create_table(
        "ai_agent_showing_guides",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_ts(),
        sa.Column(
            "ai_agent_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ai_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", pg.JSONB(), server_default="{}", nullable=False),
        sa.Column(
            "generation_status", sa.String(length=16), server_default="idle", nullable=False
        ),
        sa.Column("generation_error", sa.Text(), nullable=True),
        sa.Column(
            "approval_status", sa.String(length=12), server_default="draft", nullable=False
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("ai_agent_id", name="uq_ai_agent_showing_guide_agent"),
    )
    op.create_index(
        "ix_ai_agent_showing_guides_agent", "ai_agent_showing_guides", ["ai_agent_id"]
    )

    # ── ai_agent_exit_rules — Step 8 ────────────────────────────────
    op.create_table(
        "ai_agent_exit_rules",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_ts(),
        sa.Column(
            "ai_agent_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ai_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("max_email_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column(
            "max_no_reply_followups", sa.Integer(), server_default="4", nullable=False
        ),
        sa.Column("max_days_in_sequence", sa.Integer(), server_default="14", nullable=False),
        sa.UniqueConstraint("ai_agent_id", name="uq_ai_agent_exit_rules_agent"),
    )
    op.create_index(
        "ix_ai_agent_exit_rules_agent", "ai_agent_exit_rules", ["ai_agent_id"]
    )

    # ── ai_agent_sample_messages — Step 8 ───────────────────────────
    op.create_table(
        "ai_agent_sample_messages",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_ts(),
        sa.Column(
            "ai_agent_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ai_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("touchpoint_key", sa.String(length=40), nullable=False),
        sa.Column("channel", sa.String(length=16), server_default="email", nullable=False),
        sa.Column("sample_text", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_ai_agent_sample_messages_agent", "ai_agent_sample_messages", ["ai_agent_id"]
    )

    # ── ai_agent_test_scenarios — Step 9 ────────────────────────────
    op.create_table(
        "ai_agent_test_scenarios",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_ts(),
        sa.Column(
            "ai_agent_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ai_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("ai_response", sa.Text(), nullable=True),
        sa.Column("reviewed", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_index(
        "ix_ai_agent_test_scenarios_agent", "ai_agent_test_scenarios", ["ai_agent_id"]
    )

    # ── ai_agent_messages — the agent's outbox (drafts / warm-up / sent)
    op.create_table(
        "ai_agent_messages",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_ts(),
        sa.Column(
            "ai_agent_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ai_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "lead_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ai_agent_leads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "client_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "touchpoint_key", sa.String(length=40), server_default="intro", nullable=False
        ),
        sa.Column("channel", sa.String(length=16), server_default="email", nullable=False),
        sa.Column("subject", sa.String(length=300), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column("is_warmup", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=200), nullable=True),
    )
    op.create_index("ix_ai_agent_messages_agent", "ai_agent_messages", ["ai_agent_id"])
    op.create_index(
        "ix_ai_agent_messages_status", "ai_agent_messages", ["ai_agent_id", "status"]
    )

    # ── ai_knowledge_documents — additive classifier columns ────────
    op.add_column(
        "ai_knowledge_documents", sa.Column("doc_type", sa.String(length=60), nullable=True)
    )
    op.add_column(
        "ai_knowledge_documents", sa.Column("key_facts", pg.JSONB(), nullable=True)
    )
    op.add_column(
        "ai_knowledge_documents", sa.Column("summary", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("ai_knowledge_documents", "summary")
    op.drop_column("ai_knowledge_documents", "key_facts")
    op.drop_column("ai_knowledge_documents", "doc_type")

    for table in (
        "ai_agent_messages",
        "ai_agent_test_scenarios",
        "ai_agent_sample_messages",
        "ai_agent_exit_rules",
        "ai_agent_showing_guides",
        "ai_agent_playbooks",
        "ai_agent_training_messages",
        "ai_agent_training_sessions",
        "ai_agent_leads",
        "ai_agent_targeting",
        "ai_agent_knowledge_links",
        "ai_agent_goals",
        "ai_agents",
    ):
        op.drop_table(table)
