"""AIChatMessage actions/attachments + Loan.intake_complete_at.

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-08

The "conversational doc collector" needs structured CTAs and file
attachments on chat messages so the borrower's chat thread can
render upload buttons inline ("Upload Bank Statements") and accept
files dropped directly into the composer.

Two nullable JSONB columns on `ai_chat_messages`:
  - `actions`     — list[ChatAction], buttons under an assistant
                    bubble. Always None for user messages.
  - `attachments` — list[ChatAttachment], files riding the message.
                    User messages carry uploads from the composer;
                    assistant messages may reference docs they
                    scanned/produced.

One nullable timestamp on `loans`:
  - `intake_complete_at` — set by the AI's `complete_property_intake`
                           tool once beds/baths/sqft/units are
                           captured. Stops the intake nag.

All nullable, no backfill — pre-existing rows simply have NULLs.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_chat_messages",
        sa.Column("actions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "ai_chat_messages",
        sa.Column("attachments", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "loans",
        sa.Column("intake_complete_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("loans", "intake_complete_at")
    op.drop_column("ai_chat_messages", "attachments")
    op.drop_column("ai_chat_messages", "actions")
