"""capital_partner_applications — intake table for the public
"Become a Lending Partner" form on qualifiedcommercial.com.

One row per submitted application; super-admin reviews via QCDashboard
/admin/capital-partner-applications. On approval the operator
optionally promotes the row into a real `lenders` table entry; the
promoted_lender_id FK back-stamps that link for audit.

Revision ID: 0062
Revises: 0061
Create Date: 2026-05-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capital_partner_applications",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Company
        sa.Column("company_name", sa.String(length=160), nullable=False),
        sa.Column("legal_entity_type", sa.String(length=40), nullable=True),
        sa.Column("formation_state", sa.String(length=40), nullable=True),
        sa.Column("ein", sa.String(length=20), nullable=True),
        sa.Column("years_in_business", sa.Integer(), nullable=True),
        sa.Column("website", sa.String(length=240), nullable=True),
        # Lending appetite
        sa.Column(
            "loan_types",
            sa.dialects.postgresql.JSONB(),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("loan_size_min", sa.Integer(), nullable=True),
        sa.Column("loan_size_max", sa.Integer(), nullable=True),
        sa.Column(
            "geographic_states",
            sa.dialects.postgresql.JSONB(),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "asset_classes",
            sa.dialects.postgresql.JSONB(),
            server_default="[]",
            nullable=False,
        ),
        # Capital & volume
        sa.Column("capital_source", sa.String(length=80), nullable=True),
        sa.Column("aum_band", sa.String(length=40), nullable=True),
        sa.Column("monthly_origination_band", sa.String(length=40), nullable=True),
        # Underwriting box
        sa.Column("max_ltv", sa.Numeric(5, 4), nullable=True),
        sa.Column("max_ltc", sa.Numeric(5, 4), nullable=True),
        sa.Column("min_dscr", sa.Numeric(4, 2), nullable=True),
        sa.Column("min_fico", sa.Integer(), nullable=True),
        sa.Column("rate_range", sa.String(length=80), nullable=True),
        # Contact + submission
        sa.Column("contact_name", sa.String(length=160), nullable=False),
        sa.Column("contact_title", sa.String(length=80), nullable=True),
        sa.Column("contact_email", sa.String(length=320), nullable=False),
        sa.Column("contact_phone", sa.String(length=40), nullable=True),
        sa.Column("submission_email", sa.String(length=320), nullable=True),
        sa.Column("submission_portal_url", sa.String(length=320), nullable=True),
        sa.Column("average_response_time", sa.String(length=80), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        # Review
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column(
            "reviewed_by_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "promoted_lender_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("lenders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Consent + audit
        sa.Column("consent", sa.Boolean(), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
    )
    # Status + recency index — drives the admin list view (filter by
    # status, then sort by submission time).
    op.create_index(
        "ix_capital_partner_applications_status_created",
        "capital_partner_applications",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_capital_partner_applications_status_created",
        table_name="capital_partner_applications",
    )
    op.drop_table("capital_partner_applications")
