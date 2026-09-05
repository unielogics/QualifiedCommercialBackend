"""One debt schedule per file, whether or not the file has a dealer.

There were two debt schedules that never spoke to each other: the on-screen form
that rendered a PDF and discarded its rows, and `dos_debts`, which is the real
one — DSCR denominator, confirmation workflow, refinance workbench. A borrower
could fill in the form and the DSCR would never know.

`dos_debts` could not simply become the single store because it is keyed to
`dealer_id`, and only 8 of 45 application profiles have a dealer. So it is
keyed to the profile instead, which is a strict generalisation:
`application_profiles.dealer_id` is uniquely constrained, so every dealer maps
to exactly one profile and the backfill is unambiguous.

`dealer_id` stays, nullable, because the rep app, the refinance workbench and
the vendor rollup all still read it. Nothing is being taken away here; a second
way in is being added and made the primary one.

Six rows exist. If this ever needs reversing, that is the whole blast radius.

Revision ID: 0193_dos_debts_profile_key
Revises: 0192_financial_form_links
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0193_dos_debts_profile_key"
down_revision = "0192_financial_form_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_debts",
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_profiles.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("ix_dos_debts_profile", "dos_debts", ["profile_id"])

    # Unambiguous: application_profiles.dealer_id carries a unique constraint,
    # so a dealer resolves to at most one profile.
    op.execute(
        """
        UPDATE dos_debts d
           SET profile_id = p.id
          FROM application_profiles p
         WHERE p.dealer_id = d.dealer_id
        """
    )

    # A row can now belong to a file that has no dealer record.
    op.alter_column("dos_debts", "dealer_id", existing_type=postgresql.UUID(), nullable=True)

    # The de-duplication law moves with the key: one row per drafted obligation
    # per FILE. Still partial, because hand-added rows carry no vendor key.
    op.execute("DROP INDEX IF EXISTS uq_dos_debt_vendor")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_dos_debt_vendor
        ON dos_debts (profile_id, vendor_key)
        WHERE vendor_key IS NOT NULL AND profile_id IS NOT NULL;
        """
    )
    # Rows that predate a profile keep their old guarantee rather than losing it.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_dos_debt_vendor_dealer
        ON dos_debts (dealer_id, vendor_key)
        WHERE vendor_key IS NOT NULL AND profile_id IS NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_dos_debt_vendor_dealer")
    op.execute("DROP INDEX IF EXISTS uq_dos_debt_vendor")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_dos_debt_vendor
        ON dos_debts (dealer_id, vendor_key)
        WHERE vendor_key IS NOT NULL;
        """
    )
    # Rows written against a profile with no dealer cannot survive the reversal.
    op.execute("DELETE FROM dos_debts WHERE dealer_id IS NULL")
    op.alter_column("dos_debts", "dealer_id", existing_type=postgresql.UUID(), nullable=False)
    op.drop_index("ix_dos_debts_profile", table_name="dos_debts")
    op.drop_column("dos_debts", "profile_id")
