"""Add Marketing conversion targets and email composition mode.

Revision ID: 0222_marketing_conversion
Revises: 0221_dealer_prospect_user_access
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0222_marketing_conversion"
down_revision = "0221_dealer_prospect_user_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_rep_contact_assignments",
        sa.Column(
            "assignment_kind",
            sa.String(length=24),
            nullable=False,
            server_default="explicit",
        ),
    )
    op.create_check_constraint(
        "ck_dos_rep_contact_assignment_kind",
        "dos_rep_contact_assignments",
        "assignment_kind IN ('explicit','prospect_owner')",
    )
    # Older prospect reassignments minted ordinary contact assignments for the
    # new owner.  Both rows were inserted in the same transaction, so their
    # database timestamps match the corresponding owner-change activity. Mark
    # those rows as owner-derived, then remove any whose prospect has since
    # moved again. Explicit CRM shares keep the default and remain durable.
    op.execute(
        "UPDATE dos_rep_contact_assignments AS assignment "
        "SET assignment_kind = 'prospect_owner' "
        "FROM dealer_prospects AS prospect "
        "JOIN dealer_prospect_activities AS activity "
        "ON activity.prospect_id = prospect.id "
        "WHERE prospect.primary_contact_id = assignment.contact_id "
        "AND activity.kind = 'prospect_updated' "
        "AND (activity.metadata -> 'changed_fields') ? 'owner_user_id' "
        "AND activity.created_at = assignment.created_at"
    )
    op.execute(
        "DELETE FROM dos_rep_contact_assignments AS assignment "
        "WHERE assignment.assignment_kind = 'prospect_owner' "
        "AND NOT EXISTS ("
        "SELECT 1 FROM dealer_prospects AS prospect "
        "WHERE prospect.primary_contact_id = assignment.contact_id "
        "AND prospect.owner_user_id = assignment.user_id"
        ")"
    )

    # A conversion is immutable audit history.  Both destination rows use the
    # product's archive workflow; hard deletion must not erase the linkage.
    op.drop_constraint(
        "dealer_prospects_converted_intake_id_fkey",
        "dealer_prospects",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_dealer_prospects_converted_intake_id_public_underwriting_intakes",
        "dealer_prospects",
        "public_underwriting_intakes",
        ["converted_intake_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "dealer_prospects",
        sa.Column("conversion_target", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "dealer_prospects",
        sa.Column("converted_application_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_dealer_prospects_converted_application_id_dos_dealers",
        "dealer_prospects",
        "dos_dealers",
        ["converted_application_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        "UPDATE dealer_prospects "
        "SET conversion_target = 'dealer_ai_intake' "
        "WHERE converted_intake_id IS NOT NULL"
    )
    op.create_check_constraint(
        "ck_dealer_prospect_conversion_target",
        "dealer_prospects",
        "conversion_target IS NULL OR conversion_target IN "
        "('portfolio_application','dealer_ai_intake')",
    )
    op.create_check_constraint(
        "ck_dealer_prospect_conversion_destination",
        "dealer_prospects",
        "(conversion_target IS NULL AND converted_application_id IS NULL "
        "AND converted_intake_id IS NULL) OR "
        "(conversion_target = 'portfolio_application' AND converted_application_id IS NOT NULL "
        "AND converted_intake_id IS NULL) OR "
        "(conversion_target = 'dealer_ai_intake' AND converted_intake_id IS NOT NULL "
        "AND converted_application_id IS NULL)",
    )
    op.create_index(
        "ix_dealer_prospects_converted_application",
        "dealer_prospects",
        ["converted_application_id"],
    )

    op.add_column(
        "dealer_prospect_email_drafts",
        sa.Column(
            "compose_mode",
            sa.String(length=16),
            nullable=False,
            server_default="ai",
        ),
    )
    op.create_check_constraint(
        "ck_dealer_prospect_email_draft_compose_mode",
        "dealer_prospect_email_drafts",
        "compose_mode IN ('ai','manual')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_dealer_prospect_email_draft_compose_mode",
        "dealer_prospect_email_drafts",
        type_="check",
    )
    op.drop_column("dealer_prospect_email_drafts", "compose_mode")

    op.drop_index(
        "ix_dealer_prospects_converted_application",
        table_name="dealer_prospects",
    )
    op.drop_constraint(
        "ck_dealer_prospect_conversion_destination", "dealer_prospects", type_="check"
    )
    op.drop_constraint(
        "ck_dealer_prospect_conversion_target", "dealer_prospects", type_="check"
    )
    op.drop_constraint(
        "fk_dealer_prospects_converted_application_id_dos_dealers",
        "dealer_prospects",
        type_="foreignkey",
    )
    op.drop_column("dealer_prospects", "converted_application_id")
    op.drop_column("dealer_prospects", "conversion_target")
    op.drop_constraint(
        "fk_dealer_prospects_converted_intake_id_public_underwriting_intakes",
        "dealer_prospects",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "dealer_prospects_converted_intake_id_fkey",
        "dealer_prospects",
        "public_underwriting_intakes",
        ["converted_intake_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint(
        "ck_dos_rep_contact_assignment_kind",
        "dos_rep_contact_assignments",
        type_="check",
    )
    op.drop_column("dos_rep_contact_assignments", "assignment_kind")
