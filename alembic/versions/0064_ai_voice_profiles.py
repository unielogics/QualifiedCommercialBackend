"""AI Voice Profiles — the broker's reusable tonality baseline.

Step 8 of the AI Agent builder no longer asks the broker to lay out a
follow-up sequence (the AI handles cadence + content). Instead, the
broker creates a small set of templates — greeting, late-item ask,
under-contract message, etc. — that establish HOW they talk to
clients. The set is named and reusable across AI Agents.

This migration creates `ai_voice_profiles` (broker-owned, JSONB
template bag), adds the FK on `ai_agents`, and drops the
now-unused `ai_agent_sample_messages` table.

Revision ID: 0064
Revises: 0063
Create Date: 2026-05-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_voice_profiles",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
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
        sa.Column("templates", pg.JSONB(), server_default="{}", nullable=False),
    )
    op.create_index(
        "ix_ai_voice_profiles_broker", "ai_voice_profiles", ["broker_id"]
    )

    op.add_column(
        "ai_agents",
        sa.Column(
            "voice_profile_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ai_voice_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Drop the per-agent sample-messages table — replaced by the
    # reusable account-level voice profile.
    op.drop_index(
        "ix_ai_agent_sample_messages_agent", table_name="ai_agent_sample_messages"
    )
    op.drop_table("ai_agent_sample_messages")


def downgrade() -> None:
    op.create_table(
        "ai_agent_sample_messages",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
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

    op.drop_column("ai_agents", "voice_profile_id")
    op.drop_index("ix_ai_voice_profiles_broker", table_name="ai_voice_profiles")
    op.drop_table("ai_voice_profiles")
