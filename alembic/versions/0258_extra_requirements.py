"""Requirements a desk added to one file from the library.

A file's checklist came only from its evidence policy and its selected programs.
When the underwriter needs one more document, or one of the online forms, for
one file, there was nowhere to put it that the readiness engine would keep. The
profile now lists the requirements added to it, each pinned to the library row it
was copied from, so they are materialized, collected, reviewed and emailed like
any checklist item.

Revision ID: 0258_extra_requirements
Revises: 0257_offer_acceptance
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0258_extra_requirements"
down_revision = "0257_offer_acceptance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_profiles",
        sa.Column(
            "extra_requirements",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("application_profiles", "extra_requirements")
