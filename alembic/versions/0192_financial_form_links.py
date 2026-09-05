"""A link that opens one financial form and nothing else.

Only the SHA-256 of the token is stored. The link carries no access code, which
makes the URL the entire credential, so a database read must not be able to hand
somebody a working one. The dealer intake room already stores its token this
way; the bucket room keeps its in plaintext and gates with a PIN instead, which
is the trade this one deliberately is not making.

Revision ID: 0192_financial_form_links
Revises: 0191_financial_statements
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0192_financial_form_links"
down_revision = "0191_financial_statements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "financial_form_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column(
            "statement_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("financial_statements.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("token_hash", sa.String(96), nullable=False, unique=True),
        sa.Column("label", sa.String(120), nullable=True),
        sa.Column("invitee_email", sa.String(320), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind in ('pfs','debt_schedule')", name="ck_financial_form_links_kind"),
    )
    op.create_index("ix_financial_form_links_profile_id", "financial_form_links", ["profile_id"])
    op.create_index("ix_financial_form_links_token_hash", "financial_form_links", ["token_hash"])
    op.create_index(
        "ix_financial_form_links_profile_kind", "financial_form_links", ["profile_id", "kind"]
    )


def downgrade() -> None:
    op.drop_index("ix_financial_form_links_profile_kind", table_name="financial_form_links")
    op.drop_index("ix_financial_form_links_token_hash", table_name="financial_form_links")
    op.drop_index("ix_financial_form_links_profile_id", table_name="financial_form_links")
    op.drop_table("financial_form_links")
