"""Stop defaulting a file's industry to auto dealer, and keep a booking's address parts.

Revision ID: 0179_industry_and_booking_address
Revises: 0178_appointment_origin_backfill
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0179_industry_and_booking_address"
down_revision = "0178_appointment_origin_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A file with no stated industry said "auto_dealer", which is a claim
    # nobody made: it routed the file to the dealer vertical. Unknown is now
    # unknown. Existing rows keep whatever they hold; nothing is rewritten.
    op.alter_column(
        "dos_dealers",
        "industry",
        existing_type=sa.String(48),
        nullable=True,
        server_default=None,
    )
    # The rep types a street, city, state and ZIP; only the joined string used
    # to survive, so the file opened with three empty address blockers.
    for column in ("street", "city", "state", "zip"):
        op.add_column("dos_rep_appointments", sa.Column(column, sa.String(120), nullable=True))


def downgrade() -> None:
    for column in ("zip", "state", "city", "street"):
        op.drop_column("dos_rep_appointments", column)
    op.execute(sa.text("UPDATE dos_dealers SET industry = 'auto_dealer' WHERE industry IS NULL"))
    op.alter_column(
        "dos_dealers",
        "industry",
        existing_type=sa.String(48),
        nullable=False,
        server_default="auto_dealer",
    )
