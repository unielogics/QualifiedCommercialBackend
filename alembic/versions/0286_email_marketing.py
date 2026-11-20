"""Opted-in funding education subscriptions and campaign delivery queue."""
import sqlalchemy as sa

from alembic import op

revision = "0286_email_marketing"
down_revision = "0285_secure_ssns"
branch_labels = None
depends_on = None


def upgrade():
    uid = sa.Uuid()
    instant = sa.DateTime(timezone=True)
    op.create_table("email_marketing_subscribers",
        sa.Column("id", uid, primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("locale", sa.String(2), nullable=False),
        sa.Column("created_at", instant, server_default=sa.func.now(), nullable=False),
        sa.Column("unsubscribed_at", instant))
    op.create_table("email_marketing_enrollments",
        sa.Column("id", uid, primary_key=True),
        sa.Column("subscriber_id", uid, sa.ForeignKey("email_marketing_subscribers.id"), nullable=False),
        sa.Column("source", sa.String(120), nullable=False),
        sa.Column("program", sa.String(120)), sa.Column("outcome", sa.String(120)),
        sa.Column("consent_version", sa.String(48), nullable=False),
        sa.Column("client_id", uid, sa.ForeignKey("clients.id", ondelete="SET NULL")),
        sa.Column("intake_id", uid, sa.ForeignKey("public_underwriting_intakes.id", ondelete="SET NULL")),
        sa.Column("created_at", instant, nullable=False, server_default=sa.func.now()))
    op.create_index("ix_email_marketing_enrollments_subscriber_id", "email_marketing_enrollments", ["subscriber_id"])
    op.create_table("email_marketing_campaigns",
        sa.Column("id", uid, primary_key=True),
        sa.Column("created_by", uid, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", instant, nullable=False, server_default=sa.func.now()),
        sa.Column("name", sa.String(120), nullable=False), sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False), sa.Column("filters", sa.JSON(), nullable=False))
    op.create_table("email_marketing_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscriber_id", uid, sa.ForeignKey("email_marketing_subscribers.id"), nullable=False),
        sa.Column("campaign_id", uid, sa.ForeignKey("email_marketing_campaigns.id")),
        sa.Column("purpose_key", sa.String(48), nullable=False), sa.Column("step", sa.Integer()),
        sa.Column("due_at", instant, nullable=False), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("message_send_id", uid, sa.ForeignKey("message_sends.id", ondelete="SET NULL")),
        sa.UniqueConstraint("subscriber_id", "purpose_key", name="uq_email_marketing_delivery"))
    op.create_index("ix_email_marketing_due", "email_marketing_deliveries", ["status", "due_at"])
    op.create_index("ix_email_marketing_deliveries_campaign_id", "email_marketing_deliveries", ["campaign_id"])


def downgrade():
    op.drop_table("email_marketing_deliveries")
    op.drop_table("email_marketing_campaigns")
    op.drop_table("email_marketing_enrollments")
    op.drop_table("email_marketing_subscribers")
