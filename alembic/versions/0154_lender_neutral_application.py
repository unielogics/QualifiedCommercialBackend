"""Lender-neutral master application and unified routing fields.

Revision ID: 0154_lender_neutral_application
Revises: 0153_product_finder_taxonomy_profiles
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0154_lender_neutral_application"
down_revision = "0153_product_finder_taxonomy_profiles"
branch_labels = None
depends_on = None


PROFILE_COLUMNS = (
    ("dba_name", sa.String(180)),
    ("website", sa.String(500)),
    ("state_of_formation", sa.String(2)),
    ("location_type", sa.String(32)),
    ("mailing_address", sa.String(300)),
    ("mailing_city", sa.String(120)),
    ("mailing_state", sa.String(2)),
    ("mailing_zip", sa.String(12)),
    ("annual_sales", sa.Numeric(14, 2)),
    ("annual_cash_flow_available_for_debt", sa.Numeric(14, 2)),
    ("monthly_debt_payments", sa.Numeric(14, 2)),
    ("signer_title", sa.String(120)),
)


def upgrade() -> None:
    op.add_column("dos_dealers", sa.Column("industry_label", sa.String(180)))
    op.add_column("dos_dealers", sa.Column("subindustry_label", sa.String(180)))
    for name, type_ in PROFILE_COLUMNS:
        op.add_column("dos_application_profiles", sa.Column(name, type_))
    op.add_column(
        "dos_application_profiles",
        sa.Column("human_review_status", sa.String(24), nullable=False, server_default="pending"),
    )
    op.add_column("dos_application_profiles", sa.Column("human_review_note", sa.Text()))
    op.add_column(
        "dos_application_profiles", sa.Column("human_reviewed_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "dos_application_profiles",
        sa.Column("human_reviewed_by_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_dos_application_profiles_human_reviewer",
        "dos_application_profiles",
        "users",
        ["human_reviewed_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "dos_contract_templates",
        sa.Column(
            "render_kind",
            sa.String(24),
            nullable=False,
            server_default="uploaded_pdf",
        ),
    )
    op.add_column("dos_contract_documents", sa.Column("signer_title", sa.String(120)))
    op.add_column("dos_contract_documents", sa.Column("signature_sha256", sa.String(64)))
    op.add_column(
        "dos_application_pre_screens",
        sa.Column("self_report_routing_result", postgresql.JSONB()),
    )
    op.add_column(
        "dos_application_pre_screens",
        sa.Column("verified_routing_result", postgresql.JSONB()),
    )
    op.add_column("dos_application_pre_screens", sa.Column("routing_history", postgresql.JSONB()))
    op.alter_column(
        "dos_application_pre_screens",
        "rules_version",
        type_=sa.String(48),
        existing_type=sa.String(32),
        server_default="qc_direct_programs_v2",
        existing_nullable=False,
    )

    op.execute(
        sa.text(
            """
            INSERT INTO dos_contract_templates
              (id, key, title, s3_key, page_count, has_acroform, field_names,
               field_map, revision, active, render_kind, created_at, updated_at)
            VALUES
              (gen_random_uuid(), 'qc_business_financing_application',
               'Qualified Commercial Business Financing Application and Certifications',
               NULL, NULL, false, '[]'::jsonb, '{}'::jsonb, 1, true,
               'generated_html', now(), now())
            ON CONFLICT (key) DO UPDATE
            SET title = EXCLUDED.title,
                render_kind = EXCLUDED.render_kind,
                active = true,
                updated_at = now()
            """
        )
    )

    rules = {
        "engine": "qc_direct_programs_v2",
        "source": "Qualified Commercial normalized program policy",
        "rules_version": "qc_direct_programs_v2",
        "sba_policy": {
            "version": "sba_sop_50_10_8_notice_5000_876441",
            "effective_date": "2026-03-01",
        },
        "classification": "canonical_naics",
    }
    pricing_term = {
        "catalog_release": revision,
        "display": {"en": "13.99%-29.99%", "es": "13.99%-29.99%"},
        "scenarios": [
            {"term_months": 36, "rate_type": "fixed", "best_rate": 0.1399, "highest_rate": 0.2999, "source": "Published program guide"},
            {"term_months": 60, "rate_type": "fixed", "best_rate": 0.1399, "highest_rate": 0.2999, "source": "Published program guide"},
        ],
    }
    pricing_working = {
        "catalog_release": revision,
        "display": {"en": "Prime + 6.5%", "es": "Prime + 6.5%"},
        "scenarios": [
            {"term_months": 120, "rate_type": "indexed", "index_name": "Prime", "spread": 0.065, "index_value": None, "effective_date": None, "source": "Published program guide"}
        ],
    }
    op.execute(
        sa.text(
            """
            INSERT INTO dos_product_catalog
              (id, program_key, version, category, copy, pricing, eligibility,
               disclosures, amount_min, amount_max, term_min_months,
               term_max_months, effective_at, active, sort_order,
               updated_by_user_id, created_at, updated_at)
            SELECT gen_random_uuid(), program_key,
              (SELECT MAX(all_versions.version) + 1
               FROM dos_product_catalog AS all_versions
               WHERE all_versions.program_key = current_catalog.program_key),
              category, copy,
              CASE
                WHEN program_key = 'term_loan_3_5_year' THEN CAST(:term_pricing AS jsonb)
                WHEN program_key = 'term_loan_10_year' THEN CAST(:working_pricing AS jsonb)
                ELSE COALESCE(pricing, '{}'::jsonb)
                  || jsonb_build_object('catalog_release', CAST(:release AS text))
              END,
              CASE
                WHEN program_key IN ('term_loan_3_5_year', 'term_loan_10_year')
                  THEN (COALESCE(eligibility, '{}'::jsonb) - 'source' - 'provider' - 'lender')
                    || CAST(:rules AS jsonb)
                ELSE eligibility
              END,
              disclosures, amount_min, amount_max, term_min_months,
              term_max_months, now(), true, sort_order, updated_by_user_id,
              now(), now()
            FROM (
              SELECT DISTINCT ON (program_key) *
              FROM dos_product_catalog
              WHERE active = true
              ORDER BY program_key, version DESC
            ) AS current_catalog
            """
        ).bindparams(
            term_pricing=json.dumps(pricing_term),
            working_pricing=json.dumps(pricing_working),
            rules=json.dumps(rules),
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
                SELECT 1 FROM dos_product_catalog AS published
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
        sa.text("DELETE FROM dos_product_catalog WHERE pricing->>'catalog_release' = :release")
        .bindparams(release=revision)
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
    op.execute(
        "DELETE FROM dos_contract_templates WHERE key = 'qc_business_financing_application'"
    )
    op.drop_column("dos_application_pre_screens", "routing_history")
    op.alter_column(
        "dos_application_pre_screens",
        "rules_version",
        type_=sa.String(32),
        existing_type=sa.String(48),
        server_default="quidity_step1_v1",
        existing_nullable=False,
    )
    op.drop_column("dos_application_pre_screens", "verified_routing_result")
    op.drop_column("dos_application_pre_screens", "self_report_routing_result")
    op.drop_column("dos_contract_documents", "signature_sha256")
    op.drop_column("dos_contract_documents", "signer_title")
    op.drop_column("dos_contract_templates", "render_kind")
    op.drop_constraint(
        "fk_dos_application_profiles_human_reviewer",
        "dos_application_profiles",
        type_="foreignkey",
    )
    op.drop_column("dos_application_profiles", "human_reviewed_by_user_id")
    op.drop_column("dos_application_profiles", "human_reviewed_at")
    op.drop_column("dos_application_profiles", "human_review_note")
    op.drop_column("dos_application_profiles", "human_review_status")
    for name, _ in reversed(PROFILE_COLUMNS):
        op.drop_column("dos_application_profiles", name)
    op.drop_column("dos_dealers", "subindustry_label")
    op.drop_column("dos_dealers", "industry_label")
