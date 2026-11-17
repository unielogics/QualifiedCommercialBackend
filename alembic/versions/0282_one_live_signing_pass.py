"""One live signing pass per submitted version, kept by the database.

The service already serializes signing on the file's lock; this makes the
rule something the table itself refuses to break: a version has at most one
signing pass that still counts (in progress or executed). A voided pass does
not count, and a reopened application is signed as a new version.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0282_one_live_signing_pass"
down_revision = "0281_form_signing"
branch_labels = None
depends_on = None

INDEX = "uq_form_signing_sessions_live_version"


def upgrade() -> None:
    op.create_index(
        INDEX, "form_signing_sessions", ["version_id"], unique=True,
        postgresql_where=sa.text("status in ('in_progress','executed')"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="form_signing_sessions")
