"""ClientRequirementStatus.deal_id — deal-scoped AI requirements (Phase 3).

Revision ID: 0049
Revises: 0048
Create Date: 2026-05-12

A CRS row now belongs to one of three scopes:
- Loan-level (legacy):  loan_id NOT NULL, deal_id NULL.
- Deal-level (new):     deal_id NOT NULL, loan_id NULL.
- Client-level:         loan_id NULL, deal_id NULL.

The partial unique indexes are reshaped accordingly. A CHECK constraint
enforces "at most one scope" — both columns cannot be set.

Existing rows keep loan_id; deal_id defaults to NULL, which satisfies
the new constraints.

Note: down_revision points at 0048 (Loan funding-file fields) so this
migration runs after Phase 4's loan extensions. The actual file
ordering in alembic/versions/ is: 0047 → 0048 → 0049 → 0050 → ...
Phase 4 writes 0048 in the same branch.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "client_requirement_status",
        sa.Column(
            "deal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("deals.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_client_requirement_status_deal_id",
        "client_requirement_status",
        ["deal_id"],
    )
    # Drop the two existing partial unique indexes; recreate three
    # that explicitly partition by scope.
    op.execute("DROP INDEX IF EXISTS uq_client_requirement_status_realtor")
    op.execute("DROP INDEX IF EXISTS uq_client_requirement_status_loan")
    op.execute(
        "CREATE UNIQUE INDEX uq_crs_client_only "
        "ON client_requirement_status (client_id, requirement_key) "
        "WHERE loan_id IS NULL AND deal_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_crs_deal_scope "
        "ON client_requirement_status (client_id, deal_id, requirement_key) "
        "WHERE deal_id IS NOT NULL AND loan_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_crs_loan_scope "
        "ON client_requirement_status (client_id, loan_id, requirement_key) "
        "WHERE loan_id IS NOT NULL AND deal_id IS NULL"
    )
    # At-most-one-scope check — a CRS row belongs to exactly one of
    # client / deal / loan, never both deal AND loan at the same time.
    op.create_check_constraint(
        "ck_crs_single_scope",
        "client_requirement_status",
        "NOT (loan_id IS NOT NULL AND deal_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_crs_single_scope", "client_requirement_status", type_="check")
    op.execute("DROP INDEX IF EXISTS uq_crs_loan_scope")
    op.execute("DROP INDEX IF EXISTS uq_crs_deal_scope")
    op.execute("DROP INDEX IF EXISTS uq_crs_client_only")
    op.execute(
        "CREATE UNIQUE INDEX uq_client_requirement_status_realtor "
        "ON client_requirement_status (client_id, requirement_key) WHERE loan_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_client_requirement_status_loan "
        "ON client_requirement_status (client_id, loan_id, requirement_key) WHERE loan_id IS NOT NULL"
    )
    op.drop_index("ix_client_requirement_status_deal_id", table_name="client_requirement_status")
    op.drop_column("client_requirement_status", "deal_id")
