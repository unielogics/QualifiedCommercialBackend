"""lender packages and terms.

Revision ID: 0070
Revises: 0069
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "lender_users",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_timestamps(),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lender_id", pg.UUID(as_uuid=True), sa.ForeignKey("lenders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.UniqueConstraint("user_id", "lender_id", name="uq_lender_users_user_lender"),
    )
    op.create_index("ix_lender_users_user_id", "lender_users", ["user_id"])
    op.create_index("ix_lender_users_lender_id", "lender_users", ["lender_id"])
    op.create_index("ix_lender_users_email", "lender_users", ["email"])

    op.create_table(
        "lender_packages",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_timestamps(),
        sa.Column("loan_id", pg.UUID(as_uuid=True), sa.ForeignKey("loans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_lender_packages_loan_id", "lender_packages", ["loan_id"])
    op.create_index("ix_lender_packages_status", "lender_packages", ["status"])

    op.create_table(
        "lender_package_documents",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_timestamps(),
        sa.Column("package_id", pg.UUID(as_uuid=True), sa.ForeignKey("lender_packages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", pg.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.UniqueConstraint("package_id", "document_id", name="uq_lender_package_document"),
    )
    op.create_index("ix_lender_package_documents_package_id", "lender_package_documents", ["package_id"])
    op.create_index("ix_lender_package_documents_document_id", "lender_package_documents", ["document_id"])

    op.create_table(
        "lender_package_recipients",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_timestamps(),
        sa.Column("package_id", pg.UUID(as_uuid=True), sa.ForeignKey("lender_packages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lender_id", pg.UUID(as_uuid=True), sa.ForeignKey("lenders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="sent", nullable=False),
        sa.Column("invited_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("email_draft_id", pg.UUID(as_uuid=True), sa.ForeignKey("email_drafts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terms_submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("no_quote_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("package_id", "lender_id", "email", name="uq_lender_package_recipient"),
    )
    op.create_index("ix_lender_package_recipients_package_id", "lender_package_recipients", ["package_id"])
    op.create_index("ix_lender_package_recipients_lender_id", "lender_package_recipients", ["lender_id"])
    op.create_index("ix_lender_package_recipients_email", "lender_package_recipients", ["email"])
    op.create_index("ix_lender_package_recipients_status", "lender_package_recipients", ["status"])

    op.create_table(
        "lender_package_events",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("package_id", pg.UUID(as_uuid=True), sa.ForeignKey("lender_packages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipient_id", pg.UUID(as_uuid=True), sa.ForeignKey("lender_package_recipients.id", ondelete="CASCADE"), nullable=True),
        sa.Column("lender_id", pg.UUID(as_uuid=True), sa.ForeignKey("lenders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("detail", pg.JSONB(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_lender_package_events_package_id", "lender_package_events", ["package_id"])
    op.create_index("ix_lender_package_events_recipient_id", "lender_package_events", ["recipient_id"])
    op.create_index("ix_lender_package_events_lender_id", "lender_package_events", ["lender_id"])

    op.create_table(
        "lender_terms",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        *_timestamps(),
        sa.Column("loan_id", pg.UUID(as_uuid=True), sa.ForeignKey("loans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lender_id", pg.UUID(as_uuid=True), sa.ForeignKey("lenders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("package_id", pg.UUID(as_uuid=True), sa.ForeignKey("lender_packages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("package_recipient_id", pg.UUID(as_uuid=True), sa.ForeignKey("lender_package_recipients.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("submitted_by_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.String(length=24), server_default="manual", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="received", nullable=False),
        sa.Column("requested_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("approved_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("base_rate", sa.Numeric(9, 6), nullable=True),
        sa.Column("final_rate", sa.Numeric(9, 6), nullable=True),
        sa.Column("discount_points", sa.Numeric(5, 3), nullable=True),
        sa.Column("origination_pct", sa.Numeric(6, 4), nullable=True),
        sa.Column("lender_fees", sa.Numeric(14, 2), nullable=True),
        sa.Column("term_months", sa.Integer(), nullable=True),
        sa.Column("amortization_style", sa.String(length=24), nullable=True),
        sa.Column("interest_only", sa.Boolean(), nullable=True),
        sa.Column("prepay_penalty", sa.String(length=32), nullable=True),
        sa.Column("ltv", sa.Numeric(6, 4), nullable=True),
        sa.Column("ltc", sa.Numeric(6, 4), nullable=True),
        sa.Column("dscr", sa.Numeric(8, 4), nullable=True),
        sa.Column("reserves_required", sa.Numeric(14, 2), nullable=True),
        sa.Column("estimated_close_days", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("conditions", pg.JSONB(), nullable=True),
        sa.Column("missing_items", pg.JSONB(), nullable=True),
        sa.Column("construction_holdback_pct", sa.Numeric(5, 4), nullable=True),
        sa.Column("draw_count", sa.Integer(), nullable=True),
        sa.Column("exit_strategy", sa.String(length=16), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("selected_by_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_lender_terms_loan_id", "lender_terms", ["loan_id"])
    op.create_index("ix_lender_terms_lender_id", "lender_terms", ["lender_id"])
    op.create_index("ix_lender_terms_package_id", "lender_terms", ["package_id"])
    op.create_index("ix_lender_terms_package_recipient_id", "lender_terms", ["package_recipient_id"])
    op.create_index("ix_lender_terms_status", "lender_terms", ["status"])


def downgrade() -> None:
    op.drop_index("ix_lender_terms_status", table_name="lender_terms")
    op.drop_index("ix_lender_terms_package_recipient_id", table_name="lender_terms")
    op.drop_index("ix_lender_terms_package_id", table_name="lender_terms")
    op.drop_index("ix_lender_terms_lender_id", table_name="lender_terms")
    op.drop_index("ix_lender_terms_loan_id", table_name="lender_terms")
    op.drop_table("lender_terms")
    op.drop_index("ix_lender_package_events_lender_id", table_name="lender_package_events")
    op.drop_index("ix_lender_package_events_recipient_id", table_name="lender_package_events")
    op.drop_index("ix_lender_package_events_package_id", table_name="lender_package_events")
    op.drop_table("lender_package_events")
    op.drop_index("ix_lender_package_recipients_status", table_name="lender_package_recipients")
    op.drop_index("ix_lender_package_recipients_email", table_name="lender_package_recipients")
    op.drop_index("ix_lender_package_recipients_lender_id", table_name="lender_package_recipients")
    op.drop_index("ix_lender_package_recipients_package_id", table_name="lender_package_recipients")
    op.drop_table("lender_package_recipients")
    op.drop_index("ix_lender_package_documents_document_id", table_name="lender_package_documents")
    op.drop_index("ix_lender_package_documents_package_id", table_name="lender_package_documents")
    op.drop_table("lender_package_documents")
    op.drop_index("ix_lender_packages_status", table_name="lender_packages")
    op.drop_index("ix_lender_packages_loan_id", table_name="lender_packages")
    op.drop_table("lender_packages")
    op.drop_index("ix_lender_users_email", table_name="lender_users")
    op.drop_index("ix_lender_users_lender_id", table_name="lender_users")
    op.drop_index("ix_lender_users_user_id", table_name="lender_users")
    op.drop_table("lender_users")
