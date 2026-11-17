"""Consent-bound public funding assessments.

Revision ID: 0283_resource_assessments
Revises: 0282_one_live_signing_pass
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0283_resource_assessments"
down_revision = "0282_one_live_signing_pass"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("resource_assessments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(36), unique=True, nullable=False),
        sa.Column("goal", sa.String(40), nullable=False), sa.Column("locale", sa.String(2), nullable=False),
        sa.Column("resource_id", sa.String(80), nullable=False),
        *[sa.Column(name, postgresql.JSONB(), nullable=False) for name in
          ("contact", "answers", "assumptions", "report", "attribution")],
        sa.Column("email_marketing_consent", sa.Boolean(), nullable=False),
        sa.Column("consent_version", sa.String(60), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("application_started_at", sa.DateTime(timezone=True)),
        sa.Column("nurture_next_at", sa.DateTime(timezone=True)),
        sa.Column("application_intake_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("public_underwriting_intakes.id")),
        sa.Column("resource_token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_resource_assessments_client_id", "resource_assessments", ["client_id"])
    op.create_index("ix_resource_assessments_nurture_next_at", "resource_assessments", ["nurture_next_at"])


def downgrade():
    op.drop_index("ix_resource_assessments_nurture_next_at", table_name="resource_assessments")
    op.drop_index("ix_resource_assessments_client_id", table_name="resource_assessments")
    op.drop_table("resource_assessments")
