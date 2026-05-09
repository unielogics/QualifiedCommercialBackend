"""Manual prequal creation — adds client_id + manual_credit_override.

Revision ID: 0036
Revises: 0035
Create Date: 2026-05-09

Two columns let super-admin / underwriter create a prequal without
the borrower being on the system yet:

  client_id              FK to clients(id). For borrower-submitted
                         requests this stays NULL (the requester_id +
                         user.client linkage is enough). For
                         admin-created requests this IS the linkage —
                         the request is parented to a Client row but
                         not yet a User account.

  manual_credit_override JSONB carrying the FICO + property_count +
                         has_year_of_ownership the admin entered.
                         The approve path reads this when no real
                         CreditSummary exists, so the LTV math still
                         runs correctly.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "prequal_requests",
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "prequal_requests",
        sa.Column(
            "manual_credit_override",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_prequal_requests_client",
        "prequal_requests",
        ["client_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_prequal_requests_client", table_name="prequal_requests")
    op.drop_column("prequal_requests", "manual_credit_override")
    op.drop_column("prequal_requests", "client_id")
