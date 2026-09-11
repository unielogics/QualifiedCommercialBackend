"""Connect AI Intake program readiness, evidence, and communications.

Revision ID: 0208_ai_intake_program_readiness
Revises: 0207_booking_precall_recovery
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0208_ai_intake_program_readiness"
down_revision = "0207_booking_precall_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_profiles",
        sa.Column("program_selection_mode", sa.String(length=16), nullable=False, server_default="auto"),
    )
    op.add_column(
        "application_profiles",
        sa.Column("program_selection_locked_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "application_profiles",
        sa.Column(
            "program_selection_locked_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
    )
    # Existing files remain opt-in; new eligible AI Intake files default on.
    op.add_column(
        "application_profiles",
        sa.Column(
            "missing_item_email_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "application_profiles",
        sa.Column("missing_item_email_last_sent_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "application_profiles",
        sa.Column("missing_item_email_next_send_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "application_profiles",
        sa.Column(
            "missing_item_email_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "application_profiles",
        sa.Column("missing_item_email_requirement_key", sa.String(length=120)),
    )
    op.create_check_constraint(
        "ck_application_profiles_program_selection_mode",
        "application_profiles",
        "program_selection_mode IN ('auto', 'manual')",
    )
    op.alter_column(
        "application_profiles",
        "missing_item_email_enabled",
        server_default=sa.true(),
    )

    op.create_table(
        "application_program_selections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "playbook_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_playbook_templates.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("playbook_version", sa.Integer(), nullable=False),
        sa.Column("program_key", sa.String(length=64), nullable=False),
        sa.Column("program_name", sa.String(length=160), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False, server_default="operator"),
        sa.Column("fit_score", sa.Numeric(8, 4)),
        sa.Column("fit_confidence", sa.Numeric(5, 4)),
        sa.Column("fit_reasons", postgresql.JSONB()),
        sa.Column(
            "selected_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "removed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("source IN ('ai_auto', 'operator')", name="ck_application_program_selection_source"),
    )
    op.create_index(
        "uq_application_program_selection_active",
        "application_program_selections",
        ["profile_id", "program_key"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    op.create_index(
        "ix_application_program_selections_profile",
        "application_program_selections",
        ["profile_id", "selected_at"],
    )

    op.create_table(
        "application_requirement_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requirement_key", sa.String(length=120), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("required_level", sa.String(length=16), nullable=False, server_default="required"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="missing"),
        sa.Column(
            "requested_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bucket_requested_documents.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "evidence_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bucket_files.id", ondelete="SET NULL"),
        ),
        sa.Column("verification_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_program_keys", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("provenance", postgresql.JSONB()),
        sa.Column("state_reason", sa.Text()),
        sa.Column("first_requested_at", sa.DateTime(timezone=True)),
        sa.Column("last_requested_at", sa.DateTime(timezone=True)),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column(
            "verified_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("profile_id", "requirement_key", name="uq_application_requirement_profile_key"),
        sa.CheckConstraint(
            "status IN ('missing','requested','received_unverified','verified','waived','not_applicable','stale','failed')",
            name="ck_application_requirement_status",
        ),
    )
    op.create_index(
        "ix_application_requirement_states_profile_status",
        "application_requirement_states",
        ["profile_id", "status"],
    )

    op.create_table(
        "application_program_requirement_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "selection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_program_selections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requirement_key", sa.String(length=120), nullable=False),
        sa.Column("disposition", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("restored_at", sa.DateTime(timezone=True)),
        sa.Column(
            "restored_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "disposition IN ('waived','not_applicable','required','recommended')",
            name="ck_application_program_requirement_override_disposition",
        ),
    )
    op.create_index(
        "uq_application_program_requirement_override_active",
        "application_program_requirement_overrides",
        ["selection_id", "requirement_key"],
        unique=True,
        postgresql_where=sa.text("restored_at IS NULL"),
    )

    op.add_column("bucket_requested_documents", sa.Column("requirement_key", sa.String(length=120)))
    op.add_column("bucket_requested_documents", sa.Column("requirement_source", postgresql.JSONB()))
    op.create_index(
        "ix_bucket_requested_documents_requirement",
        "bucket_requested_documents",
        ["bucket_id", "requirement_key"],
    )

    op.add_column("application_room_deliveries", sa.Column("initiation_source", sa.String(length=32)))
    op.add_column("application_room_deliveries", sa.Column("idempotency_key", sa.String(length=160)))
    op.add_column(
        "application_room_deliveries",
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("application_room_deliveries", sa.Column("scheduled_for", sa.DateTime(timezone=True)))
    op.create_index(
        "uq_application_room_delivery_idempotency",
        "application_room_deliveries",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "bucket_ai_chat_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bucket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("buckets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bucket_ai_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "upload_link_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bucket_upload_links.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "requested_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bucket_requested_documents.id", ondelete="SET NULL"),
        ),
        sa.Column("requirement_key", sa.String(length=120), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("template_kind", sa.String(length=40)),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="available"),
        sa.Column("recipient_email", sa.String(length=320)),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "action_type IN ('upload_own','complete_now','download_template','email_template')",
            name="ck_bucket_ai_chat_action_type",
        ),
        sa.CheckConstraint(
            "status IN ('available','executed','failed','expired','disabled')",
            name="ck_bucket_ai_chat_action_status",
        ),
    )
    op.create_index(
        "ix_bucket_ai_chat_actions_message",
        "bucket_ai_chat_actions",
        ["source_message_id", "created_at"],
    )
    op.create_index(
        "ix_bucket_ai_chat_actions_profile_status",
        "bucket_ai_chat_actions",
        ["profile_id", "status"],
    )

    _seed_baseline_playbooks()


def _seed_baseline_playbooks() -> None:
    playbooks = [
        (
            uuid.UUID("18200000-0000-4000-8000-000000000001"),
            "business_baseline",
            "Universal business lending baseline",
            {"priority": 10, "fit": {"all": [{"field": "vertical", "op": "in", "value": ["dealer", "main_street"]}]}},
            [
                ("business_bank_statements_6_months", "Last 6 months business bank statements", "required", "bank_statement"),
                ("business_tax_returns_2_years", "Last 2 years business tax returns", "required", "tax_return"),
                ("ytd_p_and_l_balance_sheet", "Year-to-date P&L and balance sheet", "required", "current_p_and_l"),
                ("business_debt_schedule", "Business debt schedule", "required", "debt_schedule"),
                ("owner_personal_financial_statement", "Owner personal financial statement", "optional", "personal_financial_statement"),
            ],
        ),
        (
            uuid.UUID("18200000-0000-4000-8000-000000000002"),
            "real_estate_baseline",
            "Real estate lending baseline",
            {"priority": 20, "fit": {"field": "vertical", "op": "eq", "value": "real_estate"}},
            [
                ("real_estate_schedule", "Real estate schedule", "required", "real_estate_schedule"),
                ("property_debt_evidence", "Property debt and payoff evidence", "required", "payoff_or_mortgage_statement"),
                ("entity_or_vesting", "Entity and vesting documents", "required", "entity_or_vesting"),
                ("business_bank_statements_6_months", "Last 6 months business bank statements", "recommended", "bank_statement"),
            ],
        ),
        (
            uuid.UUID("18200000-0000-4000-8000-000000000003"),
            "mca_baseline",
            "MCA refinance baseline",
            {"priority": 30, "fit": {"field": "vertical", "op": "eq", "value": "mca"}},
            [
                ("business_bank_statements_6_months", "Last 6 months business bank statements", "required", "bank_statement"),
                ("signed_credit_authorization", "Signed credit authorization", "required", "identity"),
                ("current_advance_terms", "Current advance terms or payoff letters", "required", "floorplan_mca_inventory"),
            ],
        ),
    ]
    connection = op.get_bind()
    for playbook_id, key, name, rules, requirements in playbooks:
        connection.execute(
            sa.text(
                """
                INSERT INTO ai_playbook_templates
                    (id, owner_type, owner_id, playbook_type, product_key, name, description,
                     rules, version, status, published_at, is_active, created_at, updated_at)
                SELECT :id, 'platform', NULL, 'loan_product', CAST(:key AS VARCHAR(64)), :name,
                       'System baseline seeded by migration 0182', CAST(:rules AS jsonb),
                       1, 'published', now(), true, now(), now()
                WHERE NOT EXISTS (
                    SELECT 1 FROM ai_playbook_templates
                     WHERE playbook_type = 'loan_product'
                       AND product_key = CAST(:key AS VARCHAR(64))
                       AND status = 'published' AND is_active = true
                )
                """
            ),
            {"id": playbook_id, "key": key, "name": name, "rules": __import__("json").dumps(rules)},
        )
        actual_id = connection.execute(
            sa.text(
                """
                SELECT id FROM ai_playbook_templates
                 WHERE playbook_type = 'loan_product'
                   AND product_key = CAST(:key AS VARCHAR(64))
                   AND status = 'published' AND is_active = true
                 ORDER BY version DESC LIMIT 1
                """
            ),
            {"key": key},
        ).scalar_one()
        # An existing published playbook is authoritative. Do not inject the
        # fallback requirements into a lender-managed version with the same key.
        if actual_id != playbook_id:
            continue
        for order, (req_key, label, level, classification) in enumerate(requirements, start=1):
            requirement_id = uuid.uuid5(playbook_id, req_key)
            connection.execute(
                sa.text(
                    """
                    INSERT INTO ai_collection_requirements
                        (id, playbook_id, requirement_key, label, category, required_level,
                         applies_when, blocks_stage, visibility, can_agent_override,
                         can_underwriter_waive, verification_required, expiration_days,
                         ai_request_message_template, display_order, default_owner_type,
                         default_channels, default_cadence_hours, objective_text,
                         completion_criteria, completion_mode, depends_on,
                         inferred_depends_on, deps_confirmed, created_at, updated_at)
                    SELECT :id, :playbook_id, CAST(:req_key AS TEXT),
                           CAST(:label AS VARCHAR(200)), 'financials', :level,
                           NULL, 'underwriting', '["borrower","underwriter"]'::jsonb,
                           false, true, true, NULL,
                           'Please provide {label}.', :display_order, 'human',
                           '["portal","email"]'::jsonb, 24,
                           'Collect ' || CAST(:label AS VARCHAR(200)),
                           'A readable ' || :classification || ' document is linked and verified.',
                           'requires_human_verify', '[]'::jsonb, '[]'::jsonb, true, now(), now()
                    WHERE NOT EXISTS (
                        SELECT 1 FROM ai_collection_requirements
                         WHERE playbook_id = :playbook_id
                           AND requirement_key = CAST(:req_key AS TEXT)
                    )
                    """
                ),
                {
                    "playbook_id": actual_id,
                    "id": requirement_id,
                    "req_key": req_key,
                    "label": label,
                    "level": level,
                    "display_order": order,
                    "classification": classification,
                },
            )


def downgrade() -> None:
    seeded_playbooks = [
        uuid.UUID("18200000-0000-4000-8000-000000000001"),
        uuid.UUID("18200000-0000-4000-8000-000000000002"),
        uuid.UUID("18200000-0000-4000-8000-000000000003"),
    ]
    seeded_requirements = [
        uuid.uuid5(playbook_id, key)
        for playbook_id, keys in (
            (
                seeded_playbooks[0],
                [
                    "business_bank_statements_6_months",
                    "business_tax_returns_2_years",
                    "ytd_p_and_l_balance_sheet",
                    "business_debt_schedule",
                    "owner_personal_financial_statement",
                ],
            ),
            (
                seeded_playbooks[1],
                [
                    "real_estate_schedule",
                    "property_debt_evidence",
                    "entity_or_vesting",
                    "business_bank_statements_6_months",
                ],
            ),
            (
                seeded_playbooks[2],
                [
                    "business_bank_statements_6_months",
                    "signed_credit_authorization",
                    "current_advance_terms",
                ],
            ),
        )
        for key in keys
    ]
    op.drop_table("bucket_ai_chat_actions")
    op.drop_index("uq_application_room_delivery_idempotency", table_name="application_room_deliveries")
    op.drop_column("application_room_deliveries", "scheduled_for")
    op.drop_column("application_room_deliveries", "attempt_number")
    op.drop_column("application_room_deliveries", "idempotency_key")
    op.drop_column("application_room_deliveries", "initiation_source")
    op.drop_index("ix_bucket_requested_documents_requirement", table_name="bucket_requested_documents")
    op.drop_column("bucket_requested_documents", "requirement_source")
    op.drop_column("bucket_requested_documents", "requirement_key")
    op.drop_table("application_program_requirement_overrides")
    op.drop_table("application_requirement_states")
    op.drop_table("application_program_selections")
    connection = op.get_bind()
    requirement_parameters = {
        f"requirement_{index}": requirement_id
        for index, requirement_id in enumerate(seeded_requirements)
    }
    requirement_slots = ", ".join(f":requirement_{index}" for index in range(len(seeded_requirements)))
    connection.execute(
        sa.text(f"DELETE FROM ai_collection_requirements WHERE id IN ({requirement_slots})"),
        requirement_parameters,
    )
    playbook_parameters = {
        f"playbook_{index}": playbook_id
        for index, playbook_id in enumerate(seeded_playbooks)
    }
    playbook_slots = ", ".join(f":playbook_{index}" for index in range(len(seeded_playbooks)))
    connection.execute(
        sa.text(
            f"DELETE FROM ai_playbook_templates WHERE id IN ({playbook_slots}) "
            "AND description = 'System baseline seeded by migration 0182'"
        ),
        playbook_parameters,
    )
    op.drop_constraint(
        "ck_application_profiles_program_selection_mode",
        "application_profiles",
        type_="check",
    )
    op.drop_column("application_profiles", "missing_item_email_requirement_key")
    op.drop_column("application_profiles", "missing_item_email_attempts")
    op.drop_column("application_profiles", "missing_item_email_next_send_at")
    op.drop_column("application_profiles", "missing_item_email_last_sent_at")
    op.drop_column("application_profiles", "missing_item_email_enabled")
    op.drop_column("application_profiles", "program_selection_locked_by_user_id")
    op.drop_column("application_profiles", "program_selection_locked_at")
    op.drop_column("application_profiles", "program_selection_mode")
