"""dealer-os: proposing a payment shift creates its Plan action.

A proposed shift IS an instruction to the client ("call the vendor,
request the payment-date move") — it must appear on the Plan of Action,
per vendor, the moment the team proposes it. The link column keeps the
pair in sync through the shift lifecycle (done -> action done,
dismissed/pulled back -> open action removed) and makes re-proposals
idempotent.

Revision ID: 0122_dos_shift_plan_link
Revises: 0121_dos_shift_direction
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0122_dos_shift_plan_link"
down_revision = "0121_dos_shift_direction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_payment_shifts",
        sa.Column("plan_action_id", pg.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_dos_payment_shifts_plan_action",
        "dos_payment_shifts",
        "dos_plan_actions",
        ["plan_action_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_dos_payment_shifts_plan_action", "dos_payment_shifts", type_="foreignkey"
    )
    op.drop_column("dos_payment_shifts", "plan_action_id")
