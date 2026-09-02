"""Backfill booking origins for rows that predate them, and pin the vocabulary.

Revision ID: 0178_appointment_origin_backfill
Revises: 0177_appointment_origin
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0178_appointment_origin_backfill"
down_revision = "0177_appointment_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Bookings a rep made, or made on a dealer file, were field-desk bookings;
    # everything else came from the calendar. Deterministic, so the calendar
    # never shows "Not recorded" for rows that existed before origins did.
    op.execute(
        sa.text(
            """
            UPDATE dos_rep_appointments a
               SET origin = 'field_desk'
              FROM users u
             WHERE a.origin IS NULL
               AND a.booked_by_user_id = u.id
               AND u.role IN ('field_rep', 'broker')
            """
        )
    )
    op.execute(
        sa.text("UPDATE dos_rep_appointments SET origin = 'field_desk' WHERE origin IS NULL AND dealer_id IS NOT NULL")
    )
    op.execute(sa.text("UPDATE dos_rep_appointments SET origin = 'calendar' WHERE origin IS NULL"))
    op.create_check_constraint(
        "ck_dos_rep_appointment_origin",
        "dos_rep_appointments",
        "origin IN ('field_desk','calendar','public','intake')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_dos_rep_appointment_origin", "dos_rep_appointments", type_="check")
