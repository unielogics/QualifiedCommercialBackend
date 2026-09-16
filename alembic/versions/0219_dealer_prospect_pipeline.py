"""Add the Field Desk dealer prospect pipeline.

Revision ID: 0219_dealer_prospect_pipeline
Revises: 0218_combined_offer_deliveries
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0219_dealer_prospect_pipeline"
down_revision = "0218_combined_offer_deliveries"
branch_labels = None
depends_on = None


_STAGES = (
    ("2d6a5305-f06d-4bca-9816-470e85f8ec01", "new", "New", 0, False),
    ("2d6a5305-f06d-4bca-9816-470e85f8ec02", "emailed", "Emailed", 10, False),
    ("2d6a5305-f06d-4bca-9816-470e85f8ec03", "follow_up_1", "Follow-up 1", 20, False),
    ("2d6a5305-f06d-4bca-9816-470e85f8ec04", "follow_up_2", "Follow-up 2", 30, False),
    ("2d6a5305-f06d-4bca-9816-470e85f8ec05", "booked", "Booked", 40, False),
    ("2d6a5305-f06d-4bca-9816-470e85f8ec06", "converted", "Converted", 50, True),
    ("2d6a5305-f06d-4bca-9816-470e85f8ec07", "not_interested", "Not interested", 60, True),
)


def upgrade() -> None:
    op.create_table(
        "dealer_prospect_stage_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_terminal", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "behavior", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("key", name="uq_dealer_prospect_stage_key"),
    )
    op.create_index(
        "ix_dealer_prospect_stage_order",
        "dealer_prospect_stage_definitions",
        ["is_active", "sort_order"],
    )

    op.create_table(
        "dealer_prospect_outcome_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "action_config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("key", name="uq_dealer_prospect_outcome_key"),
    )
    op.create_index(
        "ix_dealer_prospect_outcome_order",
        "dealer_prospect_outcome_definitions",
        ["is_active", "sort_order"],
    )

    op.create_table(
        "dealer_prospects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dos_rep_companies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "primary_contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dos_rep_contacts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "stage_definition_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dealer_prospect_stage_definitions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "last_outcome_definition_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dealer_prospect_outcome_definitions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "appointment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dos_rep_appointments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "converted_intake_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("public_underwriting_intakes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("email_normalized", sa.String(length=320), nullable=False),
        sa.Column("phone_normalized", sa.String(length=20), nullable=False),
        sa.Column("dealer_name_normalized", sa.String(length=180), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="quick_add"),
        sa.Column("next_follow_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("call_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("do_not_contact", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("do_not_contact_reason", sa.String(length=240), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "archived_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("version > 0", name="ck_dealer_prospect_version_positive"),
        sa.UniqueConstraint("primary_contact_id", name="uq_dealer_prospect_primary_contact"),
    )
    op.create_index(
        "ix_dealer_prospect_owner_stage",
        "dealer_prospects",
        ["owner_user_id", "stage_definition_id"],
    )
    op.create_index("ix_dealer_prospect_follow_up", "dealer_prospects", ["next_follow_up_at"])
    op.create_index("ix_dealer_prospect_activity", "dealer_prospects", ["last_activity_at"])
    op.create_index("ix_dealer_prospect_company", "dealer_prospects", ["company_id"])
    op.create_index(
        "ix_dealer_prospect_last_outcome", "dealer_prospects", ["last_outcome_definition_id"]
    )
    op.create_index(
        "uq_dealer_prospect_email_active",
        "dealer_prospects",
        ["dealer_name_normalized", "email_normalized"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL AND email_normalized IS NOT NULL"),
    )
    op.create_index(
        "uq_dealer_prospect_phone_active",
        "dealer_prospects",
        ["dealer_name_normalized", "phone_normalized"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL AND phone_normalized IS NOT NULL"),
    )

    op.create_table(
        "dealer_prospect_activities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "prospect_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dealer_prospects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_dealer_prospect_activity_prospect",
        "dealer_prospect_activities",
        ["prospect_id", "created_at"],
    )
    op.create_index(
        "ix_dealer_prospect_activity_actor",
        "dealer_prospect_activities",
        ["actor_user_id", "created_at"],
    )

    stage_table = sa.table(
        "dealer_prospect_stage_definitions",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("key", sa.String()),
        sa.column("label", sa.String()),
        sa.column("sort_order", sa.Integer()),
        sa.column("is_active", sa.Boolean()),
        sa.column("is_terminal", sa.Boolean()),
        sa.column("is_system", sa.Boolean()),
        sa.column("behavior", postgresql.JSONB()),
    )
    op.bulk_insert(
        stage_table,
        [
            {
                "id": uuid.UUID(row_id),
                "key": key,
                "label": label,
                "sort_order": sort_order,
                "is_active": True,
                "is_terminal": is_terminal,
                "is_system": True,
                "behavior": {},
            }
            for row_id, key, label, sort_order, is_terminal in _STAGES
        ],
    )

    outcome_table = sa.table(
        "dealer_prospect_outcome_definitions",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("key", sa.String()),
        sa.column("label", sa.String()),
        sa.column("sort_order", sa.Integer()),
        sa.column("is_active", sa.Boolean()),
        sa.column("is_system", sa.Boolean()),
        sa.column("action_config", postgresql.JSONB()),
    )
    outcomes = (
        (
            "3f55d826-3be7-44dd-92cb-09461c15aa01",
            "not_connected",
            "Not connected",
            0,
            {
                "stage_strategy": "advance_follow_up",
                "increment_call_attempt": True,
                "follow_up_delay_hours": 24,
                "email_action": "missed_call",
            },
        ),
        (
            "3f55d826-3be7-44dd-92cb-09461c15aa02",
            "call_back",
            "Not available / call back",
            10,
            {
                "increment_call_attempt": True,
                "requires_follow_up": True,
                "email_action": "callback_confirmation",
            },
        ),
        (
            "3f55d826-3be7-44dd-92cb-09461c15aa03",
            "wants_to_book",
            "Wants to book",
            20,
            {"workflow_action": "book_appointment", "email_action": "booking_link"},
        ),
        (
            "3f55d826-3be7-44dd-92cb-09461c15aa04",
            "booked",
            "Booked",
            30,
            {"target_stage_key": "booked", "requires_appointment": True},
        ),
        (
            "3f55d826-3be7-44dd-92cb-09461c15aa05",
            "interested_send_information",
            "Interested / send information",
            40,
            {"target_stage_key": "emailed", "email_action": "dealer_information_pack"},
        ),
        (
            "3f55d826-3be7-44dd-92cb-09461c15aa06",
            "not_interested",
            "Not interested",
            50,
            {
                "target_stage_key": "not_interested",
                "set_do_not_contact": True,
                "clear_follow_up": True,
            },
        ),
        (
            "3f55d826-3be7-44dd-92cb-09461c15aa07",
            "bad_contact_unsubscribe",
            "Bad contact / unsubscribe",
            60,
            {"set_do_not_contact": True, "clear_follow_up": True, "suppress_email": True},
        ),
    )
    op.bulk_insert(
        outcome_table,
        [
            {
                "id": uuid.UUID(row_id),
                "key": key,
                "label": label,
                "sort_order": sort_order,
                "is_active": True,
                "is_system": True,
                "action_config": action_config,
            }
            for row_id, key, label, sort_order, action_config in outcomes
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_dealer_prospect_activity_actor", table_name="dealer_prospect_activities")
    op.drop_index("ix_dealer_prospect_activity_prospect", table_name="dealer_prospect_activities")
    op.drop_table("dealer_prospect_activities")
    op.drop_index("uq_dealer_prospect_phone_active", table_name="dealer_prospects")
    op.drop_index("uq_dealer_prospect_email_active", table_name="dealer_prospects")
    op.drop_index("ix_dealer_prospect_company", table_name="dealer_prospects")
    op.drop_index("ix_dealer_prospect_last_outcome", table_name="dealer_prospects")
    op.drop_index("ix_dealer_prospect_activity", table_name="dealer_prospects")
    op.drop_index("ix_dealer_prospect_follow_up", table_name="dealer_prospects")
    op.drop_index("ix_dealer_prospect_owner_stage", table_name="dealer_prospects")
    op.drop_table("dealer_prospects")
    op.drop_index(
        "ix_dealer_prospect_outcome_order", table_name="dealer_prospect_outcome_definitions"
    )
    op.drop_table("dealer_prospect_outcome_definitions")
    op.drop_index("ix_dealer_prospect_stage_order", table_name="dealer_prospect_stage_definitions")
    op.drop_table("dealer_prospect_stage_definitions")
