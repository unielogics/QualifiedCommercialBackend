"""ClientAIPlan.deal_id — deal-scoped AI plans (Phase 3).

Revision ID: 0050
Revises: 0049
Create Date: 2026-05-12

Mirrors the CRS scope change in alembic 0049: a plan now belongs to
one of three scopes (client / deal / loan). Partial unique indexes
are reshaped so a client can hold one client-level plan, one plan per
deal, and one plan per loan, simultaneously, without conflict.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "client_ai_plan",
        sa.Column(
            "deal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("deals.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("ix_client_ai_plan_deal_id", "client_ai_plan", ["deal_id"])

    op.execute("DROP INDEX IF EXISTS uq_client_ai_plan_realtor")
    op.execute("DROP INDEX IF EXISTS uq_client_ai_plan_loan")
    op.execute(
        "CREATE UNIQUE INDEX uq_cap_client_only "
        "ON client_ai_plan (client_id) "
        "WHERE loan_id IS NULL AND deal_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_cap_deal_scope "
        "ON client_ai_plan (client_id, deal_id) "
        "WHERE deal_id IS NOT NULL AND loan_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_cap_loan_scope "
        "ON client_ai_plan (client_id, loan_id) "
        "WHERE loan_id IS NOT NULL AND deal_id IS NULL"
    )
    op.create_check_constraint(
        "ck_cap_single_scope",
        "client_ai_plan",
        "NOT (loan_id IS NOT NULL AND deal_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_cap_single_scope", "client_ai_plan", type_="check")
    op.execute("DROP INDEX IF EXISTS uq_cap_loan_scope")
    op.execute("DROP INDEX IF EXISTS uq_cap_deal_scope")
    op.execute("DROP INDEX IF EXISTS uq_cap_client_only")
    op.execute(
        "CREATE UNIQUE INDEX uq_client_ai_plan_realtor "
        "ON client_ai_plan (client_id) WHERE loan_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_client_ai_plan_loan "
        "ON client_ai_plan (client_id, loan_id) WHERE loan_id IS NOT NULL"
    )
    op.drop_index("ix_client_ai_plan_deal_id", table_name="client_ai_plan")
    op.drop_column("client_ai_plan", "deal_id")
