"""dos: business profile fields and owners

The Credit module had no owner records and no business profile, so there was
nowhere to put the facts underwriting needs first — when the business started,
what it does (NAICS), how it is organised, and who owns it. Owners are also
the subjects of a personal credit pull: without a row per owner there is
nobody to pull for.

Revision ID: 0118_dos_owners_profile
Revises: 0117_dos_tax_detail
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0118_dos_owners_profile"
down_revision = "0117_dos_tax_detail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_dealers", sa.Column("started_on", sa.Date()))
    op.add_column("dos_dealers", sa.Column("entity_type", sa.String(32)))
    op.add_column("dos_dealers", sa.Column("naics_code", sa.String(8)))
    op.add_column("dos_dealers", sa.Column("naics_label", sa.String(180)))

    op.create_table(
        "dos_owners",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dealer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("first_name", sa.String(80), nullable=False),
        sa.Column("last_name", sa.String(80), nullable=False),
        sa.Column("email", sa.String(320)),
        sa.Column("phone", sa.String(48)),
        sa.Column("ownership_pct", sa.Numeric(5, 2)),
        sa.Column("is_guarantor", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("dob", sa.Date()),
        sa.Column("street", sa.String(240)),
        sa.Column("city", sa.String(120)),
        sa.Column("state", sa.String(8)),
        sa.Column("zip", sa.String(12)),
        # Soft-pull result summary only. No SSN and no full bureau payload is
        # stored here — the pull itself lives in credit_pulls, which is the
        # FCRA-governed record; this is the cockpit's read-only echo of it.
        sa.Column("credit_score", sa.Integer()),
        sa.Column("credit_tier", sa.String(16)),
        sa.Column("credit_pulled_at", sa.DateTime(timezone=True)),
        sa.Column("credit_pull_id", UUID(as_uuid=True)),
        sa.Column("credit_summary", JSONB()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dos_owners_dealer", "dos_owners", ["dealer_id"])


def downgrade() -> None:
    op.drop_index("ix_dos_owners_dealer", table_name="dos_owners")
    op.drop_table("dos_owners")
    for c in ("naics_label", "naics_code", "entity_type", "started_on"):
        op.drop_column("dos_dealers", c)
