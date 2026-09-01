"""booking_settings.reminder_sms_messages — what each reminder actually says

The reminder schedule already supports several SMS reminders per booking, but
every one of them sent the same hardcoded sentence. A reminder a day out and one
an hour out are doing different jobs, and the person sending them should be able
to say so.

Keyed by minutes-before rather than positioned in a list beside
reminder_sms_minutes: parallel arrays drift the moment a reminder is removed
from the middle, and minutes are already unique within a schedule (the settings
schema dedupes them). An entry with no message falls back to the default
wording, so an operator only writes the ones they care about, and a key left
behind by a deleted reminder is inert rather than wrong.

Revision ID: 0172_reminder_sms_messages
Revises: 0171_shared_calendar_outcomes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0172_reminder_sms_messages"
down_revision = "0171_shared_calendar_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "booking_settings",
        sa.Column(
            "reminder_sms_messages",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("booking_settings", "reminder_sms_messages")
