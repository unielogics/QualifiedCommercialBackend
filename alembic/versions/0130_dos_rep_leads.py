"""dos_rep_leads: the field-rep pipeline wrapper

A rep-collected file is a DealerBusiness (which gets the whole Capital OS
metrics engine) plus one of these rows, which carries ownership and where the
file sits in the process.

Additive only: one new table, no changes to existing columns. DealerBusiness
already has owner_user_id from 0108 — this migration does not add it, it just
marks the point where it stops being decorative and starts being the tenancy
key for reps.

Revision ID: 0130_dos_rep_leads
Revises: 0129_dos_dscr
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

revision = "0130_dos_rep_leads"
down_revision = "0129_dos_dscr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dos_rep_leads",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dealer_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # SET NULL, not CASCADE: a rep leaving the company must never delete
        # the files they collected.
        sa.Column(
            "rep_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_note", sa.String(240), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_history", JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_dos_rep_leads_dealer", "dos_rep_leads", ["dealer_id"])
    op.create_index(
        "ix_dos_rep_leads_rep_status", "dos_rep_leads", ["rep_user_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_dos_rep_leads_rep_status", table_name="dos_rep_leads")
    op.drop_index("ix_dos_rep_leads_dealer", table_name="dos_rep_leads")
    op.drop_table("dos_rep_leads")
