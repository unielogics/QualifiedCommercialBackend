"""dealer-os: primary operating banks and Plaid statement lineage.

Revision ID: 0140_dos_multi_owner_multi_bank
Revises: 0139_operator_file_lineage
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from alembic import op

revision = "0140_dos_multi_owner_multi_bank"
down_revision = "0139_operator_file_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_plaid_items",
        sa.Column("is_primary_operating", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index(
        "uq_dos_plaid_items_one_primary",
        "dos_plaid_items",
        ["dealer_id"],
        unique=True,
        postgresql_where=sa.text("is_primary_operating"),
    )
    # Preserve existing behavior by selecting the oldest live connection as
    # primary for every file that already has Plaid linked.
    op.execute(
        """
        UPDATE dos_plaid_items AS item
           SET is_primary_operating = true
         WHERE item.id = (
             SELECT candidate.id
               FROM dos_plaid_items AS candidate
              WHERE candidate.dealer_id = item.dealer_id
                AND candidate.status <> 'removed'
              ORDER BY candidate.created_at, candidate.id
              LIMIT 1
         )
        """
    )

    op.add_column(
        "dos_documents",
        sa.Column(
            "plaid_item_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("dos_plaid_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_dos_documents_plaid_item", "dos_documents", ["plaid_item_id"])


def downgrade() -> None:
    op.drop_index("ix_dos_documents_plaid_item", table_name="dos_documents")
    op.drop_column("dos_documents", "plaid_item_id")
    op.drop_index("uq_dos_plaid_items_one_primary", table_name="dos_plaid_items")
    op.drop_column("dos_plaid_items", "is_primary_operating")
