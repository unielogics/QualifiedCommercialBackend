"""Client.lead_intake / checklist_overrides / ai_cadence_override.

Revision ID: 0025
Revises: 0024
Create Date: 2026-05-08

Three JSONB columns to support the realtor's lead-magnet flow:

  lead_intake          — captured by the New Lead wizard's property
                         + financial context steps. Buyer leads carry
                         target price / area / timeline; seller leads
                         carry subject property + asking price.
  checklist_overrides  — per-lead disable / extras for the agent's
                         buyer/seller checklist. Applied at lead →
                         loan promotion (kickoff_loan).
  ai_cadence_override  — per-lead reminder cadence (first/second/
                         escalate days). Applied at promotion.

All nullable JSONB. No backfill needed — existing leads default to
NULL = no per-lead overrides; agent's broker_settings.cadence applies.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("lead_intake", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("checklist_overrides", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("ai_cadence_override", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("clients", "ai_cadence_override")
    op.drop_column("clients", "checklist_overrides")
    op.drop_column("clients", "lead_intake")
