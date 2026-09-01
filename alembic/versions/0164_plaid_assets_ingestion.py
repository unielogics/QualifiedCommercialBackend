"""Track Plaid Asset Report ingestion into Field Desk evidence.

Revision ID: 0164_plaid_assets_ingestion
Revises: 0163_operator_account_access
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0164_plaid_assets_ingestion"
down_revision = "0163_operator_account_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plaid_asset_reports",
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "plaid_asset_reports",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_plaid_asset_reports_document",
        "plaid_asset_reports",
        "dos_documents",
        ["document_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_plaid_asset_reports_document", "plaid_asset_reports", type_="foreignkey"
    )
    op.drop_column("plaid_asset_reports", "document_id")
    op.drop_column("plaid_asset_reports", "ingested_at")
