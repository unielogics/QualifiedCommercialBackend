"""Keep audited current funding requests separate from original submissions."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0265_current_funding_request"
down_revision = "0264_client_form_edit_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("application_profiles", sa.Column("current_funding_request", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.create_table(
        "application_funding_request_changes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("profile_id", "revision", name="uq_funding_request_revision"),
        sa.UniqueConstraint("profile_id", "idempotency_key", name="uq_funding_request_retry"),
    )
    op.create_index("ix_application_funding_request_changes_profile_id", "application_funding_request_changes", ["profile_id"])


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM application_funding_request_changes) THEN
        RAISE EXCEPTION 'Funding request correction history exists; preserve it before downgrading';
        END IF; END; $$""")
    op.drop_table("application_funding_request_changes")
    op.drop_column("application_profiles", "current_funding_request")
