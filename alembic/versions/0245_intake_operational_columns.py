"""Promote the intake facts the desk queries out of intake_state.

`intake_state` is the audit record of an AI intake: delivery receipts, legal
acceptance, chat facts, program fit, the archive marker. Two of those are
operational facts other screens need to filter on — when the file was archived
and by whom, and when the client was last sent a way in. They become columns,
backfilled from the JSON they have lived in; the JSON entries stay as the full
record.

Revision ID: 0245_intake_operational_columns
Revises: 0244_bucket_type_and_funding_category_vocabulary
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0245_intake_operational_columns"
down_revision = "0244_bucket_type_and_funding_category_vocabulary"
branch_labels = None
depends_on = None

TABLE = "public_underwriting_intakes"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        TABLE,
        sa.Column(
            "archived_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(TABLE, sa.Column("last_link_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("last_link_status", sa.String(length=32), nullable=True))
    op.add_column(TABLE, sa.Column("last_link_kind", sa.String(length=24), nullable=True))
    op.create_index("ix_public_underwriting_intakes_archived_at", TABLE, ["archived_at"])

    # The archive marker, where one was written.
    op.execute(
        sa.text(
            f"""
            UPDATE {TABLE}
               SET archived_at = NULLIF(intake_state->'_archive'->>'archived_at', '')::timestamptz
             WHERE intake_state ? '_archive'
               AND NULLIF(intake_state->'_archive'->>'archived_at', '') IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE {TABLE} AS i
               SET archived_by_user_id = (intake_state->'_archive'->>'archived_by_user_id')::uuid
             WHERE intake_state ? '_archive'
               AND (intake_state->'_archive'->>'archived_by_user_id') ~ '^[0-9a-f-]{{36}}$'
               AND EXISTS (SELECT 1 FROM users u WHERE u.id = (i.intake_state->'_archive'->>'archived_by_user_id')::uuid)
            """
        )
    )

    # The most recent link delivery of either kind.
    op.execute(
        sa.text(
            f"""
            WITH deliveries AS (
                SELECT id,
                       NULLIF(intake_state->'resume_email'->>'sent_at', '')::timestamptz AS sent_at,
                       intake_state->'resume_email'->>'status' AS status,
                       'resume' AS kind
                  FROM {TABLE}
                 WHERE NULLIF(intake_state->'resume_email'->>'sent_at', '') IS NOT NULL
                UNION ALL
                SELECT id,
                       NULLIF(intake_state->'dealer_login_email'->>'sent_at', '')::timestamptz,
                       intake_state->'dealer_login_email'->>'status',
                       'login'
                  FROM {TABLE}
                 WHERE NULLIF(intake_state->'dealer_login_email'->>'sent_at', '') IS NOT NULL
            ),
            latest AS (
                SELECT DISTINCT ON (id) id, sent_at, status, kind
                  FROM deliveries
                 ORDER BY id, sent_at DESC
            )
            UPDATE {TABLE} AS i
               SET last_link_sent_at = latest.sent_at,
                   last_link_status = LEFT(latest.status, 32),
                   last_link_kind = latest.kind
              FROM latest
             WHERE latest.id = i.id
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_public_underwriting_intakes_archived_at", table_name=TABLE)
    op.drop_column(TABLE, "last_link_kind")
    op.drop_column(TABLE, "last_link_status")
    op.drop_column(TABLE, "last_link_sent_at")
    op.drop_column(TABLE, "archived_by_user_id")
    op.drop_column(TABLE, "archived_at")
