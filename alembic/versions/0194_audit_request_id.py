"""A name the audit trails and the messages they cause can share.

Nothing in this system could say which user action produced which email or
text. There was no correlation id anywhere and no middleware to mint one, so
the only available join was a guess: same actor, same subject, adjacent
timestamps, across two tables written in one transaction.

app/request_context.py binds an id per request and per scheduler tick. This
gives the three audit trails somewhere to record it, so the message log can be
joined to its cause exactly rather than approximately.

Nullable and unbackfilled on purpose: rows written before this exist and have
no request to point at, and inventing one would be a claim rather than a
record.

Revision ID: 0194_audit_request_id
Revises: 0193_dos_debts_profile_key
"""

import sqlalchemy as sa

from alembic import op

revision = "0194_audit_request_id"
down_revision = "0193_dos_debts_profile_key"
branch_labels = None
depends_on = None

TABLES = ("bucket_activity_logs", "dos_audit_log", "activities")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("request_id", sa.String(64), nullable=True))
        op.create_index(f"ix_{table}_request_id", table, ["request_id"])


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_index(f"ix_{table}_request_id", table_name=table)
        op.drop_column(table, "request_id")
