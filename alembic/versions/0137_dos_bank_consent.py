"""dos_bank_consent — proof that a person authorised a bank connection

Section 1.4 of the Plaid MSA requires end-user consent before an end user is
sent to connect an account. There was no record of one: the flow went button
click to bank credential prompt with nothing in between, which is anomalous in
this codebase rather than typical — e-signature and the FCRA credit pull both
already refuse to proceed without stored consent.

Deliberately a sibling of dos_sms_consent rather than a reuse of it. That table
is keyed to a phone number, because SMS consent belongs to a NUMBER; this one is
keyed to the dealer file, because a bank authorisation belongs to the business
whose account is being connected. Same proof columns, different subject.

Revision ID: 0137_dos_bank_consent
Revises: 0136_dos_audit_client
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0137_dos_bank_consent"
down_revision = "0136_dos_audit_client"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dos_bank_consent",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dealer_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("granted", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("method", sa.String(24), nullable=False),
        # What was actually on screen, and proof of it. The hash is what makes
        # this auditable: it pins the wording independently of any later edit.
        sa.Column("disclosure_version", sa.String(24), nullable=False),
        sa.Column("disclosure_hash", sa.String(64), nullable=False),
        sa.Column("disclosure_text", sa.Text(), nullable=False),
        # Who was in the room, and from where. The public client room
        # authenticates the ROOM, not a person, so consenter_name is often the
        # only human identity captured.
        sa.Column(
            "captured_by_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("captured_by_name", sa.String(120)),
        sa.Column("consenter_name", sa.String(160)),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("user_agent", sa.String(400)),
        # Revocation is a new state on the same row, so the grant is never
        # erased — "they consented, then withdrew" is the auditable history.
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_reason", sa.String(120)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # The gate asks one question on every link-token request: does this dealer
    # have a live consent? Index for that, newest first.
    op.create_index(
        "ix_dos_bank_consent_dealer",
        "dos_bank_consent",
        ["dealer_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_dos_bank_consent_dealer", table_name="dos_bank_consent")
    op.drop_table("dos_bank_consent")
