"""Repair NAICS fields copied into Field Desk applications.

Revision ID: 0155_repair_naics_application_handoff
Revises: 0154_lender_neutral_application
"""

from __future__ import annotations

from alembic import op

revision = "0155_repair_naics_application_handoff"
down_revision = "0154_lender_neutral_application"
branch_labels = None
depends_on = None


def _repair(table: str) -> None:
    op.execute(
        f"""
        UPDATE {table} AS target
        SET industry = COALESCE(category.code, target.industry),
            industry_label = category.label,
            subindustry = subcategory.code,
            subindustry_label = subcategory.label,
            naics_code = activity.code,
            naics_label = activity.label
        FROM application_taxonomy_entries AS category,
             application_taxonomy_entries AS subcategory,
             application_taxonomy_entries AS activity
        WHERE target.industry_entry_id = category.id
          AND target.subindustry_entry_id = subcategory.id
          AND target.activity_entry_id = activity.id
          AND subcategory.parent_id = category.id
          AND activity.parent_id = subcategory.id
          AND category.level = 2
          AND subcategory.level = 3
          AND activity.level = 6
          AND (
              target.industry_label IS DISTINCT FROM category.label
              OR target.subindustry_label IS DISTINCT FROM subcategory.label
              OR target.naics_code IS DISTINCT FROM activity.code
              OR target.naics_label IS DISTINCT FROM activity.label
          )
        """
    )


def upgrade() -> None:
    _repair("dos_dealers")
    _repair("dos_rep_companies")


def downgrade() -> None:
    # Derived display fields are repaired from authoritative taxonomy IDs.
    # Restoring incomplete values would knowingly reintroduce the defect.
    pass
