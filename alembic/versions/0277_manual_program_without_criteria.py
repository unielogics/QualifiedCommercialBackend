"""Allow a catalog choice before its program criteria are published.

Revision ID: 0277_manual_program_criteria
Revises: 0276_structured_deal_budget
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0277_manual_program_criteria"
down_revision = "0276_structured_deal_budget"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("application_program_selections", "playbook_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.alter_column("application_program_selections", "playbook_version", existing_type=sa.Integer(), nullable=True)
    op.create_check_constraint("ck_application_program_criteria_pair", "application_program_selections", "(playbook_id IS NULL) = (playbook_version IS NULL)")
    op.create_check_constraint("ck_application_program_auto_criteria", "application_program_selections", "source = 'operator' OR playbook_id IS NOT NULL")


def downgrade():
    # Never fabricate criteria or delete an operator's catalog selection to
    # restore the old constraint. A downgrade is safe only after those choices
    # have been explicitly reconciled to real published criteria.
    remaining = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM application_program_selections WHERE playbook_id IS NULL OR playbook_version IS NULL"
    )).scalar_one()
    if remaining:
        raise RuntimeError("Reconcile program selections without published criteria before downgrading; selections were retained.")
    op.drop_constraint("ck_application_program_auto_criteria", "application_program_selections", type_="check")
    op.drop_constraint("ck_application_program_criteria_pair", "application_program_selections", type_="check")
    op.alter_column("application_program_selections", "playbook_version", existing_type=sa.Integer(), nullable=False)
    op.alter_column("application_program_selections", "playbook_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
