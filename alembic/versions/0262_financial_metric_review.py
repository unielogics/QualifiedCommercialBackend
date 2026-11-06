"""Retain audited, source-bound financial calculation corrections."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0262_financial_metric_review"
down_revision = "0261_real_estate_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("application_profiles", sa.Column("financial_metric_review", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM application_profiles
        WHERE financial_metric_review <> '{}'::jsonb) THEN
        RAISE EXCEPTION 'Financial correction history exists; retain it before downgrading';
        END IF; END; $$""")
    op.drop_column("application_profiles", "financial_metric_review")
