"""The offer acceptance as a record: when, and which terms.

The accepted amount was the only trace of the client's acceptance. The
profile now also keeps when it was recorded and which terms it answered
(kind, id, version, amount, rate, term, label), so the Underwriting tab can
say "accepted v1 on Nov 4" and notice when the terms moved on.

Revision ID: 0257_offer_acceptance
Revises: 0256_closing_stipulations
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0257_offer_acceptance"
down_revision = "0256_closing_stipulations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_profiles",
        sa.Column("underwriting_accepted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "application_profiles",
        sa.Column("accepted_terms_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("application_profiles", "accepted_terms_ref")
    op.drop_column("application_profiles", "underwriting_accepted_at")
