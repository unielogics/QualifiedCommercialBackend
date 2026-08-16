"""dealer-os desk admin trio: program settings, dealer groups, payment shifts.

- dos_program_settings: per-program override of the lending-desk sizing +
  readiness constants (absent row = code defaults in services/paths.py).
  The row carries its own change history (dos_audit_log is dealer-scoped
  and cannot record global actions).
- dos_dealer_groups + dos_dealers.group_id: one client "file" holding an
  owner's multiple LLCs. Metrics stay entirely per-dealer; the group is a
  console/reporting concept (average + ranking + star).
- dos_payment_shifts: team-authored payment-date shift suggestions (raise
  ADB by moving withdrawals later under real vendor terms). Dealers see
  status != 'draft' only.

Revision ID: 0120_dos_desk_admin
Revises: 0119_dos_provenance_goal
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0120_dos_desk_admin"
down_revision = "0119_dos_provenance_goal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dos_program_settings",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("path_key", sa.String(24), nullable=False, unique=True),
        sa.Column("sizing", pg.JSONB(), nullable=True),
        sa.Column("requirements", pg.JSONB(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_by_user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("history", pg.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "dos_dealer_groups",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column(
        "dos_dealers",
        sa.Column("group_id", pg.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_dos_dealers_group",
        "dos_dealers",
        "dos_dealer_groups",
        ["group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_dos_dealers_group", "dos_dealers", ["group_id"])

    op.create_table(
        "dos_payment_shifts",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dealer_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("vendor_key", sa.String(120), nullable=True),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("from_day", sa.Integer(), nullable=False),
        sa.Column("to_day", sa.Integer(), nullable=False),
        sa.Column("monthly_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("est_adb_impact", sa.Numeric(14, 2), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column(
            "created_by_user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dos_payment_shifts_dealer_status", "dos_payment_shifts", ["dealer_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_dos_payment_shifts_dealer_status", table_name="dos_payment_shifts")
    op.drop_table("dos_payment_shifts")
    op.drop_index("ix_dos_dealers_group", table_name="dos_dealers")
    op.drop_constraint("fk_dos_dealers_group", "dos_dealers", type_="foreignkey")
    op.drop_column("dos_dealers", "group_id")
    op.drop_table("dos_dealer_groups")
    op.drop_table("dos_program_settings")
