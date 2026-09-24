"""Add recoverable archiving for Marketing contacts.

Revision ID: 0226_marketing_contact_archive
Revises: 0225_booking_delivery_operations
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0226_marketing_contact_archive"
down_revision = "0225_booking_delivery_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_rep_contacts",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "dos_rep_contacts",
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "dos_rep_contacts",
        sa.Column(
            "restored_by_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "dos_rep_contacts",
        sa.Column(
            "archived_by_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_dos_rep_contacts_archived_by_user_id",
        "dos_rep_contacts",
        "users",
        ["archived_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_dos_rep_contacts_restored_by_user_id",
        "dos_rep_contacts",
        "users",
        ["restored_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_dos_rep_contacts_archived",
        "dos_rep_contacts",
        ["archived_at", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_dos_rep_contacts_archived", table_name="dos_rep_contacts")
    op.drop_constraint(
        "fk_dos_rep_contacts_restored_by_user_id",
        "dos_rep_contacts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_dos_rep_contacts_archived_by_user_id",
        "dos_rep_contacts",
        type_="foreignkey",
    )
    op.drop_column("dos_rep_contacts", "archived_by_user_id")
    op.drop_column("dos_rep_contacts", "restored_by_user_id")
    op.drop_column("dos_rep_contacts", "restored_at")
    op.drop_column("dos_rep_contacts", "archived_at")
