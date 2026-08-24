"""Link completed public signing sessions to executed agreements.

Revision ID: 0146_public_contract_sign_idempotency
Revises: 0145_plaid_lifecycle_assets
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0146_public_contract_sign_idempotency"
down_revision = "0145_plaid_lifecycle_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "public_contract_sign_sessions",
        sa.Column(
            "agreement_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contract_agreements.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_public_contract_sign_sessions_agreement_id",
        "public_contract_sign_sessions",
        ["agreement_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_public_contract_sign_sessions_agreement_id",
        table_name="public_contract_sign_sessions",
    )
    op.drop_column("public_contract_sign_sessions", "agreement_id")
