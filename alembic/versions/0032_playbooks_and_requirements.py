"""AI Playbooks + requirement resolver foundation.

Revision ID: 0032
Revises: 0031
Create Date: 2026-05-09

Replaces the hardcoded transaction_checklists / STARTER_BUYER_DOCS /
_LENDING_REQUIRED_FIELDS constants with a configurable layered model.

Tables:

  ai_playbook_templates       Per-owner (platform | funding | agent)
                              playbook. Versioned + publishable so an
                              in-flight deal isn't disrupted by a
                              mid-deal config edit.

  ai_collection_requirements  One row per requirement on a playbook.
                              Carries applies_when conditions, override
                              flags, visibility, and an AI message
                              template.

  client_ai_plan              The active per-client plan. Recomputed
                              every chat turn / doc upload / cadence
                              pass. Pins active_playbook_versions so
                              edits don't disrupt mid-deal.

  client_requirement_status   Per-client per-requirement state. Source
                              of truth for individual item state.

  ai_cadence_rules            Event-driven follow-up rules. Replaces
                              the old gentle/standard/aggressive
                              presets. Default approval_required=true
                              (draft-first).

  document_analysis_results   Persisted vision-scan extractions. Drives
                              contradiction detection across the deal
                              lifetime.

  ai_audit_events             Append-only audit log for any event that
                              changes AI behavior or moves a deal
                              forward.

Partial unique indexes (NOT plain UNIQUE constraints) on client_ai_plan
and client_requirement_status because Postgres treats NULL loan_id as
distinct, which would silently allow duplicate realtor-phase rows.

Seed data: 5 platform loan_product playbooks (DSCR Purchase / Refi,
Bridge, Fix & Flip, Construction), 2 platform transaction playbooks
(buyer / seller), 1 platform cadence playbook with starter rules. All
shipped at version=1, status='published'. Funding + agent overlays
are created from the UI in later phases.
"""

from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── Schema ─────────────────────────────────────────────────────────


def upgrade() -> None:
    op.create_table(
        "ai_playbook_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_type", sa.String(16), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("playbook_type", sa.String(32), nullable=False),
        sa.Column("product_key", sa.String(64), nullable=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_ai_playbook_templates_owner",
        "ai_playbook_templates",
        ["owner_type", "owner_id", "playbook_type", "product_key"],
    )

    op.create_table(
        "ai_collection_requirements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "playbook_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_playbook_templates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requirement_key", sa.String(120), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("required_level", sa.String(16), nullable=False),
        sa.Column("applies_when", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("blocks_stage", sa.String(32), nullable=True),
        sa.Column("visibility", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[\"agent\",\"underwriter\"]'::jsonb")),
        sa.Column("can_agent_override", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("can_underwriter_waive", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("verification_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expiration_days", sa.Integer(), nullable=True),
        sa.Column("ai_request_message_template", sa.Text(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("playbook_id", "requirement_key", name="uq_collection_req_playbook_key"),
    )

    op.create_table(
        "client_ai_plan",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("current_phase", sa.String(16), nullable=False),
        sa.Column(
            "active_playbook_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("custom_instructions", sa.Text(), nullable=True),
        sa.Column("required_items", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("waived_items", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("ai_suggested_items", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("next_best_question", sa.Text(), nullable=True),
        sa.Column("next_best_action", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("readiness_score", sa.Integer(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Partial unique indexes — Postgres treats NULL loan_id as distinct
    # under a plain UNIQUE constraint, so split into two partial indexes.
    op.execute(
        "CREATE UNIQUE INDEX uq_client_ai_plan_realtor "
        "ON client_ai_plan (client_id) WHERE loan_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_client_ai_plan_loan "
        "ON client_ai_plan (client_id, loan_id) WHERE loan_id IS NOT NULL"
    )

    op.create_table(
        "client_requirement_status",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("requirement_key", sa.String(120), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("due_at", sa.Date(), nullable=True),
        sa.Column("last_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_client_requirement_status_realtor "
        "ON client_requirement_status (client_id, requirement_key) WHERE loan_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_client_requirement_status_loan "
        "ON client_requirement_status (client_id, loan_id, requirement_key) WHERE loan_id IS NOT NULL"
    )
    op.create_index(
        "ix_client_requirement_status_lookup",
        "client_requirement_status",
        ["client_id", "requirement_key", "status"],
    )

    op.create_table(
        "ai_cadence_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "playbook_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_playbook_templates.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("trigger_event", sa.String(64), nullable=False),
        sa.Column("applies_to_requirement_key", sa.String(120), nullable=True),
        sa.Column("condition", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("wait_hours", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("message_template", sa.Text(), nullable=True),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="agent"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_ai_cadence_rules_lookup",
        "ai_cadence_rules",
        ["playbook_id", "trigger_event", "is_active"],
    )

    op.create_table(
        "document_analysis_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("detected_document_type", sa.String(64), nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("extracted_facts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("issues", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("recommended_action", sa.String(64), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("analyzer_version", sa.String(32), nullable=False, server_default="v1"),
    )

    op.create_table(
        "ai_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "playbook_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_playbook_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("requirement_key", sa.String(120), nullable=True),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ai_audit_events_client", "ai_audit_events", ["client_id", "created_at"])
    op.create_index("ix_ai_audit_events_loan", "ai_audit_events", ["loan_id", "created_at"])
    op.create_index("ix_ai_audit_events_playbook", "ai_audit_events", ["playbook_id", "created_at"])
    op.create_index("ix_ai_audit_events_event_type", "ai_audit_events", ["event_type", "created_at"])

    # ── Seed data ──────────────────────────────────────────────────
    _seed_platform_playbooks()


def downgrade() -> None:
    op.drop_index("ix_ai_audit_events_event_type", table_name="ai_audit_events")
    op.drop_index("ix_ai_audit_events_playbook", table_name="ai_audit_events")
    op.drop_index("ix_ai_audit_events_loan", table_name="ai_audit_events")
    op.drop_index("ix_ai_audit_events_client", table_name="ai_audit_events")
    op.drop_table("ai_audit_events")

    op.drop_table("document_analysis_results")

    op.drop_index("ix_ai_cadence_rules_lookup", table_name="ai_cadence_rules")
    op.drop_table("ai_cadence_rules")

    op.drop_index("ix_client_requirement_status_lookup", table_name="client_requirement_status")
    op.execute("DROP INDEX IF EXISTS uq_client_requirement_status_loan")
    op.execute("DROP INDEX IF EXISTS uq_client_requirement_status_realtor")
    op.drop_table("client_requirement_status")

    op.execute("DROP INDEX IF EXISTS uq_client_ai_plan_loan")
    op.execute("DROP INDEX IF EXISTS uq_client_ai_plan_realtor")
    op.drop_table("client_ai_plan")

    op.drop_table("ai_collection_requirements")

    op.drop_index("ix_ai_playbook_templates_owner", table_name="ai_playbook_templates")
    op.drop_table("ai_playbook_templates")


# ── Seed helpers ──────────────────────────────────────────────────


def _seed_platform_playbooks() -> None:
    """Insert the platform-default playbooks + their requirements + a
    starter cadence playbook. All published at version=1.

    Kept deliberately minimal in this migration — the funding team
    expands lending requirements via the Phase 3 super-admin UI; agents
    customize via the Phase 2 agent settings UI. This seed just gets
    the resolver returning a sensible list out of the box."""
    bind = op.get_bind()
    now = sa.func.now()

    # ── Buyer playbook (platform) ─────────────────────────────────
    buyer_pb_id = uuid.uuid4()
    bind.execute(
        sa.text(
            "INSERT INTO ai_playbook_templates "
            "(id, owner_type, playbook_type, name, description, rules, "
            " version, status, published_at, created_at, updated_at) "
            "VALUES (:id, 'platform', 'buyer', :name, :desc, :rules, "
            " 1, 'published', NOW(), NOW(), NOW())"
        ),
        {
            "id": buyer_pb_id,
            "name": "Default Buyer Playbook",
            "desc": "Platform-default buyer-side collection requirements + handoff gate.",
            "rules": '{"before_handoff": ["client_name", "client_phone", "target_property_type", "target_location", "target_budget", "purchase_timeline", "financing_needed"]}',
        },
    )
    _insert_requirements(bind, buyer_pb_id, _BUYER_REQUIREMENTS)

    # ── Seller playbook (platform) ────────────────────────────────
    seller_pb_id = uuid.uuid4()
    bind.execute(
        sa.text(
            "INSERT INTO ai_playbook_templates "
            "(id, owner_type, playbook_type, name, description, rules, "
            " version, status, published_at, created_at, updated_at) "
            "VALUES (:id, 'platform', 'seller', :name, :desc, :rules, "
            " 1, 'published', NOW(), NOW(), NOW())"
        ),
        {
            "id": seller_pb_id,
            "name": "Default Seller Playbook",
            "desc": "Platform-default seller-side collection requirements + listing gate.",
            "rules": '{"before_listing": ["property_address", "desired_list_price", "listing_agreement_status"]}',
        },
    )
    _insert_requirements(bind, seller_pb_id, _SELLER_REQUIREMENTS)

    # ── Loan-product playbooks (platform) ─────────────────────────
    for product_key, name, desc, requirements in _LOAN_PRODUCTS:
        pb_id = uuid.uuid4()
        bind.execute(
            sa.text(
                "INSERT INTO ai_playbook_templates "
                "(id, owner_type, playbook_type, product_key, name, description, "
                " rules, version, status, published_at, created_at, updated_at) "
                "VALUES (:id, 'platform', 'loan_product', :pk, :name, :desc, "
                " :rules, 1, 'published', NOW(), NOW(), NOW())"
            ),
            {
                "id": pb_id,
                "pk": product_key,
                "name": name,
                "desc": desc,
                "rules": '{}',
            },
        )
        _insert_requirements(bind, pb_id, requirements)

    # ── Cadence playbook (platform) ───────────────────────────────
    cadence_pb_id = uuid.uuid4()
    bind.execute(
        sa.text(
            "INSERT INTO ai_playbook_templates "
            "(id, owner_type, playbook_type, name, description, rules, "
            " version, status, published_at, created_at, updated_at) "
            "VALUES (:id, 'platform', 'cadence', :name, :desc, '{}'::jsonb, "
            " 1, 'published', NOW(), NOW(), NOW())"
        ),
        {
            "id": cadence_pb_id,
            "name": "Default Conditional Cadence",
            "desc": "Draft-first follow-up rules. Agents/funding override per-side.",
        },
    )
    for trigger, req_key, condition, wait, action, msg in _CADENCE_RULES:
        bind.execute(
            sa.text(
                "INSERT INTO ai_cadence_rules "
                "(id, playbook_id, trigger_event, applies_to_requirement_key, "
                " condition, wait_hours, action_type, approval_required, "
                " message_template, visibility, is_active, created_at, updated_at) "
                "VALUES (:id, :pb, :trig, :rk, :cond, :wait, :act, true, "
                " :msg, 'agent', true, NOW(), NOW())"
            ),
            {
                "id": uuid.uuid4(),
                "pb": cadence_pb_id,
                "trig": trigger,
                "rk": req_key,
                "cond": condition,
                "wait": wait,
                "act": action,
                "msg": msg,
            },
        )


def _insert_requirements(bind, playbook_id: uuid.UUID, requirements: list[dict]) -> None:
    for i, r in enumerate(requirements):
        bind.execute(
            sa.text(
                "INSERT INTO ai_collection_requirements "
                "(id, playbook_id, requirement_key, label, category, required_level, "
                " applies_when, blocks_stage, visibility, can_agent_override, "
                " can_underwriter_waive, verification_required, expiration_days, "
                " ai_request_message_template, display_order, created_at, updated_at) "
                "VALUES (:id, :pb, :key, :label, :cat, :lvl, "
                " :aw, :bs, :vis, :cao, "
                " :cuw, :vr, :exp, "
                " :tmpl, :ord, NOW(), NOW())"
            ),
            {
                "id": uuid.uuid4(),
                "pb": playbook_id,
                "key": r["key"],
                "label": r["label"],
                "cat": r["category"],
                "lvl": r["required_level"],
                "aw": r.get("applies_when"),
                "bs": r.get("blocks_stage"),
                "vis": r.get("visibility", '["agent","underwriter"]'),
                "cao": r.get("can_agent_override", False),
                "cuw": r.get("can_underwriter_waive", True),
                "vr": r.get("verification_required", False),
                "exp": r.get("expiration_days"),
                "tmpl": r.get("template"),
                "ord": i,
            },
        )


# ── Requirement seed data ──────────────────────────────────────────

_BUYER_REQUIREMENTS = [
    {"key": "client_name", "label": "Full name", "category": "fact", "required_level": "required", "can_agent_override": False},
    {"key": "client_phone", "label": "Phone", "category": "fact", "required_level": "required", "can_agent_override": False},
    {"key": "client_email", "label": "Email", "category": "fact", "required_level": "required", "can_agent_override": False},
    {"key": "target_property_type", "label": "Target property type", "category": "fact", "required_level": "required", "can_agent_override": True},
    {"key": "target_location", "label": "Target location", "category": "fact", "required_level": "required", "can_agent_override": True},
    {"key": "target_budget", "label": "Budget or budget range", "category": "fact", "required_level": "required", "can_agent_override": True},
    {"key": "purchase_timeline", "label": "Purchase timeline", "category": "fact", "required_level": "required", "can_agent_override": True},
    {"key": "financing_needed", "label": "Cash or financing", "category": "fact", "required_level": "required", "can_agent_override": False},
    {"key": "buyer_agency_agreement", "label": "Buyer agency agreement", "category": "agreement", "required_level": "recommended", "can_agent_override": True, "blocks_stage": "showings"},
    {"key": "prequalification_status", "label": "Prequalification status", "category": "fact", "required_level": "recommended", "can_agent_override": True},
    {"key": "proof_of_funds", "label": "Proof of funds", "category": "document", "required_level": "optional", "can_agent_override": True, "applies_when": '{"financing_needed": false}'},
]

_SELLER_REQUIREMENTS = [
    {"key": "property_address", "label": "Property address", "category": "fact", "required_level": "required", "can_agent_override": False},
    {"key": "owner_name", "label": "Owner name", "category": "fact", "required_level": "required", "can_agent_override": False},
    {"key": "client_phone", "label": "Phone", "category": "fact", "required_level": "required", "can_agent_override": False},
    {"key": "client_email", "label": "Email", "category": "fact", "required_level": "required", "can_agent_override": False},
    {"key": "desired_list_price", "label": "Desired list price", "category": "fact", "required_level": "required", "can_agent_override": True},
    {"key": "selling_timeline", "label": "Selling timeline", "category": "fact", "required_level": "required", "can_agent_override": True},
    {"key": "occupancy_status", "label": "Occupancy status", "category": "fact", "required_level": "required", "can_agent_override": True},
    {"key": "property_condition", "label": "Property condition", "category": "fact", "required_level": "recommended", "can_agent_override": True},
    {"key": "listing_agreement", "label": "Listing agreement", "category": "agreement", "required_level": "required", "can_agent_override": False, "blocks_stage": "listed"},
    {"key": "cma_task", "label": "CMA prepared", "category": "task", "required_level": "required", "can_agent_override": True, "blocks_stage": "listed"},
    {"key": "picture_day", "label": "Picture day scheduled", "category": "appointment", "required_level": "required", "can_agent_override": True, "blocks_stage": "listed"},
]

# ── Loan-product seed (intentionally minimal — funding fleshes out
# via the Phase 3 super-admin UI). Each product gets the canonical
# borrower + property + verification fields the funding team always
# needs at minimum. ────────────────────────────────────────────────

_DSCR_PURCHASE_REQS = [
    {"key": "borrower_entity_type", "label": "Borrower entity type", "category": "fact", "required_level": "required", "blocks_stage": "prequalification", "can_agent_override": False},
    {"key": "credit_authorization", "label": "Credit authorization", "category": "agreement", "required_level": "required", "blocks_stage": "prequalification", "can_agent_override": False, "verification_required": True},
    {"key": "purchase_contract", "label": "Purchase contract", "category": "document", "required_level": "required", "blocks_stage": "term_sheet", "applies_when": '{"under_contract": true}', "can_agent_override": False, "verification_required": True},
    {"key": "rent_roll", "label": "Rent roll / lease", "category": "document", "required_level": "required", "blocks_stage": "term_sheet", "can_agent_override": False, "verification_required": True},
    {"key": "operating_agreement", "label": "Operating agreement", "category": "document", "required_level": "required", "blocks_stage": "underwriting", "applies_when": '{"borrower_type": "entity"}', "can_agent_override": False, "verification_required": True},
    {"key": "bank_statements", "label": "Bank statements (last 2 months)", "category": "document", "required_level": "required", "blocks_stage": "underwriting", "can_agent_override": False, "verification_required": True, "expiration_days": 60},
    {"key": "insurance_estimate", "label": "Insurance estimate", "category": "document", "required_level": "required", "blocks_stage": "term_sheet", "can_agent_override": False, "verification_required": True},
    {"key": "property_taxes", "label": "Property tax record", "category": "document", "required_level": "required", "blocks_stage": "term_sheet", "can_agent_override": False, "verification_required": True},
    {"key": "id_document", "label": "Government ID", "category": "document", "required_level": "required", "blocks_stage": "underwriting", "can_agent_override": False, "verification_required": True},
    {"key": "appraisal", "label": "Appraisal payment confirmation", "category": "document", "required_level": "required", "blocks_stage": "closing", "can_agent_override": False, "verification_required": True},
]

_DSCR_REFI_REQS = [
    {"key": "borrower_entity_type", "label": "Borrower entity type", "category": "fact", "required_level": "required", "blocks_stage": "prequalification", "can_agent_override": False},
    {"key": "credit_authorization", "label": "Credit authorization", "category": "agreement", "required_level": "required", "blocks_stage": "prequalification", "can_agent_override": False, "verification_required": True},
    {"key": "current_mortgage_statement", "label": "Current mortgage statement", "category": "document", "required_level": "required", "blocks_stage": "term_sheet", "can_agent_override": False, "verification_required": True, "expiration_days": 30},
    {"key": "rent_roll", "label": "Rent roll / lease", "category": "document", "required_level": "required", "blocks_stage": "term_sheet", "can_agent_override": False, "verification_required": True},
    {"key": "operating_agreement", "label": "Operating agreement", "category": "document", "required_level": "required", "blocks_stage": "underwriting", "applies_when": '{"borrower_type": "entity"}', "can_agent_override": False, "verification_required": True},
    {"key": "bank_statements", "label": "Bank statements (last 2 months)", "category": "document", "required_level": "required", "blocks_stage": "underwriting", "can_agent_override": False, "verification_required": True, "expiration_days": 60},
    {"key": "insurance_declarations", "label": "Insurance declarations page", "category": "document", "required_level": "required", "blocks_stage": "term_sheet", "can_agent_override": False, "verification_required": True},
    {"key": "property_taxes", "label": "Property tax record", "category": "document", "required_level": "required", "blocks_stage": "term_sheet", "can_agent_override": False, "verification_required": True},
    {"key": "id_document", "label": "Government ID", "category": "document", "required_level": "required", "blocks_stage": "underwriting", "can_agent_override": False, "verification_required": True},
]

_BRIDGE_REQS = [
    {"key": "borrower_entity_type", "label": "Borrower entity type", "category": "fact", "required_level": "required", "blocks_stage": "prequalification", "can_agent_override": False},
    {"key": "credit_authorization", "label": "Credit authorization", "category": "agreement", "required_level": "required", "blocks_stage": "prequalification", "can_agent_override": False, "verification_required": True},
    {"key": "purchase_contract", "label": "Purchase contract", "category": "document", "required_level": "required", "blocks_stage": "term_sheet", "applies_when": '{"under_contract": true}', "can_agent_override": False, "verification_required": True},
    {"key": "exit_strategy", "label": "Exit strategy", "category": "fact", "required_level": "required", "blocks_stage": "term_sheet", "can_agent_override": False},
    {"key": "operating_agreement", "label": "Operating agreement", "category": "document", "required_level": "required", "blocks_stage": "underwriting", "applies_when": '{"borrower_type": "entity"}', "can_agent_override": False, "verification_required": True},
    {"key": "bank_statements", "label": "Bank statements (last 2 months)", "category": "document", "required_level": "required", "blocks_stage": "underwriting", "can_agent_override": False, "verification_required": True, "expiration_days": 60},
    {"key": "id_document", "label": "Government ID", "category": "document", "required_level": "required", "blocks_stage": "underwriting", "can_agent_override": False, "verification_required": True},
]

_FIX_FLIP_REQS = _BRIDGE_REQS + [
    {"key": "rehab_budget", "label": "Rehab budget", "category": "document", "required_level": "required", "blocks_stage": "term_sheet", "can_agent_override": False, "verification_required": True},
    {"key": "scope_of_work", "label": "Scope of work", "category": "document", "required_level": "required", "blocks_stage": "term_sheet", "can_agent_override": False, "verification_required": True},
    {"key": "experience_tier", "label": "Past investment deals", "category": "fact", "required_level": "required", "blocks_stage": "prequalification", "can_agent_override": False},
]

_CONSTRUCTION_REQS = _FIX_FLIP_REQS + [
    {"key": "permits", "label": "Permit set", "category": "document", "required_level": "required", "blocks_stage": "term_sheet", "can_agent_override": False, "verification_required": True},
    {"key": "general_contractor", "label": "GC license + insurance", "category": "document", "required_level": "required", "blocks_stage": "term_sheet", "can_agent_override": False, "verification_required": True},
    {"key": "land_appraisal", "label": "Land appraisal", "category": "document", "required_level": "required", "blocks_stage": "term_sheet", "can_agent_override": False, "verification_required": True},
]

_LOAN_PRODUCTS = [
    ("dscr_purchase", "DSCR Purchase", "Investment-property purchase using DSCR underwriting.", _DSCR_PURCHASE_REQS),
    ("dscr_refi", "DSCR Refinance", "Investment-property refinance using DSCR underwriting.", _DSCR_REFI_REQS),
    ("bridge", "Bridge Loan", "Short-term bridge financing.", _BRIDGE_REQS),
    ("fix_flip", "Fix & Flip", "Short-term acquisition + rehab loan.", _FIX_FLIP_REQS),
    ("construction", "Ground-Up Construction", "Land + vertical construction financing.", _CONSTRUCTION_REQS),
]


# ── Cadence rules seed (platform default — draft-first, all
# approval_required=true). Funding + agent overlay rules added via
# Phase 2/3 UI. ──────────────────────────────────────────────────

_CADENCE_RULES = [
    # (trigger_event, applies_to_requirement_key, condition, wait_hours, action_type, message_template)
    (
        "requirement_missing",
        None,
        '{"hours_since_request": 24}',
        24,
        "draft_message",
        "Quick nudge — we still need {{requirement_label}} to keep things moving. Want me to draft a follow-up?",
    ),
    (
        "closing_date_near",
        None,
        '{"days_until_closing": 14, "key_doc_missing": true}',
        0,
        "escalate",
        "Closing in {{days_until_closing}} days but {{requirement_label}} still missing — flagging for the funding team.",
    ),
    (
        "borrower_unresponsive",
        None,
        '{"days_unresponsive": 5, "stage": "prequalification"}',
        0,
        "mark_stalled",
        None,
    ),
    (
        "agreement_unsigned",
        "buyer_agency_agreement",
        '{"hours_since_sent": 24}',
        24,
        "draft_message",
        "Buyer agreement still unsigned — want me to draft a friendly nudge?",
    ),
    (
        "agreement_unsigned",
        "listing_agreement",
        '{"hours_since_sent": 48}',
        48,
        "create_task",
        "Listing agreement at 48h. Drafting a follow-up call task.",
    ),
]
