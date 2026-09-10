"""The merchant-processing offer.

A processing partner prices a client's card processing and sends us the
terms. The desk drops that PDF on the file, the system reads it, the client
accepts or declines from their room, and the partner is emailed the answer.
This is the row that carries the read numbers, the estimated saving, the
client's answer with signature-grade evidence, and the outcome of telling
the partner. See app/models/merchant_processing_offer.py.

One current offer per file, enforced by a partial unique index.

Revision ID: 0200_merchant_processing_offers
Revises: 0199_user_phone_reconciliation
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0200_merchant_processing_offers"
down_revision = "0199_user_phone_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "merchant_processing_offers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bucket_files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "lender_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("lenders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="uploaded"),
        sa.Column("terms", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("desk_terms", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("terms_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("estimated_monthly_savings", sa.Numeric(12, 2), nullable=True),
        sa.Column("estimated_annual_savings", sa.Numeric(12, 2), nullable=True),
        sa.Column("savings_basis", sa.String(24), nullable=True),
        sa.Column("extraction_confidence", sa.String(12), nullable=True),
        sa.Column("extraction_error", sa.Text, nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "sent_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("client_response", sa.String(16), nullable=True),
        sa.Column("client_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_response_reason", sa.Text, nullable=True),
        sa.Column("client_response_name", sa.String(160), nullable=True),
        sa.Column("client_response_ip", sa.String(80), nullable=True),
        sa.Column("client_response_user_agent", sa.String(500), nullable=True),
        sa.Column("disclaimer_version", sa.String(24), nullable=True),
        sa.Column("partner_email_status", sa.String(24), nullable=True),
        sa.Column("partner_email_message_id", sa.String(160), nullable=True),
        sa.Column("partner_email_error", sa.Text, nullable=True),
        sa.Column("partner_email_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_merchant_processing_offers_profile", "merchant_processing_offers", ["profile_id"])
    op.create_index("ix_merchant_processing_offers_source_file", "merchant_processing_offers", ["source_file_id"])
    op.create_index(
        "uq_merchant_processing_offers_current",
        "merchant_processing_offers",
        ["profile_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('superseded', 'withdrawn')"),
    )


def downgrade() -> None:
    op.drop_index("uq_merchant_processing_offers_current", table_name="merchant_processing_offers")
    op.drop_index("ix_merchant_processing_offers_source_file", table_name="merchant_processing_offers")
    op.drop_index("ix_merchant_processing_offers_profile", table_name="merchant_processing_offers")
    op.drop_table("merchant_processing_offers")
