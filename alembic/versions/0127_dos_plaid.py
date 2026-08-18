"""dealer-os: Plaid statement connections.

dos_plaid_items — one row per connected bank (Plaid Item). The access token
is encrypted at rest (Fernet, provider_secrets key-derivation recipe). The
30-day auto-refresh cadence lives in next_refresh_at; a daily scheduler tick
pulls whatever is due. dos_documents.plaid_statement_id makes statement
ingestion idempotent — each Plaid statement lands exactly once per dealer.

Revision ID: 0127_dos_plaid
Revises: 0126_dos_refi
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0127_dos_plaid"
down_revision = "0126_dos_refi"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dos_plaid_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dealer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_id", sa.String(64), nullable=False, unique=True),
        sa.Column("institution_name", sa.String(160), nullable=True),
        sa.Column("encrypted_access_token", sa.Text(), nullable=True),
        sa.Column("encryption_provider", sa.String(16), nullable=False, server_default="fernet"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("last_pulled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_refresh_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_dos_plaid_items_dealer", "dos_plaid_items", ["dealer_id"])
    op.add_column("dos_documents", sa.Column("plaid_statement_id", sa.String(64), nullable=True))
    op.create_index(
        "uq_dos_documents_plaid_stmt",
        "dos_documents",
        ["dealer_id", "plaid_statement_id"],
        unique=True,
        postgresql_where=sa.text("plaid_statement_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_dos_documents_plaid_stmt", table_name="dos_documents")
    op.drop_column("dos_documents", "plaid_statement_id")
    op.drop_index("ix_dos_plaid_items_dealer", table_name="dos_plaid_items")
    op.drop_table("dos_plaid_items")
