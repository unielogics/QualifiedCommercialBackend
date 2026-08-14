"""Dealer Capital OS messaging & sessions — dos_messages + dos_sessions.

Revision ID: 0109_dos_messaging
Revises: 0108_dos_core
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "0109_dos_messaging"
down_revision = "0108_dos_core"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", pg.UUID(as_uuid=True), primary_key=True)


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def _dealer_fk(nullable=False):
    return sa.Column(
        "dealer_id", pg.UUID(as_uuid=True), sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=nullable
    )


def upgrade() -> None:
    op.create_table(
        "dos_messages",
        _id(),
        _dealer_fk(),
        sa.Column("author_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("author_name", sa.String(120)),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("internal", sa.Boolean(), nullable=False, server_default="false"),
        *_ts(),
    )
    op.create_index("ix_dos_messages_dealer_created", "dos_messages", ["dealer_id", "created_at"])
    op.create_table(
        "dos_sessions",
        _id(),
        _dealer_fk(),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False, server_default="call"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("join_url", sa.String(500)),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_ts(),
    )
    op.create_index("ix_dos_sessions_dealer_starts", "dos_sessions", ["dealer_id", "starts_at"])


def downgrade() -> None:
    op.drop_index("ix_dos_sessions_dealer_starts", table_name="dos_sessions")
    op.drop_table("dos_sessions")
    op.drop_index("ix_dos_messages_dealer_created", table_name="dos_messages")
    op.drop_table("dos_messages")
