"""dos_sms_consent: proof of SMS opt-in

Carriers and the TCPA both treat consent as evidence you have to produce, not a
setting you toggle, so this stores the wording shown, a hash of it, who was in
the room, from where, and when. Revocation is a new state on the same row so the
grant is never destroyed.

Revision ID: 0131_dos_sms_consent
Revises: 0130_dos_rep_leads
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0131_dos_sms_consent"
down_revision = "0130_dos_rep_leads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dos_sms_consent",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dealer_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phone_e164", sa.String(20), nullable=False),
        sa.Column("consent_kind", sa.String(16), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("method", sa.String(24), nullable=False),
        sa.Column("disclosure_version", sa.String(24), nullable=False),
        sa.Column("disclosure_hash", sa.String(64), nullable=False),
        sa.Column("disclosure_text", sa.Text(), nullable=False),
        sa.Column(
            "captured_by_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("captured_by_name", sa.String(120)),
        sa.Column("consenter_name", sa.String(160)),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("user_agent", sa.String(400)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_reason", sa.String(120)),
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
    op.create_index("ix_dos_sms_consent_dealer", "dos_sms_consent", ["dealer_id"])
    op.create_index(
        "ix_dos_sms_consent_phone_kind", "dos_sms_consent", ["phone_e164", "consent_kind"]
    )


def downgrade() -> None:
    op.drop_index("ix_dos_sms_consent_phone_kind", table_name="dos_sms_consent")
    op.drop_index("ix_dos_sms_consent_dealer", table_name="dos_sms_consent")
    op.drop_table("dos_sms_consent")
