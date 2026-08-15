"""Dealer OS — consolidated document hub (ZIP parents + AI classification).

dos_documents grows three nullable columns:
- parent_document_id: children of an expanded ZIP archive point at their
  parent row (CASCADE — deleting the parent removes the expansion).
- detected_kind: the AI-classified document type (bank_statement, tax_return,
  profit_and_loss, balance_sheet, debt_schedule, credit_report, other,
  archive). The uploader-declared `kind` column stays untouched.
- doc_meta: compact classifier payload summary (tax years, P&L months, debts).

Revision ID: 0114_dos_doc_hub
Revises: 0113_dos_dealer_address
Create Date: 2026-08-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0114_dos_doc_hub"
down_revision = "0113_dos_dealer_address"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_documents",
        sa.Column("parent_document_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_dos_documents_parent",
        "dos_documents",
        "dos_documents",
        ["parent_document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.add_column("dos_documents", sa.Column("detected_kind", sa.String(24), nullable=True))
    op.add_column("dos_documents", sa.Column("doc_meta", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("dos_documents", "doc_meta")
    op.drop_column("dos_documents", "detected_kind")
    op.drop_constraint("fk_dos_documents_parent", "dos_documents", type_="foreignkey")
    op.drop_column("dos_documents", "parent_document_id")
