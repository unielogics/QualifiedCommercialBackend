"""dos_dealers.case_ref — the reference a rep reads down a phone

Backfills every existing file in creation order so nothing is left without
one. A file with no reference cannot be quoted on a call or printed on a
contract, and "the one in Fresno" is not a reference.

Sequence is per calendar year and derived from created_at, so the numbering
reads as a real history rather than restarting at the migration.

Revision ID: 0133_dos_case_ref
Revises: 0132_dos_message_channels
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0133_dos_case_ref"
down_revision = "0132_dos_message_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_dealers", sa.Column("case_ref", sa.String(24)))
    # Backfill: QC-{year}-{5-digit sequence within that year}, ordered by when
    # the file was actually opened.
    op.execute(
        """
        WITH numbered AS (
            SELECT id,
                   'QC-' || to_char(created_at, 'YYYY') || '-' ||
                   lpad(
                       (row_number() OVER (
                           PARTITION BY date_part('year', created_at)
                           ORDER BY created_at, id
                       ))::text,
                       5, '0'
                   ) AS ref
            FROM dos_dealers
        )
        UPDATE dos_dealers d
        SET case_ref = n.ref
        FROM numbered n
        WHERE d.id = n.id
        """
    )
    op.create_unique_constraint("uq_dos_dealers_case_ref", "dos_dealers", ["case_ref"])


def downgrade() -> None:
    op.drop_constraint("uq_dos_dealers_case_ref", "dos_dealers", type_="unique")
    op.drop_column("dos_dealers", "case_ref")
