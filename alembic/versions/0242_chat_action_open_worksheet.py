"""Admit the open_worksheet chat action.

The client-facing AI now offers "Open the worksheet" beneath its message, a
button that mints a PIN-protected link to the four financial forms as one live
grid. The check constraint written in 0208 named the four action types that
existed then; without widening it the insert fails at flush inside the chat
reply, and the borrower gets no answer at all.

Revision ID: 0242_chat_action_open_worksheet
Revises: 0241_production_legal_retention
"""

from alembic import op

revision = "0242_chat_action_open_worksheet"
down_revision = "0241_production_legal_retention"
branch_labels = None
depends_on = None

TABLE = "bucket_ai_chat_actions"
NAME = "ck_bucket_ai_chat_action_type"
BEFORE = "action_type IN ('upload_own','complete_now','download_template','email_template')"
AFTER = BEFORE[:-1] + ",'open_worksheet')"


def upgrade() -> None:
    op.drop_constraint(NAME, TABLE, type_="check")
    op.create_check_constraint(NAME, TABLE, AFTER)


def downgrade() -> None:
    # A row of the new type would violate the narrower constraint; retire it
    # first so the downgrade cannot fail halfway with the constraint dropped.
    op.execute(f"DELETE FROM {TABLE} WHERE action_type = 'open_worksheet'")
    op.drop_constraint(NAME, TABLE, type_="check")
    op.create_check_constraint(NAME, TABLE, BEFORE)
