"""Two-way Google Calendar sync columns on calendar_events.

Adds the Google linkage + loop-guard fields:
- google_event_id / google_calendar_id — the Google event this row mirrors.
- google_etag + sync_origin — loop guard: a pulled change whose etag matches
  what we last pushed is our own echo and is skipped.
- synced_at — last successful sync timestamp.

(google_accounts.calendar_sync_token was already added in 0093.)

Revision ID: 0094_calendar_google_sync
Revises: 0093_google_accounts
Create Date: 2026-07-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0094_calendar_google_sync"
down_revision = "0093_google_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("calendar_events", sa.Column("google_event_id", sa.String(length=255), nullable=True))
    op.add_column("calendar_events", sa.Column("google_calendar_id", sa.String(length=255), nullable=True))
    op.add_column("calendar_events", sa.Column("google_etag", sa.String(length=255), nullable=True))
    op.add_column("calendar_events", sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("calendar_events", sa.Column("sync_origin", sa.String(length=16), nullable=True))
    op.create_index("ix_calendar_events_google_event_id", "calendar_events", ["google_event_id"])
    # One internal row per (owner, google event). Google shares an event id across
    # all attendees, so uniqueness must be per-user, not global. Partial (only rows
    # actually linked to Google) so it never constrains normal internal events.
    op.create_index(
        "uq_calendar_events_owner_google_event",
        "calendar_events",
        ["owner_user_id", "google_event_id"],
        unique=True,
        postgresql_where=sa.text("google_event_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_calendar_events_owner_google_event", table_name="calendar_events")
    op.drop_index("ix_calendar_events_google_event_id", table_name="calendar_events")
    op.drop_column("calendar_events", "sync_origin")
    op.drop_column("calendar_events", "synced_at")
    op.drop_column("calendar_events", "google_etag")
    op.drop_column("calendar_events", "google_calendar_id")
    op.drop_column("calendar_events", "google_event_id")
