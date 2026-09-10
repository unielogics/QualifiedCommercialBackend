"""Two business statements typed into our forms, and one link that opens all four.

`business_financial_statements` holds a profit and loss statement or a
balance sheet a borrower or the desk typed into the on-screen form — its own
table, so the by-id PFS routes can never be handed a balance-sheet id. One
live row per (profile, kind); the derived figures are written on every save
from the schema's totals.

`financial_form_links` learns two more kinds — the two new forms are links
like the PFS and the debt schedule — and a nullable `packet_id`. The forms
packet is not a kind: it is four links whose tokens derive from one base and
which share a `packet_id`, so the desk can list a packet and close all four
in one action. The CHECK on `kind` is dropped and recreated with the four
values; the downgrade restores the two-value CHECK, which fails loudly if a
row of a new kind still exists — the right outcome.

Revision ID: 0203_business_statements_and_packets
Revises: 0202_debt_schedule_full_row
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0203_business_statements_and_packets"
down_revision = "0202_debt_schedule_full_row"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_financial_statements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("as_of_date", sa.Date(), nullable=True),
        sa.Column("body", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("gross_revenue", sa.Numeric(16, 2), nullable=True),
        sa.Column("net_income", sa.Numeric(16, 2), nullable=True),
        sa.Column("ebitda", sa.Numeric(16, 2), nullable=True),
        sa.Column("total_assets", sa.Numeric(16, 2), nullable=True),
        sa.Column("total_liabilities", sa.Numeric(16, 2), nullable=True),
        sa.Column("total_equity", sa.Numeric(16, 2), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "submitted_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "bucket_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bucket_files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind in ('p_and_l','balance_sheet')", name="ck_business_financial_statements_kind"
        ),
        sa.CheckConstraint(
            "status in ('draft','submitted')", name="ck_business_financial_statements_status"
        ),
    )
    op.create_index(
        "ix_business_financial_statements_profile_id",
        "business_financial_statements",
        ["profile_id"],
    )
    op.create_index(
        "ix_business_financial_statements_profile_kind_status",
        "business_financial_statements",
        ["profile_id", "kind", "status"],
    )

    op.add_column(
        "financial_form_links",
        sa.Column("packet_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_financial_form_links_packet_id", "financial_form_links", ["packet_id"]
    )
    op.drop_constraint("ck_financial_form_links_kind", "financial_form_links", type_="check")
    op.create_check_constraint(
        "ck_financial_form_links_kind",
        "financial_form_links",
        "kind in ('pfs','debt_schedule','p_and_l','balance_sheet')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_financial_form_links_kind", "financial_form_links", type_="check")
    op.create_check_constraint(
        "ck_financial_form_links_kind",
        "financial_form_links",
        "kind in ('pfs','debt_schedule')",
    )
    op.drop_index("ix_financial_form_links_packet_id", table_name="financial_form_links")
    op.drop_column("financial_form_links", "packet_id")

    op.drop_index(
        "ix_business_financial_statements_profile_kind_status",
        table_name="business_financial_statements",
    )
    op.drop_index(
        "ix_business_financial_statements_profile_id",
        table_name="business_financial_statements",
    )
    op.drop_table("business_financial_statements")
