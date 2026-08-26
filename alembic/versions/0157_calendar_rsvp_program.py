"""calendar RSVP tracking and catalog-backed appointment programs

Revision ID: 0157_calendar_rsvp_program
Revises: 0156_step5_finalization
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0157_calendar_rsvp_program"
down_revision = "0156_step5_finalization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_rep_appointments", sa.Column("program_key", sa.String(length=64), nullable=True))
    op.add_column(
        "dos_rep_appointments",
        sa.Column("client_rsvp_status", sa.String(length=16), nullable=False, server_default="unknown"),
    )
    op.add_column("dos_rep_appointments", sa.Column("client_rsvp_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("dos_rep_appointments", sa.Column("rsvp_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_dos_rep_appointments_rsvp",
        "dos_rep_appointments",
        ["client_rsvp_status", "starts_at"],
    )

    # Force one bounded full pull so existing Google appointments receive an
    # attendee-based status instead of inheriting their historical local state.
    op.execute("UPDATE google_accounts SET calendar_sync_token = NULL WHERE calendar_connected IS TRUE")


def downgrade() -> None:
    op.drop_index("ix_dos_rep_appointments_rsvp", table_name="dos_rep_appointments")
    op.drop_column("dos_rep_appointments", "rsvp_checked_at")
    op.drop_column("dos_rep_appointments", "client_rsvp_at")
    op.drop_column("dos_rep_appointments", "client_rsvp_status")
    op.drop_column("dos_rep_appointments", "program_key")
