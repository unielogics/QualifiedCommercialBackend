"""Underwriter fine-tuning fields on loans.

Revision ID: 0044
Revises: 0043
Create Date: 2026-05-12

Adds the columns the Criteria-tab underwriter workbench needs to fine-tune
a loan on top of the base pricing inputs:

  • amortization_style          — fully_amortizing | interest_only
  • prepay_penalty              — DSCR prepay schedule
  • vacancy_pct                 — 0..1, reduces effective NOI for DSCR
  • expense_ratio_pct           — 0..1, reduces effective NOI for DSCR
  • reserves_required           — required cash reserves at close
  • lender_fees                 — flat lender fees rolled into HUD
  • fico_override               — UW override for the client's FICO
  • entity_type                 — borrower entity wrapper
  • experience_tier             — F&F experience bucket
  • construction_holdback_pct   — F&F / Ground Up holdback fraction
  • draw_count                  — F&F / Ground Up planned draws
  • exit_strategy               — sale / refinance / hold
  • cash_to_borrower            — Cash-Out target wire amount
  • seasoning_months            — Cash-Out title seasoning
  • property_count              — Portfolio loan property count

All nullable to keep existing rows compatible.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("loans", sa.Column("amortization_style", sa.String(24), nullable=True))
    op.add_column("loans", sa.Column("prepay_penalty", sa.String(16), nullable=True))
    op.add_column("loans", sa.Column("vacancy_pct", sa.Numeric(5, 4), nullable=True))
    op.add_column("loans", sa.Column("expense_ratio_pct", sa.Numeric(5, 4), nullable=True))
    op.add_column("loans", sa.Column("reserves_required", sa.Numeric(14, 2), nullable=True))
    op.add_column("loans", sa.Column("lender_fees", sa.Numeric(14, 2), nullable=True))
    op.add_column("loans", sa.Column("fico_override", sa.Integer(), nullable=True))
    op.add_column("loans", sa.Column("entity_type", sa.String(24), nullable=True))
    op.add_column("loans", sa.Column("experience_tier", sa.String(24), nullable=True))
    op.add_column("loans", sa.Column("construction_holdback_pct", sa.Numeric(5, 4), nullable=True))
    op.add_column("loans", sa.Column("draw_count", sa.Integer(), nullable=True))
    op.add_column("loans", sa.Column("exit_strategy", sa.String(16), nullable=True))
    op.add_column("loans", sa.Column("cash_to_borrower", sa.Numeric(14, 2), nullable=True))
    op.add_column("loans", sa.Column("seasoning_months", sa.Integer(), nullable=True))
    op.add_column("loans", sa.Column("property_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("loans", "property_count")
    op.drop_column("loans", "seasoning_months")
    op.drop_column("loans", "cash_to_borrower")
    op.drop_column("loans", "exit_strategy")
    op.drop_column("loans", "draw_count")
    op.drop_column("loans", "construction_holdback_pct")
    op.drop_column("loans", "experience_tier")
    op.drop_column("loans", "entity_type")
    op.drop_column("loans", "fico_override")
    op.drop_column("loans", "lender_fees")
    op.drop_column("loans", "reserves_required")
    op.drop_column("loans", "expense_ratio_pct")
    op.drop_column("loans", "vacancy_pct")
    op.drop_column("loans", "prepay_penalty")
    op.drop_column("loans", "amortization_style")
