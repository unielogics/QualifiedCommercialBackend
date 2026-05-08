"""Client.stage + funnel fields (alembic 0024).

Revision ID: 0024
Revises: 0023
Create Date: 2026-05-08

Adds the per-client lead funnel state the AgentHomeView KPIs and the
LeadsPipelineView kanban depend on. Five new columns:

  stage               (lead | contacted | verified | ready_for_lending |
                       processing | funded | lost)
  client_type         (buyer | seller, NULL = unknown)
  contacted_at        (timestamptz; first outreach)
  intake_started_at   (timestamptz; SmartIntake begun)
  intake_completed_at (timestamptz; SmartIntake submitted)

Backfill order (strict — first match wins):
  1. funded               if funded_count > 0
  2. processing           if any loan with stage NOT IN
                          ('prequalified', 'funded')
  3. ready_for_lending    if any loan with stage = 'prequalified'
  4. lead                 otherwise

Earlier draft had rules in reverse order which would mis-tag a client
with one funded + one prequal loan as 'ready_for_lending'.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column(
            "stage", sa.String(length=32),
            nullable=False, server_default="lead",
        ),
    )
    op.add_column(
        "clients",
        sa.Column("client_type", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("contacted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("intake_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("intake_completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Strict-order backfill. Each rule overwrites the prior one only
    # if its condition matches; we run them low → high so the highest-
    # qualifying rule wins per client.
    #
    # Rule 1 last: funded_count > 0  → 'funded'
    # Rule 2: any non-prequal/non-funded loan → 'processing'
    # Rule 3 first: any prequalified loan → 'ready_for_lending'
    # Rule 4 (default already set by server_default 'lead')
    op.execute("""
        UPDATE clients SET stage = 'ready_for_lending'
        WHERE id IN (
            SELECT DISTINCT client_id FROM loans
            WHERE stage = 'prequalified'
        )
    """)
    op.execute("""
        UPDATE clients SET stage = 'processing'
        WHERE id IN (
            SELECT DISTINCT client_id FROM loans
            WHERE stage NOT IN ('prequalified', 'funded')
        )
    """)
    op.execute("""
        UPDATE clients SET stage = 'funded'
        WHERE funded_count > 0
    """)


def downgrade() -> None:
    op.drop_column("clients", "intake_completed_at")
    op.drop_column("clients", "intake_started_at")
    op.drop_column("clients", "contacted_at")
    op.drop_column("clients", "client_type")
    op.drop_column("clients", "stage")
