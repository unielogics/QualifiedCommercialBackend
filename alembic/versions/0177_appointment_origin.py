"""Where a booking came from, so the calendar can open the right file for it.

Revision ID: 0177_appointment_origin
Revises: 0176_precall_prep
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0177_appointment_origin"
down_revision = "0176_precall_prep"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # field_desk | calendar | public | intake — null for rows that predate this.
    op.add_column("dos_rep_appointments", sa.Column("origin", sa.String(24), nullable=True))
    op.create_index("ix_dos_rep_appointments_origin", "dos_rep_appointments", ["origin"])


def downgrade() -> None:
    op.drop_index("ix_dos_rep_appointments_origin", table_name="dos_rep_appointments")
    op.drop_column("dos_rep_appointments", "origin")
