"""The pre-call video the client watches before the call.

The pre-call email is where we point the client at a short video and at their
own room, so they arrive having connected their bank and run the soft pull. The
URL is a setting rather than a constant so the host can swap the video without a
deploy, and it renders through the {video} placeholder.

Revision ID: 0184_booking_precall_video
Revises: 0183_client_thread_takeover
"""

from alembic import op
import sqlalchemy as sa

revision = "0184_booking_precall_video"
down_revision = "0183_client_thread_takeover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("booking_settings", sa.Column("precall_video_url", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("booking_settings", "precall_video_url")
