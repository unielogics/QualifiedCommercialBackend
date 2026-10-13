"""Publish canonical Equipment Financing terms, scope, and evidence.

Revision ID: 0236_equipment_financing_sync
Revises: 0235_capital_readiness

The stable ``equipment_financing`` catalog identity already exists.  This
migration versions its rules and commercial terms instead of creating a
second product, and reconciles the older Field Desk catalog as one active
compatibility row.  Historical versions and application selections remain
intact.
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0236_equipment_financing_sync"
down_revision = "0235_capital_readiness"
branch_labels = None
depends_on = None

PROGRAM_KEY = "equipment_financing"
PUBLIC_SLUG = "equipment-financing"
MINIMUM_AMOUNT = 25_000
MAXIMUM_AMOUNT = 2_000_000
MINIMUM_TERM_MONTHS = 24
MAXIMUM_TERM_MONTHS = 84

PLAYBOOK_ID = "02360000-0000-4000-8000-000000000001"
QUOTE_REQUIREMENT_ID = "02360000-0000-4000-8000-000000000002"
DETAIL_REQUIREMENT_ID = "02360000-0000-4000-8000-000000000003"
TERMS_ID = "02360000-0000-4000-8000-000000000004"
DEALER_SCOPE_ID = "02360000-0000-4000-8000-000000000005"
MAIN_STREET_SCOPE_ID = "02360000-0000-4000-8000-000000000006"
FIELD_DESK_CATALOG_ID = "02360000-0000-4000-8000-000000000007"
SCOPE_KEY = "qc_equipment_v1"
DEALER_SCOPE_INTENT_KEYS: list[str] = []
MAIN_STREET_SCOPE_INTENT_KEYS = ["equipment"]

REQUIRED_FACT_KEYS = [
    "equipment_financing_intent",
    "business_age_years",
    "annual_revenue",
    "credit_score",
    "cash_flow",
    "requested_amount",
]

EQUIPMENT_DETAIL_FIELDS = [
    "description",
    "age_years",
    "condition",
    "remaining_useful_life_years",
    "seller_name",
    "seller_contact",
    "seller_type",
    "purchase_price",
    "delivery_timing",
    "intended_business_use",
]

EQUIPMENT_FIT_RULES = {
    "priority": 40,
    "fit": {
        "all": [
            {"field": "equipment_financing_intent", "op": "eq", "value": True},
            {"field": "business_age_years", "op": "gte", "value": 1},
            {"field": "annual_revenue", "op": "gte", "value": 100_000},
            {"field": "credit_score", "op": "gte", "value": 640},
            {"field": "cash_flow", "op": "gt", "value": 0},
            {"field": "requested_amount", "op": "gte", "value": MINIMUM_AMOUNT},
            {"field": "requested_amount", "op": "lte", "value": MAXIMUM_AMOUNT},
        ]
    },
    "published_program": {
        "amount_min": MINIMUM_AMOUNT,
        "amount_max": MAXIMUM_AMOUNT,
        "term_min_months": MINIMUM_TERM_MONTHS,
        "term_max_months": MAXIMUM_TERM_MONTHS,
        "unknown_result": "needs_information",
        "outside_amount_range": "custom_review",
    },
    "source": {
        "migration": revision,
        "status": "qc_published_baseline",
    },
}


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _program_id(bind: sa.engine.Connection):
    program_id = bind.execute(
        sa.text(
            "SELECT id FROM funding_program_catalog WHERE program_key = :program_key"
        ),
        {"program_key": PROGRAM_KEY},
    ).scalar_one_or_none()
    if program_id is None:
        raise RuntimeError(
            "Canonical equipment_financing program is missing; refusing to create a duplicate"
        )
    return program_id


def _insert_versioned_scope(
    bind: sa.engine.Connection,
    *,
    scope_id: str,
    program_id: object,
    vertical: str,
    intake_variants: list[str],
    intent_keys: list[str],
) -> None:
    """Add this release's scope without mutating a pre-existing admin scope.

    The resolver accepts any matching active scope for a vertical, while the
    published fit rule still enforces every preliminary threshold. A distinct
    routing reference makes downgrade lossless: only rows owned by this
    migration are removed.
    """

    bind.execute(
        sa.text(
            """
            INSERT INTO funding_program_scopes (
                id, program_id, vertical, scope_key, intake_variants,
                intent_keys, naics_prefixes, excluded_naics_prefixes,
                industry_keys, required_fact_keys, is_active,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS uuid), :program_id, :vertical, :scope_key,
                CAST(:intake_variants AS jsonb), CAST(:intent_keys AS jsonb),
                '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
                CAST(:required_fact_keys AS jsonb), true, now(), now()
            )
            ON CONFLICT (program_id, vertical, scope_key) DO NOTHING
            """
        ),
        {
            "id": scope_id,
            "program_id": program_id,
            "vertical": vertical,
            "scope_key": SCOPE_KEY,
            "intake_variants": _json(intake_variants),
            "intent_keys": _json(intent_keys),
            "required_fact_keys": _json(REQUIRED_FACT_KEYS),
        },
    )


def _insert_requirement(
    bind: sa.engine.Connection,
    *,
    requirement_id: str,
    key: str,
    label: str,
    category: str,
    order: int,
    objective: str,
    criteria: str,
    checks: list[dict[str, str]],
) -> None:
    bind.execute(
        sa.text(
            """
            INSERT INTO ai_collection_requirements (
                id, playbook_id, requirement_key, label, category,
                required_level, blocks_stage, visibility,
                can_agent_override, can_underwriter_waive,
                verification_required, display_order, objective_text,
                completion_criteria, completion_mode, review_checks,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS uuid), CAST(:playbook_id AS uuid), :key, :label, :category,
                'required', 'underwriting', '["agent","borrower","underwriter"]'::jsonb,
                false, true, true, :display_order, :objective,
                :criteria, 'requires_human_verify', CAST(:review_checks AS jsonb),
                now(), now()
            )
            """
        ),
        {
            "id": requirement_id,
            "playbook_id": PLAYBOOK_ID,
            "key": key,
            "label": label,
            "category": category,
            "display_order": order,
            "objective": objective,
            "criteria": criteria,
            "review_checks": _json(checks),
        },
    )


def upgrade() -> None:
    bind = op.get_bind()
    program_id = _program_id(bind)

    bind.execute(
        sa.text(
            """
            UPDATE funding_program_catalog
            SET public_slug = :public_slug,
                name = 'Equipment Financing',
                short_description =
                    'Financing for eligible new or used business equipment, subject to file review.',
                aliases = COALESCE(aliases, '[]'::jsonb) || CAST(:aliases AS jsonb),
                status = 'active', retired_at = NULL, retired_by_user_id = NULL,
                updated_at = now()
            WHERE id = :program_id
            """
        ),
        {
            "program_id": program_id,
            "public_slug": PUBLIC_SLUG,
            "aliases": _json(["equipment", "equipment_loan", "equipment_finance"]),
        },
    )

    _insert_versioned_scope(
        bind,
        scope_id=MAIN_STREET_SCOPE_ID,
        program_id=program_id,
        vertical="main_street",
        intake_variants=["main_street_v1"],
        intent_keys=MAIN_STREET_SCOPE_INTENT_KEYS,
    )
    _insert_versioned_scope(
        bind,
        scope_id=DEALER_SCOPE_ID,
        program_id=program_id,
        vertical="dealer",
        intake_variants=["dealer_gatekeeper_v1"],
        # Dealer profiles do not have Main Street's structured intent field.
        # The equipment_financing_intent fit fact remains mandatory below.
        intent_keys=DEALER_SCOPE_INTENT_KEYS,
    )

    # Published applications retain their pinned playbook IDs. Archiving the
    # older current version only changes which rules new selections receive.
    bind.execute(
        sa.text(
            """
            UPDATE ai_playbook_templates
            SET status = 'archived', is_active = false, updated_at = now()
            WHERE funding_program_id = :program_id AND status = 'published'
            """
        ),
        {"program_id": program_id},
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO ai_playbook_templates (
                id, owner_type, owner_id, playbook_type, product_key,
                funding_program_id, name, description, rules, version,
                status, published_at, is_active, created_at, updated_at
            ) VALUES (
                CAST(:id AS uuid), 'platform', NULL, 'loan_product', :program_key,
                :program_id, 'Equipment Financing published criteria',
                'QC preliminary Equipment Financing screen and transaction evidence.',
                CAST(:rules AS jsonb),
                COALESCE((SELECT MAX(version) + 1 FROM ai_playbook_templates
                          WHERE funding_program_id = :program_id), 1),
                'published', now(), true, now(), now()
            )
            """
        ),
        {
            "id": PLAYBOOK_ID,
            "program_key": PROGRAM_KEY,
            "program_id": program_id,
            "rules": _json(EQUIPMENT_FIT_RULES),
        },
    )

    _insert_requirement(
        bind,
        requirement_id=QUOTE_REQUIREMENT_ID,
        key="equipment_quote",
        label="Vendor quote or invoice for the equipment",
        category="financials",
        order=10,
        objective="Collect and review the current equipment quote or invoice.",
        criteria=(
            "Confirm the seller, equipment identity, purchase price, expected delivery, "
            "business use and disclosed liens. Missing facts remain open questions."
        ),
        checks=[
            {
                "key": "custom_equipment_identity",
                "label": "Identify the equipment",
                "instructions": "Record the equipment type and identifying description, including make/model when available.",
                "severity": "block",
            },
            {
                "key": "custom_equipment_seller_price",
                "label": "Verify seller and price",
                "instructions": "Reconcile seller identity, contact details and purchase price to the quote or invoice.",
                "severity": "block",
            },
            {
                "key": "custom_equipment_delivery",
                "label": "Confirm expected delivery",
                "instructions": "Record the expected delivery timing and escalate unresolved availability.",
                "severity": "review",
            },
        ],
    )
    _insert_requirement(
        bind,
        requirement_id=DETAIL_REQUIREMENT_ID,
        key="equipment_transaction_details",
        label="Equipment and transaction details",
        category="borrower_info",
        order=20,
        objective="Collect the structured equipment and transaction facts.",
        criteria=(
            "Record description, age, condition, remaining useful life, seller details, "
            "purchase price, delivery timing and intended business use."
        ),
        checks=[
            {
                "key": "custom_equipment_useful_life",
                "label": "Review age, condition and useful life",
                "instructions": "Compare remaining useful life with the requested financing term and escalate unsupported assumptions.",
                "severity": "block",
            },
            {
                "key": "custom_equipment_business_use",
                "label": "Confirm business purpose",
                "instructions": "Document the intended business use and confirm that this is a business-purpose request.",
                "severity": "block",
            },
        ],
    )

    bind.execute(
        sa.text(
            """
            UPDATE funding_program_commercial_terms
            SET status = 'retired', updated_at = now()
            WHERE program_id = :program_id AND status = 'published'
            """
        ),
        {"program_id": program_id},
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO funding_program_commercial_terms (
                id, program_id, version, status, minimum_amount, maximum_amount,
                minimum_term_months, maximum_term_months, pricing_basis,
                disclosures, source_reference, effective_date, published_at,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS uuid), :program_id,
                COALESCE((SELECT MAX(version) + 1 FROM funding_program_commercial_terms
                          WHERE program_id = :program_id), 1),
                'published', :minimum_amount, :maximum_amount,
                :minimum_term_months, :maximum_term_months, 'file_specific',
                CAST(:disclosures AS jsonb),
                'QC approved Equipment Financing published terms',
                CURRENT_DATE, now(), now(), now()
            )
            """
        ),
        {
            "id": TERMS_ID,
            "program_id": program_id,
            "minimum_amount": MINIMUM_AMOUNT,
            "maximum_amount": MAXIMUM_AMOUNT,
            "minimum_term_months": MINIMUM_TERM_MONTHS,
            "maximum_term_months": MAXIMUM_TERM_MONTHS,
            "disclosures": _json(
                [
                    "Preliminary fit only; not a commitment to lend.",
                    "Pricing, down payment, structure and timing remain subject to lender review, eligibility, underwriting and documentation.",
                    "Requests outside the published amount range require custom review or another suitable program.",
                ]
            ),
        },
    )

    # Field Desk still reads its original versioned table. Keep exactly one
    # active compatibility row, pointed at the canonical rules and terms.
    bind.execute(
        sa.text(
            """
            UPDATE dos_product_catalog
            SET active = false, updated_at = now()
            WHERE program_key = :program_key AND active = true
            """
        ),
        {"program_key": PROGRAM_KEY},
    )
    field_desk_eligibility = {
        "engine": "canonical_program_rules_v2",
        "canonical_program_key": PROGRAM_KEY,
        "unknown_result": "needs_information",
        "outside_amount_range": "custom_review",
        "fit": EQUIPMENT_FIT_RULES["fit"],
        "equipment_detail_fields": EQUIPMENT_DETAIL_FIELDS,
    }
    bind.execute(
        sa.text(
            """
            INSERT INTO dos_product_catalog (
                id, program_key, version, category, copy, pricing, eligibility,
                disclosures, amount_min, amount_max, term_min_months,
                term_max_months, effective_at, active, sort_order,
                created_at, updated_at
            ) VALUES (
                CAST(:id AS uuid), CAST(:program_key AS varchar),
                COALESCE((SELECT MAX(version) + 1 FROM dos_product_catalog
                          WHERE program_key = CAST(:program_key AS varchar)), 1),
                'asset', CAST(:copy AS jsonb), CAST(:pricing AS jsonb),
                CAST(:eligibility AS jsonb), CAST(:disclosures AS jsonb),
                :minimum_amount, :maximum_amount, :minimum_term_months,
                :maximum_term_months, now(), true, 50, now(), now()
            )
            """
        ),
        {
            "id": FIELD_DESK_CATALOG_ID,
            "program_key": PROGRAM_KEY,
            "copy": _json(
                {
                    "en": {
                        "name": "Equipment Financing",
                        "summary": "Financing for eligible new or used business equipment.",
                    },
                    "es": {
                        "name": "Financiamiento de equipo",
                        "summary": "Financiamiento para equipo comercial nuevo o usado que sea elegible.",
                    },
                }
            ),
            "pricing": _json(
                {
                    "en": "Pricing subject to file and lender review",
                    "es": "Precio sujeto a la evaluacion del expediente y del prestamista",
                }
            ),
            "eligibility": _json(field_desk_eligibility),
            "disclosures": _json(
                {
                    "en": "Preliminary fit only; not a commitment to lend.",
                    "es": "Evaluacion preliminar; no es un compromiso de prestamo.",
                }
            ),
            "minimum_amount": MINIMUM_AMOUNT,
            "maximum_amount": MAXIMUM_AMOUNT,
            "minimum_term_months": MINIMUM_TERM_MONTHS,
            "maximum_term_months": MAXIMUM_TERM_MONTHS,
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    program_id = _program_id(bind)

    bind.execute(
        sa.text("DELETE FROM dos_product_catalog WHERE id = CAST(:id AS uuid)"),
        {"id": FIELD_DESK_CATALOG_ID},
    )
    bind.execute(
        sa.text(
            """
            UPDATE dos_product_catalog
            SET active = true, updated_at = now()
            WHERE id = (
                SELECT id FROM dos_product_catalog
                WHERE program_key = :program_key
                ORDER BY version DESC LIMIT 1
            )
            """
        ),
        {"program_key": PROGRAM_KEY},
    )

    bind.execute(
        sa.text(
            "DELETE FROM funding_program_commercial_terms WHERE id = CAST(:id AS uuid)"
        ),
        {"id": TERMS_ID},
    )
    bind.execute(
        sa.text(
            """
            UPDATE funding_program_commercial_terms
            SET status = 'published', updated_at = now()
            WHERE id = (
                SELECT id FROM funding_program_commercial_terms
                WHERE program_id = :program_id AND status = 'retired'
                ORDER BY version DESC LIMIT 1
            )
            """
        ),
        {"program_id": program_id},
    )

    bind.execute(
        sa.text("DELETE FROM ai_playbook_templates WHERE id = CAST(:id AS uuid)"),
        {"id": PLAYBOOK_ID},
    )
    bind.execute(
        sa.text(
            """
            UPDATE ai_playbook_templates
            SET status = 'published', is_active = true, updated_at = now()
            WHERE id = (
                SELECT id FROM ai_playbook_templates
                WHERE funding_program_id = :program_id AND status = 'archived'
                ORDER BY version DESC LIMIT 1
            )
            """
        ),
        {"program_id": program_id},
    )

    bind.execute(
        sa.text(
            "DELETE FROM funding_program_scopes WHERE id IN "
            "(CAST(:dealer AS uuid), CAST(:main_street AS uuid))"
        ),
        {"dealer": DEALER_SCOPE_ID, "main_street": MAIN_STREET_SCOPE_ID},
    )
    bind.execute(
        sa.text(
            """
            UPDATE funding_program_catalog
            SET short_description = 'Asset-backed financing for business equipment and vehicles.',
                updated_at = now()
            WHERE id = :program_id
            """
        ),
        {"program_id": program_id},
    )

