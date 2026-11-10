"""Allow versioned operating plans and explicitly scoped client links."""
from alembic import op

revision = "0270_operating_plan_form"
down_revision = "0269_dealer_document_versions"
branch_labels = None
depends_on = None

CHECKS = (
    ("financial_form_families", "ck_financial_form_family_kind", "kind", "'pfs','debt_schedule','p_and_l','balance_sheet','real_estate_schedule'"),
    ("financial_form_links", "ck_financial_form_links_kind", "kind", "'pfs','debt_schedule','p_and_l','balance_sheet','real_estate_schedule','worksheet'"),
    ("financial_form_link_sheets", "ck_financial_form_link_sheets_kind", "sheet_kind", "'pfs','debt_schedule','p_and_l','balance_sheet','real_estate_schedule'"),
)


def upgrade() -> None:
    for table, constraint, column, allowed in CHECKS:
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, f"{column} in ({allowed},'operating_plan')")


def downgrade() -> None:
    # Refuse to erase immutable proposals or invalidate outstanding share scopes.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM financial_form_families WHERE kind='operating_plan')
        OR EXISTS (SELECT 1 FROM financial_form_links WHERE kind='operating_plan')
        OR EXISTS (SELECT 1 FROM financial_form_link_sheets WHERE sheet_kind='operating_plan')
        THEN RAISE EXCEPTION 'Operating plans or share scopes exist; preserve them before downgrading';
        END IF;
    END; $$""")
    for table, constraint, column, allowed in CHECKS:
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, f"{column} in ({allowed})")
