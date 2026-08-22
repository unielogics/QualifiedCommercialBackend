"""dos rep workflows — appointments, contact shares, inbox, step 4 profile

Revision ID: 0138_dos_rep_workflows
Revises: 0137_dos_bank_consent
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

revision = "0138_dos_rep_workflows"
down_revision = "0137_dos_bank_consent"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "dos_application_profiles",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("dealer_id", PG_UUID(as_uuid=True), sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("landlord_mortgagee", sa.String(200)),
        sa.Column("guarantor_home_address", sa.String(300)),
        sa.Column("guarantor_dob", sa.Date()),
        sa.Column("selected_program", sa.String(80)),
        sa.Column("term_requested_months", sa.Integer()),
        sa.Column("collateral_description", sa.Text()),
        sa.Column("use_of_proceeds_text", sa.Text()),
        sa.Column("updated_by_user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
        sa.UniqueConstraint("dealer_id", name="uq_dos_application_profiles_dealer"),
    )
    op.create_index("ix_dos_application_profiles_dealer", "dos_application_profiles", ["dealer_id"])

    op.create_table(
        "dos_rep_appointments",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("dealer_id", PG_UUID(as_uuid=True), sa.ForeignKey("dos_dealers.id", ondelete="SET NULL")),
        sa.Column("owner_user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("calendar_event_id", PG_UUID(as_uuid=True), sa.ForeignKey("calendar_events.id", ondelete="SET NULL")),
        sa.Column("kind", sa.String(32), nullable=False, server_default="callback"),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_min", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("timezone", sa.String(80), nullable=False, server_default="America/New_York"),
        sa.Column("invitee_name", sa.String(160), nullable=False),
        sa.Column("invitee_email", sa.String(320)),
        sa.Column("invitee_phone", sa.String(32)),
        sa.Column("join_url", sa.String(500)),
        sa.Column("notes", sa.Text()),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("booked_by_user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
    )
    op.create_index("ix_dos_rep_appointments_dealer", "dos_rep_appointments", ["dealer_id", "starts_at"])
    op.create_index("ix_dos_rep_appointments_owner", "dos_rep_appointments", ["owner_user_id", "starts_at"])
    op.create_index("ix_dos_rep_appointments_event", "dos_rep_appointments", ["calendar_event_id"])

    op.create_table(
        "dos_underwriting_review_preferences",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("dealer_id", PG_UUID(as_uuid=True), sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rep_user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("timezone", sa.String(80), nullable=False, server_default="America/New_York"),
        sa.Column("slots", JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("selected_slot_at", sa.DateTime(timezone=True)),
        sa.Column("selected_by_user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("appointment_id", PG_UUID(as_uuid=True), sa.ForeignKey("dos_rep_appointments.id", ondelete="SET NULL")),
        *_timestamps(),
    )
    op.create_index("ix_dos_uw_review_prefs_dealer", "dos_underwriting_review_preferences", ["dealer_id", "submitted_at"])
    op.create_index("ix_dos_uw_review_prefs_status", "dos_underwriting_review_preferences", ["status"])

    op.create_table(
        "dos_rep_contacts",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("dealer_id", PG_UUID(as_uuid=True), sa.ForeignKey("dos_dealers.id", ondelete="SET NULL")),
        sa.Column("full_name", sa.String(160), nullable=False),
        sa.Column("company", sa.String(180)),
        sa.Column("email", sa.String(320)),
        sa.Column("phone_e164", sa.String(20)),
        sa.Column("source", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.Column("sms_transactional_consented_at", sa.DateTime(timezone=True)),
        sa.Column("sms_marketing_consented_at", sa.DateTime(timezone=True)),
        sa.Column("sms_consent_meta", JSONB()),
        sa.Column("sms_opted_out_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index("ix_dos_rep_contacts_owner", "dos_rep_contacts", ["owner_user_id", "last_activity_at"])
    op.create_index("ix_dos_rep_contacts_email", "dos_rep_contacts", ["owner_user_id", "email"])
    op.create_index("ix_dos_rep_contacts_phone", "dos_rep_contacts", ["owner_user_id", "phone_e164"])

    op.create_table(
        "dos_rep_contact_shares",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("contact_id", PG_UUID(as_uuid=True), sa.ForeignKey("dos_rep_contacts.id", ondelete="SET NULL")),
        sa.Column("dealer_id", PG_UUID(as_uuid=True), sa.ForeignKey("dos_dealers.id", ondelete="SET NULL")),
        sa.Column("recipient_name", sa.String(160), nullable=False),
        sa.Column("recipient_email", sa.String(320)),
        sa.Column("recipient_phone_e164", sa.String(20)),
        sa.Column("channel", sa.String(24), nullable=False, server_default="email"),
        sa.Column("card_token", sa.String(48), nullable=False),
        sa.Column("subject", sa.String(180), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("email_status", sa.String(24), nullable=False, server_default="not_requested"),
        sa.Column("sms_status", sa.String(24), nullable=False, server_default="not_requested"),
        sa.Column("provider_refs", JSONB()),
        sa.Column("created_by_user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
    )
    op.create_index("ix_dos_rep_contact_shares_owner", "dos_rep_contact_shares", ["owner_user_id", "created_at"])
    op.create_index("ix_dos_rep_contact_shares_contact", "dos_rep_contact_shares", ["contact_id"])
    op.create_index("ix_dos_rep_contact_shares_token", "dos_rep_contact_shares", ["card_token"], unique=True)

    op.create_table(
        "dos_rep_inbox_threads",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("contact_id", PG_UUID(as_uuid=True), sa.ForeignKey("dos_rep_contacts.id", ondelete="SET NULL")),
        sa.Column("dealer_id", PG_UUID(as_uuid=True), sa.ForeignKey("dos_dealers.id", ondelete="SET NULL")),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False, server_default="email"),
        sa.Column("source", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column("unread_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        *_timestamps(),
    )
    op.create_index("ix_dos_rep_inbox_threads_owner", "dos_rep_inbox_threads", ["owner_user_id", "last_message_at"])
    op.create_index("ix_dos_rep_inbox_threads_contact", "dos_rep_inbox_threads", ["contact_id"])
    op.create_index("ix_dos_rep_inbox_threads_dealer", "dos_rep_inbox_threads", ["dealer_id"])

    op.create_table(
        "dos_rep_inbox_messages",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("thread_id", PG_UUID(as_uuid=True), sa.ForeignKey("dos_rep_inbox_threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("contact_id", PG_UUID(as_uuid=True), sa.ForeignKey("dos_rep_contacts.id", ondelete="SET NULL")),
        sa.Column("dealer_id", PG_UUID(as_uuid=True), sa.ForeignKey("dos_dealers.id", ondelete="SET NULL")),
        sa.Column("direction", sa.String(12), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("subject", sa.String(200)),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(32)),
        sa.Column("provider_message_id", sa.String(160)),
        sa.Column("delivery_status", sa.String(24), nullable=False, server_default="stored"),
        sa.Column("sender", sa.String(320)),
        sa.Column("recipient", sa.String(320)),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index("ix_dos_rep_inbox_messages_thread", "dos_rep_inbox_messages", ["thread_id", "created_at"])
    op.create_index("ix_dos_rep_inbox_messages_owner", "dos_rep_inbox_messages", ["owner_user_id", "created_at"])
    op.create_index(
        "uq_dos_rep_inbox_provider_message",
        "dos_rep_inbox_messages",
        ["provider", "provider_message_id"],
        unique=True,
        postgresql_where=sa.text("provider_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_dos_rep_inbox_provider_message", table_name="dos_rep_inbox_messages")
    op.drop_index("ix_dos_rep_inbox_messages_owner", table_name="dos_rep_inbox_messages")
    op.drop_index("ix_dos_rep_inbox_messages_thread", table_name="dos_rep_inbox_messages")
    op.drop_table("dos_rep_inbox_messages")

    op.drop_index("ix_dos_rep_inbox_threads_dealer", table_name="dos_rep_inbox_threads")
    op.drop_index("ix_dos_rep_inbox_threads_contact", table_name="dos_rep_inbox_threads")
    op.drop_index("ix_dos_rep_inbox_threads_owner", table_name="dos_rep_inbox_threads")
    op.drop_table("dos_rep_inbox_threads")

    op.drop_index("ix_dos_rep_contact_shares_token", table_name="dos_rep_contact_shares")
    op.drop_index("ix_dos_rep_contact_shares_contact", table_name="dos_rep_contact_shares")
    op.drop_index("ix_dos_rep_contact_shares_owner", table_name="dos_rep_contact_shares")
    op.drop_table("dos_rep_contact_shares")

    op.drop_index("ix_dos_rep_contacts_phone", table_name="dos_rep_contacts")
    op.drop_index("ix_dos_rep_contacts_email", table_name="dos_rep_contacts")
    op.drop_index("ix_dos_rep_contacts_owner", table_name="dos_rep_contacts")
    op.drop_table("dos_rep_contacts")

    op.drop_index("ix_dos_uw_review_prefs_status", table_name="dos_underwriting_review_preferences")
    op.drop_index("ix_dos_uw_review_prefs_dealer", table_name="dos_underwriting_review_preferences")
    op.drop_table("dos_underwriting_review_preferences")

    op.drop_index("ix_dos_rep_appointments_event", table_name="dos_rep_appointments")
    op.drop_index("ix_dos_rep_appointments_owner", table_name="dos_rep_appointments")
    op.drop_index("ix_dos_rep_appointments_dealer", table_name="dos_rep_appointments")
    op.drop_table("dos_rep_appointments")

    op.drop_index("ix_dos_application_profiles_dealer", table_name="dos_application_profiles")
    op.drop_table("dos_application_profiles")
