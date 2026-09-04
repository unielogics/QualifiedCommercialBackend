"""More than one video, each insertable into any message.

The first cut carried a single pre-call video. A host wants a small library —
one explaining the bank connection, another the soft credit check, another for a
particular program — and to drop whichever one fits into a given email or text.

Each video keeps a stable key, and that key is what a message references, so
renaming or re-recording a video does not silently break every template that
points at it. The existing single URL becomes the first entry in the list, which
keeps {video} rendering exactly what it rendered yesterday.

Revision ID: 0186_booking_video_library
Revises: 0185_provenance_trail
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0186_booking_video_library"
down_revision = "0185_provenance_trail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "booking_settings",
        sa.Column(
            "precall_videos",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # Anyone who already set a video keeps it, as the primary entry: {video}
    # resolves to the first in the list, so their templates do not change.
    op.execute(
        """
        UPDATE booking_settings
           SET precall_videos = jsonb_build_array(
                 jsonb_build_object('key', 'intro', 'label', 'Before your call', 'url', precall_video_url)
               )
         WHERE precall_video_url IS NOT NULL
           AND btrim(precall_video_url) <> ''
        """
    )


def downgrade() -> None:
    # The primary survives in precall_video_url, which was never dropped.
    op.drop_column("booking_settings", "precall_videos")
