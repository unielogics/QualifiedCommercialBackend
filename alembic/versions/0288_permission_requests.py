"""Track internal permission reviews and one-time invitations initiated by staff."""
import sqlalchemy as sa

from alembic import op

revision = "0288_permission_requests"
down_revision = "0287_campaign_composer"
branch_labels = None
depends_on = None


def upgrade():
    uid = sa.Uuid()
    instant = sa.DateTime(timezone=True)
    op.create_table("email_marketing_permission_batches",
        sa.Column("id", uid, primary_key=True),
        sa.Column("created_at", instant, server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", uid, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("fingerprint", sa.String(64), nullable=False), sa.Column("action", sa.String(20), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=False), sa.Column("requested_count", sa.Integer(), nullable=False))
    op.create_table("email_marketing_permission_requests",
        sa.Column("id", uid, primary_key=True), sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("locale", sa.String(2), nullable=False), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", instant, server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", uid, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("batch_id", uid, sa.ForeignKey("email_marketing_permission_batches.id"), nullable=False),
        sa.Column("invite_batch_id", uid, sa.ForeignKey("email_marketing_permission_batches.id")),
        sa.Column("due_at", instant, nullable=False), sa.Column("expires_at", instant), sa.Column("resolved_at", instant),
        sa.Column("message_send_id", uid, sa.ForeignKey("message_sends.id", ondelete="SET NULL")),
        sa.Column("task_event_id", uid, sa.ForeignKey("calendar_events.id", ondelete="SET NULL")))
    op.create_index("ix_marketing_permission_due", "email_marketing_permission_requests", ["status", "due_at"])


def downgrade():
    op.drop_table("email_marketing_permission_requests")
    op.drop_table("email_marketing_permission_batches")
