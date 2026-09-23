"""Global prospect identity lookup indexes.

Revision ID: 0224_prospect_identity_followups
Revises: 0223_booking_reliability
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0224_prospect_identity_followups"
down_revision = "0223_booking_reliability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing installations may contain legacy cross-dealer duplicates.  The
    # application serializes new writes with transaction advisory locks and
    # rescans globally; non-unique indexes make that OR lookup fast without a
    # destructive migration that guesses which historical record to archive.
    op.create_index(
        "ix_dealer_prospect_email_identity",
        "dealer_prospects",
        ["email_normalized"],
        unique=False,
        postgresql_where=sa.text("email_normalized IS NOT NULL"),
    )
    op.create_index(
        "ix_dealer_prospect_phone_identity",
        "dealer_prospects",
        ["phone_normalized"],
        unique=False,
        postgresql_where=sa.text("phone_normalized IS NOT NULL"),
    )
    op.create_index(
        "ix_dos_rep_contacts_email_identity",
        "dos_rep_contacts",
        [sa.text("lower(email)")],
        unique=False,
        postgresql_where=sa.text("email IS NOT NULL"),
    )
    op.create_index(
        "ix_dos_rep_contacts_phone_identity",
        "dos_rep_contacts",
        ["phone_e164"],
        unique=False,
        postgresql_where=sa.text("phone_e164 IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_dos_rep_contacts_phone_identity", table_name="dos_rep_contacts")
    op.drop_index("ix_dos_rep_contacts_email_identity", table_name="dos_rep_contacts")
    op.drop_index("ix_dealer_prospect_phone_identity", table_name="dealer_prospects")
    op.drop_index("ix_dealer_prospect_email_identity", table_name="dealer_prospects")
