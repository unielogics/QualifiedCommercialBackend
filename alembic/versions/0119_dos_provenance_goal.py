"""dealer-os: source-document provenance + funding goal.

- dos_cash_events.document_id: which DealerDocument a ledger line was
  extracted from — the "reference the PDF" backbone for the Activity
  explorer and vendor drill (SET NULL so deleting a document never
  destroys ledger rows). Legacy rows stay NULL and render as "—".
- dos_tax_filings.document_id: the tax return the filing row was read from.
- dos_dealers.funding_goal / funding_purpose: how much the client is
  looking for — drives program sizing + reverse-engineered metric targets.

Revision ID: 0119_dos_provenance_goal
Revises: 0118_dos_owners_profile
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0119_dos_provenance_goal"
down_revision = "0118_dos_owners_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_cash_events",
        sa.Column("document_id", pg.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_dos_cash_events_document",
        "dos_cash_events",
        "dos_documents",
        ["document_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_dos_cash_events_dealer_document",
        "dos_cash_events",
        ["dealer_id", "document_id"],
    )
    op.create_index(
        "ix_dos_cash_events_dealer_occurred",
        "dos_cash_events",
        ["dealer_id", "occurred_on"],
    )

    op.add_column(
        "dos_tax_filings",
        sa.Column("document_id", pg.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_dos_tax_filings_document",
        "dos_tax_filings",
        "dos_documents",
        ["document_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "dos_dealers",
        sa.Column("funding_goal", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "dos_dealers",
        sa.Column("funding_purpose", sa.String(48), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dos_dealers", "funding_purpose")
    op.drop_column("dos_dealers", "funding_goal")
    op.drop_constraint("fk_dos_tax_filings_document", "dos_tax_filings", type_="foreignkey")
    op.drop_column("dos_tax_filings", "document_id")
    op.drop_index("ix_dos_cash_events_dealer_occurred", table_name="dos_cash_events")
    op.drop_index("ix_dos_cash_events_dealer_document", table_name="dos_cash_events")
    op.drop_constraint("fk_dos_cash_events_document", "dos_cash_events", type_="foreignkey")
    op.drop_column("dos_cash_events", "document_id")
