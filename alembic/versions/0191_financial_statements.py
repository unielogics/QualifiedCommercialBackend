"""A Personal Financial Statement that survives being submitted.

The on-screen PFS rendered what a borrower typed into a PDF and discarded the
rows. That was a deliberate data-minimisation choice, and it made four things
impossible: reopening a form, correcting one, finishing one on a client's
behalf, and attaching a statement to an applicant at all — the only link to a
person was a free-text name.

These tables keep the rows. Still no SSN: the privacy reasoning behind the
original decision is answered rather than dropped.

`financial_statement_owners` carries two nullable owner columns with a check
that exactly one is set, because owners live in two tables — `application_owners`
for profile-backed files, `dos_owners` for dealer-backed ones. A single
polymorphic id would have no foreign key behind it and would rot when an owner
is deleted.

Nothing is backfilled. Three statements have ever been submitted through the
form and only their derived totals survive, so there is nothing to recover.

Revision ID: 0191_financial_statements
Revises: 0190_user_phone_and_title
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0191_financial_statements"
down_revision = "0190_user_phone_and_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "financial_statements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("statement_date", sa.Date(), nullable=True),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="sba413.v1"),
        sa.Column("body", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("total_assets", sa.Numeric(16, 2), nullable=True),
        sa.Column("total_liabilities", sa.Numeric(16, 2), nullable=True),
        sa.Column("net_worth", sa.Numeric(16, 2), nullable=True),
        sa.Column("liquid_assets", sa.Numeric(16, 2), nullable=True),
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
        sa.CheckConstraint("status in ('draft','submitted')", name="ck_financial_statements_status"),
    )
    op.create_index("ix_financial_statements_profile_id", "financial_statements", ["profile_id"])
    op.create_index(
        "ix_financial_statements_profile_status", "financial_statements", ["profile_id", "status"]
    )

    op.create_table(
        "financial_statement_owners",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "statement_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("financial_statements.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "application_owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_owners.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "dealer_owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dos_owners.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "(application_owner_id is not null)::int + (dealer_owner_id is not null)::int = 1",
            name="ck_financial_statement_owners_exactly_one",
        ),
        sa.UniqueConstraint(
            "statement_id", "application_owner_id", name="uq_statement_application_owner"
        ),
        sa.UniqueConstraint("statement_id", "dealer_owner_id", name="uq_statement_dealer_owner"),
    )
    op.create_index(
        "ix_financial_statement_owners_statement", "financial_statement_owners", ["statement_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_financial_statement_owners_statement", table_name="financial_statement_owners"
    )
    op.drop_table("financial_statement_owners")
    op.drop_index("ix_financial_statements_profile_status", table_name="financial_statements")
    op.drop_index("ix_financial_statements_profile_id", table_name="financial_statements")
    op.drop_table("financial_statements")
