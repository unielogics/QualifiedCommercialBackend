"""Canonical funding catalog, evidence policies, and AI decisions.

Revision ID: 0211_program_catalog_ai_evidence
Revises: 0210_multi_agent_collecting_docs
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0211_program_catalog_ai_evidence"
down_revision = "0210_multi_agent_collecting_docs"
branch_labels = None
depends_on = None


PROGRAMS = [
    (
        "sba_7a",
        "sba-7a",
        "SBA 7(a)",
        "Flexible SBA financing for acquisitions, expansion, working capital, and owner-occupied property.",
    ),
    (
        "sba_504",
        "sba-504",
        "SBA 504",
        "Long-term fixed-asset and owner-occupied commercial real-estate financing.",
    ),
    (
        "sba_express",
        "sba-express",
        "SBA Express",
        "Streamlined SBA financing for smaller, time-sensitive business needs.",
    ),
    (
        "dscr",
        "dscr-rental",
        "DSCR Rental",
        "Rental-property financing underwritten primarily on property cash flow.",
    ),
    (
        "fix_flip",
        "fix-and-flip",
        "Fix & Flip",
        "Short-term acquisition and rehabilitation capital for value-add property.",
    ),
    (
        "bridge_purchase",
        "bridge",
        "Bridge / Purchase",
        "Short-term acquisition capital for time-sensitive real-estate transactions.",
    ),
    (
        "construction_capital",
        "construction",
        "Construction Capital",
        "Draw-based capital for ground-up and major rehabilitation projects.",
    ),
    (
        "portfolio_lending",
        "portfolio",
        "Portfolio Lending",
        "One facility underwritten across multiple income-producing properties.",
    ),
    (
        "dealer_working_capital",
        "dealer-working-capital",
        "Dealer Working Capital",
        "Working capital sized to dealership operations and cash flow.",
    ),
    (
        "dealer_real_estate_capital",
        "dealer-real-estate-backed",
        "Real-Estate-Backed Dealer Capital",
        "Dealer capital supported by declared commercial real-estate collateral.",
    ),
    (
        "mca_refinance",
        "mca-refinance",
        "MCA Refinance",
        "Restructuring for disclosed merchant-cash-advance obligations.",
    ),
    (
        "floorplan_support",
        "floorplan-support",
        "Floorplan Support",
        "Inventory and floorplan capital for eligible dealerships.",
    ),
    (
        "revenue_based_financing",
        "revenue-based-financing",
        "Revenue-Based Financing",
        "Operating capital sized from verified business revenue and deposits.",
    ),
    (
        "ez_term",
        "ez-term",
        "EZ Term",
        "Fixed-payment term financing for established operating businesses.",
    ),
    (
        "microcap",
        "microcap",
        "MicroCap",
        "Longer-amortization working capital for qualified small businesses.",
    ),
    (
        "line_of_credit",
        "lines-of-credit",
        "Lines of Credit",
        "Revolving access to working capital based on business performance.",
    ),
    (
        "equipment_financing",
        "equipment-financing",
        "Equipment Financing",
        "Asset-backed financing for business equipment and vehicles.",
    ),
    (
        "jumbo_term",
        "jumbo-term",
        "Jumbo Term",
        "Large-balance term financing for higher-revenue operating businesses.",
    ),
    (
        "hybrid_term_loc",
        "hybrid-term-loc",
        "Hybrid Term / LOC",
        "A fixed term component paired with revolving availability.",
    ),
    (
        "transportation_finance",
        "transportation-finance",
        "Transportation Finance",
        "Equipment and working capital for transportation operations.",
    ),
    (
        "sba_grocery",
        "sba-grocery",
        "SBA Grocery",
        "SBA financing for qualifying grocery, food, and distribution businesses.",
    ),
    (
        "sba_made_in_america",
        "sba-made-in-america",
        "SBA Made in America",
        "SBA financing for qualifying domestic manufacturing activity.",
    ),
]

ALIASES = {
    "sba_7a": ["sba", "sba_7a_standard"],
    "dscr": ["dscr_purchase", "dscr_refi", "dscr_rental"],
    "fix_flip": ["fix_and_flip"],
    "bridge_purchase": ["bridge", "purchase_bridge"],
    "construction_capital": ["construction", "ground_up", "ground_up_construction"],
    "portfolio_lending": ["portfolio"],
    "dealer_real_estate_capital": ["real_estate_backed", "dealer_real_estate_backed"],
    "ez_term": ["term_loan_3_5_year", "ez_term_loan"],
    "microcap": ["term_loan_10_year", "microcap_working_capital"],
    "jumbo_term": ["jumbo_term_loan"],
    "hybrid_term_loc": ["term_loan_loc_hybrid"],
}


def _scope(
    program_id: uuid.UUID,
    vertical: str,
    *,
    scope_key: str = "default",
    intake_variants: list[str] | None = None,
    intent_keys: list[str] | None = None,
    naics_prefixes: list[str] | None = None,
    industry_keys: list[str] | None = None,
    required_fact_keys: list[str] | None = None,
) -> dict:
    return {
        "id": uuid.uuid4(),
        "program_id": program_id,
        "vertical": vertical,
        "scope_key": scope_key,
        "intake_variants": intake_variants or [],
        "intent_keys": intent_keys or [],
        "naics_prefixes": naics_prefixes or [],
        "industry_keys": industry_keys or [],
        "required_fact_keys": required_fact_keys or [],
        "is_active": True,
    }


def _catalog_scopes(ids: dict[str, uuid.UUID]) -> list[dict]:
    scopes: list[dict] = []
    for key in ("sba_7a", "sba_504", "sba_express"):
        for vertical in ("real_estate", "dealer", "main_street"):
            scopes.append(_scope(ids[key], vertical))
    for key in ("dscr", "fix_flip", "bridge_purchase", "construction_capital", "portfolio_lending"):
        scopes.append(_scope(ids[key], "real_estate"))
    scopes.extend(
        [
            _scope(ids["dealer_working_capital"], "dealer"),
            _scope(
                ids["dealer_real_estate_capital"],
                "dealer",
                required_fact_keys=["declared_collateral"],
            ),
            _scope(
                ids["mca_refinance"],
                "dealer",
                scope_key="dealer_mca",
                required_fact_keys=["mca_obligations_present"],
            ),
            _scope(ids["mca_refinance"], "mca", scope_key="mca_intake"),
            _scope(
                ids["floorplan_support"],
                "dealer",
                required_fact_keys=["floorplan_inventory_present"],
            ),
            _scope(ids["revenue_based_financing"], "dealer"),
        ]
    )
    for key in (
        "ez_term",
        "microcap",
        "line_of_credit",
        "equipment_financing",
        "jumbo_term",
        "hybrid_term_loc",
    ):
        scopes.append(_scope(ids[key], "main_street"))
    scopes.extend(
        [
            _scope(
                ids["transportation_finance"],
                "main_street",
                scope_key="transportation",
                naics_prefixes=["48", "49"],
                industry_keys=["trucking_logistics"],
            ),
            _scope(
                ids["sba_grocery"],
                "main_street",
                scope_key="grocery_food_distribution",
                naics_prefixes=["311", "4244", "4245", "445"],
                industry_keys=["grocery_commodities", "restaurant_food_service"],
            ),
            _scope(
                ids["sba_made_in_america"],
                "main_street",
                scope_key="manufacturing",
                naics_prefixes=["31", "32", "33"],
                industry_keys=["manufacturing"],
            ),
        ]
    )
    return scopes


def upgrade() -> None:
    op.create_table(
        "funding_program_catalog",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("program_key", sa.String(length=64), nullable=False),
        sa.Column("public_slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("short_description", sa.Text(), nullable=True),
        sa.Column(
            "aliases", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "retired_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('active','retired')", name="ck_funding_program_catalog_status"
        ),
        sa.UniqueConstraint("program_key", name="uq_funding_program_catalog_key"),
        sa.UniqueConstraint("public_slug", name="uq_funding_program_catalog_slug"),
    )
    op.create_index(
        "ix_funding_program_catalog_status_order",
        "funding_program_catalog",
        ["status", "display_order"],
    )
    op.create_table(
        "funding_program_scopes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "program_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("funding_program_catalog.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("vertical", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.String(length=80), nullable=False, server_default="default"),
        sa.Column(
            "intake_variants",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "intent_keys", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "naics_prefixes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "industry_keys",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "required_fact_keys",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "vertical IN ('real_estate','dealer','main_street','mca')",
            name="ck_funding_program_scope_vertical",
        ),
        sa.UniqueConstraint("program_id", "vertical", "scope_key", name="uq_funding_program_scope"),
    )
    op.create_index(
        "ix_funding_program_scopes_vertical",
        "funding_program_scopes",
        ["vertical", "program_id"],
    )

    catalog_table = sa.table(
        "funding_program_catalog",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("program_key", sa.String()),
        sa.column("public_slug", sa.String()),
        sa.column("name", sa.String()),
        sa.column("short_description", sa.Text()),
        sa.column("aliases", postgresql.JSONB()),
        sa.column("display_order", sa.Integer()),
        sa.column("status", sa.String()),
    )
    ids = {key: uuid.uuid4() for key, _slug, _name, _description in PROGRAMS}
    op.bulk_insert(
        catalog_table,
        [
            {
                "id": ids[key],
                "program_key": key,
                "public_slug": slug,
                "name": name,
                "short_description": description,
                "aliases": ALIASES.get(key, []),
                "display_order": order,
                "status": "active",
            }
            for order, (key, slug, name, description) in enumerate(PROGRAMS, start=10)
        ],
    )

    scope_table = sa.table(
        "funding_program_scopes",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("program_id", postgresql.UUID(as_uuid=True)),
        sa.column("vertical", sa.String()),
        sa.column("scope_key", sa.String()),
        sa.column("intake_variants", postgresql.JSONB()),
        sa.column("intent_keys", postgresql.JSONB()),
        sa.column("naics_prefixes", postgresql.JSONB()),
        sa.column("industry_keys", postgresql.JSONB()),
        sa.column("required_fact_keys", postgresql.JSONB()),
        sa.column("is_active", sa.Boolean()),
    )
    op.bulk_insert(scope_table, _catalog_scopes(ids))

    op.add_column(
        "ai_playbook_templates",
        sa.Column("funding_program_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_ai_playbook_templates_funding_program",
        "ai_playbook_templates",
        "funding_program_catalog",
        ["funding_program_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_ai_playbook_templates_funding_program",
        "ai_playbook_templates",
        ["funding_program_id", "status", "version"],
    )
    op.add_column(
        "application_program_selections",
        sa.Column("needs_scope_review", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "application_requirement_states",
        sa.Column(
            "source_policy_keys",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "buckets",
        sa.Column("name_sync_mode", sa.String(length=16), nullable=False, server_default="custom"),
    )
    op.create_check_constraint(
        "ck_buckets_name_sync_mode",
        "buckets",
        "name_sync_mode IN ('linked','custom')",
    )

    op.create_table(
        "application_evidence_policy_selections",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
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
        sa.Column("policy_key", sa.String(length=64), nullable=False),
        sa.Column("policy_name", sa.String(length=160), nullable=False),
        sa.Column(
            "selected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("replaced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "uq_application_evidence_policy_active",
        "application_evidence_policy_selections",
        ["profile_id", "policy_key"],
        unique=True,
        postgresql_where=sa.text("replaced_at IS NULL"),
    )
    op.create_index(
        "ix_application_evidence_policy_profile",
        "application_evidence_policy_selections",
        ["profile_id", "selected_at"],
    )
    op.create_table(
        "application_requirement_evidence_decisions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "requirement_evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_requirement_evidence_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "analysis_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bucket_file_analyses.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("analysis_version", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("reason_code", sa.String(length=48), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=True),
        sa.Column("actor_kind", sa.String(length=16), nullable=False, server_default="ai"),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "supersedes_decision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_requirement_evidence_decisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "decision IN ('processing','accepted','needs_more','rejected','failed')",
            name="ck_application_requirement_evidence_decision",
        ),
        sa.CheckConstraint(
            "actor_kind IN ('ai','staff','system')",
            name="ck_application_requirement_evidence_decision_actor",
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_application_requirement_evidence_decision_idempotency"
        ),
    )
    op.create_index(
        "ix_application_requirement_evidence_decision_evidence",
        "application_requirement_evidence_decisions",
        ["requirement_evidence_id", "created_at"],
    )
    op.create_index(
        "ix_application_requirement_evidence_decision_analysis",
        "application_requirement_evidence_decisions",
        ["analysis_id"],
    )

    # Baselines are checklists, never products. Pin their latest published
    # versions to every existing profile before retiring active product rows.
    op.execute(
        """
        UPDATE ai_playbook_templates
        SET playbook_type = 'evidence_policy', funding_program_id = NULL,
            name = CASE product_key
                WHEN 'real_estate_baseline' THEN 'Initial real-estate evidence checklist'
                WHEN 'mca_baseline' THEN 'Initial MCA evidence checklist'
                ELSE 'Initial business evidence checklist'
            END
        WHERE product_key IN ('business_baseline','real_estate_baseline','mca_baseline')
        """
    )
    op.execute(
        """
        UPDATE ai_collection_requirements AS requirement
        SET verification_required = false,
            completion_mode = 'ai_can_complete'
        FROM ai_playbook_templates AS playbook
        WHERE requirement.playbook_id = playbook.id
          AND playbook.playbook_type = 'evidence_policy'
          AND requirement.requirement_key IN (
              'business_bank_statements_6_months',
              'business_tax_returns_2_years',
              'ytd_p_and_l_balance_sheet',
              'business_debt_schedule',
              'owner_personal_financial_statement',
              'real_estate_schedule',
              'property_debt_evidence',
              'current_advance_terms'
          )
        """
    )
    op.execute(
        """
        INSERT INTO application_evidence_policy_selections (
            id, profile_id, playbook_id, playbook_version, policy_key,
            policy_name, selected_at, created_at, updated_at
        )
        SELECT gen_random_uuid(), profile.id, policy.id, policy.version,
               policy.product_key, policy.name, now(), now(), now()
        FROM application_profiles AS profile
        JOIN LATERAL (
            SELECT playbook.id, playbook.version, playbook.product_key, playbook.name
            FROM ai_playbook_templates AS playbook
            WHERE playbook.playbook_type = 'evidence_policy'
              AND playbook.status = 'published'
              AND playbook.is_active = true
              AND playbook.product_key = CASE
                  WHEN profile.vertical = 'real_estate' THEN 'real_estate_baseline'
                  WHEN profile.vertical = 'mca' THEN 'mca_baseline'
                  ELSE 'business_baseline'
              END
            ORDER BY playbook.version DESC, playbook.created_at DESC
            LIMIT 1
        ) AS policy ON true
        """
    )
    op.execute(
        """
        UPDATE application_program_selections
        SET removed_at = COALESCE(removed_at, now())
        WHERE program_key IN ('business_baseline','real_estate_baseline','mca_baseline')
          AND removed_at IS NULL
        """
    )
    op.execute(
        """
        UPDATE application_requirement_states
        SET source_policy_keys = CASE
                WHEN source_program_keys ? 'real_estate_baseline' THEN '["real_estate_baseline"]'::jsonb
                WHEN source_program_keys ? 'mca_baseline' THEN '["mca_baseline"]'::jsonb
                WHEN source_program_keys ? 'business_baseline' THEN '["business_baseline"]'::jsonb
                ELSE source_policy_keys
            END,
            source_program_keys = source_program_keys
                - 'business_baseline' - 'real_estate_baseline' - 'mca_baseline'
        """
    )

    # Associate every legacy alias with its canonical product without rewriting
    # historical playbook keys or deleting old decisions.
    op.execute(
        """
        UPDATE ai_playbook_templates AS playbook
        SET funding_program_id = catalog.id
        FROM funding_program_catalog AS catalog
        WHERE playbook.playbook_type = 'loan_product'
          AND (
              playbook.product_key = catalog.program_key
              OR catalog.aliases ? playbook.product_key
          )
        """
    )
    op.execute(
        """
        WITH normalized AS (
            SELECT selection.id,
                   row_number() OVER (
                       PARTITION BY selection.profile_id, catalog.program_key
                       ORDER BY selection.selected_at DESC, selection.id DESC
                   ) AS position
            FROM application_program_selections AS selection
            JOIN ai_playbook_templates AS playbook ON playbook.id = selection.playbook_id
            JOIN funding_program_catalog AS catalog ON catalog.id = playbook.funding_program_id
            WHERE selection.removed_at IS NULL
        )
        UPDATE application_program_selections AS selection
        SET removed_at = now(), needs_scope_review = true
        FROM normalized
        WHERE selection.id = normalized.id AND normalized.position > 1
        """
    )
    op.execute(
        """
        UPDATE application_program_selections AS selection
        SET program_key = catalog.program_key,
            program_name = catalog.name
        FROM ai_playbook_templates AS playbook,
             funding_program_catalog AS catalog
        WHERE selection.playbook_id = playbook.id
          AND playbook.funding_program_id = catalog.id
          AND selection.removed_at IS NULL
        """
    )
    op.execute(
        """
        UPDATE application_program_selections AS selection
        SET needs_scope_review = true
        FROM application_profiles AS profile,
             ai_playbook_templates AS playbook
        WHERE selection.profile_id = profile.id
          AND selection.playbook_id = playbook.id
          AND selection.removed_at IS NULL
          AND playbook.funding_program_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM funding_program_scopes AS scope
              WHERE scope.program_id = playbook.funding_program_id
                AND scope.vertical = profile.vertical
                AND scope.is_active = true
          )
        """
    )
    op.execute(
        """
        UPDATE buckets AS bucket
        SET name_sync_mode = 'linked'
        WHERE EXISTS (
            SELECT 1 FROM application_profiles AS profile
            WHERE profile.primary_bucket_id = bucket.id
        ) OR EXISTS (
            SELECT 1 FROM public_underwriting_intakes AS intake
            WHERE intake.bucket_id = bucket.id
        ) OR EXISTS (
            SELECT 1 FROM dos_dealers AS dealer
            WHERE dealer.bucket_id = bucket.id
        )
        """
    )


def downgrade() -> None:
    op.drop_table("application_requirement_evidence_decisions")
    op.drop_index(
        "ix_application_evidence_policy_profile",
        table_name="application_evidence_policy_selections",
    )
    op.drop_index(
        "uq_application_evidence_policy_active", table_name="application_evidence_policy_selections"
    )
    op.drop_table("application_evidence_policy_selections")
    op.drop_constraint("ck_buckets_name_sync_mode", "buckets", type_="check")
    op.drop_column("buckets", "name_sync_mode")
    op.drop_column("application_requirement_states", "source_policy_keys")
    op.drop_column("application_program_selections", "needs_scope_review")
    op.drop_index("ix_ai_playbook_templates_funding_program", table_name="ai_playbook_templates")
    op.drop_constraint(
        "fk_ai_playbook_templates_funding_program", "ai_playbook_templates", type_="foreignkey"
    )
    op.drop_column("ai_playbook_templates", "funding_program_id")
    op.drop_index("ix_funding_program_scopes_vertical", table_name="funding_program_scopes")
    op.drop_table("funding_program_scopes")
    op.drop_index("ix_funding_program_catalog_status_order", table_name="funding_program_catalog")
    op.drop_table("funding_program_catalog")
