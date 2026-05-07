"""Calendar v2 — status / source / owner / external-ref columns + idempotency
index. Loan summary dirty flags. Client living-profile JSONB.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-07

What this migration does:

  calendar_events.status           VARCHAR(16) NOT NULL DEFAULT 'pending'
  calendar_events.source           VARCHAR(16) NOT NULL DEFAULT 'manual'
  calendar_events.owner_user_id    UUID FK users.id ON DELETE SET NULL
  calendar_events.external_ref_kind  VARCHAR(32) NULL
  calendar_events.external_ref_id    VARCHAR(64) NULL
  calendar_events.description      TEXT NULL

  Partial unique index on (external_ref_kind, external_ref_id) WHERE
  external_ref_kind IS NOT NULL — created CONCURRENTLY (outside the
  migration's transaction) to avoid locking the table during the
  index build.

  Companion indexes for the audience-scoping helper + dirty-drain job:
    ix_calendar_events_starts_at      (already exists or no-op)
    ix_calendar_events_loan
    ix_calendar_events_owner_status

  Living-loan-file dirty flags on `loans`:
    summary_dirty            BOOLEAN NOT NULL DEFAULT FALSE
    summary_refreshed_at     TIMESTAMPTZ NULL

  Per-client living profile on `clients`:
    living_profile           JSONB NULL
    living_summary           TEXT  NULL
    living_refreshed_at      TIMESTAMPTZ NULL

Existing rows backfill safely: every column is nullable or has a
sensible NOT NULL default. No data is destroyed.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# CONCURRENT index creation cannot run inside a transaction. Tell
# alembic to issue this one outside the migration's main txn.
# https://alembic.sqlalchemy.org/en/latest/cookbook.html#run-multiple-scripts-with-alembic-via-cli
# (we set this on the op directly when creating the index)


def upgrade() -> None:
    # ── calendar_events new columns ──────────────────────────────────
    op.add_column(
        "calendar_events",
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
    )
    op.add_column(
        "calendar_events",
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
    )
    op.add_column(
        "calendar_events",
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "calendar_events",
        sa.Column("external_ref_kind", sa.String(32), nullable=True),
    )
    op.add_column(
        "calendar_events",
        sa.Column("external_ref_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "calendar_events",
        sa.Column("description", sa.Text(), nullable=True),
    )

    op.create_index(
        "ix_calendar_events_starts_at",
        "calendar_events",
        ["starts_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_calendar_events_loan",
        "calendar_events",
        ["loan_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_calendar_events_owner_status",
        "calendar_events",
        ["owner_user_id", "status"],
        if_not_exists=True,
    )

    # Partial unique index for idempotent (kind, id) upserts. Built
    # CONCURRENTLY so we don't hold a write lock on calendar_events
    # while the index materializes. Concurrent index creation cannot
    # be inside a transaction — alembic by default runs each migration
    # in a txn, so we close + create + re-open here. The
    # `with op.get_context().autocommit_block()` pattern is the
    # alembic-blessed way to do this.
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS
              ix_calendar_events_external
              ON calendar_events (external_ref_kind, external_ref_id)
              WHERE external_ref_kind IS NOT NULL
            """
        )

    # ── loans dirty flags ─────────────────────────────────────────────
    op.add_column(
        "loans",
        sa.Column("summary_dirty", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "loans",
        sa.Column("summary_refreshed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_loans_summary_dirty",
        "loans",
        ["summary_dirty"],
        postgresql_where=sa.text("summary_dirty = true"),
        if_not_exists=True,
    )

    # ── clients living profile ────────────────────────────────────────
    op.add_column(
        "clients",
        sa.Column("living_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("living_summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("living_refreshed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("clients", "living_refreshed_at")
    op.drop_column("clients", "living_summary")
    op.drop_column("clients", "living_profile")

    op.drop_index("ix_loans_summary_dirty", table_name="loans", if_exists=True)
    op.drop_column("loans", "summary_refreshed_at")
    op.drop_column("loans", "summary_dirty")

    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_calendar_events_external")
    op.drop_index("ix_calendar_events_owner_status", table_name="calendar_events", if_exists=True)
    op.drop_index("ix_calendar_events_loan", table_name="calendar_events", if_exists=True)
    op.drop_index("ix_calendar_events_starts_at", table_name="calendar_events", if_exists=True)
    op.drop_column("calendar_events", "description")
    op.drop_column("calendar_events", "external_ref_id")
    op.drop_column("calendar_events", "external_ref_kind")
    op.drop_column("calendar_events", "owner_user_id")
    op.drop_column("calendar_events", "source")
    op.drop_column("calendar_events", "status")
