"""sms_opt_out — a number that must never be texted

dos_sms_consent records the lifecycle of a grant, and sms_consent.revoke() marks
grants revoked. But its WHERE clause only matches rows that are granted and not
yet revoked, so a STOP from a number that never granted anything matches nothing
and leaves no trace — the number stays textable. Clients on the main CRM side
have no consent rows at all, so revocation had nothing to bite on there.

This is the other half: a suppression list keyed on the number, needing no prior
grant, consulted before every outbound message from any subsystem. Together they
extend the invariant dos_sms_consent already states for dealer files to
everything — a number that opted out anywhere is unreachable everywhere.

Deliberately NOT a column on clients: the subject is a phone number, not a
person or a file. The same number can appear on several clients and on dealer
files, and opting out once has to cover all of them.

Revision ID: 0168_sms_opt_out
Revises: 0167_calendar_v2_crm_workspace
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0168_sms_opt_out"
down_revision = "0167_calendar_v2_crm_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sms_opt_out",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("phone_e164", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=120), nullable=False, server_default="STOP"),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="sms_reply"),
        sa.Column("note", sa.Text(), nullable=True),
        # NULL means the suppression is live. Rows are never deleted on
        # re-opt-in, so "they said stop, then later said start" stays provable.
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # One suppression row per number — the lookup before every send is by number
    # alone, and it must be fast and unambiguous.
    op.create_unique_constraint("uq_sms_opt_out_phone", "sms_opt_out", ["phone_e164"])
    op.create_index("ix_sms_opt_out_phone_e164", "sms_opt_out", ["phone_e164"])


def downgrade() -> None:
    op.drop_index("ix_sms_opt_out_phone_e164", table_name="sms_opt_out")
    op.drop_constraint("uq_sms_opt_out_phone", "sms_opt_out", type_="unique")
    op.drop_table("sms_opt_out")
