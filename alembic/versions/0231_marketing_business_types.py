"""Add Field Desk business types and type-aware outreach collateral.

Revision ID: 0231_marketing_business_types
Revises: 0230_shared_use_of_funds

Existing prospects remain dealers. Existing applications inherit a durable
profile or handoff-intake classification when one exists and otherwise remain
dealers. Existing collateral bytes and draft attachment snapshots are not
rewritten; the new profile and bundle snapshots add immutable metadata around
those records.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0231_marketing_business_types"
down_revision = "0230_shared_use_of_funds"
branch_labels = None
depends_on = None


LEAD_TYPE_CHECK = "lead_type IN ('dealer','main_street','real_estate')"
FUNDING_INTENT_CHECK = (
    "funding_intent IS NULL OR funding_intent IN "
    "('general_capital','working_capital','equipment','business_acquisition',"
    "'real_estate','debt_refinance','mca_refinance','other')"
)
PURPOSES = (
    "information",
    "missed_call",
    "callback_confirmation",
    "client_will_call_back",
    "booking",
    "general",
)


def _purpose_templates(*, audience: str, desk: str) -> dict[str, dict[str, str]]:
    return {
        "information": {
            "subject": "Commercial financing resources for {business_name}",
            "body": (
                "Hi {first_name},\n\nI am following up with an overview of {audience}-focused "
                "programs Qualified Commercial can discuss with {business_name}.\n\nReply with "
                "what you are planning and we can help identify practical next steps."
            ),
        },
        "missed_call": {
            "subject": "Sorry we missed you — {business_name}",
            "body": (
                "Hi {first_name},\n\nI tried to reach you and wanted to leave a quick note. "
                f"Qualified Commercial's {desk} is available to learn about what your "
                "{audience} is planning. Reply when it is convenient and we can discuss "
                "practical next steps."
            ),
        },
        "callback_confirmation": {
            "subject": "Following up with {business_name}",
            "body": (
                "Hi {first_name},\n\nThank you for speaking with me. I will follow up at the time "
                "we discussed. If anything changes, reply here and we can find a better time."
            ),
        },
        "client_will_call_back": {
            "subject": "Thank you for the update — {business_name}",
            "body": (
                "Hi {first_name},\n\nThank you for the update. I will watch for your call and am "
                "happy to discuss what your {audience} is planning when the timing works for you. "
                "You can also reply here with any questions."
            ),
        },
        "booking": {
            "subject": "Next steps for {business_name}",
            "body": (
                "Hi {first_name},\n\nThank you for your interest. Choose a time below that works "
                "for you, and we can discuss what your {audience} is planning and the information "
                "lender review may require."
            ),
        },
        "general": {
            "subject": "Following up with {business_name}",
            "body": (
                "Hi {first_name},\n\nI wanted to follow up and learn more about what your "
                "{audience} is planning. Reply when it is convenient and we can discuss practical "
                "next steps."
            ),
        },
    }


def _profile_seeds() -> tuple[dict[str, Any], ...]:
    dealer_templates = _purpose_templates(audience="dealership", desk="Dealer Desk")
    # Preserve the original Dealer Desk general-email behavior.
    dealer_templates["general"] = dict(dealer_templates["information"])
    return (
        {
            "lead_type": "dealer",
            "version": 1,
            "status": "active",
            "display_name": "Dealer",
            "desk_name": "Dealer Desk",
            "audience_label": "dealership",
            "audience_plural": "dealers",
            "website_url": "https://qualifiedcommercial.com/industries/auto",
            "drafting_guidance": (
                "Use concise, practical language for a dealership owner or operator. "
                "Focus on inventory, property, equipment, or working-capital plans only when supplied."
            ),
            "purpose_templates": dealer_templates,
        },
        {
            "lead_type": "main_street",
            "version": 1,
            "status": "active",
            "display_name": "Main Street Business",
            "desk_name": "Business Desk",
            "audience_label": "business",
            "audience_plural": "business owners",
            "website_url": "https://qualifiedcommercial.com/industries/business",
            "drafting_guidance": (
                "Use approachable, plain language for an owner-operated business. "
                "Keep the message practical and avoid dealership or property-investor terminology."
            ),
            "purpose_templates": _purpose_templates(
                audience="business", desk="Business Desk"
            ),
        },
        {
            "lead_type": "real_estate",
            "version": 1,
            "status": "active",
            "display_name": "Commercial Real Estate",
            "desk_name": "Real Estate Desk",
            "audience_label": "property business",
            "audience_plural": "property owners and investors",
            "website_url": "https://qualifiedcommercial.com/industries/realestate",
            "drafting_guidance": (
                "Use professional commercial-real-estate language for an owner, investor, or operator. "
                "Do not assume a property type, transaction, value, leverage, or occupancy."
            ),
            "purpose_templates": _purpose_templates(
                audience="property business", desk="Real Estate Desk"
            ),
        },
    )


def _profile_snapshot(seed: dict[str, Any]) -> dict[str, Any]:
    return {
        "lead_type": seed["lead_type"],
        "version": seed["version"],
        "display_name": seed["display_name"],
        "desk_name": seed["desk_name"],
        "audience_label": seed["audience_label"],
        "audience_plural": seed["audience_plural"],
        "website_url": seed["website_url"],
        "drafting_guidance": seed["drafting_guidance"],
        "purpose_templates": seed["purpose_templates"],
    }


def _snapshot_hash(snapshot: dict[str, Any]) -> str:
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sql_slug(expression: str) -> str:
    return (
        "lower(replace(replace(trim(COALESCE("
        f"{expression}, '')), '-', '_'), ' ', '_'))"
    )


def _lead_type_sql(expression: str, *, fallback: str) -> str:
    slug = _sql_slug(expression)
    return (
        "CASE "
        f"WHEN {slug} IN "
        "('dealer','dealership','auto_dealer','car_dealer','dealer_gatekeeper_v1') "
        "THEN 'dealer' "
        f"WHEN {slug} IN "
        "('main_street','mainstreet','business','operating_business','main_street_v1',"
        "'mca_refi_v1','mca','mca_refinance') THEN 'main_street' "
        f"WHEN {slug} IN "
        "('real_estate','realestate','property','real_estate_dscr_v1','funding_review',"
        "'commercial_foreclosure_bailout_v1','foreclosure_rescue') THEN 'real_estate' "
        f"ELSE {fallback} END"
    )


def _intake_variant_lead_type_sql(expression: str) -> str:
    slug = _sql_slug(expression)
    return (
        "CASE "
        f"WHEN {slug} LIKE '%mca%' THEN 'main_street' "
        f"WHEN {slug} LIKE '%main_street%' "
        f"OR {slug} IN ('business','operating_business') THEN 'main_street' "
        f"WHEN {slug} LIKE '%dealer%' THEN 'dealer' "
        f"WHEN {slug} LIKE '%real_estate%' "
        f"OR {slug} LIKE '%funding_review%' "
        f"OR {slug} LIKE '%foreclosure%' THEN 'real_estate' "
        "ELSE 'dealer' END"
    )


def _funding_intent_sql(expression: str, *, fallback: str = "NULL") -> str:
    slug = _sql_slug(expression)
    return (
        "CASE "
        f"WHEN {slug} IN ('general_capital','general','capital','floorplan','floor_plan') "
        "THEN 'general_capital' "
        f"WHEN {slug} = 'working_capital' THEN 'working_capital' "
        f"WHEN {slug} = 'equipment' THEN 'equipment' "
        f"WHEN {slug} IN ('business_acquisition','acquisition','purchase_business') "
        "THEN 'business_acquisition' "
        f"WHEN {slug} IN ('real_estate','property','purchase','cash_out','construction') "
        "THEN 'real_estate' "
        f"WHEN {slug} IN ('debt_refinance','refinance','refinance_debt',"
        "'debt_consolidation') THEN 'debt_refinance' "
        f"WHEN {slug} IN ('mca_refinance','mca_refi',"
        "'merchant_cash_advance_refinance','mca') THEN 'mca_refinance' "
        f"WHEN {slug} = 'other' THEN 'other' "
        f"ELSE {fallback} END"
    )


def _assert_seed_contract() -> None:
    seeds = _profile_seeds()
    if {seed["lead_type"] for seed in seeds} != {
        "dealer",
        "main_street",
        "real_estate",
    }:
        raise RuntimeError("Field Desk migration must seed exactly three outreach profiles")
    if len(PURPOSES) != 6 or len(set(PURPOSES)) != len(PURPOSES):
        raise RuntimeError("Field Desk migration must seed six unique collateral purposes")
    if PURPOSES[0] != "information":
        raise RuntimeError("The generic outreach default purpose must be information")
    if any(set(seed["purpose_templates"]) != set(PURPOSES) for seed in seeds):
        raise RuntimeError("Every outreach profile must define all collateral purposes")


def _add_business_classification(
    table_name: str,
    *,
    lead_constraint: str,
    intent_constraint: str,
) -> None:
    op.add_column(
        table_name,
        sa.Column(
            "lead_type",
            sa.String(length=32),
            nullable=False,
            server_default="dealer",
        ),
    )
    op.add_column(
        table_name,
        sa.Column("funding_intent", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(lead_constraint, table_name, LEAD_TYPE_CHECK)
    op.create_check_constraint(intent_constraint, table_name, FUNDING_INTENT_CHECK)
    op.create_index(f"ix_{table_name}_lead_type", table_name, ["lead_type"])


def _upgrade_prospect_opportunity_uniqueness() -> None:
    """Scope race-proof duplicate guards to one contact opportunity."""

    op.drop_constraint(
        "uq_dealer_prospect_primary_contact",
        "dealer_prospects",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_dealer_prospect_contact_opportunity",
        "dealer_prospects",
        ["primary_contact_id", "dealer_name_normalized", "lead_type"],
    )
    op.drop_index(
        "uq_dealer_prospect_email_active", table_name="dealer_prospects"
    )
    op.drop_index(
        "uq_dealer_prospect_phone_active", table_name="dealer_prospects"
    )
    op.create_index(
        "uq_dealer_prospect_email_active",
        "dealer_prospects",
        ["dealer_name_normalized", "lead_type", "email_normalized"],
        unique=True,
        postgresql_where=sa.text(
            "archived_at IS NULL AND email_normalized IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_dealer_prospect_phone_active",
        "dealer_prospects",
        ["dealer_name_normalized", "lead_type", "phone_normalized"],
        unique=True,
        postgresql_where=sa.text(
            "archived_at IS NULL AND phone_normalized IS NOT NULL"
        ),
    )


def _downgrade_prospect_opportunity_uniqueness() -> None:
    # The prior schema permits only one prospect per contact and does not
    # distinguish active identity guards by lead type.  Refuse to partially
    # downgrade once production has valid multi-opportunity data that cannot
    # be represented by those legacy constraints.
    bind = op.get_bind()
    legacy_conflicts = {
        "more than one opportunity for the same contact": """
            SELECT EXISTS (
                SELECT 1
                FROM dealer_prospects
                GROUP BY primary_contact_id
                HAVING count(*) > 1
            )
        """,
        "active email opportunities separated only by lead type": """
            SELECT EXISTS (
                SELECT 1
                FROM dealer_prospects
                WHERE archived_at IS NULL AND email_normalized IS NOT NULL
                GROUP BY dealer_name_normalized, email_normalized
                HAVING count(*) > 1
            )
        """,
        "active phone opportunities separated only by lead type": """
            SELECT EXISTS (
                SELECT 1
                FROM dealer_prospects
                WHERE archived_at IS NULL AND phone_normalized IS NOT NULL
                GROUP BY dealer_name_normalized, phone_normalized
                HAVING count(*) > 1
            )
        """,
    }
    for description, query in legacy_conflicts.items():
        if bind.execute(sa.text(query)).scalar_one():
            raise RuntimeError(
                "Cannot downgrade marketing business types: " + description
            )

    op.drop_index(
        "uq_dealer_prospect_phone_active", table_name="dealer_prospects"
    )
    op.drop_index(
        "uq_dealer_prospect_email_active", table_name="dealer_prospects"
    )
    op.drop_constraint(
        "uq_dealer_prospect_contact_opportunity",
        "dealer_prospects",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_dealer_prospect_primary_contact",
        "dealer_prospects",
        ["primary_contact_id"],
    )
    op.create_index(
        "uq_dealer_prospect_email_active",
        "dealer_prospects",
        ["dealer_name_normalized", "email_normalized"],
        unique=True,
        postgresql_where=sa.text(
            "archived_at IS NULL AND email_normalized IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_dealer_prospect_phone_active",
        "dealer_prospects",
        ["dealer_name_normalized", "phone_normalized"],
        unique=True,
        postgresql_where=sa.text(
            "archived_at IS NULL AND phone_normalized IS NOT NULL"
        ),
    )


def _upgrade_generic_information_defaults() -> None:
    # Keep historical draft values unchanged: dispatch canonicalizes the
    # dealer_information alias.  Only future inserts use the neutral default.
    op.execute(
        sa.text(
            """
            UPDATE dealer_prospect_outcome_definitions
            SET action_config = jsonb_set(
                action_config,
                '{email_action}',
                '"information_pack"'::jsonb,
                true
            )
            WHERE key = 'interested_send_information'
              AND is_system = true
              AND action_config ->> 'email_action' = 'dealer_information_pack'
            """
        )
    )


def _downgrade_generic_information_defaults() -> None:
    op.execute(
        sa.text(
            """
            UPDATE dealer_prospect_outcome_definitions
            SET action_config = jsonb_set(
                action_config,
                '{email_action}',
                '"dealer_information_pack"'::jsonb,
                true
            )
            WHERE key = 'interested_send_information'
              AND is_system = true
              AND action_config ->> 'email_action' = 'information_pack'
            """
        )
    )


def _create_outreach_profiles() -> None:
    op.create_table(
        "prospect_outreach_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("lead_type", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="active"
        ),
        sa.Column("display_name", sa.String(length=80), nullable=False),
        sa.Column("desk_name", sa.String(length=80), nullable=False),
        sa.Column("audience_label", sa.String(length=120), nullable=False),
        sa.Column("audience_plural", sa.String(length=120), nullable=False),
        sa.Column("website_url", sa.String(length=500), nullable=False),
        sa.Column("drafting_guidance", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "purpose_templates",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "retired_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "lead_type", "version", name="uq_prospect_outreach_profile_version"
        ),
        sa.CheckConstraint(
            "status IN ('active','retired')",
            name="ck_prospect_outreach_profile_status",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_prospect_outreach_profile_version"
        ),
    )
    op.create_index(
        "uq_prospect_outreach_profile_active",
        "prospect_outreach_profiles",
        ["lead_type"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    profile_table = sa.table(
        "prospect_outreach_profiles",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("lead_type", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("status", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("desk_name", sa.String()),
        sa.column("audience_label", sa.String()),
        sa.column("audience_plural", sa.String()),
        sa.column("website_url", sa.String()),
        sa.column("drafting_guidance", sa.Text()),
        sa.column("purpose_templates", postgresql.JSONB()),
    )
    op.bulk_insert(
        profile_table,
        [{"id": uuid.uuid4(), **seed} for seed in _profile_seeds()],
    )


def _upgrade_collateral_assets() -> None:
    op.add_column(
        "marketing_collateral_assets",
        sa.Column(
            "lead_type",
            sa.String(length=32),
            nullable=False,
            server_default="dealer",
        ),
    )
    op.add_column(
        "marketing_collateral_assets",
        sa.Column(
            "purposes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[\"information\"]'::jsonb"),
        ),
    )
    op.add_column(
        "marketing_collateral_assets",
        sa.Column(
            "included_by_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )

    # Legacy Dealer Desk PDFs were globally available rather than curated by
    # purpose.  Preserve that reach across all six seeded bundles.  The column
    # default above intentionally remains information-only for future uploads.
    op.execute(
        sa.text(
            """
            UPDATE marketing_collateral_assets
            SET lead_type = 'dealer',
                purposes = jsonb_build_array(
                    'information',
                    'missed_call',
                    'callback_confirmation',
                    'client_will_call_back',
                    'booking',
                    'general'
                ),
                included_by_default = true
            """
        )
    )

    op.drop_constraint(
        "uq_marketing_collateral_version",
        "marketing_collateral_assets",
        type_="unique",
    )
    op.drop_index(
        "ix_marketing_collateral_active_order",
        table_name="marketing_collateral_assets",
    )
    op.drop_index(
        "uq_marketing_collateral_one_active_version",
        table_name="marketing_collateral_assets",
    )
    op.create_unique_constraint(
        "uq_marketing_collateral_version",
        "marketing_collateral_assets",
        ["assignment", "lead_type", "logical_key", "version"],
    )
    op.create_index(
        "ix_marketing_collateral_active_order",
        "marketing_collateral_assets",
        ["assignment", "lead_type", "status", "sort_order"],
    )
    op.create_index(
        "uq_marketing_collateral_one_active_version",
        "marketing_collateral_assets",
        ["assignment", "lead_type", "logical_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def _create_collateral_bundles() -> dict[str, uuid.UUID]:
    op.create_table(
        "marketing_collateral_bundles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("lead_type", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=48), nullable=False),
        sa.Column("name", sa.String(length=180), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="draft"
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "published_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "retired_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "lead_type",
            "purpose",
            "version",
            name="uq_marketing_collateral_bundle_version",
        ),
        sa.CheckConstraint(
            "status IN ('draft','published','retired')",
            name="ck_marketing_collateral_bundle_status",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_marketing_collateral_bundle_version"
        ),
        sa.CheckConstraint(
            "revision >= 1", name="ck_marketing_collateral_bundle_revision"
        ),
    )
    op.create_index(
        "uq_marketing_collateral_bundle_published",
        "marketing_collateral_bundles",
        ["lead_type", "purpose"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )
    op.create_index(
        "ix_marketing_collateral_bundle_lookup",
        "marketing_collateral_bundles",
        ["lead_type", "purpose", "status"],
    )

    op.create_table(
        "marketing_collateral_bundle_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bundle_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("marketing_collateral_bundles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("marketing_collateral_assets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("inclusion_mode", sa.String(length=16), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("asset_name", sa.String(length=180), nullable=False),
        sa.Column("asset_version", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(length=240), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.UniqueConstraint(
            "bundle_id",
            "asset_id",
            name="uq_marketing_collateral_bundle_item_asset",
        ),
        sa.UniqueConstraint(
            "bundle_id",
            "sort_order",
            name="uq_marketing_collateral_bundle_item_order",
        ),
        sa.CheckConstraint(
            "inclusion_mode IN ('default','optional')",
            name="ck_marketing_collateral_bundle_item_mode",
        ),
        sa.CheckConstraint(
            "sort_order >= 0", name="ck_marketing_collateral_bundle_item_order"
        ),
        sa.CheckConstraint(
            "size_bytes > 0", name="ck_marketing_collateral_bundle_item_size"
        ),
        sa.CheckConstraint(
            "char_length(sha256) = 64",
            name="ck_marketing_collateral_bundle_item_sha",
        ),
    )
    op.create_index(
        "ix_marketing_collateral_bundle_items_order",
        "marketing_collateral_bundle_items",
        ["bundle_id", "sort_order"],
    )

    published_at = datetime.now(UTC)
    bundle_ids = {purpose: uuid.uuid4() for purpose in PURPOSES}
    bundle_table = sa.table(
        "marketing_collateral_bundles",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("lead_type", sa.String()),
        sa.column("purpose", sa.String()),
        sa.column("name", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("revision", sa.Integer()),
        sa.column("status", sa.String()),
        sa.column("published_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        bundle_table,
        [
            {
                "id": bundle_ids[purpose],
                "lead_type": "dealer",
                "purpose": purpose,
                "name": f"Dealer {purpose.replace('_', ' ').title()}",
                "version": 1,
                "revision": 2,
                "status": "published",
                "published_at": published_at,
            }
            for purpose in PURPOSES
        ],
    )
    return bundle_ids


def _seed_legacy_bundle_items(bundle_ids: dict[str, uuid.UUID]) -> None:
    assets = (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT id, name, version, file_name, size_bytes, sha256,
                       included_by_default
                FROM marketing_collateral_assets
                WHERE assignment = 'dealer_outreach'
                  AND lead_type = 'dealer'
                  AND status = 'active'
                  AND validation_status = 'passed_antivirus'
                  AND content_type = 'application/pdf'
                ORDER BY sort_order, logical_key, version
                """
            )
        )
        .mappings()
        .all()
    )
    if not assets:
        return

    item_table = sa.table(
        "marketing_collateral_bundle_items",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("bundle_id", postgresql.UUID(as_uuid=True)),
        sa.column("asset_id", postgresql.UUID(as_uuid=True)),
        sa.column("inclusion_mode", sa.String()),
        sa.column("sort_order", sa.Integer()),
        sa.column("asset_name", sa.String()),
        sa.column("asset_version", sa.Integer()),
        sa.column("file_name", sa.String()),
        sa.column("size_bytes", sa.Integer()),
        sa.column("sha256", sa.String()),
    )
    op.bulk_insert(
        item_table,
        [
            {
                "id": uuid.uuid4(),
                "bundle_id": bundle_ids[purpose],
                "asset_id": asset["id"],
                "inclusion_mode": (
                    "default" if asset["included_by_default"] else "optional"
                ),
                "sort_order": index * 10,
                "asset_name": asset["name"],
                "asset_version": asset["version"],
                "file_name": asset["file_name"],
                "size_bytes": asset["size_bytes"],
                "sha256": asset["sha256"],
            }
            for purpose in PURPOSES
            for index, asset in enumerate(assets)
        ],
    )


def _upgrade_email_drafts() -> None:
    op.alter_column(
        "dealer_prospect_email_drafts",
        "purpose",
        existing_type=sa.String(length=32),
        type_=sa.String(length=48),
        existing_nullable=False,
        existing_server_default="dealer_information",
        server_default="information",
    )
    op.add_column(
        "dealer_prospect_email_drafts",
        sa.Column(
            "lead_type",
            sa.String(length=32),
            nullable=False,
            server_default="dealer",
        ),
    )
    op.add_column(
        "dealer_prospect_email_drafts",
        sa.Column("funding_intent", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "dealer_prospect_email_drafts",
        sa.Column(
            "outreach_profile_key",
            sa.String(length=32),
            nullable=False,
            server_default="dealer",
        ),
    )
    op.add_column(
        "dealer_prospect_email_drafts",
        sa.Column(
            "outreach_profile_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "dealer_prospect_email_drafts",
        sa.Column("outreach_profile_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "dealer_prospect_email_drafts",
        sa.Column(
            "outreach_profile_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "dealer_prospect_email_drafts",
        sa.Column("collateral_bundle_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "dealer_prospect_email_drafts",
        sa.Column("collateral_bundle_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "dealer_prospect_email_drafts",
        sa.Column(
            "collateral_bundle_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_foreign_key(
        "fk_dealer_prospect_email_drafts_collateral_bundle_id",
        "dealer_prospect_email_drafts",
        "marketing_collateral_bundles",
        ["collateral_bundle_id"],
        ["id"],
        ondelete="SET NULL",
    )

    dealer_seed = next(
        seed for seed in _profile_seeds() if seed["lead_type"] == "dealer"
    )
    snapshot = _profile_snapshot(dealer_seed)
    op.get_bind().execute(
        sa.text(
            """
            UPDATE dealer_prospect_email_drafts
            SET lead_type = 'dealer',
                outreach_profile_key = 'dealer',
                outreach_profile_version = 1,
                outreach_profile_hash = :snapshot_hash,
                outreach_profile_snapshot = CAST(:snapshot AS jsonb),
                collateral_bundle_id = NULL,
                collateral_bundle_version = NULL,
                collateral_bundle_snapshot = '{}'::jsonb
            """
        ),
        {
            "snapshot_hash": _snapshot_hash(snapshot),
            "snapshot": json.dumps(snapshot, separators=(",", ":")),
        },
    )
    op.alter_column(
        "dealer_prospect_email_drafts",
        "outreach_profile_hash",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_dealer_prospect_email_draft_profile_hash",
        "dealer_prospect_email_drafts",
        "char_length(outreach_profile_hash) = 64",
    )
    op.create_check_constraint(
        "ck_dealer_prospect_email_draft_profile_version",
        "dealer_prospect_email_drafts",
        "outreach_profile_version >= 1",
    )
    op.create_check_constraint(
        "ck_dealer_prospect_email_draft_bundle_identity",
        "dealer_prospect_email_drafts",
        "(collateral_bundle_id IS NULL AND collateral_bundle_version IS NULL) OR "
        "(collateral_bundle_id IS NOT NULL AND collateral_bundle_version >= 1)",
    )


def _assert_database_backfill() -> None:
    bind = op.get_bind()
    profile_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM prospect_outreach_profiles
            WHERE status = 'active' AND version = 1
              AND lead_type IN ('dealer','main_street','real_estate')
            """
        )
    ).scalar_one()
    if int(profile_count) != 3:
        raise RuntimeError("Field Desk outreach profile seed backfill is incomplete")

    legacy_information_actions = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM dealer_prospect_outcome_definitions
            WHERE key = 'interested_send_information' AND is_system = true
              AND action_config ->> 'email_action' = 'dealer_information_pack'
            """
        )
    ).scalar_one()
    if int(legacy_information_actions):
        raise RuntimeError("Generic information-pack workflow backfill is incomplete")

    bundles_match = bind.execute(
        sa.text(
            """
            WITH qualifying_assets AS (
                SELECT count(*) AS asset_count
                FROM marketing_collateral_assets
                WHERE assignment = 'dealer_outreach'
                  AND lead_type = 'dealer'
                  AND status = 'active'
                  AND validation_status = 'passed_antivirus'
                  AND content_type = 'application/pdf'
            ), seeded_bundles AS (
                SELECT bundle.id, count(item.id) AS item_count
                FROM marketing_collateral_bundles AS bundle
                LEFT JOIN marketing_collateral_bundle_items AS item
                  ON item.bundle_id = bundle.id
                WHERE bundle.lead_type = 'dealer'
                  AND bundle.version = 1
                  AND bundle.status = 'published'
                  AND bundle.purpose IN (
                      'information','missed_call','callback_confirmation',
                      'client_will_call_back','booking','general'
                  )
                GROUP BY bundle.id
            )
            SELECT count(*) = 6
               AND COALESCE(bool_and(
                   seeded_bundles.item_count = qualifying_assets.asset_count
               ), false)
            FROM seeded_bundles CROSS JOIN qualifying_assets
            """
        )
    ).scalar_one()
    if not bundles_match:
        raise RuntimeError("Legacy Dealer Desk collateral bundle backfill is incomplete")

    invalid_asset_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM marketing_collateral_assets
            WHERE lead_type <> 'dealer'
               OR NOT purposes @> '[
                   "information", "missed_call", "callback_confirmation",
                   "client_will_call_back", "booking", "general"
               ]'::jsonb
            """
        )
    ).scalar_one()
    if int(invalid_asset_count):
        raise RuntimeError("Legacy collateral purpose compatibility backfill is incomplete")

    invalid_appointment_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM dos_rep_appointments AS appointment
            LEFT JOIN dealer_prospects AS prospect
              ON prospect.id = appointment.prospect_id
            LEFT JOIN dos_dealers AS dealer
              ON dealer.id = appointment.dealer_id
            WHERE (
                appointment.prospect_id IS NOT NULL
                AND (
                    appointment.lead_type IS DISTINCT FROM prospect.lead_type
                    OR appointment.funding_intent IS DISTINCT FROM prospect.funding_intent
                )
            ) OR (
                appointment.prospect_id IS NULL
                AND appointment.dealer_id IS NOT NULL
                AND (
                    appointment.lead_type IS DISTINCT FROM dealer.lead_type
                    OR appointment.funding_intent IS DISTINCT FROM dealer.funding_intent
                )
            )
            """
        )
    ).scalar_one()
    if int(invalid_appointment_count):
        raise RuntimeError("Appointment business classification backfill is incomplete")


def upgrade() -> None:
    _assert_seed_contract()
    _add_business_classification(
        "dealer_prospects",
        lead_constraint="ck_dealer_prospect_lead_type",
        intent_constraint="ck_dealer_prospect_funding_intent",
    )
    _upgrade_prospect_opportunity_uniqueness()
    _add_business_classification(
        "dos_dealers",
        lead_constraint="ck_dos_dealers_lead_type",
        intent_constraint="ck_dos_dealers_funding_intent",
    )
    _add_business_classification(
        "dos_rep_appointments",
        lead_constraint="ck_dos_rep_appointment_lead_type",
        intent_constraint="ck_dos_rep_appointment_funding_intent",
    )
    _upgrade_generic_information_defaults()

    # Start with the legacy Portfolio vocabulary as the lowest-precedence
    # classification source.
    op.execute(
        sa.text(
            f"""
            UPDATE dos_dealers
            SET funding_intent = {_funding_intent_sql('funding_purpose')}
            """
        )
    )

    # A durable ApplicationProfile is authoritative for a Portfolio file.
    # Older ``mca`` profiles become Main Street + the explicit MCA intent.
    profile_vertical_slug = _sql_slug("profile.vertical")
    op.execute(
        sa.text(
            f"""
            UPDATE dos_dealers AS dealer
            SET lead_type = {_lead_type_sql('profile.vertical', fallback="'dealer'")},
                funding_intent = CASE
                    WHEN {profile_vertical_slug} LIKE '%mca%'
                    THEN 'mca_refinance'
                    ELSE COALESCE(
                        {_funding_intent_sql('profile.funding_category')},
                        dealer.funding_intent
                    )
                END
            FROM application_profiles AS profile
            WHERE profile.dealer_id = dealer.id
            """
        )
    )

    # Files without a profile can still carry classification in the intake
    # handoff.  Explicit intake state wins over the older variant aliases.
    intake_variant_type = _intake_variant_lead_type_sql("intake.variant")
    intake_variant_slug = _sql_slug("intake.variant")
    op.execute(
        sa.text(
            f"""
            UPDATE dos_dealers AS dealer
            SET lead_type = {_lead_type_sql(
                "intake.intake_state ->> 'lead_type'",
                fallback=intake_variant_type,
            )},
                funding_intent = CASE
                    WHEN {intake_variant_slug} LIKE '%mca%'
                    THEN 'mca_refinance'
                    ELSE COALESCE(
                        {_funding_intent_sql("intake.intake_state ->> 'funding_intent'")},
                        {_funding_intent_sql('intake.loan_purpose')},
                        dealer.funding_intent
                    )
                END
            FROM public_underwriting_intakes AS intake
            WHERE dealer.handoff_intake_id = intake.id
              AND NOT EXISTS (
                  SELECT 1
                  FROM application_profiles AS profile
                  WHERE profile.dealer_id = dealer.id
              )
            """
        )
    )

    # Prospect-origin bookings inherit the prospect snapshot; all other
    # Portfolio bookings inherit their linked business.  Funding intent is
    # copied exactly, including a deliberate NULL.
    op.execute(
        sa.text(
            """
            UPDATE dos_rep_appointments AS appointment
            SET lead_type = CASE
                    WHEN appointment.prospect_id IS NOT NULL THEN COALESCE((
                        SELECT prospect.lead_type
                        FROM dealer_prospects AS prospect
                        WHERE prospect.id = appointment.prospect_id
                    ), 'dealer')
                    WHEN appointment.dealer_id IS NOT NULL THEN COALESCE((
                        SELECT dealer.lead_type
                        FROM dos_dealers AS dealer
                        WHERE dealer.id = appointment.dealer_id
                    ), 'dealer')
                    ELSE appointment.lead_type
                END,
                funding_intent = CASE
                    WHEN appointment.prospect_id IS NOT NULL THEN (
                        SELECT prospect.funding_intent
                        FROM dealer_prospects AS prospect
                        WHERE prospect.id = appointment.prospect_id
                    )
                    WHEN appointment.dealer_id IS NOT NULL THEN (
                        SELECT dealer.funding_intent
                        FROM dos_dealers AS dealer
                        WHERE dealer.id = appointment.dealer_id
                    )
                    ELSE appointment.funding_intent
                END
            WHERE appointment.prospect_id IS NOT NULL
               OR appointment.dealer_id IS NOT NULL
            """
        )
    )

    _create_outreach_profiles()
    _upgrade_collateral_assets()
    bundle_ids = _create_collateral_bundles()
    _seed_legacy_bundle_items(bundle_ids)
    _upgrade_email_drafts()
    _assert_database_backfill()


def downgrade() -> None:
    op.drop_constraint(
        "ck_dealer_prospect_email_draft_bundle_identity",
        "dealer_prospect_email_drafts",
        type_="check",
    )
    op.drop_constraint(
        "ck_dealer_prospect_email_draft_profile_version",
        "dealer_prospect_email_drafts",
        type_="check",
    )
    op.drop_constraint(
        "ck_dealer_prospect_email_draft_profile_hash",
        "dealer_prospect_email_drafts",
        type_="check",
    )
    op.drop_constraint(
        "fk_dealer_prospect_email_drafts_collateral_bundle_id",
        "dealer_prospect_email_drafts",
        type_="foreignkey",
    )
    for column in (
        "collateral_bundle_snapshot",
        "collateral_bundle_version",
        "collateral_bundle_id",
        "outreach_profile_snapshot",
        "outreach_profile_hash",
        "outreach_profile_version",
        "outreach_profile_key",
        "funding_intent",
        "lead_type",
    ):
        op.drop_column("dealer_prospect_email_drafts", column)
    op.alter_column(
        "dealer_prospect_email_drafts",
        "purpose",
        existing_type=sa.String(length=48),
        type_=sa.String(length=32),
        existing_nullable=False,
        existing_server_default="information",
        server_default="dealer_information",
    )

    op.drop_index(
        "ix_marketing_collateral_bundle_items_order",
        table_name="marketing_collateral_bundle_items",
    )
    op.drop_table("marketing_collateral_bundle_items")
    op.drop_index(
        "ix_marketing_collateral_bundle_lookup",
        table_name="marketing_collateral_bundles",
    )
    op.drop_index(
        "uq_marketing_collateral_bundle_published",
        table_name="marketing_collateral_bundles",
    )
    op.drop_table("marketing_collateral_bundles")

    op.drop_index(
        "uq_marketing_collateral_one_active_version",
        table_name="marketing_collateral_assets",
    )
    op.drop_index(
        "ix_marketing_collateral_active_order",
        table_name="marketing_collateral_assets",
    )
    op.drop_constraint(
        "uq_marketing_collateral_version",
        "marketing_collateral_assets",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_marketing_collateral_version",
        "marketing_collateral_assets",
        ["assignment", "logical_key", "version"],
    )
    op.create_index(
        "ix_marketing_collateral_active_order",
        "marketing_collateral_assets",
        ["assignment", "status", "sort_order"],
    )
    op.create_index(
        "uq_marketing_collateral_one_active_version",
        "marketing_collateral_assets",
        ["assignment", "logical_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    for column in ("included_by_default", "purposes", "lead_type"):
        op.drop_column("marketing_collateral_assets", column)

    op.drop_index(
        "uq_prospect_outreach_profile_active",
        table_name="prospect_outreach_profiles",
    )
    op.drop_table("prospect_outreach_profiles")

    _downgrade_generic_information_defaults()
    _downgrade_prospect_opportunity_uniqueness()

    for table_name, lead_constraint, intent_constraint in (
        (
            "dos_rep_appointments",
            "ck_dos_rep_appointment_lead_type",
            "ck_dos_rep_appointment_funding_intent",
        ),
        (
            "dos_dealers",
            "ck_dos_dealers_lead_type",
            "ck_dos_dealers_funding_intent",
        ),
        (
            "dealer_prospects",
            "ck_dealer_prospect_lead_type",
            "ck_dealer_prospect_funding_intent",
        ),
    ):
        op.drop_index(f"ix_{table_name}_lead_type", table_name=table_name)
        op.drop_constraint(intent_constraint, table_name, type_="check")
        op.drop_constraint(lead_constraint, table_name, type_="check")
        op.drop_column(table_name, "funding_intent")
        op.drop_column(table_name, "lead_type")
