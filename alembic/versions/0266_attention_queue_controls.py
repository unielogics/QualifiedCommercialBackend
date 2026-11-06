"""Persist reversible shared attention-queue controls without changing files."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0266_attention_queue_controls"
down_revision = "0265_current_funding_request"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operator_attention_controls",
        sa.Column("item_key", sa.String(120), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("duplicate_of_key", sa.String(120), sa.ForeignKey("operator_attention_controls.item_key", ondelete="RESTRICT")),
        sa.Column("duplicate_snapshot", postgresql.JSONB()),
        sa.Column("reason", sa.String(1000)),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("actor_name", sa.String(255)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active','archived','dismissed','duplicate')", name="ck_attention_control_status"),
        sa.CheckConstraint("revision >= 0", name="ck_attention_control_revision"),
        sa.CheckConstraint("(status = 'duplicate' AND duplicate_of_key IS NOT NULL AND duplicate_of_key <> item_key AND duplicate_snapshot IS NOT NULL) OR (status <> 'duplicate' AND duplicate_of_key IS NULL)", name="ck_attention_control_duplicate"),
    )
    op.create_index("ix_operator_attention_controls_status", "operator_attention_controls", ["status"])
    op.create_table(
        "operator_attention_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("item_key", sa.String(120), sa.ForeignKey("operator_attention_controls.item_key", ondelete="RESTRICT"), nullable=False),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("previous_status", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(1000)),
        sa.Column("duplicate_of_key", sa.String(120)),
        sa.Column("duplicate_snapshot", postgresql.JSONB()),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("actor_name", sa.String(255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_operator_attention_event_retry"),
        sa.UniqueConstraint("item_key", "revision", name="uq_operator_attention_event_revision"),
        sa.CheckConstraint("action IN ('archive','dismiss','duplicate','restore')", name="ck_attention_event_action"),
        sa.CheckConstraint("revision > 0", name="ck_attention_event_revision"),
    )
    op.create_index("ix_operator_attention_events_item_time", "operator_attention_events", ["item_key", "occurred_at"])


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM operator_attention_events)
        OR EXISTS (SELECT 1 FROM operator_attention_controls) THEN
        RAISE EXCEPTION 'Attention control history exists; preserve it before downgrading';
        END IF; END; $$""")
    op.drop_table("operator_attention_events")
    op.drop_table("operator_attention_controls")
