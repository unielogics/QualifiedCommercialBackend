"""Product Finder taxonomy, pricing versions, and Field Desk profiles.

Revision ID: 0153_product_finder_taxonomy_profiles
Revises: 0152_appointment_management
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0153_product_finder_taxonomy_profiles"
down_revision = "0152_appointment_management"
branch_labels = None
depends_on = None


def _taxonomy_columns(table: str, include_labels: bool) -> None:
    if include_labels:
        op.add_column(table, sa.Column("industry_label", sa.String(180)))
        op.add_column(table, sa.Column("subindustry_label", sa.String(180)))
        op.add_column(table, sa.Column("naics_label", sa.String(180)))
    op.add_column(table, sa.Column("subindustry", sa.String(120)))
    op.add_column(table, sa.Column("naics_code", sa.String(8))) if table == "dos_rep_companies" else None
    for name in ("industry_entry_id", "subindustry_entry_id", "activity_entry_id"):
        op.add_column(table, sa.Column(name, postgresql.UUID(as_uuid=True)))
        op.create_foreign_key(
            f"fk_{table}_{name}",
            table,
            "application_taxonomy_entries",
            [name],
            ["id"],
            ondelete="SET NULL",
        )


def upgrade() -> None:
    _taxonomy_columns("dos_rep_companies", include_labels=True)
    _taxonomy_columns("dos_dealers", include_labels=False)
    op.add_column(
        "dos_rep_inbox_messages", sa.Column("provider_error", sa.String(500))
    )

    op.create_table(
        "dos_field_desk_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(160)),
        sa.Column("title", sa.String(120)),
        sa.Column("phone", sa.String(32)),
        sa.Column("display_email", sa.String(320)),
        sa.Column("short_bio", sa.Text()),
        sa.Column("preferred_locale", sa.String(2), nullable=False, server_default="en"),
        sa.Column("card_visible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("headshot_s3_key", sa.String(720)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", name="uq_dos_field_desk_profile_user"),
    )
    op.create_index(
        "ix_dos_field_desk_profiles_visible",
        "dos_field_desk_profiles",
        ["card_visible", "updated_at"],
    )

    ez_pricing = {
        "catalog_release": revision,
        "display": {"en": "13.99%-29.99%", "es": "13.99%-29.99%"},
        "scenarios": [
            {
                "term_months": 36,
                "rate_type": "fixed",
                "best_rate": 0.1399,
                "highest_rate": 0.2999,
                "source": "Quidity EZ Term Loan published program endpoints",
            },
            {
                "term_months": 60,
                "rate_type": "fixed",
                "best_rate": 0.1399,
                "highest_rate": 0.2999,
                "source": "Quidity EZ Term Loan published program endpoints",
            },
        ],
    }
    micro_pricing = {
        "catalog_release": revision,
        "display": {"en": "Prime + 6.5%", "es": "Prime + 6.5%"},
        "scenarios": [
            {
                "term_months": 120,
                "rate_type": "indexed",
                "index_name": "Prime",
                "spread": 0.065,
                "index_value": None,
                "effective_date": None,
                "source": "Quidity MicroCap published pricing formula",
            }
        ],
    }

    # Preserve every prior catalog row and publish a complete v2 snapshot.
    op.execute(
        sa.text(
            """
            INSERT INTO dos_product_catalog
              (id, program_key, version, category, copy, pricing, eligibility,
               disclosures, amount_min, amount_max, term_min_months,
               term_max_months, effective_at, active, sort_order,
               updated_by_user_id, created_at, updated_at)
            SELECT gen_random_uuid(), program_key, version + 1, category, copy,
              CASE
                WHEN program_key = 'term_loan_3_5_year' THEN CAST(:ez AS jsonb)
                WHEN program_key = 'term_loan_10_year' THEN CAST(:micro AS jsonb)
                ELSE jsonb_build_object(
                  'catalog_release', :release,
                  'display', pricing,
                  'scenarios', '[]'::jsonb
                )
              END,
              eligibility, disclosures, amount_min, amount_max,
              term_min_months, term_max_months, now(), true, sort_order,
              updated_by_user_id, now(), now()
            FROM (
              SELECT DISTINCT ON (program_key) *
              FROM dos_product_catalog
              WHERE active = true
              ORDER BY program_key, version DESC
            ) AS catalog_source
            """
        ).bindparams(
            ez=json.dumps(ez_pricing),
            micro=json.dumps(micro_pricing),
            release=revision,
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE dos_product_catalog AS prior
            SET active = false
            WHERE prior.active = true
              AND COALESCE(prior.pricing->>'catalog_release', '') <> :release
              AND EXISTS (
                SELECT 1
                FROM dos_product_catalog AS published
                WHERE published.program_key = prior.program_key
                  AND published.active = true
                  AND published.pricing->>'catalog_release' = :release
                  AND published.version > prior.version
              )
            """
        ).bindparams(release=revision)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM dos_product_catalog WHERE pricing->>'catalog_release' = :release"
        ).bindparams(release=revision)
    )
    op.execute(
        """
        UPDATE dos_product_catalog AS catalog
        SET active = true
        WHERE catalog.version = (
          SELECT MAX(previous.version)
          FROM dos_product_catalog AS previous
          WHERE previous.program_key = catalog.program_key
        )
        """
    )
    op.drop_index("ix_dos_field_desk_profiles_visible", table_name="dos_field_desk_profiles")
    op.drop_table("dos_field_desk_profiles")
    op.drop_column("dos_rep_inbox_messages", "provider_error")
    for table, include_labels in (("dos_dealers", False), ("dos_rep_companies", True)):
        for name in ("activity_entry_id", "subindustry_entry_id", "industry_entry_id"):
            op.drop_constraint(f"fk_{table}_{name}", table, type_="foreignkey")
            op.drop_column(table, name)
        op.drop_column(table, "subindustry")
        if table == "dos_rep_companies":
            op.drop_column(table, "naics_code")
        if include_labels:
            op.drop_column(table, "naics_label")
            op.drop_column(table, "subindustry_label")
            op.drop_column(table, "industry_label")
